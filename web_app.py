"""Web UI：浏览器里的 DeepSeek 风格对话。

架构：UI 是日志的投影的第二个渲染器——复用现有框架（Session/Agent/
build_tools/loop 一行不改），session.on_event 订阅事件，经 asyncio.Queue
桥接成 SSE 流推给浏览器。CLI 是终端投影，Web 是 DOM 投影，同一份日志。

功能：多会话（左侧栏列出 .sessions/*.jsonl，可新建/切换）+ 流式输出 +
思考折叠 + 工具卡片 + Markdown 渲染。approval 在 Web 下走默认实现
（无 stdin → EOF → fail-safe 拒绝），Web 批准/拒绝按钮是迭代项。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from persistence import load_events
from session import Session

from main import build_agent, load_env

app = FastAPI(title='agent-demo web')
# 前端依赖（marked / DOMPurify 本地 vendor，免 CDN）
app.mount('/vendor', StaticFiles(directory=Path(__file__).parent / 'web' / 'vendor'), name='vendor')

_sessions_dir: Path | None = None
_args: argparse.Namespace | None = None
_session: Session | None = None
_agent = None
_current_sid: str = ''

DONE_MARKER = object()


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
    _agent = build_agent(session, _args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
    return {'id': sid, 'history': [message_to_payload(m) for m in session.derive_messages()]}


def _validate_sid(sid: str) -> None:
    """会话 id 只允许安全字符（防路径穿越：.sessions/../.env 之类）。"""
    if not sid or sid != Path(sid).name or '..' in sid:
        raise HTTPException(400, f'invalid session id: {sid!r}')


def _scan_sessions() -> list[dict]:
    """扫描会话目录：id / 事件数 / 更新时间 / 首条用户消息摘要。"""
    items: list[dict] = []
    for path in sorted((_sessions_dir or Path('.sessions')).glob('*.jsonl')):
        events = 0
        summary = ''
        with path.open(encoding='utf-8') as fh:
            for line in fh:
                events += 1
                # 找第一条 user/message 当列表摘要（磁盘行内快扫，不整包解析）
                if not summary and '"type": "user/message"' in line:
                    try:
                        data = (json.loads(line).get('data') or {}).get('$message') or {}
                        for block in data.get('content') or []:
                            if '$text' in block:
                                summary = block['$text'][:60]
                                break
                    except (TypeError, ValueError, KeyError):
                        pass
        items.append({
            'id': path.stem,
            'events': events,
            'updated': path.stat().st_mtime,
            'summary': summary or '(empty)',
        })
    items.sort(key=lambda item: item['updated'], reverse=True)
    return items


def init_web(workspace: Path, fake: bool = False, model: str = 'deepseek-chat',
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
        return {'type': 'tool_call', 'name': event.data['name'], 'arguments': event.data['arguments']}
    if event.type == 'tool/result':
        block = event.data.content[0]
        return {
            'type': 'tool_result',
            'content': block.content[:500],
            'is_error': getattr(block, 'is_error', False),
        }
    if event.type == 'turn/end':
        return {'type': 'turn_end', 'reason': event.data['reason']}
    return None


def message_to_payload(message) -> dict:
    """折叠出的模型消息 → 历史渲染用（页面加载时一次性展示）。"""
    texts = [b.text for b in message.content if getattr(b, 'type', '') == 'text']
    tool_calls = [
        {'name': b.name, 'arguments': b.arguments}
        for b in message.content if getattr(b, 'type', '') == 'tool-call'
    ]
    tool_results = [
        {'content': b.content[:500], 'is_error': getattr(b, 'is_error', False)}
        for b in message.content if getattr(b, 'type', '') == 'tool-result'
    ]
    return {
        'role': message.role,
        'text': '\n'.join(texts),
        'tool_calls': tool_calls,
        'tool_results': tool_results,
    }


@app.get('/')
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / 'web' / 'index.html')


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


@app.get('/history')
def history() -> list[dict]:
    _check_init()
    return [message_to_payload(m) for m in _session.derive_messages()]


@app.post('/chat')
async def chat(request: Request) -> StreamingResponse:
    """发起一轮对话并以 SSE 流返回事件；客户端断开即取消 agent。"""
    _check_init()
    body = await request.json()
    message = (body.get('message') or '').strip()
    if not message:
        raise HTTPException(400, 'message must not be empty')

    queue: asyncio.Queue = asyncio.Queue()
    unsubscribe = _session.on_event(lambda event: queue.put_nowait(event))

    async def run_agent() -> None:
        try:
            _agent.followup(message)
            await _agent.when_idle()
        finally:
            await queue.put(DONE_MARKER)

    task = asyncio.create_task(run_agent())

    async def sse_stream():
        try:
            while True:
                event = await queue.get()
                if event is DONE_MARKER:
                    break
                payload = event_to_payload(event)
                if payload is not None:
                    yield f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'
        finally:
            # 客户端断开（停止按钮 / 关页面）：取消 agent，记账由循环层完成
            task.cancel()
            unsubscribe()

    return StreamingResponse(sse_stream(), media_type='text/event-stream')


def main() -> None:
    parser = argparse.ArgumentParser(description='agent-demo Web UI (DeepSeek-style chat)')
    parser.add_argument('--workspace', type=Path, required=True,
                        help='workspace root directory — tools may only read/write inside it (required)')
    parser.add_argument('--fake', action='store_true', help='offline scripted model (architecture demo)')
    parser.add_argument('--model', default='deepseek-chat', help='model id for the OpenAI-compatible API')
    parser.add_argument('--host', default='127.0.0.1', help='bind host (default 127.0.0.1)')
    parser.add_argument('--port', default=8000, type=int, help='bind port (default 8000)')
    args = parser.parse_args()
    load_env(Path(__file__).parent / '.env')  # 与 CLI 一致：注入 .env 的 API key
    init_web(args.workspace, fake=args.fake, model=args.model)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
