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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

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
_session: Session | None = None
_agent = None
_current_sid: str = ''

# Web approval：敏感工具（bash/write/edit）执行前推送请求给浏览器，等待人工批准/拒绝
_approval_futures: dict[str, asyncio.Future] = {}
_active_queue: asyncio.Queue | None = None
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


def _should_auto_title(session: Session) -> bool:
    """首条消息后是否值得自动起名：真模型 + 还没有标题 + 尚无任何用户消息。

    一旦会话已有 user/message（resume 继续对话）就不再自动起名——
    标题应基于"第一条"消息，晚了不如让用户手动改。
    """
    if _args is None or _args.fake:
        return False
    if any(e.type == 'session/title' for e in session.events):
        return False
    if any(e.type == 'user/message' for e in session.events):
        return False
    return True


async def _auto_title(session: Session, first_text: str) -> None:
    """自动起名：复用 agent 的 LLM 客户端发一个小请求，结果落 session/title。

    后台任务、与主对话并发互不干扰；任何失败（网络/超时/空输出）都静默
    跳过——列表继续显示 fallback 摘要，起名失败绝不打扰主流程。
    """
    if _agent is None:
        return
    try:
        request = LlmRequest(
            system=TITLE_SYSTEM,
            model=_args.model,
            messages=(create_user_message([TextBlock(text=first_text[:400])]),),
            max_tokens=30,
        )
        parts: list[str] = []

        async def collect() -> None:
            async for chunk in _agent.llm.stream(request):
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
        session.append('session/title', {'title': title, 'source': 'auto'})
    except Exception as error:  # noqa: BLE001 - 起名失败不影响主流程
        print(f'[title] auto-title skipped: {type(error).__name__}: {error}', flush=True)


def _append_title(sid: str, title: str, source: str) -> None:
    """往一个会话追加 session/title 事件（标题 = 日志投影，改完即落盘）。

    目标是当前会话直接用全局 session；否则临时重放目标日志 + bind_store
    append（不切换当前会话——从列表里改别的会话名是允许的）。
    """
    if sid == _current_sid and _session is not None:
        _session.append('session/title', {'title': title, 'source': source})
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


async def web_approval(name: str, arguments: dict) -> bool:
    """Web 版 approval 钩子：推送 approval_request 到 SSE 流，await 前端的 respond。

    只在当前对话的 SSE 流活跃时可用；超时/无流一律 fail-safe 拒绝。
    """
    if _active_queue is None:
        return False
    aid = uuid.uuid4().hex
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _approval_futures[aid] = fut
    await _active_queue.put({
        'type': 'approval_request', 'id': aid, 'name': name, 'arguments': arguments,
    })
    try:
        return await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT_S)
    except TimeoutError:
        return False
    finally:
        _approval_futures.pop(aid, None)


def _check_init() -> None:
    if _session is None or _agent is None:
        raise HTTPException(503, 'web session not initialized')


def _open_session(sid: str, *, allow_missing: bool) -> dict:
    """打开一个会话（校验 id → adopt 重放 → 重建 agent），返回会话描述。

    allow_missing=True：会话文件不存在时也允许（首次启动/新建，首条消息才落盘）。
    """
    global _session, _agent, _current_sid
    log_path = (_sessions_dir or Path('.sessions')) / f'{sid}.jsonl'
    if not allow_missing and not log_path.exists():
        raise HTTPException(404, f'session {sid!r} not found')
    session = Session(id=sid)
    if log_path.exists():
        for event in load_events(log_path):
            session.adopt(event)
    session.bind_store(log_path)  # 追加式实时落盘（没有状态不进日志）
    if not log_path.exists():
        # 新会话立即落盘（空文件）：列表可见、可切换——"会话存在 = 有文件"
        log_path.touch()
    _session = session
    _current_sid = sid
    hooks = Hooks()
    hooks.approval = web_approval   # Web 版确认：前端弹"批准/拒绝"按钮
    _agent = build_agent(session, _args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0}, hooks=hooks)
    return {
        'id': sid,
        'history': [message_to_payload(m) for m in session.derive_messages()],
        'todos': fold_todos(session) or [],  # 当前 todo 投影：切换会话时恢复 dock
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
             sessions_dir: Path | None = None, sid: str = 'web') -> None:
    """初始化全局状态（测试可注入 workspace / fake / sessions_dir）。"""
    global _sessions_dir, _args
    _sessions_dir = Path(sessions_dir or '.sessions')
    _args = argparse.Namespace(
        fake=fake, model=model, workspace=workspace, hide_reasoning=False,
        session=sid, sessions=str(_sessions_dir), prompt='', resume=False, verbose=False,
    )
    _open_session(sid, allow_missing=True)


def event_to_payload(event) -> dict | None:
    """会话事件 → 前端最小协议（只挑前端关心的；其余事件前端不渲染）。"""
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
        return {'type': 'turn_end', 'reason': event.data['reason']}
    return None


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
    if role == 'user' and not texts and tool_results:
        role = 'tool_result'  # 纯工具结果消息：并入当前工具活动块
    return {
        'role': role,
        'text': '\n'.join(texts),
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
def history() -> dict:
    """当前会话的历史消息 + 当前 todo 投影（页面加载/刷新时恢复 UI）。"""
    _check_init()
    assert _session is not None
    return {
        'history': [message_to_payload(m) for m in _session.derive_messages()],
        'todos': fold_todos(_session) or [],
    }


@app.post('/chat')
async def chat(request: Request) -> StreamingResponse:
    """发起一轮对话并以 SSE 流返回事件；客户端断开即取消 agent。"""
    _check_init()
    assert _session is not None and _agent is not None  # 单例运行时保证非空
    session, agent = _session, _agent
    body = await request.json()
    message = (body.get('message') or '').strip()
    if not message:
        raise HTTPException(400, 'message must not be empty')

    queue: asyncio.Queue = asyncio.Queue()
    unsubscribe = session.on_event(lambda event: queue.put_nowait(event))

    # 首条消息才触发自动起名：标题 = 日志投影，起名失败静默降级 fallback
    need_title = _should_auto_title(session)
    if need_title:
        session_for_title = session

    async def run_agent() -> None:
        try:
            agent.followup(message)
            await agent.when_idle()
            if need_title:
                # 回合跑完再起名：标题基于首条消息全文，此刻消息已在日志里；
                # 后台任务与响应返回解耦，浏览器回合结束后稍等再刷列表即可看到
                asyncio.create_task(_auto_title(session_for_title, message))
        finally:
            await queue.put(DONE_MARKER)

    task = asyncio.create_task(run_agent())

    async def sse_stream():
        global _active_queue
        _active_queue = queue   # approval 钩子经它推送请求
        try:
            while True:
                item = await queue.get()
                if item is DONE_MARKER:
                    break
                if isinstance(item, dict):
                    # Web approval 请求（钩子直接放的自定义载荷，非 session 事件）
                    yield f'data: {json.dumps(item, ensure_ascii=False)}\n\n'
                    continue
                payload = event_to_payload(item)
                if payload is not None:
                    yield f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'
        finally:
            # 客户端断开（停止按钮 / 关页面）：取消 agent，记账由循环层完成
            task.cancel()
            unsubscribe()
            if _active_queue is queue:
                _active_queue = None

    return StreamingResponse(sse_stream(), media_type='text/event-stream')


@app.post('/approval/respond')
async def approval_respond(request: Request) -> dict:
    """浏览器对 approval 请求的响应：批准（true）或拒绝（false），唤醒钩子。"""
    body = await request.json()
    aid = body.get('id')
    fut = _approval_futures.get(aid)
    if fut is None:
        raise HTTPException(404, f'unknown approval id {aid!r}')
    if not fut.done():
        fut.set_result(bool(body.get('approved')))
    return {'ok': True}


def main() -> None:
    parser = argparse.ArgumentParser(description='agent-demo Web UI (DeepSeek-style chat)')
    parser.add_argument('--workspace', type=Path, required=True,
                        help='workspace root directory — tools may only read/write inside it (required)')
    parser.add_argument('--fake', action='store_true', help='offline scripted model (architecture demo)')
    parser.add_argument('--model', default='deepseek-v4-flash', help='model id for the OpenAI-compatible API')
    parser.add_argument('--host', default='127.0.0.1', help='bind host (default 127.0.0.1)')
    parser.add_argument('--port', default=8000, type=int, help='bind port (default 8000)')
    args = parser.parse_args()
    load_env(_ROOT / '.env')  # 与 CLI 一致：注入 .env 的 API key
    init_web(args.workspace, fake=args.fake, model=args.model)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
