"""状态层：双队列待处理消息，agent/inbox/spliced 事件的持久化投影。

先记账后投影：每次改动先把 spliced 事件落日志，再改内存列表。
进程重启后，构造时重放日志里的 spliced 事件即可恢复队列——
队列不是状态，日志才是，队列只是重放结果。

队列投影（queued_items）也在这里，和 session.derive_messages 并列：
两者都是"日志 → 不可变投影"的纯函数，只是一个折 surface（模型可见的
记忆），一个折 _state（还没浮上水面的待处理输入）。**折叠实现只有一份**
（_apply / _splice 共用同一套 splice 语义），上层（web/cli）只做序列化。
"""
from __future__ import annotations

from typing import cast

from .session import Session
from .values import Message, QueuedItem, QueuedPlacement

# 两个队列：
# next-turn=普通输入（等本轮干完再处理），
# next-step=插队输入（打断当前步，下一步立刻处理，steer 用）。
TARGETS = ('next-turn', 'next-step')

# 队列（存在形式）→ placement（语义标签）：语义在前端与日志里都用这个词。
# 顺序即投影顺序：先普通排队，再插队（前端队列区按此从上到下显示）。
PLACEMENTS: dict[str, QueuedPlacement] = {'next-turn': 'queued', 'next-step': 'steering'}


class InboxNotifications:
    """入队/丢弃/认领的实时通知（demo 里只记日志；harness 里是 agent 事件）。"""

    def inserted(self, message: Message) -> None:  # noqa: ARG002
        pass

    def discarded(self, message: Message) -> None:  # noqa: ARG002
        pass

    def claimed(self, message: Message, turn: int) -> None:  # noqa: ARG002
        pass


class Inbox:
    """待处理消息的双队列——注意它不是独立状态，而是日志的投影。

    核心不变式（"入队即记账"）：
    _splice 的每个改动都先把 agent/inbox/spliced 事件落日志，
    再改内存。进程崩溃时日志永远 ≥ 内存，重启后构造时
    重放 spliced 事件即可原地恢复队列——队列只是重放结果。
    """

    def __init__(self, session: Session, notifications: InboxNotifications | None = None) -> None:
        self._session = session
        self._notifications = notifications if notifications is not None else InboxNotifications()
        self._state: dict[str, list[Message]] = {'next-turn': [], 'next-step': []}
        # 重放恢复：把日志里的 spliced 事件全部 _apply 一遍，队列原地复活。
        for event in session.events:
            if event.type == 'agent/inbox/spliced':
                self._apply(cast(dict, event.data))

    @property
    def next_turn(self) -> tuple[Message, ...]:
        """next-turn 队列的不可变视图。"""
        return tuple(self._state['next-turn'])

    @property
    def next_step(self) -> tuple[Message, ...]:
        """next-step 队列的不可变视图。"""
        return tuple(self._state['next-step'])

    @property
    def has_pending(self) -> bool:
        """还有没有待处理消息——循环层靠它决定是否继续下一回合。"""
        return bool(self._state['next-turn'] or self._state['next-step'])

    def queued_items(self) -> tuple[QueuedItem, ...]:
        """待处理消息投影（前端队列区 / 任何宿主的唯一数据源）。

        纯函数式投影：_state 本身就是重放 agent/inbox/spliced 的结果
        （构造时 _apply 过；_splice 也是"先落日志再同一套语义改内存"），
        所以这里直接读它——**不再有第二份折叠实现**。此前 web_app 自己
        重放过一遍 spliced，同一事件类型两份折叠会分叉，且投影落在 web
        层（CLI 看不到、测试要绕过 web 模块才测得到）。

        顺序：TARGETS 顺序 = 先 next-turn（queued）后 next-step（steering）。
        """
        return tuple(
            QueuedItem(placement=PLACEMENTS[target], message=message)
            for target in TARGETS
            for message in self._state[target]
        )

    def append(self, target: str, message: Message) -> None:
        """入队（队尾）。"""
        if target not in TARGETS:
            raise ValueError(f'unknown inbox target: {target}')
        self._splice(target, len(self._state[target]), 0, [message])

    def prepend(self, target: str, message: Message) -> None:
        """入队（队头，插队）。"""
        if target not in TARGETS:
            raise ValueError(f'unknown inbox target: {target}')
        self._splice(target, 0, 0, [message])

    def clear(self) -> None:
        """持久化取消所有待处理输入：先清 next-step，再清 next-turn。

        顺序有意义：先清插队队列，避免刚清完 next-turn 又冒出 next-step。
        """
        self._splice('next-step', 0, len(self._state['next-step']), [])
        self._splice('next-turn', 0, len(self._state['next-turn']), [])

    def remove(self, message_id: str) -> bool:
        """按 id 撤回一条尚未认领的消息（Web 队列区的 × 按钮）。

        撤回 = 从队列里 splice 掉：走的仍是 _splice 这条唯一通道，
        所以 spliced 事件（带 outcome='canceled'）照样先落日志——
        "入队即记账"对撤回同样成立，重放日志不会复活已撤回的消息。
        返回 False = 没找到（多半已经被 claim 落成 surface 了）。
        """
        for target in TARGETS:
            for index, message in enumerate(self._state[target]):
                if message.id == message_id:
                    self._splice(target, index, 1, [])
                    return True
        return False

    def claim(self, target: str, turn: int) -> list[Message]:
        """认领一步的完整批次：先取空整个 next-step，再从 next-turn 取一条。

        discard=False：认领不是丢弃，不触发 discarded 通知。
        循环层每步开头调用，认领结果经 pre_step 钩子后落 user/message。
        """
        claimed = self._splice('next-step', 0, len(self._state['next-step']), [], discard=False)
        if target == 'next-turn':
            claimed.extend(self._splice('next-turn', 0, 1, [], discard=False))
        for message in claimed:
            self._notifications.claimed(message, turn)
        return claimed

    def _splice(
        self,
        target: str,
        start: int,
        delete_count: int,
        inserted: list[Message],
        discard: bool = True,
    ) -> list[Message]:
        """队列改动的唯一入口（append/prepend/clear/claim 都归结于此）。

        顺序严格：先给 Session 落 spliced 事件（记账），再改内存（投影）。
        返回被移除的消息列表。
        """
        state = self._state[target]
        start = max(0, min(start, len(state)))
        delete_count = max(0, min(delete_count, len(state) - start))
        if delete_count == 0 and not inserted:
            return []
        removed = state[start:start + delete_count]
        self._validate(target, start, delete_count, inserted)
        splice_data = {
            'target': target,
            'start': start,
            'removed_count': delete_count,
            'inserted': list(inserted),
            **({'outcome': 'canceled'} if discard and delete_count else {}),
        }
        self._session.append('agent/inbox/spliced', splice_data)  # 先记账
        del state[start:start + delete_count]
        state[start:start] = list(inserted)
        if discard:
            for message in removed:
                self._notifications.discarded(message)
        for message in inserted:
            self._notifications.inserted(message)
        return removed

    def _validate(self, target: str, start: int, delete_count: int, inserted: list[Message]) -> None:
        """防错：同一条消息（按 id）不能同时 pending 在两个队列里。"""
        other = self._state['next-step' if target == 'next-turn' else 'next-turn']
        ids = {message.id for message in other}
        state = self._state[target]
        candidate = state[:start] + inserted + state[start + delete_count:]
        for message in candidate:
            if message.id in ids:
                raise ValueError(f'message {message.id!r} is already pending')
            ids.add(message.id)

    def _apply(self, data: dict) -> None:
        """_splice 的"只改内存"版本：重放时用，不再落日志。"""
        state = self._state[data['target']]
        start = data['start']
        del state[start:start + data.get('removed_count', 0)]
        state[start:start] = list(data['inserted'])
