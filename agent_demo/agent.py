"""入口层：被动状态机。消息进 inbox → wake 拉起 driver → 跑空回 idle。

agent 从不主动干活：谁要跟它说话谁就拍它一下（wake）。忙时拍不醒，
置 wake_requested 标记，本轮回 idle 的瞬间补拉。Python 里
asyncio.CancelledError 扮演 harness AbortSignal 的角色：cancel()
取消 driver 任务，取消顺着 await 链传到流式循环。
"""
from __future__ import annotations

import asyncio
import logging
from typing import cast

from .hooks import Hooks
from .inbox import Inbox, InboxNotifications
from .loop import run_turn
from .prompt import PromptRegistry
from .registry import ToolRegistry
from .session import Session
from .values import Message, TextBlock, UserSource, create_user_message

log = logging.getLogger('agent')


class _Notifications(InboxNotifications):
    """inbox 变动的实时通知：demo 里只翻译成 debug 日志。

    harness 里这里是真正的 agent 事件总线——入队/丢弃/认领都会
    对外发事件，UI 订阅它们渲染。demo 简化为日志，接口形状保留。
    """

    def __init__(self, agent) -> None:
        self._agent = agent

    def inserted(self, message: Message) -> None:
        log.debug('agent %s: inbox inserted %s', self._agent.id, message.id)

    def discarded(self, message: Message) -> None:
        log.debug('agent %s: inbox discarded %s', self._agent.id, message.id)

    def claimed(self, message: Message, turn: int) -> None:
        log.debug('agent %s: inbox claimed %s in turn %s', self._agent.id, message.id, turn)


class Agent:
    """入口层：被动状态机——agent 从不主动干活。

    生命周期：
    idle --wake(拍一下)--> running --跑空 inbox--> idle

    - 忙时拍不醒：置 _wake_requested 标记，本轮回 idle 的瞬间补拉
    - _driver 是唯一的驱动任务（asyncio.Task），wake 创建、跑空自毁
    - 取消语义：cancel() → driver.cancel()，CancelledError 沿 await 链
      穿过循环层，每层记账后放行，回到 _kick 被吞掉
    """

    def __init__(
        self,
        session: Session,
        llm,
        prompt: PromptRegistry,
        tools: ToolRegistry,
        options: dict | None = None,
        hooks: Hooks | None = None,
    ) -> None:
        self.id = session.id
        self.session = session
        self.llm = llm
        self.prompt = prompt
        self.tools = tools
        self.options = dict(options or {})
        self.hooks = hooks if hooks is not None else Hooks()
        self.phase = 'idle'
        self._last_turn = self._restore_last_turn()
        self._wake_requested = False
        self._driver: asyncio.Task | None = None
        self.inbox = Inbox(session, notifications=_Notifications(self))

    def _restore_last_turn(self) -> int:
        """从日志恢复回合号：扫 turn/start 事件取最后一个——resume 时回合号连续。"""
        last = 0
        for event in self.session.events:
            if event.type == 'turn/start':
                last = cast(dict, event.data)['turn']
        return last

    @property
    def status(self) -> str:
        """对外暴露的两态状态：idle / running。"""
        return 'idle' if self.phase == 'idle' else 'running'

    def followup(self, text: str, rpc_id: str = '') -> str:
        """用户新输入：入队 next-turn 并唤醒（开启一个新回合）。

        rpc_id = 前端提交身份（对齐 dsh 的 prompt requestId）：落到消息 source 上，
        让"本地回显 → durable 消息"能对上号。返回消息 id。
        """
        message = create_user_message([TextBlock(text=text)], UserSource(rpc_id=rpc_id))
        self.send(message, 'next-turn', wakeup=True)
        return message.id

    def steer(self, text: str, rpc_id: str = '') -> str:
        """插队本回合：入队 next-step 并唤醒（当前回合内即时生效）。

        返回消息 id：Web 端据此在队列区标记这条待处理消息（消息要等
        step 边界 claim 才落 user/message surface，不能干等）；claim 后
        SSE 帧带同一个 id，前端据此把它从队列区移进消息流。
        """
        message = create_user_message([TextBlock(text=text)], UserSource(rpc_id=rpc_id))
        self.send(message, 'next-step', wakeup=True)
        return message.id

    def unqueue(self, message_id: str) -> bool:
        """撤回一条还没被认领的待处理消息（Web 队列区的 × 按钮）。

        只动 inbox：未 claim 的消息本来就没有 surface，撤回后日志里
        只剩 spliced（outcome='canceled'）这一条痕迹——它不进模型记忆
        （surface 才进），所以撤回是"干净"的。已 claim 的返回 False。
        """
        return self.inbox.remove(message_id)

    def update_queue(self, message_id: str, action: str, text: str = '') -> str:
        """队列项操作（对齐 dsh 的 `updateQueue(itemId, QueueAction)`）。

        action 三选一，与 dsh 的 QueueAction.kind 同名：
        - 'edit'   就地改写还没轮到的消息（text 是新文案；同样没进模型记忆，改是安全的）
        - 'remove' 撤回（未 claim 的消息没有 surface，撤回不留残影）
        - 'steer'  提升为插队：next-turn → next-step（本轮下一步就做）

        返回状态码（不是 bool）——因为前端要区分三种结局，这与 dsh 的
        RemoteResult error code 一一对应：
        - 'ok'                 成功
        - 'queue-item-not-found'  那条已经不在了（多半刚好被 claim 掉）——
                                  dsh 里这种"并发已处理"是**静默收敛**，不报错
        - 'steer-unavailable'  提升插队要求 agent 正在跑（空闲时没有"下一步"，
                                  提升没有意义，对应 dsh 的 session/steer-unavailable）
        - 'unknown-action'     动作词表外的值（严格校验：宁炸勿静默）
        """
        if action == 'edit':
            return 'ok' if self.inbox.edit(message_id, text) else 'queue-item-not-found'
        if action == 'remove':
            return 'ok' if self.inbox.remove(message_id) else 'queue-item-not-found'
        if action == 'steer':
            # 先看那条还在不在：并发下"刚好被 claim 掉"比"agent 空闲"更准确
            # （DSH 里排队项消失是静默收敛，不该报成不可用）
            if not any(item.id == message_id for item in self.inbox.queued_items()):
                return 'queue-item-not-found'
            if self.status != 'running':
                return 'steer-unavailable'
            return 'ok' if self.inbox.promote(message_id) else 'queue-item-not-found'
        return 'unknown-action'

    def send(self, message: Message, target: str = 'next-turn', wakeup: bool = True) -> None:
        """唯一的入队入口：消息进 inbox，然后拍一下状态机。"""
        self.inbox.append(target, message)
        if wakeup:
            self._wake()

    def _wake(self) -> None:
        """拍醒：idle 就拉起 driver；忙就置标记，回 idle 的瞬间补拉。"""
        if self.phase != 'idle':
            self._wake_requested = True
            return
        self._wake_requested = False
        self.phase = 'running'
        self._driver = asyncio.ensure_future(self._kick())

    def cancel(self, keep_inbox: bool = False) -> None:
        """取消当前活动。默认清空待处理输入；abort 顺着 await 链传播。

        keep_inbox=True：只打断当前活动，队列里没处理的消息保留。
        """
        if not keep_inbox:
            self.inbox.clear()
            self._wake_requested = False
        if self._driver is not None and not self._driver.done():
            self._driver.cancel()

    async def when_idle(self) -> None:
        """等待当前活动收敛。do/while 语义：期间新起的活动会继续等。

        关键细节：
        - asyncio.shield(driver) 保护 driver 不被外部取消殃及
        - 等完后检查 driver is self._driver：还是原来那个才真收敛；
          等待期间起了新活动就再等一轮
        - 传进来的 CancelledError 不是我们主动 cancel 的（driver 没被取消），
          就原样抛给调用者
        """
        while True:
            driver = self._driver
            if driver is None:
                return
            try:
                await asyncio.shield(driver)
            except asyncio.CancelledError:
                if not (driver.cancelled() or driver.done()):
                    raise
            if driver is self._driver:
                return

    async def _kick(self) -> None:
        """driver 任务体：跑 turn 直到 inbox 空；任何错误都在边界收住。

        错误处理哲学：
        - CancelledError：run_turn 已记 turn/end aborted，这里吞掉即可
        - LlmError 等已报告错误：run_turn 已记 turn/end error 并抛出，
          这里 log 后吞掉——错误不逃逸到调用者，但日志里有完整记录
        - finally 里回 idle 并补拉：干活期间被拍过且有货，自己再拉起
        """
        try:
            while await run_turn(self):
                pass
        except asyncio.CancelledError:
            pass  # run_turn 已记 turn/end aborted
        except Exception as error:  # noqa: BLE001 - driver 边界吞掉已报告的错误
            log.exception('agent %s: driver error: %s', self.id, error)
        finally:
            self.phase = 'idle'
            if self._wake_requested and self.inbox.has_pending:
                self._wake()
