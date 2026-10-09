"""状态层：追加式事件日志，唯一事实源。

append(type, data, surface_op, shadowed) 落一条事件。surface 事件
（user/message、assistant/message、tool/result）必须带 surface_op：
- 'append'：追加到投影尾部（普通消息）
- 'replace'：遮蔽一段旧区间（compaction 的 checkpoint 顶替旧对话）——
  被遮蔽事件仍在日志（append-only 不删行），只是从投影（模型可见）
  中消失；shadowed=(start_seq, end_seq) 记录被顶替的区间。
derive_messages 只折叠 surface 投影，是纯函数：同一段日志永远推导出
同一份消息序列（replace 遮蔽也由日志重建，恢复后投影一致）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import cast

from ..values.messages import (
    TRACE_FRAME_TYPES,
    Message,
    SessionEvent,
    StreamFrame,
    new_event,
    new_session_header,
)
from ..values.persistence import iter_events, scan_index

# 唯一能"浮上水面变成模型消息"的三类事件。
# surface_op 校验：这三类必须带 surface_op（'append' 或 'replace'），
# 其他类型带了就报错——保证在写入时刻暴露，而不是 derive 时才发现。
SURFACE_EVENT_TYPES = frozenset({'user/message', 'assistant/message', 'tool/result'})


class Session:
    """会话：追加式事件日志，整个架构的唯一事实源。

    三个内部结构：
    - _log：完整事件序列（含痕迹数据：chunk、边界、todo）
    - _surface：surface 事件的 seq 列表（投影，不存事件本体）——
      'replace' 会把被遮蔽 seq 移除、新 seq 原位插入，所以它是
      "当前模型可见顺序"，顺序正确（摘要在前、新对话在后）
    - _listeners：订阅者（持久化/UI 都通过订阅消费日志）
    - _stream_listeners：**瞬时帧**的订阅者（issue #51）——流式帧不落日志，见 `emit_stream`

    两个写入路径不对称，这是 resume 机制的全部秘密：
    - append：新事件 → 校验 + 落日志 + 更新投影 + 通知 listener
    - adopt：磁盘重放 → 只重建日志与投影，不触发监听、不重跑逻辑
    """

    def __init__(self, id: str) -> None:
        self.id = id
        self._log: list[SessionEvent] = []
        self._surface: list[int] = []
        self._listeners: list[Callable[[SessionEvent], None]] = []
        self._stream_listeners: list[Callable[[StreamFrame], None]] = []
        # `events` 的按变更缓存（issue #46 ②）：`None` = 脏，下次访问重建一次。
        # 失效点只有两个（`append` / `adopt`）——`_log` 没有别的写入者。
        self._events_cache: tuple[SessionEvent, ...] | None = None
        # seq → 事件（issue #46 ③）：**只在日志稀疏时才需要**。
        # "跳帧重放"打开的会话里 seq 不连续（保留 3 与 10644、跳掉中间），`_log[seq]` 会越界。
        # 但**稠密会话绝不能为此再存一份映射**（那等于每个事件两份内存，与省内存的初衷相反），
        # 所以 `by_seq` 先走"下标即 seq"的快路，只有不成立时才查这张表；表也只在真稀疏时才填。
        self._sparse: dict[int, SessionEvent] = {}
        # **seq 游标**（不是 `len(_log)`）：跳帧重放后 `_log` 只装"该留的"事件，
        # 长度与真实 seq 差一个数量级，拿长度当 seq 会写出**重复 seq**、破坏日志契约。
        self._next_seq = 0

    def by_seq(self, seq: int) -> SessionEvent:
        """按 seq 取事件（**稀疏日志也成立**；稠密时零额外开销）。

        为什么不是无条件 `self._log[seq]`：`_log` 的下标只在"重放一条不漏"时等于 seq；
        `Session.from_path(skip_types=…)` 刻意跳帧之后这个前提就不成立了。
        """
        if seq < len(self._log) and self._log[seq].seq == seq:
            return self._log[seq]
        return self._sparse[seq]

    @classmethod
    def from_path(cls, path, session_id: str = '',
                  skip_types: tuple[str, ...] = TRACE_FRAME_TYPES) -> Session:
        """从日志**重放**出一个会话——所有"打开/读取一个会话"的入口都该走这里（issue #46 ③）。

        与 `load_events` + `adopt` 的区别只有一个：**整类跳过 `skip_types`**（默认流式帧）。
        它们连 payload 都不解析，所以老日志（帧占 98.9%）的重放代价降两个数量级；
        新会话（v2）本就没有帧，这条路径对它是恒等的。

        **为什么跳过是安全的**：帧是纯痕迹、不在检索面里（`SURFACE` 不含），内容也不是独有的
        （`assistant/message` 有正文与工具参数全文、`assistant/reasoning` 有思维链全文，
        #51 有逐字节等价的机械证明）。所以 `derive_messages` / 召回 / 历史渲染的结果**不变**。
        需要看帧的场景（帧时代的实时回放）本来也只在**当时的进程内**发生，不靠重放。
        """
        index = scan_index(path, skip_types=skip_types)
        session = cls(session_id or getattr(path, 'stem', ''))
        for event in iter_events(index, skip_types=skip_types):
            session.adopt(event)
        return session

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        """完整事件序列（不可变视图）。**按变更缓存**：O(n) 只在变更后付一次。

        为什么必须缓存（实测，2026-10-03，42.5 万事件的会话）：整表拷贝单次 **12.1 ms**
        （对比 `events_since` 的 0.031 ms），而**每个请求**至少读它 5 次
        （inbox 判待处理、todo 状态栏、召回 L0 目录、循环、web 历史）——每次请求几十毫秒
        与反复重建 40 万元组（峰值内存的主因）。

        缓存是安全的：返回的是**不可变元组**，`_log` 只在 `append` / `adopt` 里增长，
        两处都会把缓存置脏。所以"同一份日志 → 同一个元组对象"，调用方可以放心长期持有。
        只要长度或增量，仍优先用 `event_count` / `events_since`。
        """
        if self._events_cache is None:
            self._events_cache = tuple(self._log)
        return self._events_cache

    @property
    def event_count(self) -> int:
        """日志长度（O(1)，不拷贝）——增量投影的游标用。"""
        return len(self._log)

    def events_since(self, index: int) -> tuple[SessionEvent, ...]:
        """从 `index` 起的事件（只拷贝新增的那一段）。

        给"事件只追加"的增量投影用：整表 `events` 在这个场景下等于每请求重扫一遍。
        """
        return tuple(self._log[index:])

    @property
    def surface(self) -> tuple[int, ...]:
        """surface 事件的 seq 序列（不可变视图，含 replace 后的原位）。"""
        return tuple(self._surface)

    def on_event(self, listener: Callable[[SessionEvent], None]):
        """订阅新事件（UI/持久化/投影都是这样消费日志）。返回退订函数。"""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def on_stream(self, listener: Callable[[StreamFrame], None]):
        """订阅**瞬时流帧**（issue #51）：只给"此刻正在看的人"，不落盘。

        与 `on_event` 分开是必须的：`bind_store` 就是把 `save_event` 挂在 `on_event` 上——
        若流帧也走那条通道，它就会被写进日志（那正是写放大的来源）。
        """
        self._stream_listeners.append(listener)
        return lambda: self._stream_listeners.remove(listener)

    def emit_stream(self, type_: str, data=None) -> None:
        """发一帧瞬时数据（流式打字机）。**不落日志、不占 seq、不进投影。**

        判据：**流帧不是状态**。模型的可见内容由 `assistant/message`（正文 + 工具调用参数）
        与 `assistant/reasoning`（思维链全文）保证——帧只是这些内容的"传输过程"，
        没有它不影响任何可重建事实（见 `docs/notes/implemented/…`）。
        """
        frame = StreamFrame(type=type_, data=data)
        for listener in list(self._stream_listeners):
            listener(frame)

    def bind_store(self, path):
        """把后续事件实时追加落盘（listener 在 append 提交后触发）。返回解绑函数。

        **顺带写会话头**（`SessionHeader`，JSONL 第一行）：新建/空文件时写一条，
        已有内容则不动（头只在文件出生时写一次）。头带了 `format_version`，
        所以"日志格式"从这一刻起有锚点——老文件没有头，读取端按 v0 处理。
        """
        from ..values.persistence import save_event, save_header

        path.parent.mkdir(parents=True, exist_ok=True)
        save_header(path, new_session_header(self.id))
        return self.on_event(lambda event: save_event(path, event))

    def append(self, type_: str, data=None, surface_op: str | None = None,
               shadowed: tuple | None = None, ignorable: bool = False) -> SessionEvent:
        """落一条新事件：先校验 → 再记日志 → 更新投影 → 通知 listener。

        顺序很重要：listener 在 append 提交之后才触发，
        保证订阅者（比如落盘）看到的状态和日志一致。

        `ignorable=True`：这条事件对**旧读取者**可以"不认识就跳过"（词汇增长不 bump 版本）。
        """
        if type_ in SURFACE_EVENT_TYPES:
            if surface_op not in ('append', 'replace'):
                raise ValueError(
                    f"surface event {type_!r} requires surface_op='append' or 'replace'")
        elif surface_op is not None:
            raise ValueError(f'non-surface event {type_!r} cannot carry surface_op')
        if surface_op == 'replace' and (not shadowed or len(shadowed) != 2):
            raise ValueError(f"replace event {type_!r} requires shadowed=(start_seq, end_seq)")
        if surface_op != 'replace' and shadowed is not None:
            raise ValueError(f'shadowed is only valid with surface_op="replace" (got {surface_op!r})')
        event = new_event(self._next_seq, type_, data, surface_op, shadowed, ignorable)
        self._next_seq = event.seq + 1
        self._log.append(event)
        if event.seq != len(self._log) - 1:   # 只有稀疏（跳帧重放）才需要映射
            self._sparse[event.seq] = event
        self._events_cache = None      # 缓存置脏（下一个读的人重建一次）
        self._apply_surface(event)
        for listener in list(self._listeners):
            listener(event)
        return event

    def adopt(self, event: SessionEvent) -> None:
        """从磁盘重放：只重建投影、不触发监听、不重跑任何逻辑。"""
        self._log.append(event)
        self._next_seq = max(self._next_seq, event.seq + 1)
        if event.seq != len(self._log) - 1:   # 只有稀疏（跳帧重放）才需要映射
            self._sparse[event.seq] = event
        self._events_cache = None      # 重放同样要让缓存失效
        self._apply_surface(event)

    def _apply_surface(self, event: SessionEvent) -> None:
        """把一条 surface 事件应用到投影（append 尾插 / replace 原位顶替）。

        replace 语义：shadowed=(start_seq, end_seq) 标定被顶替的旧区间——
        但遮蔽目标是 surface **列表里从 start 位置到 end 位置这一段连续节点**
        （位置语义，不是 seq 数值范围）。为什么必须是位置：
        checkpoint 的 seq 大于被它顶替的旧 seq（追加式），replace 后 surface
        不再是 seq 单调序（如 (4,2,3)）——若按 start<=seq<=end 数值过滤，
        第二次压缩的区间会把范围里的无关节点误吞（演示见测试）。用两端 seq
        在 surface 中的索引定位连续段，就能正确处理嵌套/连续多次 replace。
        """
        if event.surface_op == 'append':
            self._surface.append(event.seq)
            return
        if event.surface_op != 'replace':
            return
        assert event.shadowed is not None  # replace 必须带 shadowed（append 校验保证）
        start_seq, end_seq = event.shadowed
        # 两端 seq 必须在投影里（compaction 引擎基于当前 surface 选区，保证合法）
        start_idx = self._surface.index(start_seq)
        end_idx = self._surface.index(end_seq)
        # 遮蔽的是 [start_idx, end_idx] 这段连续节点（含两端），原位插入 checkpoint
        del self._surface[start_idx:end_idx + 1]
        self._surface.insert(start_idx, event.seq)

    def derive_messages(self) -> list[Message]:
        """模型可见的消息历史：按 surface 顺序折叠，每个节点投影一次。

        纯函数：同一段日志永远推导出同一份消息序列（不变式②）。
        注意 assistant/message 的空 content 会被跳过——
        比如 finish_reason=length 但没有任何输出时，不产生模型消息。
        """
        out: list[Message] = []
        for seq in self._surface:
            event = self.by_seq(seq)
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

    def workspace(self) -> str | None:
        """本会话选定的工作区路径（最后一条 `session/workspace` 事件）。

        与 `request_header` 同一个模式：**配置事实从日志里读回来**，不另存一份状态
        （"日志是唯一事实源"）。返回 None = 这个会话没记录过工作区——旧会话（本功能
        落地前建的）与 CLI 会话都属这一类，调用方回退到宿主默认工作区。

        注意它是**痕迹事件**：不进 `derive_messages`（不变式 ②），但随日志重放，
        所以换个进程、隔几天再打开，这个会话仍然回到自己的工作区。

        **性能警告：它是 O(n) 的反向扫描**——会话里**没有**这条事件时（旧会话）
        要把整个日志扫一遍，实测 20 万事件的会话 ~10 ms。所以别把它放进"每个请求都要
        跑"的路径：运行时会话目录在那里踩过一次（见 `app/recall.py` 的增量游标，
        它自己记工作区，不调这里）。
        """
        for event in reversed(self._log):
            if event.type == 'session/workspace':
                return cast(dict, event.data).get('workspace')
        return None
