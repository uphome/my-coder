"""Web UI：浏览器里的 DeepSeek 风格对话。

架构：UI 是日志的投影的第二个渲染器——复用现有框架（Session/Agent/
build_tools/loop 一行不改），session.on_event 订阅事件，经 asyncio.Queue
桥接成 SSE 流推给浏览器。CLI 是终端投影，Web 是 DOM 投影，同一份日志。

功能：多会话（左侧栏列出 .sessions/*.jsonl，可新建/切换/删除/改名）+
自动会话标题（对齐 harness session-title 的三级来源）+ 流式输出 +
思考折叠 + 工具卡片（变体图标/状态点/摘要，仿 harness ui-tool）+ Markdown 渲染 +
approval 按钮（钩子推送 approval_request 到 SSE，浏览器批准/拒绝）。"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .compaction import (
    cache_hit_rate,
    estimate_context_tokens,
    run_compaction,
    select_compact_range,
    session_token_totals,
)
from .constants import MODEL_CONTEXT_WINDOW
from .factory import build_agent, load_env
from .hooks import Hooks
from .llm import LlmRequest
from .persistence import load_events
from .session import Session
from .tools.todo import fold_todos
from .values import TextBlock, create_user_message

# 仓库根 = 包上一级：静态资源（web/）与 .env 都在根
_ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title='agent-demo web')
# 前端依赖（marked / DOMPurify 本地 vendor，免 CDN）
app.mount('/vendor', StaticFiles(directory=_ROOT / 'web' / 'vendor'), name='vendor')

_sessions_dir: Path | None = None
_args: argparse.Namespace | None = None

# ---- 会话 seat 注册表（Web 并发隔离的核心）----
# 每个打开的会话一个 Seat：session（事件日志）+ agent（状态机/driver）+
# 该会话自己的活跃 SSE 队列与审批等待表。不同会话互不共享可变状态，
# 因此可以同时并行跑多个 agent（标签页 A 跑会话 X、标签页 B 跑会话 Y）。
# "焦点"概念仍在（_current_sid/_session/_agent 指向最近切换的会话），
# 但那只是前端"正在看哪个"的便捷别名——运行中的资源都挂在 Seat 里。
class Seat:
    def __init__(self, sid: str, session, agent) -> None:
        self.sid = sid
        self.session = session
        self.agent = agent
        # 该会话当前活跃的 SSE 队列（approval 请求经它推给本会话的浏览器）。
        # 每会话同时最多一条活跃对话流（前端单标签页串行使用）；None = 空闲。
        self.queue: asyncio.Queue | None = None
        # 该会话自己的审批等待表（aid → Future）；拒绝/批准经 /approval/respond 唤醒。
        self.approvals: dict[str, asyncio.Future] = {}
        # 会话上下文快照（供 _context_payload 等按会话取数，不依赖全局焦点）
        self.context = None

_seats: dict[str, Seat] = {}
_session: Session | None = None
_agent = None
_current_sid: str = ''

# 与历史测试兼容的便捷函数：焦点会话的 seat（无焦点时 None）
def _focus_seat() -> Seat | None:
    return _seats.get(_current_sid)

# Web approval：敏感工具（bash/write/edit）执行前推送请求给浏览器，等待人工批准/拒绝
APPROVAL_TIMEOUT_S = 300  # 用户不点则 fail-safe 拒绝（防模型永久卡住）

DONE_MARKER = object()

# 工具结果预览上限：展开卡片想看到全文（对齐 CLI 的 BASH_MAX_OUTPUT_CHARS 量级），
# 只在真正超长时截断；chars/lines/truncated 让前端能显示统计与截断徽标。
RESULT_MAX_CHARS = 20000

# ---- 会话标题（对齐 harness session-title 的三级来源设计）----
# 日志里一条 session/title 事件（data: {'title', 'source'}）就是标题——
# 标题 = 日志投影，恢复/列表都从日志读，不存在第二份状态源。
# 来源：'user'（手动改名，优先级最高）/ 'auto'（LLM 自动起名）。
# 'fallback' 不落日志：没有标题事件时，列表显示首条用户消息摘要（现有逻辑）。
TITLE_MAX_CHARS = 40
# 起名 prompt 的核心约束：总结意图、禁止逐字复读原文——"你好"的标题应是
# "问候"而不是把原话再抄一遍（对齐 chat.deepseek 的概括式起名）
TITLE_SYSTEM = (
    'You are a conversation titler for a coding-agent session. Given the first '
    'user message, reply with ONLY a short title that SUMMARIZES THE USER\'S '
    'INTENT (same language as the message, under 12 words). '
    'Rules: never repeat the user message verbatim or near-verbatim; condense it '
    'into a category/action label (e.g. greeting, bug fix, code review, install '
    'deps). No quotes, no punctuation, no explanation.'
)
AUTO_TITLE_TIMEOUT_S = 25  # 起名失败静默降级（fallback 摘要兜底），别拖垮主流程


def _clean_title(raw: str) -> str:
    """模型返回的标题规范化：去引号/多余空白/尾标点，限长。空则视为无标题。"""
    title = (raw or '').strip().strip('"\'“”‘’「」『』').strip()
    title = ' '.join(title.split()).strip(' .,;:。，；：！!？?、-—')
    return title[:TITLE_MAX_CHARS]


def _is_verbatim_copy(title: str, first_text: str) -> bool:
    """标题是否只是逐字/近逐字复读首条消息——是则视为起名失败（退回 fallback）。

    模型对短消息（寒暄/单句）容易偷懒把原文抄回来当标题，那不是标题。
    归一化后比较：完全相等、或标题是原文的子串（方向各一）。
    """
    def norm(s: str) -> str:
        return ''.join((s or '').split()).lower()
    t, m = norm(title), norm(first_text)
    if not t or not m:
        return False
    return t == m or t in m or m in t


def _first_text_of(message) -> str:
    """从 user/message 事件载荷（一条 Message）提取文本（TextBlock 拼接）。"""
    texts = [b.text for b in getattr(message, 'content', ()) if getattr(b, 'type', '') == 'text']
    return ''.join(texts)


def _should_auto_title(session: Session) -> bool:
    """首条消息后是否值得自动起名：真模型 + 还没有标题 + 尚无任何用户消息。

    用于 chat 请求开始时（user/message 尚未 append）的判断。
    """
    if _args is None or _args.fake:
        return False
    if any(e.type == 'session/title' for e in session.events):
        return False
    if any(e.type == 'user/message' for e in session.events):
        return False
    return True


def _first_user_message_just_landed(session: Session) -> bool:
    """事件已落日志后的判断：这是否恰是第一条 user/message（且无标题、真模型）。

    事件监听回调发生在 append 之后，此刻 session 里 user/message 计数已是 1——
    若还套用 _should_auto_title（要求 0 条）就永远 False。所以单独判断：
    真模型 + 无 title + user/message 恰好 1 条（= 刚落地的那条是第一条）。
    """
    if _args is None or _args.fake:
        return False
    if any(e.type == 'session/title' for e in session.events):
        return False
    user_messages = [e for e in session.events if e.type == 'user/message']
    return len(user_messages) == 1


async def _auto_title(seat: Seat, first_text: str) -> None:
    """自动起名：复用该会话 agent 的 LLM 客户端发一个小请求，结果落 session/title。

    后台任务、与主对话并发互不干扰；任何失败（网络/超时/空输出）都静默
    跳过——列表继续显示 fallback 摘要，起名失败绝不打扰主流程。
    按 seat 取 agent（并发隔离：起名请求用本会话的 llm，不读全局焦点）。
    """
    agent = seat.agent
    if agent is None:
        return
    try:
        request = LlmRequest(
            system=TITLE_SYSTEM,
            model=agent.options.get('model', ''),
            messages=(create_user_message([TextBlock(text=first_text[:400])]),),
            max_tokens=30,
            thinking=False,  # 起名是短请求：关 thinking，别让 30 token 预算被思维链耗尽
        )
        parts: list[str] = []

        async def collect() -> None:
            async for chunk in agent.llm.stream(request):
                if chunk.text:
                    parts.append(chunk.text)
                if chunk.finish_reason:
                    break

        await asyncio.wait_for(collect(), timeout=AUTO_TITLE_TIMEOUT_S)
        title = _clean_title(''.join(parts))
        if not title:
            return
        if _is_verbatim_copy(title, first_text):
            # 逐字复读原文 = 起名失败：不落 auto 事件，列表继续显示 fallback
            print('[title] auto-title rejected: verbatim copy of user message', flush=True)
            return
        seat.session.append('session/title', {'title': title, 'source': 'auto'})
    except Exception as error:  # noqa: BLE001 - 起名失败不影响主流程
        print(f'[title] auto-title skipped: {type(error).__name__}: {error}', flush=True)


def _append_title(sid: str, title: str, source: str) -> None:
    """往一个会话追加 session/title 事件（标题 = 日志投影，改完即落盘）。

    优先用该会话的 Seat（在跑/打开过的会话直接 append，不打断 agent）；
    否则临时重放目标日志 + bind_store append（从列表里改没打开过的会话）。
    """
    seat = _seats.get(sid)
    if seat is not None:
        seat.session.append('session/title', {'title': title, 'source': source})
        return
    path = (_sessions_dir or Path('.sessions')) / f'{sid}.jsonl'
    if not path.exists():
        raise HTTPException(404, f'session {sid!r} not found')
    temp = Session(id=sid)
    for event in load_events(path):
        temp.adopt(event)
    temp.bind_store(path)
    temp.append('session/title', {'title': title, 'source': source})


def _result_stats(content: str) -> tuple[int, int]:
    """工具结果统计：(字符数, 行数)——前端摘要行显示用。"""
    return len(content), content.count('\n') + 1 if content else 0


def _result_payload(block) -> dict:
    """ToolResultBlock → 前端载荷：预览内容 + 全文统计（截断标记交给前端徽标）。"""
    content = getattr(block, 'content', '') or ''
    chars, lines = _result_stats(content)
    return {
        'call_id': getattr(block, 'tool_call_id', ''),
        'content': content[:RESULT_MAX_CHARS],
        'chars': chars,
        'lines': lines,
        'truncated': chars > RESULT_MAX_CHARS,
        'is_error': bool(getattr(block, 'is_error', False)),
    }


# 后台任务注册表：保住未完成任务的强引用，防事件循环 GC 丢弃
# （asyncio 文档明确：create_task 的返回值若无引用，任务可能在执行前被回收）
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """创建并登记一个后台任务；完成时自动从注册表移除。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _approval_for(seat: Seat):
    """生成绑定到某个会话 seat 的 approval 钩子（build_agent 时挂载）。

    Web 并发隔离的关键：审批必须回到"提出请求的那个会话"的浏览器。
    旧实现读全局 _active_queue——两会话并行时 A 的审批会推到 B 的流。
    现在钩子闭包捕获自己的 seat：请求推 seat.queue、等待表存 seat.approvals，
    各会话天然隔离。无活跃流/超时一律 fail-safe 拒绝。
    """
    async def approval(name: str, arguments: dict) -> bool:
        queue = seat.queue
        if queue is None:
            return False  # 该会话当前没有活跃 SSE 流
        aid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        seat.approvals[aid] = fut
        await queue.put({
            'type': 'approval_request', 'id': aid, 'name': name, 'arguments': arguments,
        })
        try:
            return await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT_S)
        except TimeoutError:
            return False
        finally:
            seat.approvals.pop(aid, None)
    return approval


def _check_init() -> None:
    if _session is None or _agent is None:
        raise HTTPException(503, 'web session not initialized')


def _open_session_seat(sid: str, *, allow_missing: bool) -> Seat:
    """取/建一个会话的 Seat（不切换全局焦点）。

    Seat get-or-create 语义：同一 sid 第二次调用复用已有 Seat（其 agent
    若在跑继续跑、事件日志自然延续），而不是重建——这正是并发隔离的要义：
    两会话并行时各自 Seat 独立，互不销毁。首次调用 adopt 重放日志 =
    恢复该会话的记忆。
    """
    log_path = (_sessions_dir or Path('.sessions')) / f'{sid}.jsonl'
    if not allow_missing and not log_path.exists():
        raise HTTPException(404, f'session {sid!r} not found')
    seat = _seats.get(sid)
    if seat is not None:
        return seat
    session = Session(id=sid)
    if log_path.exists():
        for event in load_events(log_path):
            session.adopt(event)
    session.bind_store(log_path)  # 追加式实时落盘（没有状态不进日志）
    if not log_path.exists():
        # 新会话立即落盘（空文件）：列表可见、可切换——"会话存在 = 有文件"
        log_path.touch()
    seat = Seat(sid, session, None)
    _seats[sid] = seat  # 先登记：审批钩子闭包引用 seat，创建 agent 前就位
    hooks = Hooks()
    hooks.approval = _approval_for(seat)   # Web 版确认：弹本会话的批准/拒绝
    seat.agent = build_agent(
        session, _args,
        {'reasoning_started': False, 'request_no': 0, 'tool_no': 0},
        hooks=hooks,
    )
    return seat


def _surface_with_seq(session) -> list[tuple[int, object]]:
    """按 surface 序的 (seq, Message)——镜像 derive_messages 但保留 seq。

    历史渲染需要知道每条 user 消息属于第几个回合（turn/start 到 turn/end
    之间）——同回合后续的 user 消息 = steer 插队，前端不该画新回合分隔线。
    derive_messages 只返回 Message（无 seq），这里补上 seq 供回合映射用。
    保持与 derive_messages 相同的折叠规则（assistant 空 content 跳过）。
    """
    out: list[tuple[int, object]] = []
    events = session.events
    for seq in session.surface:
        event = events[seq]
        if event.type == 'user/message':
            out.append((seq, event.data))
        elif event.type == 'assistant/message':
            message = cast(dict, event.data)['message']
            if message.content:
                out.append((seq, message))
        elif event.type == 'tool/result':
            out.append((seq, event.data))
    return out


def _user_message_turns(session) -> dict[int, int]:
    """user/message surface 事件的 seq → 回合号（扫描 turn/start 划界）。

    只有真人发言（surface append 的 user/message）需要回合归属；checkpoint
    （replace 顶替）单独由 role 识别，不进此表。
    """
    mapping: dict[int, int] = {}
    turn = 0
    for event in session.events:
        if event.type == 'turn/start':
            turn = int(event.data['turn'])
        elif event.type == 'user/message' and event.surface_op == 'append':
            mapping[event.seq] = turn
    return mapping


def _history_payloads(session) -> list[dict]:
    """会话历史消息载荷（页面加载/刷新用），user 消息附带其回合归属。

    message_to_payload 是纯消息 → dict，不知道回合；这里在构造处补上
    'turn' 字段，前端据此区分"新回合首条"与"同回合插队（steer）"。
    """
    turns = _user_message_turns(session)
    payloads = []
    for seq, message in _surface_with_seq(session):
        payload = message_to_payload(message)
        if payload['role'] == 'user' and seq in turns:
            payload['turn'] = turns[seq]
        payloads.append(payload)
    return payloads


def _open_session(sid: str, *, allow_missing: bool) -> dict:
    """打开/切换到会话：Seat get-or-create + 设置全局焦点（_session/_agent 别名）。"""
    global _session, _agent, _current_sid
    seat = _open_session_seat(sid, allow_missing=allow_missing)
    # 设置全局焦点（便捷别名：测试与旧路由仍读 _session/_agent）
    _session = seat.session
    _agent = seat.agent
    _current_sid = sid
    return {
        'id': sid,
        'history': _history_payloads(seat.session),
        'todos': fold_todos(seat.session) or [],  # 当前 todo 投影：切换会话时恢复 dock
        'context': _context_payload(seat.session),  # 上下文占用：切换会话时恢复圆环
    }


def _validate_sid(sid: str) -> None:
    """会话 id 只允许安全字符（防路径穿越：.sessions/../.env 之类）。"""
    if not sid or sid != Path(sid).name or '..' in sid:
        raise HTTPException(400, f'invalid session id: {sid!r}')


def _scan_sessions() -> list[dict]:
    """扫描会话目录：id / 事件数 / 更新时间 / 标题 / 首条用户消息摘要。

    磁盘行内快扫（不整包解析）：标题 = 最后一条 session/title 事件的 title
    （user 手动 > auto 自动，按追加顺序后者覆盖）；无标题事件时列表显示
    首条 user/message 摘要作为 fallback（对齐 harness 的三级来源）。
    """
    items: list[dict] = []
    for path in sorted((_sessions_dir or Path('.sessions')).glob('*.jsonl')):
        events = 0
        summary = ''
        title = ''
        title_source = ''
        with path.open(encoding='utf-8') as fh:
            for line in fh:
                events += 1
                # 找第一条 user/message 当 fallback 摘要
                if not summary and '"type": "user/message"' in line:
                    try:
                        data = (json.loads(line).get('data') or {}).get('$message') or {}
                        for block in data.get('content') or []:
                            if '$text' in block:
                                summary = block['$text'][:60]
                                break
                    except (TypeError, ValueError, KeyError):
                        pass
                # 最后一条 session/title 事件即当前标题（后者覆盖前者）
                if '"type": "session/title"' in line:
                    try:
                        data = (json.loads(line).get('data') or {}).get('$dict') or {}
                        title = data.get('title', '')
                        title_source = data.get('source', '')
                    except (TypeError, ValueError, KeyError):
                        pass
        items.append({
            'id': path.stem,
            'events': events,
            'updated': path.stat().st_mtime,
            'title': title or None,
            'title_source': title_source or None,
            'summary': title or summary or '(empty)',  # 标题优先，摘要兜底
        })
    items.sort(key=lambda item: item['updated'], reverse=True)
    return items


def init_web(workspace: Path, fake: bool = False, model: str = 'deepseek-v4-flash',
             sessions_dir: Path | None = None, sid: str = 'web',
             compact_at: int | None = None) -> None:
    """初始化全局状态（测试可注入 workspace / fake / sessions_dir / compact_at）。

    Seat 注册表是模块级缓存：init_web 每次调用都要清空重来——测试每个
    init_web 用独立 sessions_dir，若不清空，旧目录的 seat（同 sid 如 'web'）
    会被复用，把上个会话的内存日志带进新初始化。
    """
    global _sessions_dir, _args, _session, _agent, _current_sid
    _seats.clear()
    _session = None
    _agent = None
    _current_sid = ''
    _sessions_dir = Path(sessions_dir or '.sessions')
    _args = argparse.Namespace(
        fake=fake, model=model, workspace=workspace, hide_reasoning=False,
        session=sid, sessions=str(_sessions_dir), prompt='', resume=False, verbose=False,
        compact_at=compact_at,
    )
    _open_session(sid, allow_missing=True)


def event_to_payload(event, session: Session | None = None) -> dict | None:
    """会话事件 → 前端最小协议（只挑前端关心的；其余事件前端不渲染）。

    session 参数：turn/end 附带的 context 属于"这个事件的会话"（并发下
    多个会话各自跑，不能读全局焦点会话的占用）——SSE 循环按 seat 传入。
    """
    if event.type == 'assistant/chunk':
        text = event.data['chunk']['text']
        return {'type': 'chunk', 'text': text} if text else None
    if event.type == 'assistant/reasoning/chunk':
        reasoning = event.data['reasoning']
        return {'type': 'reasoning', 'text': reasoning} if reasoning else None
    if event.type == 'tool/call':
        return {
            'type': 'tool_call',
            'call_id': event.data['call_id'],
            'name': event.data['name'],
            'arguments': event.data['arguments'],  # 原始 JSON 字符串
        }
    if event.type == 'tool/result':
        block = event.data.content[0]
        return {'type': 'tool_result', **_result_payload(block)}
    if event.type == 'todo/write':
        # 常驻 dock 的实时更新：模型每次重写清单，前端面板跟着变
        todos = event.data.get('todos') if isinstance(event.data, dict) else None
        return {'type': 'todo_update', 'todos': todos or []}
    if event.type == 'turn/end':
        # 回合结束附带上下文占用（圆环数据）：真实 usage 估算 / 1M 窗口。
        # 用事件所属会话的占用（并发隔离：不要读全局焦点会话的）
        return {
            'type': 'turn_end',
            'reason': event.data['reason'],
            'context': _context_payload(session),
        }
    return None


def _context_payload(session: Session | None = None) -> dict | None:
    """当前上下文占用（前端圆环）+ 会话级累计账（消耗 token / 缓存命中率）。

    used/percent/window 是"当前上下文快照"（最后一条真实 usage 或估算）；
    cache_hit_pct / total_tokens 是"全会话累计"（对齐 dsh StatsLine 的
    totals 投影语义：Σ 求和、token 加权，不是逐请求平均，也不是单次快照）。
    """
    target = session if session is not None else _session
    if target is None:
        return None
    used = estimate_context_tokens(target)
    percent = min(100, round(used * 100 / MODEL_CONTEXT_WINDOW))
    payload: dict = {'used': used, 'window': MODEL_CONTEXT_WINDOW, 'percent': percent}
    totals = session_token_totals(target)
    if totals is None:
        return payload  # 无真实 usage（如 fake）：只有占用快照，没有累计账
    rate = cache_hit_rate(target)
    payload['session'] = {
        'total_tokens': totals['input_tokens'] + totals['output_tokens'],
        'input_tokens': totals['input_tokens'],
        'output_tokens': totals['output_tokens'],
    }
    if rate is not None:  # 有请求报了缓存拆分才带命中率（fake/全缺拆分不显示）
        payload['session']['cache_hit_pct'] = round(rate * 100)
    return payload


def message_to_payload(message) -> dict:
    """折叠出的模型消息 → 历史渲染用（页面加载时一次性展示）。

    tool/result 在内部是 user 角色消息（README：wire 层才变 role: tool），
    但历史渲染里它们应并入工具活动块而不是当用户消息——这里显式标记
    role='tool_result'，避免前端把"空文本的 user 角色消息"渲染成空白气泡。
    """
    texts = [b.text for b in message.content if getattr(b, 'type', '') == 'text']
    tool_calls = [
        {'call_id': b.id, 'name': b.name, 'arguments': b.arguments}
        for b in message.content if getattr(b, 'type', '') == 'tool-call'
    ]
    tool_results = [
        _result_payload(b)
        for b in message.content if getattr(b, 'type', '') == 'tool-result'
    ]
    role = message.role
    text = '\n'.join(texts)
    if role == 'user' and not texts and tool_results:
        role = 'tool_result'  # 纯工具结果消息：并入当前工具活动块
    elif role == 'user' and '<compacted-summary>' in text:
        # 压缩 checkpoint（replace 顶替旧回合的 user/message）：不是真人发言，
        # 前端渲染成可折叠的"上下文已压缩"卡片而非用户气泡
        role = 'checkpoint'
    return {
        'role': role,
        'text': text,
        'tool_calls': tool_calls,
        'tool_results': tool_results,
    }


@app.get('/')
def index() -> FileResponse:
    return FileResponse(_ROOT / 'web' / 'index.html')


@app.get('/meta')
def meta() -> dict:
    """前端元信息：workspace 根（工具路径相对化显示用）+ 模型名 + fake 标记。"""
    _check_init()
    assert _args is not None  # init_web 已初始化（_check_init 保证）
    return {
        'workspace': str(Path(_args.workspace).resolve()),  # 绝对路径，前端剥前缀显示相对路径
        'model': _args.model,
        'fake': bool(_args.fake),
    }


@app.get('/sessions')
def sessions() -> list[dict]:
    _check_init()
    return _scan_sessions()


@app.post('/sessions/new')
def new_session() -> dict:
    """新建会话并切换（id 带时间戳；首条消息才落盘）。"""
    _check_init()
    sid = f'web-{int(time.time())}'
    return _open_session(sid, allow_missing=True)


@app.post('/sessions/{sid}/switch')
def switch_session(sid: str) -> dict:
    """切换到已有会话（重放日志 = 恢复该会话的记忆）。"""
    _check_init()
    _validate_sid(sid)
    return _open_session(sid, allow_missing=False)


@app.post('/sessions/{sid}/delete')
def delete_session(sid: str) -> dict:
    """删除会话文件。当前会话不可删（删了状态会混乱）——先切换走再删。"""
    _check_init()
    _validate_sid(sid)
    if sid == _current_sid:
        raise HTTPException(400, 'cannot delete the active session — switch away first')
    path = (_sessions_dir or Path('.sessions')) / f'{sid}.jsonl'
    if not path.exists():
        raise HTTPException(404, f'session {sid!r} not found')
    path.unlink()
    return {'deleted': sid}


@app.post('/sessions/{sid}/title')
async def session_title(sid: str, request: Request) -> dict:
    """手动改名：校验后 append session/title（source='user'），覆盖自动标题。

    目标会话不要求是当前会话（列表里改任意会话名）；标题即日志投影。
    """
    _check_init()
    _validate_sid(sid)
    body = await request.json()
    title = _clean_title(str(body.get('title') or ''))
    if not title:
        raise HTTPException(400, 'title must not be empty')
    _append_title(sid, title, source='user')
    return {'id': sid, 'title': title, 'source': 'user'}


@app.get('/history')
def history(sid: str | None = None) -> dict:
    """指定会话的历史消息 + 当前 todo 投影（页面加载/刷新时恢复 UI）。

    query ?sid= 可选：缺省 = 全局焦点会话（旧前端/测试不带 sid 也能跑）；
    多标签页并行时前端带自己看的 sid，各取各的 Seat。
    """
    _check_init()
    target_sid = sid or _current_sid
    seat = _seats.get(target_sid)
    if seat is None:
        raise HTTPException(404, f'session {target_sid!r} not open — switch to it first')
    return {
        'history': _history_payloads(seat.session),
        'todos': fold_todos(seat.session) or [],
        'context': _context_payload(seat.session),
    }


@app.post('/chat')
async def chat(request: Request) -> StreamingResponse:
    """发起一轮对话并以 SSE 流返回事件；客户端断开即取消该会话 agent。

    body.sid 可选：缺省 = 全局焦点会话（旧前端/测试不带 sid 也能跑）。
    显式带 sid 时定位到对应 seat——两会话各开一条 SSE 流，互不干扰。
    """
    _check_init()
    assert _session is not None and _agent is not None  # 初始化后必有焦点 seat
    body = await request.json()
    message = (body.get('message') or '').strip()
    if not message:
        raise HTTPException(400, 'message must not be empty')
    sid = body.get('sid') or _current_sid
    seat = _seats.get(sid) or _open_session_seat(sid, allow_missing=True)
    session, agent = seat.session, seat.agent

    queue: asyncio.Queue = asyncio.Queue()
    unsubscribe = session.on_event(lambda event: queue.put_nowait(event))

    # 自动起名：首条用户消息一旦落日志立即触发（不等回合结束——回合可能因
    # approval / 长任务迟迟不结束；标题只依赖第一条消息，尽早起名体验最好）。
    # 对齐 harness：监听 user/message，title 事件是独立小请求。
    title_spawned = {'done': False}

    def _watch_first_user_message(event) -> None:
        if title_spawned['done']:
            return
        if event.type == 'user/message' and _first_user_message_just_landed(session):
            title_spawned['done'] = True
            _spawn(_auto_title(seat, _first_text_of(event.data)))

    watch = session.on_event(_watch_first_user_message)

    async def run_agent() -> None:
        try:
            agent.followup(message)
            await agent.when_idle()
        finally:
            await queue.put(DONE_MARKER)

    task = asyncio.create_task(run_agent())

    async def sse_stream():
        # 本会话的活跃队列挂到 seat：per-seat approval 钩子经它推送请求。
        # （旧实现写全局 _active_queue——两会话并行时会被互相覆盖）
        seat.queue = queue
        try:
            while True:
                item = await queue.get()
                if item is DONE_MARKER:
                    break
                if isinstance(item, dict):
                    # Web approval 请求（钩子直接放的自定义载荷，非 session 事件）
                    yield f'data: {json.dumps(item, ensure_ascii=False)}\n\n'
                    continue
                payload = event_to_payload(item, session)
                if payload is not None:
                    yield f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'
        finally:
            # 客户端断开（停止按钮 / 关页面）：取消该会话 agent，记账由循环层完成
            task.cancel()
            unsubscribe()
            watch()   # 退订首条消息监听（会话切换后不留悬挂监听）
            if seat.queue is queue:   # 只有自己挂的才清（并发：别清掉别的流的）
                seat.queue = None

    return StreamingResponse(sse_stream(), media_type='text/event-stream')


@app.post('/steer')
async def steer(request: Request) -> dict:
    """运行中插队：把消息塞进当前回合的 next-step 队列（steer，即时生效）。

    与 /chat 的分工：idle 时新开回合走 /chat（followup）；回合进行中
    改方向/加指令走 /steer——消息作为当前回合的下一步处理，事件继续
    沿已打开的 SSE 流推送（回合不结束）。
    前置：该会话的 agent 必须在跑（有活跃对话流）；idle 时用 /chat。
    """
    _check_init()
    assert _session is not None and _agent is not None
    body = await request.json()
    message = (body.get('message') or '').strip()
    if not message:
        raise HTTPException(400, 'message must not be empty')
    sid = body.get('sid') or _current_sid
    seat = _seats.get(sid)
    if seat is None:
        raise HTTPException(404, f'session {sid!r} not open — switch to it first')
    if seat.queue is None:
        raise HTTPException(409, '会话没有活跃对话流——用 /chat 开新回合')
    if seat.agent.status != 'running':
        # 竞态窗口：回合刚 turn_end、SSE 尚未收尾（DONE 未发）时 queue 还在，
        # 但 agent 已 idle——插队入队后事件会没人读（流即将关闭）。拒绝，
        # 前端会把输入放回，等回合真正结束后走 /chat。
        raise HTTPException(409, 'agent 已空闲——回合即将结束，请稍后用普通消息')
    seat.agent.steer(message)
    return {'ok': True, 'sid': sid, 'queued': 'next-step'}


@app.post('/compact')
async def compact(request: Request) -> dict:
    """手动压缩会话：把旧回合折叠成 checkpoint（复用 run_compaction）。

    body.sid 可选（缺省 = 全局焦点会话；无 body 也允许——旧前端/测试
    直接 POST 空体压缩当前会话）。前置校验（对齐 dsh /compact 命令的
    串行语义）：
    - fake 模式拒绝：脚本模型不能生成摘要（自动压缩本来也不挂）
    - agent 运行中拒绝：压缩事务会动 surface，与进行中的回合冲突
    - 无可压段（旧回合不足）→ compacted=False + reason，由前端提示
    """
    _check_init()
    assert _session is not None and _agent is not None
    if _args is not None and _args.fake:
        raise HTTPException(400, 'fake 模式不支持手动压缩（脚本模型不能生成摘要）')
    # body 可选：空体也允许（压缩焦点会话）
    raw = await request.body()
    sid = _current_sid
    if raw:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as error:
            raise HTTPException(400, 'invalid JSON body') from error
        sid = (body or {}).get('sid') or _current_sid
    seat = _seats.get(sid)
    if seat is None:
        raise HTTPException(404, f'session {sid!r} not open — switch to it first')
    agent = seat.agent
    if agent.status != 'idle':
        raise HTTPException(409, 'agent 正在运行——回合结束后再压缩')
    if select_compact_range(seat.session, keep_turns=1) is None:
        return {'compacted': False, 'reason': '没有可压缩的旧回合'}
    ok = await run_compaction(
        seat.session, agent.llm, keep_turns=1,
        model=agent.options.get('model', ''),
    )
    return {'compacted': ok,
            'reason': '压缩完成' if ok else '压缩未完成（摘要生成失败或摘要未通过校验）'}


@app.post('/approval/respond')
async def approval_respond(request: Request) -> dict:
    """浏览器对 approval 请求的响应：批准（true）或拒绝（false），唤醒钩子。

    aid 在各 seat 的 approvals 里查（并发：每个会话的审批表独立，按 aid 唯一）。
    """
    body = await request.json()
    aid = body.get('id')
    for seat in _seats.values():
        fut = seat.approvals.get(aid)
        if fut is not None:
            if not fut.done():
                fut.set_result(bool(body.get('approved')))
            return {'ok': True, 'sid': seat.sid}
    raise HTTPException(404, f'unknown approval id {aid!r}')


def main() -> None:
    parser = argparse.ArgumentParser(description='agent-demo Web UI (DeepSeek-style chat)')
    parser.add_argument('--workspace', type=Path, required=True,
                        help='workspace root directory — tools may only read/write inside it (required)')
    parser.add_argument('--fake', action='store_true', help='offline scripted model (architecture demo)')
    parser.add_argument('--model', default='deepseek-v4-flash', help='model id for the OpenAI-compatible API')
    parser.add_argument('--host', default='127.0.0.1', help='bind host (default 127.0.0.1)')
    parser.add_argument('--port', default=8000, type=int, help='bind port (default 8000)')
    parser.add_argument('--compact-at', type=int, default=None, metavar='TOKENS',
                        help='auto-compact when the routed context exceeds TOKENS '
                             '(deepseek-v4 window is 1M; default off)')
    args = parser.parse_args()
    load_env(_ROOT / '.env')  # 与 CLI 一致：注入 .env 的 API key
    init_web(args.workspace, fake=args.fake, model=args.model, compact_at=args.compact_at)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
