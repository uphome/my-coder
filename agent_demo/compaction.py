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

# 摘要请求的 system 提示：把被压对话折叠成结构化 checkpoint（dsh 风格结构，
# 但用中文教学口径）。文件清单由代码拼入（extract_file_ops），模型只负责
# 提炼对话本身，不靠它猜路径（学 PI：路径准确性不能交给模型）。
COMPACT_SYSTEM = (
    'You are a compaction engine. Condense the conversation messages below into a '
    'concise structured checkpoint that lets another model continue the work with no '
    'loss of essential context. Output EXACTLY this Markdown structure, keep every '
    'section in order; write "(none)" for empty sections:'
    '\n\n## Primary Request and Intent\n## Key Technical Concepts\n'
    '## Errors and Fixes\n## Current Work\n## Next Step\n'
    '\nRules: preserve exact file paths, commands, error strings, identifiers; '
    'capture user feedback faithfully; output only the checkpoint text.'
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
        # 摘要必须比被压内容小（宁拒勿滥——压缩要真省）
        if len(summary) >= len(conversation):
            session.append(COMPACTION_END, {'compaction_id': cid, 'error': 'summary not smaller than content'})
            return False
        # 文件操作清单由代码拼入（学 PI：路径不能靠模型猜）
        file_ops = extract_file_ops(session, start_seq, end_seq)
        ops_text = file_ops.as_text()
        checkpoint_body = summary + ('\n\n' + ops_text if ops_text else '')
        session.append(COMPACTION_SUMMARY, {
            'compaction_id': cid,
            'range': [start_seq, end_seq],
            'summary': checkpoint_body,
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
