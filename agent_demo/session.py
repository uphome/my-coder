"""状态层：追加式事件日志，唯一事实源。

append(type, data, surface_op) 落一条事件。surface 事件（user/message、
assistant/message、tool/result）必须携带 surface_op='append'——"哪些
事件会变成模型消息"是结构上写死的，不是运行时猜的。derive_messages
只折叠 surface 节点，是纯函数：同一段日志永远推导出同一份消息序列。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import cast

from .values import Message, SessionEvent, new_event

# 唯一能"浮上水面变成模型消息"的三类事件。
# append 写入时校验：这三类必须带 surface_op='append'，
# 其他类型带了就报错——保证在写入时刻暴露，而不是 derive 时才发现。
SURFACE_EVENT_TYPES = frozenset({'user/message', 'assistant/message', 'tool/result'})


class Session:
    """会话：追加式事件日志，整个架构的唯一事实源。

    三个内部结构：
    - _log：完整事件序列（含痕迹数据：chunk、边界、todo）
    - _surface：surface 事件的 seq 列表（投影，不存事件本体）
    - _listeners：订阅者（持久化/UI 都通过订阅消费日志）

    两个写入路径不对称，这是 resume 机制的全部秘密：
    - append：新事件 → 校验 + 落日志 + 更新投影 + 通知 listener
    - adopt：磁盘重放 → 只重建日志与投影，不触发监听、不重跑逻辑
    """

    def __init__(self, id: str) -> None:
        self.id = id
        self._log: list[SessionEvent] = []
        self._surface: list[int] = []
        self._listeners: list[Callable[[SessionEvent], None]] = []

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        """完整事件序列（不可变视图）。"""
        return tuple(self._log)

    @property
    def surface(self) -> tuple[int, ...]:
        """surface 事件的 seq 序列（不可变视图）。"""
        return tuple(self._surface)

    def on_event(self, listener: Callable[[SessionEvent], None]):
        """订阅新事件（UI/持久化/投影都是这样消费日志）。返回退订函数。"""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def bind_store(self, path):
        """把后续事件实时追加落盘（listener 在 append 提交后触发）。返回解绑函数。"""
        from .persistence import save_event

        path.parent.mkdir(parents=True, exist_ok=True)
        return self.on_event(lambda event: save_event(path, event))

    def append(self, type_: str, data=None, surface_op: str | None = None) -> SessionEvent:
        """落一条新事件：先校验 → 再记日志 → 更新投影 → 通知 listener。

        顺序很重要：listener 在 append 提交之后才触发，
        保证订阅者（比如落盘）看到的状态和日志一致。
        """
        if type_ in SURFACE_EVENT_TYPES:
            if surface_op != 'append':
                raise ValueError(f"surface event {type_!r} requires surface_op='append'")
        elif surface_op is not None:
            raise ValueError(f'non-surface event {type_!r} cannot carry surface_op')
        event = new_event(len(self._log), type_, data, surface_op)
        self._log.append(event)
        if surface_op == 'append':
            self._surface.append(event.seq)
        for listener in list(self._listeners):
            listener(event)
        return event

    def adopt(self, event: SessionEvent) -> None:
        """从磁盘重放：只重建投影，不触发监听、不重跑任何逻辑。"""
        self._log.append(event)
        if event.surface_op == 'append':
            self._surface.append(event.seq)

    def derive_messages(self) -> list[Message]:
        """模型可见的消息历史：按 surface 顺序折叠，每个节点投影一次。

        纯函数：同一段日志永远推导出同一份消息序列（不变式②）。
        注意 assistant/message 的空 content 会被跳过——
        比如 finish_reason=length 但没有任何输出时，不产生模型消息。
        """
        out: list[Message] = []
        for seq in self._surface:
            event = self._log[seq]
            if event.type == 'user/message':
                out.append(cast(Message, event.data))
            elif event.type == 'assistant/message':
                # assistant/message 的 data = {'message': Message}——表面上的入口点
                message = cast(dict, event.data)['message']
                if message.content:
                    out.append(message)
            elif event.type == 'tool/result':
                out.append(cast(Message, event.data))
        return out

    def request_header(self) -> dict | None:
        """最后一次请求配置快照——resume 时恢复"上次用什么模型"。"""
        for event in reversed(self._log):
            if event.type == 'request/header':
                return cast(dict, event.data)
        return None
