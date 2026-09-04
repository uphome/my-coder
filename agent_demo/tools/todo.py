"""应用层工具：todo_write——结构化任务清单（记录 + 更新 + 读回）。

对齐 harness tool-todo 的语义：每次调用发送 ENTIRE 整表（整表替换，无
部分更新），每项带 status（pending / in_progress / completed）。清单是
"模型跨工具调用的记忆锚点"——关键在**读回**：折叠出日志里最后一次
todo/write 快照注入 prompt 上下文，模型每轮都看到当前清单、完成一项
标一项 completed，不会重复规划或忘记进行到哪。

不变式：todo/write 仍是痕迹事件（非 surface，不进 derive_messages）——
它不污染模型消息历史，而是以"当前状态"的形式从上下文注入（与 harness
的 sessionProjections 同构，只是我们用日志折叠实现投影）。
"""
from __future__ import annotations

from ..registry import ToolOutcome, ToolSpec

# 合法状态：pending（未开始）/ in_progress（正在做）/ completed（已完成）
TODO_STATUSES = ('pending', 'in_progress', 'completed')
# 默认单活动任务纪律（顺序执行 agent 的常态；未来并行执行再放开）
ALLOW_PARALLEL_IN_PROGRESS = False

_DESCRIPTION = (
    'Record and update a structured task list for the current work. Send the ENTIRE '
    'list every call — it REPLACES the previous list (there are no partial updates, '
    'no per-item edits). Use it to plan multi-step work and show progress: add one '
    'todo per concrete step before you start. '
    'Keep AT MOST ONE todo `in_progress` at a time; while work remains, exactly one '
    'active task should be `in_progress`. '
    'Mark a todo `completed` the moment it is done (do not batch completions), and '
    'allow no `in_progress` item only once all work is complete. Skip the list for '
    'trivial single-step tasks. Statuses: `pending` (not started), `in_progress` '
    '(being worked on now), `completed` (finished).'
)


def validate_todos(raw_todos) -> ToolOutcome | None:
    """校验模型给的清单：形状/去重/单活动约束。失败返回 is_error（不炸循环）。

    raw_todos 已经过 ToolSpec schema（每项 {content, status}）——这里补
    schema 表达不了的语义约束：content 非空且唯一、至多一个 in_progress。
    """
    if not isinstance(raw_todos, list):
        return ToolOutcome(content='todos must be an array', is_error=True)
    seen: set[str] = set()
    active = 0
    for item in raw_todos:
        content = str(item.get('content', '')).strip() if isinstance(item, dict) else ''
        status = item.get('status') if isinstance(item, dict) else ''
        if not content:
            return ToolOutcome(content='invalid todo: `content` must be a non-empty string', is_error=True)
        if content in seen:
            return ToolOutcome(content=f'invalid todos: duplicate content {content!r}', is_error=True)
        seen.add(content)
        if status == 'in_progress':
            active += 1
    if not ALLOW_PARALLEL_IN_PROGRESS and active > 1:
        return ToolOutcome(
            content=f'invalid todos: at most one task may be in_progress (got {active})',
            is_error=True,
        )
    return None


def fold_todos(session) -> list | None:
    """从日志折叠当前清单：最后一次 todo/write 快照；从未写过返回 None。

    这是 todo 的"读回"通道——和 derive_messages 一样是纯函数投影：
    同一段日志永远折叠出同一张表（resume 重放后清单自动恢复）。

    对齐 harness 的 todos projection 折叠规则：
    - `todo/write` → 整表替换（last-write-wins）
    - `turn/start` → 清空（null）——todo 是"当前回合的工作计划"：
      turn/end 保留完成清单可见（收尾展示全勾），但下个回合（用户发
      新消息）开始就归零，dock / prompt 不再携带上个任务的旧清单
    - 其他事件 → 保持现状
    """
    latest: list | None = None
    for event in session.events:
        if event.type == 'todo/write':
            latest = event.data.get('todos') if isinstance(event.data, dict) else None
        elif event.type == 'turn/start':
            latest = None
    return latest


def _counts(todos: list) -> dict:
    def count(status: str) -> int:
        return sum(1 for t in todos if t.get('status') == status)
    return {
        'pending': count('pending'),
        'in_progress': count('in_progress'),
        'completed': count('completed'),
    }


def register(registry) -> None:
    async def todo_write(args, agent, signal):
        rejected = validate_todos(args.get('todos'))
        if rejected is not None:
            return rejected
        todos = [{'content': str(t['content']).strip(), 'status': t['status']} for t in args['todos']]
        agent.session.append('todo/write', {'todos': todos})
        counts = _counts(todos)
        return ToolOutcome(content=(
            f'Updated todo list: {counts["pending"]} pending, '
            f'{counts["in_progress"]} in progress, {counts["completed"]} completed.'
        ))

    registry.register(ToolSpec(
        name='todo_write',
        description=_DESCRIPTION,
        parameters={
            'type': 'object',
            'properties': {
                'todos': {
                    'type': 'array',
                    'description': 'The COMPLETE task list, replacing any previous list.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'content': {'type': 'string', 'description': 'What the task is — a short imperative line.'},
                            'status': {
                                'type': 'string',
                                'enum': list(TODO_STATUSES),
                                'description': 'pending (not started) | in_progress (now) | completed (done).',
                            },
                        },
                        'required': ['content', 'status'],
                    },
                },
            },
            'required': ['todos'],
        },
        execute=todo_write,
    ))
