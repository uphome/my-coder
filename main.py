"""CLI 入口：一句话任务 → 跑完整个 agent → 流式打印。

演示"UI 是日志的投影"：屏幕上的输出不是从模型回调来的，
而是订阅 session 事件（assistant/chunk、tool/call）渲染的。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from agent import Agent
from llm import FakeLlm, OpenAiCompatibleLlm
from persistence import load_events
from prompt import PromptRegistry
from session import Session
from tools import ToolOutcome, ToolRegistry, ToolSpec
from values import TextBlock

DEMO_SCRIPT = [
    {
        'tool_calls': [{'id': 'call-1', 'name': 'read_file', 'arguments': json.dumps({'file_path': 'README.md'})}],
        'finish_reason': 'tool_calls',
    },
    {'text': 'README 讲的是这个 demo 的四层架构。任务完成。', 'finish_reason': 'stop'},
]


def load_env(path: Path) -> None:
    """把 .env 里的 KEY=VALUE 注入进程环境；已存在的环境变量优先（不覆盖）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def build_tools() -> ToolRegistry:
    registry = ToolRegistry()

    async def read_file(args, agent, signal):
        path = _resolve(args['file_path'])
        if not path.is_file():
            return ToolOutcome(content=f'file not found: {args["file_path"]}', is_error=True)
        return ToolOutcome(content=path.read_text(encoding='utf-8', errors='replace'))

    async def list_files(args, agent, signal):
        path = _resolve(args.get('dir_path', '.'))
        if not path.is_dir():
            return ToolOutcome(content=f'not a directory: {path}', is_error=True)
        names = sorted(entry.name for entry in path.iterdir())
        return ToolOutcome(content='\n'.join(names) if names else '(empty)')

    async def write_file(args, agent, signal):
        path = _resolve(args['file_path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args['content'], encoding='utf-8')
        return ToolOutcome(content=f'wrote {path}')

    async def todo_write(args, agent, signal):
        agent.session.append('todo/write', {'todos': args.get('todos', [])})
        return ToolOutcome(content='todo list updated')

    registry.register(ToolSpec(
        name='read_file',
        description='Read a UTF-8 text file. Results include the full content.',
        parameters={
            'type': 'object',
            'properties': {'file_path': {'type': 'string'}},
            'required': ['file_path'],
        },
        execute=read_file,
    ))
    registry.register(ToolSpec(
        name='list_files',
        description='List entries in a directory.',
        parameters={
            'type': 'object',
            'properties': {'dir_path': {'type': 'string'}},
        },
        execute=list_files,
    ))
    registry.register(ToolSpec(
        name='write_file',
        description='Create or overwrite a UTF-8 text file.',
        parameters={
            'type': 'object',
            'properties': {
                'file_path': {'type': 'string'},
                'content': {'type': 'string'},
            },
            'required': ['file_path', 'content'],
        },
        execute=write_file,
    ))
    registry.register(ToolSpec(
        name='todo_write',
        description='Record and update a structured task list. The ENTIRE list replaces the previous one.',
        parameters={
            'type': 'object',
            'properties': {
                'todos': {'type': 'array', 'items': {'type': 'object'}},
            },
            'required': ['todos'],
        },
        execute=todo_write,
    ))
    return registry


def _resolve(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else Path.cwd() / path


def build_agent(session: Session, args) -> Agent:
    prompt = PromptRegistry()
    prompt.section('identity', -100, 'You are a coding agent powered by DeepSeek Harness (Python demo).')
    prompt.section('persona', 0, 'You run on the {{model}} model in {{cwd}}.\nVerify work by running code or tests. Keep answers brief.')
    prompt.section('tool:todo', 110, 'Use todo_write to plan multi-step work before you start.')
    prompt.variable('model', lambda ctx: ctx['agent'].options.get('model', ''))
    prompt.variable('cwd', lambda ctx: os.getcwd())

    if args.fake:
        llm = FakeLlm(script=DEMO_SCRIPT, provider='fake', model='fake-model')
        options = {'provider': 'fake', 'model': 'fake-model'}
    else:
        api_key = os.environ.get('DEEPSEEK_API_KEY')
        if not api_key:
            raise SystemExit('missing DEEPSEEK_API_KEY (set it in the environment or run with --fake)')
        llm = OpenAiCompatibleLlm(
            base_url=os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com'),
            api_key=api_key,
            model=args.model,
            provider='deepseek',
        )
        options = {'provider': 'deepseek', 'model': args.model}

    agent = Agent(session=session, llm=llm, prompt=prompt, tools=build_tools(), options=options)

    def on_event(event) -> None:
        if event.type == 'assistant/chunk':
            print(event.data['chunk']['text'], end='', flush=True)
        elif event.type == 'tool/call':
            print(f'\n[tool] {event.data["name"]}({event.data["arguments"]})', flush=True)
        elif event.type == 'tool/result':
            result = event.data
            block = result.content[0]
            print(f'[result] {block.content[:200]}{"…" if len(block.content) > 200 else ""}', flush=True)

    session.on_event(on_event)
    return agent


async def run(args) -> None:
    load_env(Path(__file__).parent / '.env')
    session_path = Path(args.sessions) / f'{args.session}.jsonl'
    if session_path.exists() and not args.resume:
        raise SystemExit(
            f'session {args.session!r} already exists at {session_path} — '
            f'pass --resume to continue it, pick a new --session id, or delete the file',
        )
    session = Session(id=args.session)
    if args.resume and session_path.exists():
        for event in load_events(session_path):
            session.adopt(event)
        print(f'resumed {args.session}: {len(session.events)} events restored')
    session.bind_store(session_path)
    agent = build_agent(session, args)
    agent.followup(args.prompt)
    await agent.when_idle()
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description='Python demo of the harness agent architecture')
    parser.add_argument('prompt', help='the task to run')
    parser.add_argument('--session', default='main', help='session id (JSONL file under --sessions)')
    parser.add_argument('--sessions', default='.sessions', help='directory for JSONL session logs')
    parser.add_argument('--model', default='deepseek-chat', help='model id for the OpenAI-compatible API')
    parser.add_argument('--resume', action='store_true', help='resume the session from its JSONL log')
    parser.add_argument('--fake', action='store_true', help='offline scripted model (architecture demo)')
    parser.add_argument('--verbose', action='store_true', help='debug logging')
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
