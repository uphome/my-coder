"""应用层工具：上下文召回——`session_manifest`（L1 清单）+ `read_turn`（L2 原文）。

为什么需要它们（issue #3 的 M1）：压缩把旧回合折叠进 checkpoint 后，**原文还在日志里**，
但模型看不见了。这两个工具就是"回到原文"的两步：

1. `session_manifest`：拿一个会话的用户话清单（真人发言 + 每回合足迹）——导航入口；
2. `read_turn`：按回合号（可加 step）读那一段原文。

设计取舍（实测依据见 `CONTEXT_BUDGET_DESIGN.md` §4）：

- **不给相关性检索**：语料是自述的时间线，模型看清单自己挑回合比任何排序都准
  （Codex 的 history 工具也只给"清单 + 结构过滤 + 精确区间读"）。
- **坐标对模型暴露的是回合号**（可加 step），seq 只在内部用——模型按回合思考。
- **只读、无副作用**：所以声明 `execution_mode='parallel'`（并发安全）；
  executor 体内全是同步读盘/JSON 解析（没有 await），所以 `offload=True`——
  与 `skill` 工具同一形状。**不碰 agent/session 的内存状态**（这正是能卸载的前提）。
- **跨会话授权**：只允许读**同一工作区**的会话（每个对话有自己的工作区，
  见 `agent.md` §10）；跨工作区的请求返回 `is_error`，不泄漏别的项目的内容。
"""
from __future__ import annotations

from pathlib import Path

from ..app.recall import build_turns, load_session_log, render_manifest, render_turn, scan_sessions
from ..state.registry import ToolSpec
from ..values.messages import ToolOutcome

MANIFEST_DESCRIPTION = (
    'List what the user asked, turn by turn, in one session — the navigation index for '
    'this conversation\'s history. Each line is one turn: a short excerpt of what the agent '
    'concluded, its footprint (steps, tools, files touched, outcome, and whether that turn '
    'has since been compacted away), plus the user\'s own words. Use it when you need to '
    'find which earlier turn dealt with something; then read that turn with read_turn. '
    'Omit session_id for the current session.'
)

READ_TURN_DESCRIPTION = (
    'Read the original messages of one turn (optionally one step of it). This is the source '
    'of truth for details that a compaction summary may have dropped — exact paths, numbers, '
    'commands, error strings, or what the user originally said. Read the manifest first to '
    'pick the turn number. Output is bounded: if it says it was truncated, read again with a '
    'step number.'
)


def _resolve_session(session_id: str, agent, sessions_dir: Path,
                     default_workspace: str) -> tuple[object | None, str]:
    """按 id 找会话：当前会话用内存里的；别的从磁盘重放。

    返回 `(session, error)`——**跨工作区一律拒绝**（同工作区才放行），错误串里
    只说"属于另一个工作区"，不泄漏对方的目录与内容。
    """
    current = agent.session
    if not session_id or session_id == current.id:
        return current, ''
    if not sessions_dir.exists():
        return None, 'no session directory is available in this host'
    path = sessions_dir / f'{session_id}.jsonl'
    if not path.exists():
        return None, f'no session named {session_id!r}'
    mine = current.workspace() or default_workspace
    theirs = None
    for row in scan_sessions(sessions_dir, default_workspace):
        if row.session_id == session_id:
            # 没记录工作区的旧会话 = 跟随宿主默认（与工作区选择策略同一判据），
            # 所以要把 default 补上再比；否则"旧会话"会被当成"工作区为空"而放行
            theirs = row.workspace or default_workspace
            break
    if theirs is None:
        # **fail-closed**：判不出目标归属就不读（读不了 / 坏行都会走到这里）。
        # 放行的代价是可能的跨工作区泄漏，拒绝的代价只是模型换一条路——不对称
        return None, (f'cannot determine the workspace of session {session_id!r}; '
                      'refusing to read it')
    if mine != theirs:
        return None, (f'session {session_id!r} belongs to another workspace; '
                      'this conversation can only recall its own workspace')
    try:
        return load_session_log(path, session_id), ''
    except OSError as error:
        return None, f'cannot read session {session_id!r}: {error}'


def register(registry, sessions_dir: Path, default_workspace: str = '') -> None:
    """注册两个召回工具。`sessions_dir` 由 host 注入（state 层不认识磁盘布局）。"""

    async def session_manifest(args, agent, signal):
        session_id = str(args.get('session_id') or '').strip()
        session, error = _resolve_session(session_id, agent, sessions_dir, default_workspace)
        if session is None:
            return ToolOutcome(content=error, is_error=True)
        turns = build_turns(session)
        text = render_manifest(
            turns,
            include_shadowed=bool(args.get('include_shadowed', True)),
            with_footprint=bool(args.get('with_footprint', True)),
            with_commands=bool(args.get('with_commands', False)),
        )
        if not text:
            return ToolOutcome(
                content=f'session {session.id!r} has no user messages yet', is_error=True)
        return ToolOutcome(content=text)

    async def read_turn(args, agent, signal):
        session_id = str(args.get('session_id') or '').strip()
        session, error = _resolve_session(session_id, agent, sessions_dir, default_workspace)
        if session is None:
            return ToolOutcome(content=error, is_error=True)
        raw_turn = args.get('turn')
        if not isinstance(raw_turn, int):
            return ToolOutcome(content='turn must be an integer', is_error=True)
        step = args.get('step')
        if step is not None and not isinstance(step, int):
            return ToolOutcome(content='step must be an integer when given', is_error=True)
        text = render_turn(session, raw_turn, step=step,
                           scope=str(args.get('scope') or 'surface'))
        if not text:
            known = ', '.join(str(t.turn) for t in build_turns(session))[:200] or '(none)'
            return ToolOutcome(
                content=f'no turn {raw_turn}'
                        + (f' step {step}' if step is not None else '')
                        + f' in session {session.id!r}; known turns: {known}',
                is_error=True)
        return ToolOutcome(content=text)

    registry.register(ToolSpec(
        name='session_manifest',
        description=MANIFEST_DESCRIPTION,
        parameters={
            'type': 'object',
            'properties': {
                'session_id': {
                    'type': 'string',
                    'description': 'Session to list; omit for the current session.',
                },
                'include_shadowed': {
                    'type': 'boolean',
                    'description': 'Include turns whose messages were compacted away '
                                   '(default true — those are exactly what you may need).',
                },
                'with_commands': {
                    'type': 'boolean',
                    'description': 'Also show the shell command each turn ran (default false); '
                                   'useful when a turn only ran commands and touched no file.',
                },
            },
        },
        execute=session_manifest,
        execution_mode='parallel',   # 纯读，不写任何共享状态
        offload=True,                # 体内全是同步读盘 + JSON 解析，没有 await
    ))

    registry.register(ToolSpec(
        name='read_turn',
        description=READ_TURN_DESCRIPTION,
        parameters={
            'type': 'object',
            'properties': {
                'turn': {'type': 'integer', 'description': 'Turn number from the manifest.'},
                'step': {
                    'type': 'integer',
                    'description': 'Optional step within that turn, when the turn is too big '
                                   'and the previous read said it was truncated.',
                },
                'session_id': {
                    'type': 'string',
                    'description': 'Session to read from; omit for the current session.',
                },
                'scope': {
                    'type': 'string',
                    'enum': ['surface', 'trace'],
                    'description': "Default 'surface' (what the model saw). 'trace' also "
                                   "includes that turn's reasoning — useful to recover why a "
                                   'decision was made.',
                },
            },
            'required': ['turn'],
        },
        execute=read_turn,
        execution_mode='parallel',
        offload=True,
    ))
