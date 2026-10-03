"""阶段 2（冷热分层）的**语义基线**：这些结果一个字都不许变。

为什么先写这个：阶段 2 要让 `Session` 只保留"最近 K 回合的完整对象 + 其余只留索引"，
而 `derive_messages` / 召回 / 压缩全都建立在"内存里就是整份日志"之上。改之前先把**可观察
语义**钉成 golden——任何阶段 2 的改动让它们变了，就是语义回归（这也是回滚的判据）。

三条基线：
① **直播 vs 重放等价**：同一批事件，`append` 建出来的会话与"从盘上重放"（`load_events`→`adopt`，
   以及 `scan_index`→`iter_events`→`adopt`）必须给出**逐字节一致**的记忆；
② **被遮蔽内容仍可召回**：压缩（`replace`）之后，`build_turns` 仍列出全部回合、
   `render_turn` 仍能取回被遮蔽的原回合文本；
③ **索引与整表读看到同一批事件**（阶段 1 已有测试，这里再加一条"跨压缩"的）。
"""
from __future__ import annotations

from my_coder.app.recall import build_turns, render_manifest, render_turn
from my_coder.state.session import Session
from my_coder.values.messages import TextBlock, create_user_message
from my_coder.values.persistence import (
    iter_events,
    load_events,
    save_event,
    scan_index,
)


def _live_session() -> Session:
    """造一个带压缩的真实形状：两回合对话 + 工具结果 + 一次 checkpoint 遮蔽前两回合。"""
    session = Session(id='live')
    for turn in (1, 2):
        session.append('turn/start', {'turn': turn})
        session.append('user/message',
                       create_user_message([TextBlock(text=f'问题{turn}')]), surface_op='append')
        session.append('assistant/message', {
            'turn': turn, 'step': 1,
            'message': create_user_message([TextBlock(text=f'回答{turn}')]),
        }, surface_op='append')
        session.append('turn/end', {'turn': turn, 'reason': 'completed'})
    # 压缩遮蔽的是**surface 上的区间**（`turn/start`/`turn/end` 不是 surface 事件，
    # 不在 `_surface` 里）——这与压缩引擎的选区口径一致
    first, last = session.surface[0], session.surface[-1]
    session.append('turn/start', {'turn': 3})
    session.append('user/message',
                   create_user_message([TextBlock(text='<compacted-summary>前两回合的摘要')]),
                   surface_op='replace', shadowed=(first, last))
    session.append('user/message',
                   create_user_message([TextBlock(text='问题3')]), surface_op='append')
    session.append('assistant/message', {
        'turn': 3, 'step': 1,
        'message': create_user_message([TextBlock(text='回答3')]),
    }, surface_op='append')
    return session


def _render(session: Session) -> list[str]:
    return [f'{message.role}:{message.content}' for message in session.derive_messages()]


def test_live_and_replayed_memories_are_identical(tmp_path):
    """① 直播 = 整表重放 = 索引重放（逐字节一致）。"""
    live = _live_session()
    expected = _render(live)
    assert expected, '基线本身不能是空的'

    path = tmp_path / 's.jsonl'
    for event in live.events:
        save_event(path, event)

    whole = Session(id='replay')
    for event in load_events(path):
        whole.adopt(event)

    indexed = Session(id='indexed')
    for event in iter_events(scan_index(path)):
        indexed.adopt(event)

    assert _render(whole) == expected, '整表重放必须与直播逐字节一致'
    assert _render(indexed) == expected, '索引重放必须与直播逐字节一致'


def test_shadowed_turns_stay_recoverable_after_compaction(tmp_path):
    """② 压缩只改变"看得见什么"：被遮蔽的回合仍能被召回层读到。"""
    live = _live_session()
    turns = build_turns(live)
    assert [turn.turn for turn in turns] == [1, 2, 3], '被遮蔽的回合仍要出现在 L1 清单里'
    # L1 要**标出**它们已被压缩（查渲染文本，不猜 TurnInfo 的字段名）
    assert '[已压缩]' in render_manifest(turns), 'L1 必须标注哪些回合已被压缩'

    text = render_turn(live, 1, max_chars=4000)
    assert '问题1' in text and '回答1' in text, 'L2 必须能读回被遮蔽回合的原文'


def test_index_and_full_load_agree_across_compaction(tmp_path):
    """③ 跨压缩的一次：索引与整表读看到同一批事件（seq / type 逐条一致）。"""
    live = _live_session()
    path = tmp_path / 's.jsonl'
    for event in live.events:
        save_event(path, event)

    index = scan_index(path)
    events = load_events(path)
    assert list(index.seqs) == [event.seq for event in events]
    assert list(index.types) == [event.type for event in events]
