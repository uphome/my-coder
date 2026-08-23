"""Web UI：浏览器里的 DeepSeek 风格对话。

架构：UI 是日志的投影的第二个渲染器——复用现有框架（Session/Agent/
build_tools/loop 一行不改），session.on_event 订阅事件，经 asyncio.Queue
桥接成 SSE 流推给浏览器。CLI 是终端投影，Web 是 DOM 投影，同一份日志。

最小方案：单个会话（固定 id='web'，启动时重放已有日志）+ 流式输出 +
思考折叠 + 工具卡片。approval 在 Web 下走默认实现（服务端无交互 stdin →
EOF → fail-safe 拒绝），Web 上的批准/拒绝按钮是迭代项。
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from persistence import load_events
from session import Session

from main import build_agent, load_env

app = FastAPI(title='agent-demo web')

_session: Session | None = None
_agent = None

DONE_MARKER = object()


def init_web(workspace: Path, fake: bool = False, model: str = 'deepseek-chat') -> None:
    """初始化全局会话与 agent（测试可注入 workspace / fake）。

    会话固定 id='web'：启动时重放已有日志（记忆延续，刷新页面不丢对话）。
    """
    global _session, _agent
    args = argparse.Namespace(
        fake=fake, model=model, workspace=workspace, hide_reasoning=False,
        session='web', sessions='.sessions', prompt='', resume=False, verbose=False,
    )
    session = Session(id='web')
    log_path = Path(args.sessions) / 'web.jsonl'
    if log_path.exists():
        for event in load_events(log_path):
            session.adopt(event)
    _session = session
    _agent = build_agent(session, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})


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


@app.get('/history')
def history() -> list[dict]:
    if _session is None:
        raise HTTPException(503, 'web session not initialized')
    return [message_to_payload(m) for m in _session.derive_messages()]


@app.post('/chat')
async def chat(request: Request) -> StreamingResponse:
    """发起一轮对话并以 SSE 流返回事件；客户端断开即取消 agent。"""
    if _session is None or _agent is None:
        raise HTTPException(503, 'web session not initialized')
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
