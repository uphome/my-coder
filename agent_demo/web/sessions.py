"""Web 宿主：会话与 seat 生命周期（有状态，可脱离路由单独测）。

职责（都是"宿主级"的动作，不涉及 HTTP 路由声明）：

- **Seat get-or-create**：`open_session_seat` —— 同一 sid 第二次调用复用已有 Seat
  （其 agent 若在跑继续跑、事件日志自然延续），而不是重建。这正是并发隔离的要义：
  两会话并行时各自 Seat 独立、互不销毁；首次调用 adopt 重放日志 = 恢复该会话的记忆，
  并在恢复入口顺手自愈崩溃留下的悬空工具调用（`recovery`）。
- **焦点切换**：`open_session` —— 设置"前端正在看哪个会话"的便捷别名，并组装该会话的
  完整响应（历史 / todo / 队列 / 上下文占用）。
- **会话文件**：`scan_sessions`（列表快扫）/ `validate_sid`（防路径穿越）/
  `append_title`（标题即日志投影，改任意会话都落它自己的日志）。
- **每会话审批钩子**：`approval_for` —— Web 版 approval，请求推到**提出它的那个会话**的
  SSE 流，等待表也在 seat 上（旧实现读全局 `_active_queue`，两会话并行时 A 的审批会
  推到 B 的流，这是被修掉的真实 bug）。
- **后台任务登记**：`spawn` —— 保住 `create_task` 的强引用（asyncio 文档：无引用的任务
  可能在执行前被 GC）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from fastapi import HTTPException

from ..app.factory import build_agent
from ..app.workspace import resolve_workspace
from ..capability.hooks import Hooks
from ..state.recovery import repair_dangling_tool_calls
from ..state.session import Session
from ..tools.todo import fold_todos
from ..values.persistence import load_events
from .payload import context_payload, history_payloads, queue_rows
from .state import Seat, state

# Web approval：敏感工具（bash/write/edit）执行前推送请求给浏览器，等待人工批准/拒绝。
# 用户不点则 fail-safe 拒绝（防模型永久卡住）。
APPROVAL_TIMEOUT_S = 300

# 工作区在会话创建时定下，之后给已有会话再传 workspace 一律拒绝（两条入口共用一句话：
# 一条是内存里已有 seat，一条是磁盘上有日志但还没建 seat——后者如果"静默忽略"，
# 调用方会以为换成功了，实际工具还在旧根上，属于最坏的一类不一致）
WORKSPACE_FIXED_MESSAGE = ('workspace is fixed when the session is created — '
                           'start a new conversation to use another one')


def spawn(coro) -> asyncio.Task:
    """创建并登记一个后台任务；完成时自动从注册表移除。"""
    task = asyncio.create_task(coro)
    state.background_tasks.add(task)
    task.add_done_callback(state.background_tasks.discard)
    return task


def approval_for(seat: Seat):
    """生成绑定到某个会话 seat 的 approval 钩子（build_agent 时挂载）。

    Web 并发隔离的关键：审批必须回到"提出请求的那个会话"的浏览器。
    钩子闭包捕获自己的 seat：请求推 seat.queue、等待表存 seat.approvals，
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


def open_session_seat(sid: str, *, allow_missing: bool,
                      workspace: str | Path | None = None) -> Seat:
    """取/建一个会话的 Seat（不切换焦点）。

    Seat get-or-create 语义：同一 sid 第二次调用复用已有 Seat（其 agent
    若在跑继续跑、事件日志自然延续），而不是重建——这正是并发隔离的要义：
    两会话并行时各自 Seat 独立，互不销毁。首次调用 adopt 重放日志 =
    恢复该会话的记忆。

    **工作区在创建时定下，之后固定**（2026-09 决策，对齐 DSH：一个对话属于一个文件夹）：

    - 新建会话：用调用方给的 `workspace`（空 = 宿主默认），并**落一条
      `session/workspace` 痕迹事件**——"这个对话属于哪个文件夹"因此成为日志的事实，
      重开/重启都回到同一个地方；
    - 打开已有会话：从日志里读回工作区（`session.workspace()`）；此时**再传 workspace
      一律 400**（`WORKSPACE_FIXED_MESSAGE`）——想换工作区就新建对话（半个对话换了沙箱根，
      前几轮读的是 A、后几轮写的是 B，语义上说不清楚）。两条入口都要拦：内存里已有 seat 的
      那条，以及"磁盘上有日志但还没建 seat"的那条（后者若静默忽略，调用方会以为换成功了）；
    - 老会话（本功能之前建的，日志里没有这条事件）：跟随**宿主默认工作区**，
      并且**不回头改写它的日志**（历史保持原样）。
    """
    log_path = (state.sessions_dir or Path('.sessions')) / f'{sid}.jsonl'
    if not allow_missing and not log_path.exists():
        raise HTTPException(404, f'session {sid!r} not found')
    is_new = not log_path.exists()
    seat = state.seats.get(sid)
    if seat is not None:
        if workspace:
            raise HTTPException(400, WORKSPACE_FIXED_MESSAGE)
        return seat
    if not is_new and workspace:
        raise HTTPException(400, WORKSPACE_FIXED_MESSAGE)
    session = Session(id=sid)
    if log_path.exists():
        for event in load_events(log_path):
            session.adopt(event)
    session.bind_store(log_path)  # 追加式实时落盘（没有状态不进日志）
    # 崩溃/被 kill 留下的"悬空工具调用"在这里自愈：不修的话模型记忆里会留下
    # "请求了工具却没有结果"的 assistant 消息，之后每次发送都是 400（见 state/recovery.py）
    repaired = repair_dangling_tool_calls(session)
    if repaired:
        print(f'[repair] session {sid}: {len(repaired)} dangling tool call(s) repaired',
              flush=True)
    resolved = _resolve_seat_workspace(workspace=workspace if is_new else None,
                                       logged=session.workspace() if not is_new else None)
    if is_new:
        if not log_path.exists():
            # 新会话立即落盘（空文件）：列表可见、可切换——"会话存在 = 有文件"
            log_path.touch()
        # source 用**去掉空白后**是否还有内容来判断：前端清空输入框提交的空串等于"没选"，
        # 记成 'user' 会让日后分不清"用户选的"与"跟随默认"（判据要落在同一个表示上）
        session.append('session/workspace', {
            'workspace': str(resolved),
            'source': 'user' if str(workspace or '').strip() else 'default',
        })
    seat = Seat(sid, session, _args_for_workspace(resolved), None)
    state.seats[sid] = seat  # 先登记：审批钩子闭包引用 seat，创建 agent 前就位
    hooks = Hooks()
    hooks.approval = approval_for(seat)   # Web 版确认：弹本会话的批准/拒绝
    seat.agent = build_agent(
        session, seat.args,
        {'reasoning_started': False, 'request_no': 0, 'tool_no': 0},
        hooks=hooks,
    )
    return seat


def _args_for_workspace(workspace: Path) -> argparse.Namespace:
    """复制宿主装配参数并把 workspace 换成这个会话的（其余宿主级，逐字段继承）。

    为什么复制而不是就地改 `state.args.workspace`：宿主默认工作区要留给**后面新建**的
    对话继续当默认值，改它会把"默认"变成"最近一个会话的工作区"。
    """
    assert state.args is not None  # init_web 已初始化（调用方 _check_init 保证）
    return argparse.Namespace(**{**vars(state.args), 'workspace': workspace})


def _resolve_seat_workspace(*, workspace: str | Path | None, logged: str | None) -> Path:
    """定下这个会话的工作区：显式选择 → 日志记录 → 宿主默认。

    日志里记着的工作区**必须仍然存在**，否则响亮报错：静默回退到宿主默认会让工具
    指向另一个项目，而模型以为自己还在这个对话原来的目录里——那是"看起来正常、
    实际读了别的仓库"的最坏情况。用户有两条出路：恢复那个目录，或删掉这个会话。
    """
    assert state.args is not None
    default = Path(state.args.workspace)
    if workspace:
        try:
            return resolve_workspace(workspace, default=default)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
    if logged:
        # 日志里记的应该是绝对路径（我们自己写的），但手改过的日志可能有相对路径/`~`——
        # 统一 resolve 一次再判断，判断与使用落在同一个值上
        recorded = Path(logged).expanduser().resolve()
        if not recorded.is_dir():
            raise HTTPException(
                409, f'this session\'s workspace is gone: {recorded} — '
                     'restore it, or delete the session and start a new conversation')
        return recorded
    return resolve_workspace(None, default=default)


def open_session(sid: str, *, allow_missing: bool,
                 workspace: str | Path | None = None) -> dict:
    """打开/切换到会话：Seat get-or-create + 设置焦点别名，返回该会话的完整投影。"""
    seat = open_session_seat(sid, allow_missing=allow_missing, workspace=workspace)
    state.session = seat.session
    state.agent = seat.agent
    state.current_sid = sid
    return {
        'id': sid,
        'workspace': str(seat.args.workspace),     # 本会话的工作区（顶栏/相对路径显示用）
        'history': history_payloads(seat.session),
        'todos': fold_todos(seat.session) or [],   # 当前 todo 投影：切换会话时恢复 dock
        'queue': queue_rows(seat.agent),           # 待处理队列投影：切换会话时恢复队列区
        'context': context_payload(seat.session),  # 上下文占用：切换会话时恢复圆环
    }


def validate_sid(sid: str) -> None:
    """会话 id 只允许安全字符（防路径穿越：.sessions/../.env 之类）。"""
    if not sid or sid != Path(sid).name or '..' in sid:
        raise HTTPException(400, f'invalid session id: {sid!r}')


def scan_sessions() -> list[dict]:
    """扫描会话目录：id / 事件数 / 更新时间 / 标题 / 首条用户消息摘要 / 工作区。

    磁盘行内快扫（不整包解析）：标题 = 最后一条 session/title 事件的 title
    （user 手动 > auto 自动，按追加顺序后者覆盖）；无标题事件时列表显示
    首条 user/message 摘要作为 fallback（对齐 harness 的三级来源）。
    工作区同理取最后一条 `session/workspace`（后写覆盖前写）；没有就报宿主默认——
    列表里因此永远显示"这个对话实际上会用哪个目录"，不会有一栏空着。
    """
    # 没记录工作区的旧会话显示**宿主默认的绝对路径**（`--workspace .` 要解析成实际目录：
    # 前端拿它做相对路径显示的前缀，给个 "." 会对不上任何消息里的绝对路径）
    default_workspace = (str(Path(state.args.workspace).expanduser().resolve())
                         if state.args is not None else '')
    items: list[dict] = []
    for path in sorted((state.sessions_dir or Path('.sessions')).glob('*.jsonl')):
        events = 0
        summary = ''
        title = ''
        title_source = ''
        workspace = ''
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
                # 最后一条 session/workspace 事件即当前工作区（同上）
                if '"type": "session/workspace"' in line:
                    try:
                        data = (json.loads(line).get('data') or {}).get('$dict') or {}
                        workspace = data.get('workspace', '')
                    except (TypeError, ValueError, KeyError):
                        pass
        items.append({
            'id': path.stem,
            'events': events,
            'updated': path.stat().st_mtime,
            'title': title or None,
            'title_source': title_source or None,
            'summary': title or summary or '(empty)',  # 标题优先，摘要兜底
            'workspace': workspace or default_workspace,  # 没记录过 = 跟随宿主默认
        })
    items.sort(key=lambda item: item['updated'], reverse=True)
    return items


def append_title(sid: str, title: str, source: str) -> None:
    """往一个会话追加 session/title 事件（标题 = 日志投影，改完即落盘）。

    优先用该会话的 Seat（在跑/打开过的会话直接 append，不打断 agent）；
    否则临时重放目标日志 + bind_store append（从列表里改没打开过的会话）。
    """
    seat = state.seats.get(sid)
    if seat is not None:
        seat.session.append('session/title', {'title': title, 'source': source})
        return
    path = (state.sessions_dir or Path('.sessions')) / f'{sid}.jsonl'
    if not path.exists():
        raise HTTPException(404, f'session {sid!r} not found')
    temp = Session(id=sid)
    for event in load_events(path):
        temp.adopt(event)
    temp.bind_store(path)
    temp.append('session/title', {'title': title, 'source': source})
