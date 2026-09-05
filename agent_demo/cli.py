"""CLI 入口：一句话任务 → 跑完整个 agent → 流式打印；无任务 → REPL 多轮对话。

演示"UI 是日志的投影"：屏幕上的输出不是从模型回调来的，
而是订阅 session 事件（assistant/chunk、tool/call）渲染的。

用法：
    python -m agent_demo.cli "任务" --workspace <root> [--fake]   # 单次任务
    python -m agent_demo.cli --workspace <root> [--fake]          # REPL（多轮）
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


async def _prepare(args) -> tuple[Session, dict]:
    """加载/重放会话并返回 session 与渲染状态（单次任务与 REPL 共用）。"""
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
    return session, ui_state


async def run(args) -> None:
    """单次任务：跑完一个回合即退出。"""
    load_env(Path(__file__).resolve().parent.parent / '.env')
    session, ui_state = await _prepare(args)
    agent = build_agent(session, args, ui_state)
    agent.followup(args.prompt)
    await agent.when_idle()
    print()


async def run_repl(args) -> None:
    """REPL 多轮对话：/exit 退出，每轮新回合（替代 '每次跑一次命令 + --resume'）。

    顺序模型：输入 → 回合跑完（when_idle）→ 再提示。运行中打断（steer /
    双队列 next-step）由 Web 端承担——CLI 下若回合运行中读 stdin 会与
    approval 的 stdin 交互竞争输入（y/n 可能被当消息吃掉），故不复用。
    敏感工具（bash/write/edit）仍走默认 approval：轮询到敏感工具时
    会停下问 y/N（stdin 此刻空闲，无竞争）。
    """
    load_env(Path(__file__).resolve().parent.parent / '.env')
    session, ui_state = await _prepare(args)
    agent = build_agent(session, args, ui_state)
    print('agent-demo REPL — 输入任务开始，/exit 退出，/compact 手动压缩。')
    while True:
        try:
            line = await asyncio.to_thread(input, 'agent> ')
        except (EOFError, KeyboardInterrupt):
            print()
            break
        text = line.strip()
        if not text:
            continue
        if text == '/exit':
            break
        if text == '/compact':
            from .compaction import run_compaction as do_compact
            ok = await do_compact(session, agent.llm, keep_turns=1,
                                  model=agent.options.get('model', ''))
            print('[compact]', '完成' if ok else '无可压缩内容或失败')
            continue
        agent.followup(text)
        await agent.when_idle()   # 顺序：等本回合收敛再提示下一条
        print()
    print('bye')


def main() -> None:
    parser = argparse.ArgumentParser(description='Python demo of the harness agent architecture')
    parser.add_argument('prompt', nargs='?', default=None,
                        help='the task to run (omit to enter the interactive REPL)')
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
    if args.prompt:
        asyncio.run(run(args))
    else:
        asyncio.run(run_repl(args))


if __name__ == '__main__':
    main()
