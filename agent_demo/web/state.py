"""Web 宿主的状态容器：`Seat`（每会话资源）+ `WebState`（宿主级单例）。

为什么把可变状态单独收进一个模块：拆分前它们是 `web_app.py` 里的 5 个模块私有全局
（`_session` / `_agent` / `_seats` / `_current_sid` / `_args`），散在 993 行里，测试只能
戳私有名字。收进显式对象后有两个好处：

- **可测**：测试注入 `state.args`、读 `state.seats`，不再依赖下划线私有全局；
- **并发模型一眼可见**：`Seat` 里全是**每会话**资源（session / agent / SSE 队列 / 审批
  等待表），`WebState` 里是**宿主级**单例（装配参数、会话目录、seat 注册表、焦点别名）。
  "焦点"（`session` / `agent` / `current_sid`）只是"前端正在看哪个会话"的便捷别名——
  运行中的资源都挂在各自的 Seat 里，所以两个会话可以真正并行。
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from ..session import Session


@dataclass
class Seat:
    """一个打开过的会话的运行时资源（**并发隔离的边界**）。"""
    sid: str
    session: Session
    agent: object | None = None
    # 该会话当前活跃的 SSE 队列（approval 请求经它推给本会话的浏览器）。
    # 每会话同时最多一条活跃对话流（前端单标签页串行使用）；None = 空闲。
    queue: asyncio.Queue | None = None
    # 该会话自己的审批等待表（aid → Future）；拒绝/批准经 /approval/respond 唤醒。
    approvals: dict[str, asyncio.Future] = field(default_factory=dict)


@dataclass
class WebState:
    """宿主级单例：装配参数 + seat 注册表 + 焦点别名。"""
    args: argparse.Namespace | None = None
    sessions_dir: Path | None = None
    seats: dict[str, Seat] = field(default_factory=dict)
    # 焦点：仅"前端正在看哪个会话"的便捷别名（不是状态源——状态源永远是各 seat）
    session: Session | None = None
    agent: object | None = None
    current_sid: str = ''
    # 后台任务注册表：保住未完成任务的强引用，防事件循环 GC 丢弃
    # （asyncio 文档明确：create_task 的返回值若无引用，任务可能在执行前被回收）
    background_tasks: set[asyncio.Task] = field(default_factory=set)

    def reset(self, sessions_dir: Path, args: argparse.Namespace) -> None:
        """`init_web` 用：清空 seat 注册表并换掉装配参数。

        Seat 注册表是宿主级缓存，`init_web` 每次调用都要清空重来——测试每个用例用
        独立 sessions_dir，若不清空，旧目录的 seat（同 sid，如 'web'）会被复用，
        把上个会话的内存日志带进新初始化。
        """
        self.seats.clear()
        self.session = None
        self.agent = None
        self.current_sid = ''
        self.sessions_dir = sessions_dir
        self.args = args


# 进程内单例：一个 Web 宿主一份（`init_web` 换参数，不换对象）
state = WebState()
