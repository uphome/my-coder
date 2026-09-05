"""CLI 入口：一句话任务 → 跑完整个 agent → 流式打印。

演示"UI 是日志的投影"：屏幕上的输出不是从模型回调来的，
而是订阅 session 事件（assistant/chunk、tool/call）渲染的。

用法：python -m agent_demo.cli <task> --workspace <root> [--fake]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .factory import build_agent, load_env
from .persistence import load_events
from .session import Session
from .ui import render_event


async def run(args) -> None:
    load_env(Path(__file__).resolve().parent.parent / '.env')
    session_path = Path(args.sessions) / f'{args.session}.jsonl'
    if session_path.exists() and not args.resume:
        raise SystemExit(
            f'session {args.session!r} already exists at {session_path} — '
            f'pass --resume to continue it, pick a new --session id, or delete the file',
        )
    # 渲染状态（request_no/tool_no/思维链开关）——resume 重放与新事件共用，
    # 所以打开会话 = 看到完整历史对话，序号从 1 连续到新回合。
    ui_state = {'reasoning_started': False, 'request_no': 0, 'tool_no': 0}
    session = Session(id=args.session)
    if args.resume and session_path.exists():
        for event in load_events(session_path):
            session.adopt(event)
            render_event(event, args.hide_reasoning, ui_state)  # 重放历史到终端
        print(f'resumed {args.session}: {len(session.events)} events restored')
    session.bind_store(session_path)
    agent = build_agent(session, args, ui_state)
    agent.followup(args.prompt)
    await agent.when_idle()
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description='Python demo of the harness agent architecture')
    parser.add_argument('prompt', help='the task to run')
    parser.add_argument('--session', default='main', help='session id (JSONL file under --sessions)')
    parser.add_argument('--sessions', default='.sessions', help='directory for JSONL session logs')
    parser.add_argument('--workspace', type=Path, required=True,
                        help='workspace root directory — tools may only read/write inside it (required)')
    parser.add_argument('--model', default='deepseek-v4-flash', help='model id for the OpenAI-compatible API')
    parser.add_argument('--resume', action='store_true', help='resume the session from its JSONL log')
    parser.add_argument('--fake', action='store_true', help='offline scripted model (architecture demo)')
    parser.add_argument('--hide-reasoning', action='store_true',
                        help='折叠（隐藏）思维链，只记录到日志，不打印到终端')
    parser.add_argument('--verbose', action='store_true', help='debug logging')
    parser.add_argument('--compact-at', type=int, default=None, metavar='TOKENS',
                        help='auto-compact threshold (default 524288 = half of the 1M '
                             'deepseek-v4 window); pass 0 to disable')
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
