"""应用层：上下文压缩引擎——把旧回合折叠成 checkpoint，模型上下文变小。

架构位置：纯应用逻辑（事件溯源之上、框架不动）。对齐 dsh 的
compaction/start → summary → replace → end 四步事务；选段策略借鉴
"保留最近 N 个回合"的直觉（简化 dsh 的 token 预算 + PI 的回合切分）。

关键语义（见 session.py 的 surface replace）：
- 遮蔽区间按 surface 的【位置】切，不是 seq 数值——引擎基于当前 surface
  投影选区，两端 seq 必然是 surface 成员
- 遮蔽边界尽量落在回合边界上（不切开正在进行的工作）
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import cast

from .llm import LlmRequest
from .session import Session
from .values import Message, TextBlock, create_user_message

# ---- 压缩提示词（融合 dsh 8 段骨架 + PI 的 Progress 三态 / 有序 Next Steps）----
# 文件清单由代码拼入（extract_file_ops），模型只负责提炼对话本身，
# 不靠它猜路径（学 PI：路径准确性不能交给模型）。
SUMMARY_OPEN_TAG = '<compacted-summary>'
SUMMARY_CLOSE_TAG = '</compacted-summary>'

# 摘要请求的 system 提示：把被压对话折叠成结构化 checkpoint。
# 结构 = dsh 的会话快照段（Primary Request / Key Technical Concepts /
# Files and Code / Errors and Fixes / Pending Jobs / Current Work /
# Next Step / Critical Context），其中 Current Work 吸收 PI 的
# Done / In Progress / Blocked 三态，Next Step 采 PI 的有序列表。
COMPACT_SYSTEM = (
    'You are now acting as a compaction engine for a coding agent. Condense the '
    'conversation messages below into a structured checkpoint that lets another '
    'model resume the work with no loss of essential context.\n'
    'Output EXACTLY the Markdown structure below; keep every section, in order. '
    'Write "(none)" for an empty section — never drop a section.\n\n'
    '## Primary Request and Intent\n'
    "- [the user's original and evolving goals; quote verbatim where wording matters]\n"
    '## Key Technical Concepts\n'
    '- [technologies, frameworks, patterns, conventions in play]\n'
    '## Files and Code\n'
    '- [exact path: why it matters, key changes or snippets]\n'
    '## Errors and Fixes\n'
    '- [error: how it was resolved, plus related user feedback]\n'
    '## Pending Jobs\n'
    '- [explicitly requested work not yet completed]\n'
    '## Current Work\n'
    '### Done\n- [x] [completed items]\n'
    '### In Progress\n- [ ] [currently worked items]\n'
    '### Blocked\n- [issues preventing progress, or "(none)"]\n'
    '## Next Steps\n'
    '1. [ordered actions — first is the immediate next one]\n'
    '## Critical Context\n'
    '- [decisions and rationale, constraints, user preferences, open questions]\n'
    'Rules: write concise engineering prose; preserve exact file paths, commands, '
    'error strings, identifiers, numeric values, function signatures; capture user '
    'feedback and corrections faithfully; do NOT mention this summarization request '
    'or that the context was compacted; output only the checkpoint text.\n'
    f'If the conversation already contains a {SUMMARY_OPEN_TAG} block, it is a '
    'PRIOR checkpoint — do not copy it forward verbatim: preserve still-true facts, '
    'drop stale ones, merge newer information into a single consolidated summary '
    'under the same structure.'
)

# checkpoint 的引导语：告诉后续模型"这是被压缩的既定背景，别复述，直接继续"。
# 正对 replace 语义——checkpoint 顶替旧回合后，后面还跟着保留的新回合消息。
CHECKPOINT_PREAMBLE = (
    'This is an automatically generated checkpoint condensing an earlier span of the '
    'conversation to free up context. Treat the captured context as established '
    'background and build on it without restating it. Continue the task directly '
    'from the messages that follow, without acknowledging this checkpoint.'
)

# 事务事件序列（对齐 dsh：start → summary → replace → end）
COMPACTION_START = 'compaction/start'
COMPACTION_SUMMARY = 'compaction/summary'
COMPACTION_END = 'compaction/end'


def _turn_of(events, seq: int) -> int:
    """seq 属于第几个回合：按 turn/start 事件划界（含进行中的回合）。"""
    turn = 1
    for event in events:
        if event.seq >= seq:
            break
        if event.type == 'turn/start':
            turn += 1
    return turn


def select_compact_range(session: Session, keep_turns: int) -> tuple | None:
    """选出可压缩的 surface 区间（保留最近 keep_turns 个回合）。

    返回 (start_seq, end_seq)（surface 位置语义的两端 seq），或 None
    （没有足够的旧回合可压）。策略：找到第一个属于"保留回合"的 surface
    节点位置，它之前的前缀就是可压段——旧回合在前、新回合在后，且前缀
    是连续位置段，压缩不会切开进行中的回合。
    """
    if keep_turns < 1:
        raise ValueError('keep_turns must be >= 1')
    events = session.events
    surface = list(session.surface)
    if not surface:
        return None

    # 每个 surface 节点的回合归属（从日志扫，surface seq 必在 events 里）
    turn_of = {}
    turn = 1
    for event in events:
        if event.type == 'turn/start':
            turn += 1
        if event.surface_op in ('append', 'replace'):
            turn_of[event.seq] = turn
    latest_turn = max(turn_of.values()) if turn_of else 1
    keep_from_turn = max(1, latest_turn - keep_turns + 1)
    # 保留回合 = [keep_from_turn, latest_turn]；之前的都算旧
    keep_seqs = {seq for seq in surface if turn_of.get(seq, latest_turn) >= keep_from_turn}

    keep_idxs = [surface.index(seq) for seq in keep_seqs if seq in surface]
    if not keep_idxs:
        return None
    first_keep_idx = min(keep_idxs)
    if first_keep_idx == 0:
        return None  # 前缀没有可压内容（最近 keep_turns 回合就占了全部）
    # 遮蔽 [0, first_keep_idx) 这段位置 → 两端 seq
    return (surface[0], surface[first_keep_idx - 1])


@dataclass(frozen=True)
class FileOps:
    """被压对话里动过的文件（学 PI：编码场景的刚需，摘要后由代码拼入）。"""
    read: frozenset = frozenset()
    written: frozenset = frozenset()
    edited: frozenset = frozenset()

    def as_text(self) -> str:
        lines = []
        if self.read:
            lines.append('Read files: ' + ', '.join(sorted(self.read)))
        if self.written:
            lines.append('Written files: ' + ', '.join(sorted(self.written)))
        if self.edited:
            lines.append('Edited files: ' + ', '.join(sorted(self.edited)))
        return '\n'.join(lines)


def _extract_path(arguments: dict) -> str | None:
    """从工具参数取文件路径（按工具的 key 名猜测，够教学用）。"""
    for key in ('file_path', 'dir_path', 'path'):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def estimate_context_tokens(session: Session) -> int:
    """估算当前模型可见上下文的 token 数（粗估：字符 / 2，中文约 1.5~2 字/token）。

    教学实现：真实 token 计费需 provider usage，demo 用字符近似对齐
    "压缩要真省"的直觉——估算偏保守没关系，阈值可调。仅统计
    derive_messages() 折叠出的文本（模型真正看到的）。
    """
    total_chars = 0
    for message in session.derive_messages():
        for block in getattr(message, 'content', ()):
            if getattr(block, 'type', '') == 'text':
                total_chars += len(block.text)
    # 粗略：英文约 4 字符/token、中文约 1.5 字符/token；教学用 /2 折中，
    # 让触发偏早不偏晚（宁压缩勿溢出）
    return max(1, total_chars // 2)


def wire_auto_compaction(agent, *, max_tokens: int, keep_turns: int = 3) -> object:
    """给 agent 挂自动压缩监听；返回退订函数。

    接线方式（决策走注册声明，不动 loop）：session.on_event 监听
    turn/end——回合收尾后量上下文，超 max_tokens 就在后台压缩。
    llm 复用 agent.llm（摘要请求同客户端）；任务登记防 asyncio GC。
    """
    return agent.session.on_event(
        lambda event: _on_turn_event(agent, event, max_tokens, keep_turns))


def _on_turn_event(agent, event, max_tokens: int, keep_turns: int) -> None:
    """同步监听回调：turn/end completed 且上下文超阈值 → 调度后台压缩。"""
    if event.type != 'turn/end' or event.data.get('reason') != 'completed':
        return
    if estimate_context_tokens(agent.session) < max_tokens:
        return
    import asyncio

    task = asyncio.ensure_future(run_compaction(
        agent.session, agent.llm, keep_turns=keep_turns,
        model=agent.options.get('model', ''),
    ))
    # 持引用防事件循环 GC 丢弃未完成的后台压缩
    _background_compactions.add(task)
    task.add_done_callback(_background_compactions.discard)


_background_compactions: set = set()


def extract_file_ops(session: Session, start_seq: int, end_seq: int) -> FileOps:
    """从被压区间 [start_seq..end_seq] 的 tool/call 痕迹提取文件操作。

    read=只读工具的目标文件；written=write_file；edited=edit。
    只读工具含 search（grep/glob/list_files）按 path/dir_path 记录，
    无法确定是读哪个具体文件就不记（保持保守）。
    """
    read: set[str] = set()
    written: set[str] = set()
    edited: set[str] = set()
    # 定位区间两端 seq 在日志里的位置：日志按 seq 排序，区间 [a..b] 是
    # seq 值连续的（日志 seq 单调）——直接按 seq 值过滤即可
    for event in session.events:
        if not (start_seq <= event.seq <= end_seq):
            continue
        if event.type != 'tool/call':
            continue
        data = event.data if isinstance(event.data, dict) else {}
        name = data.get('name', '')
        raw_args = data.get('arguments', '')
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else {}
        except (json.JSONDecodeError, TypeError):
            continue
        path = _extract_path(arguments)
        if name == 'write_file' and path:
            written.add(path)
        elif name == 'edit' and path:
            edited.add(path)
        elif name in ('read_file',) and path:
            read.add(path)
        # grep/glob/list_files 是 search：只读但目标是目录/模式，不记具体文件
    return FileOps(read=frozenset(read), written=frozenset(written), edited=frozenset(edited))


def _shadowed_messages(session: Session, start_seq: int, end_seq: int) -> list:
    """被压区间的模型可见消息（surface 折叠后按顺序取区间内的）。"""
    msgs = []
    for seq in session.surface:
        if not (start_seq <= seq <= end_seq):
            continue
        event = session.events[seq]
        if event.type == 'user/message':
            msgs.append(cast(Message, event.data))
        elif event.type == 'assistant/message':
            message = cast(dict, event.data)['message']
            if message.content:
                msgs.append(message)
        elif event.type == 'tool/result':
            msgs.append(cast(Message, event.data))
    return msgs


async def run_compaction(session: Session, llm, *, keep_turns: int = 3,
                         model: str = '') -> bool:
    """执行一次压缩事务；无可压或失败返回 False。

    四步事务（对齐 dsh）：
      1. compaction/start    加锁（审计：压缩开始）
      2. compaction/summary  审计（摘要全文 + 遮蔽范围 + 文件操作）
      3. user/message        surface replace（checkpoint 原位顶替旧回合）
      4. compaction/end      解锁（成功/失败都落）
    摘要请求：复用调用方 llm 发一个小请求；摘要必须比被压内容小
    （字符近似 token；宁拒勿滥——压缩要真省）。失败（LLM 错误/摘要
    不够小）→ 落 compaction/end{error}，日志完好可重试。
    """
    selection = select_compact_range(session, keep_turns)
    if selection is None:
        return False
    start_seq, end_seq = selection
    # 1) 加锁：记录压缩开始（compaction id 供 summary/end 关联）
    cid = uuid.uuid4().hex
    session.append(COMPACTION_START, {'compaction_id': cid, 'range': [start_seq, end_seq]})

    try:
        shadowed = _shadowed_messages(session, start_seq, end_seq)
        if not shadowed:
            session.append(COMPACTION_END, {'compaction_id': cid, 'error': 'no messages to summarize'})
            return False
        # 消息转 wire 文本（TextBlock 拼接；截断防撑爆摘要请求）
        parts = []
        for msg in shadowed:
            texts = [b.text for b in msg.content if getattr(b, 'type', '') == 'text']
            text = '\n'.join(texts)
            if text:
                parts.append(f'[{msg.role}] {text[:2000]}')
        conversation = '\n\n'.join(parts) or '(no text)'
        request = LlmRequest(
            system=COMPACT_SYSTEM,
            model=model or '',
            messages=(create_user_message([TextBlock(text=conversation)]),),
            max_tokens=600,
        )
        # 2) 生成摘要（复用调用方 llm；失败降级为结果，不炸）
        collected = []
        async for chunk in llm.stream(request):
            if chunk.text:
                collected.append(chunk.text)
            if chunk.finish_reason:
                break
        summary = ''.join(collected).strip()
        if not summary:
            session.append(COMPACTION_END, {'compaction_id': cid, 'error': 'empty summary'})
            return False
        # 摘要必须比被压内容小（宁拒勿滥——压缩要真省）；比较只看模型输出本体，
        # 不含后面我们自加的 preamble/标签固定开销
        if len(summary) >= len(conversation):
            session.append(COMPACTION_END, {'compaction_id': cid, 'error': 'summary not smaller than content'})
            return False
        # 文件操作清单由代码拼入（学 PI：路径不能靠模型猜）
        file_ops = extract_file_ops(session, start_seq, end_seq)
        ops_text = file_ops.as_text()
        body = summary + ('\n\n' + ops_text if ops_text else '')
        # checkpoint 落盘格式：preamble 引导后续模型 + <compacted-summary> 标签包裹。
        # 标签让"下一次压缩"能识别旧 checkpoint（迭代合并由 prompt 指令保证）；
        # preamble 让它把压缩历史当既定背景、不向保留的新回合复述。
        checkpoint_body = (
            f'{CHECKPOINT_PREAMBLE}\n\n{SUMMARY_OPEN_TAG}\n{body}\n{SUMMARY_CLOSE_TAG}'
        )
        session.append(COMPACTION_SUMMARY, {
            'compaction_id': cid,
            'range': [start_seq, end_seq],
            'summary': body,          # 审计存模型产出本体（不含包装）
            'file_ops': {
                'read': sorted(file_ops.read),
                'written': sorted(file_ops.written),
                'edited': sorted(file_ops.edited),
            },
        })
        # 3) checkpoint 落盘：surface replace 顶替旧区间
        session.append(
            'user/message',
            create_user_message([TextBlock(text=checkpoint_body)]),
            surface_op='replace', shadowed=(start_seq, end_seq),
        )
        # 4) 解锁
        session.append(COMPACTION_END, {'compaction_id': cid})
        return True
    except Exception as error:  # noqa: BLE001 - 压缩失败不影响主流程
        session.append(COMPACTION_END, {'compaction_id': cid, 'error': str(error)})
        return False
