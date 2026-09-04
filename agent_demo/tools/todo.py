"""应用层工具：todo_write——结构化任务清单（记录 + 更新，整表替换）。"""
from __future__ import annotations

from ..registry import ToolOutcome, ToolSpec


def register(registry) -> None:
    async def todo_write(args, agent, signal):
        agent.session.append('todo/write', {'todos': args.get('todos', [])})
        return ToolOutcome(content='todo list updated')

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
