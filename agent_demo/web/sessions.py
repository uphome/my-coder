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

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import HTTPException

from ..factory import build_agent
from ..hooks import Hooks
from ..persistence import load_events
from ..recovery import repair_dangling_tool_calls
from ..session import Session
from ..tools.todo import fold_todos
from .payload import context_payload, history_payloads, queue_rows
from .state import Seat, state

# Web approval：敏感工具（bash/write/edit）执行前推送请求给浏览器，等待人工批准/拒绝。
# 用户不点则 fail-safe 拒绝（防模型永久卡住）。
APPROVAL_TIMEOUT_S = 300


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


def open_session_seat(sid: str, *, allow_missing: bool) -> Seat:
    """取/建一个会话的 Seat（不切换焦点）。

    Seat get-or-create 语义：同一 sid 第二次调用复用已有 Seat（其 agent
    若在跑继续跑、事件日志自然延续），而不是重建——这正是并发隔离的要义：
    两会话并行时各自 Seat 独立，互不销毁。首次调用 adopt 重放日志 =
    恢复该会话的记忆。
    """
    log_path = (state.sessions_dir or Path('.sessions')) / f'{sid}.jsonl'
    if not allow_missing and not log_path.exists():
        raise HTTPException(404, f'session {sid!r} not found')
    seat = state.seats.get(sid)
    if seat is not None:
        return seat
    session = Session(id=sid)
    if log_path.exists():
        for event in load_events(log_path):
            session.adopt(event)
    session.bind_store(log_path)  # 追加式实时落盘（没有状态不进日志）
    # 崩溃/被 kill 留下的"悬空工具调用"在这里自愈：不修的话模型记忆里会留下
    # "请求了工具却没有结果"的 assistant 消息，之后每次发送都是 400（见 recovery.py）
    repaired = repair_dangling_tool_calls(session)
    if repaired:
        print(f'[repair] session {sid}: {len(repaired)} dangling tool call(s) repaired',
              flush=True)
    if not log_path.exists():
        # 新会话立即落盘（空文件）：列表可见、可切换——"会话存在 = 有文件"
        log_path.touch()
    seat = Seat(sid, session, None)
    state.seats[sid] = seat  # 先登记：审批钩子闭包引用 seat，创建 agent 前就位
    hooks = Hooks()
    hooks.approval = approval_for(seat)   # Web 版确认：弹本会话的批准/拒绝
    seat.agent = build_agent(
        session, state.args,
        {'reasoning_started': False, 'request_no': 0, 'tool_no': 0},
        hooks=hooks,
    )
    return seat


def open_session(sid: str, *, allow_missing: bool) -> dict:
    """打开/切换到会话：Seat get-or-create + 设置焦点别名，返回该会话的完整投影。"""
    seat = open_session_seat(sid, allow_missing=allow_missing)
    state.session = seat.session
    state.agent = seat.agent
    state.current_sid = sid
    return {
        'id': sid,
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
    """扫描会话目录：id / 事件数 / 更新时间 / 标题 / 首条用户消息摘要。

    磁盘行内快扫（不整包解析）：标题 = 最后一条 session/title 事件的 title
    （user 手动 > auto 自动，按追加顺序后者覆盖）；无标题事件时列表显示
    首条 user/message 摘要作为 fallback（对齐 harness 的三级来源）。
    """
    items: list[dict] = []
    for path in sorted((state.sessions_dir or Path('.sessions')).glob('*.jsonl')):
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
