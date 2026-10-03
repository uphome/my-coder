"""#46 ② 的效果与不变量测试（零 API）。

① 效果：同一份日志反复读 `events` 应当是 **O(1)**（同一个元组对象），不再每次 12 ms；
② 正确性：`append` / `adopt` 之后必须看到新事件（缓存按变更失效），
   而且 `derive_messages()` 的结果与"从零重放"完全一致（缓存不能改变语义）。
"""
from __future__ import annotations

import time

from my_coder.state.session import Session
from my_coder.values.messages import TextBlock, create_user_message


def _fill(session: Session, count: int) -> None:
    for index in range(count):
        session.append('user/message', create_user_message([TextBlock(text=f'm{index}')]),
                       surface_op='append')


def test_events_is_cached_per_change():
    session = Session(id='cache')
    _fill(session, 50)

    first = session.events
    assert session.events is first, '没有变更时不该重新拷贝（这就是 12 ms 的来源）'
    assert len(first) == 50

    session.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_user_message([TextBlock(text='a')]),
    }, surface_op='append')
    second = session.events
    assert second is not first, '变更后必须是新的元组（否则读到旧日志）'
    assert len(second) == 51

    adopted = Session(id='replay')
    adopted.adopt(second[0])
    assert len(adopted.events) == 1, 'adopt 也要让缓存失效'
    adopted.adopt(second[1])
    assert len(adopted.events) == 2


def test_cached_events_do_not_change_semantics():
    """缓存不能改变语义：带压缩（replace）的会话，缓存后的 derive_messages 与重放一致。"""
    live = Session(id='live')
    _fill(live, 6)
    checkpoint_seq = live.events[2].seq
    live.append('user/message', create_user_message([TextBlock(text='<compacted-summary>摘要')]),
                surface_op='replace', shadowed=(checkpoint_seq, live.events[-1].seq))
    _fill(live, 3)
    live.derive_messages()          # 先读一次（让缓存热起来）
    live.append('user/message', create_user_message([TextBlock(text='再来一条')]),
                surface_op='append')

    replayed = Session(id='replay')
    for event in live.events:
        replayed.adopt(event)

    live_texts = [str(m.content) for m in live.derive_messages()]
    replay_texts = [str(m.content) for m in replayed.derive_messages()]
    assert live_texts == replay_texts, '缓存后的投影必须与从零重放逐字节一致'


def test_cached_access_is_flat_in_log_length():
    """效果护栏：42 万事件下，重复读 events 的**总**时间应远小于"每次拷贝"。

    用比值而不是绝对时间（避免机器差异导致 flaky）：一次冷读 vs 之后 200 次热读。
    """
    session = Session(id='big')
    _fill(session, 200_000)
    started = time.perf_counter()
    _ = session.events                  # 冷：一次 O(n) 拷贝
    cold = time.perf_counter() - started
    started = time.perf_counter()
    for _ in range(200):
        _ = session.events              # 热：应为 O(1)
    warm = time.perf_counter() - started
    assert warm < cold, f'热读总耗时应小于单次冷读（cold={cold:.4f}s warm={warm:.4f}s）'
