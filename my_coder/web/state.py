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

from ..runtime.agent import Agent
from ..state.session import Session


@dataclass
class Seat:
    """一个打开过的会话的运行时资源（**并发隔离的边界**）。"""
    sid: str
    session: Session
    # 该会话自己的装配参数（**与宿主参数只有一处不同：workspace**）。工作区可以按对话
    # 指定，而工具沙箱、指令文件探测、技能表、`{{workspace}}` 叙事全都是 build_agent
    # 当场从 args.workspace 派生的——所以"每会话一个 args"就够，不必给 Seat 挂一堆
    # 派生对象。fake/model/compact_at 这些仍是宿主级（一个进程一套模型凭据）。
    args: argparse.Namespace
    agent: Agent | None = None
    # 该会话当前活跃的 SSE 队列（approval 请求经它推给本会话的浏览器）。
    # 每会话同时最多一条活跃对话流（前端单标签页串行使用）；None = 空闲。
    queue: asyncio.Queue | None = None
    # 该会话自己的审批等待表（aid → Future）；拒绝/批准经 /approval/respond 唤醒。
    approvals: dict[str, asyncio.Future] = field(default_factory=dict)
    # 最近一次被"用到"的时刻（issue #46：冷却释放按它挑最久没用的席位）。
    # 粗粒度就够——更新点是 `open_session_seat`（切会话 / 发消息都要经过它），
    # 不必每个路由都戳一次；判据是"释放一个**空闲**席位"，不是精确 LRU。
    last_used: float = 0.0

    def is_idle(self) -> bool:
        """能不能安全释放：**没有在跑的回合、没有活跃 SSE 流、没有待审批**。

        三个条件缺一不可。释放 = 丢掉这个 Seat 的全部内存状态（Session / Agent），
        重开时靠重放日志恢复（磁盘日志永远 ≥ 内存状态，见不变式③）。所以"正在跑"的
        绝不能碰——那会把一次进行中的回合连根拔掉，日志里只剩一半。
        """
        running = self.agent is not None and self.agent.status == 'running'
        return not running and self.queue is None and not self.approvals


@dataclass
class WebState:
    """宿主级单例：装配参数 + seat 注册表 + 焦点别名。"""
    # 宿主级装配参数（**模板**）：`--workspace` 是"默认工作区"，每个 Seat 会复制一份
    # 并把自己的 workspace 换进去（见 Seat.args）。
    args: argparse.Namespace | None = None
    sessions_dir: Path | None = None
    seats: dict[str, Seat] = field(default_factory=dict)
    # 焦点：仅"前端正在看哪个会话"的便捷别名（不是状态源——状态源永远是各 seat）
    session: Session | None = None
    agent: Agent | None = None
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
