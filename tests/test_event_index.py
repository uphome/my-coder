"""issue #46 ③ 阶段 1：事件索引（不建 payload 的重放）+ 定点读原文。

判据：索引与 `load_events` 看到的是**同一批事件**；定点读回来的**与整表读的那条一致**；
守卫一条不放松；索引与文件不一致时 **fail-closed**。
"""
from __future__ import annotations

import json

import pytest

from my_coder.state.session import Session
from my_coder.values.messages import (
    TextBlock,
    create_user_message,
    new_event,
)
from my_coder.values.persistence import (
    UnknownSessionEventError,
    iter_events,
    load_events,
    read_event_at,
    save_event,
    scan_index,
)


def _write(path, count: int = 5):
    for index in range(count):
        save_event(path, new_event(
            index, 'user/message', create_user_message([TextBlock(text=f'm{index}')]),
            surface_op='append'))
    return path


def test_index_sees_the_same_events_as_load_events(tmp_path):
    path = _write(tmp_path / 's.jsonl', 6)
    index = scan_index(path)
    events = load_events(path)

    assert len(index) == len(events) == 6
    assert list(index.seqs) == [event.seq for event in events]
    assert list(index.types) == [event.type for event in events]


def test_read_event_at_matches_the_full_load(tmp_path):
    path = _write(tmp_path / 's.jsonl', 6)
    index = scan_index(path)
    events = load_events(path)

    for position, expected in enumerate(events):
        got = read_event_at(index, expected.seq)
        assert got.seq == expected.seq
        assert got.type == expected.type
        assert got.surface_op == expected.surface_op
        assert str(got.data.content) == str(expected.data.content)
        assert position == expected.seq        # 这份日志里 seq 与位置一致


def test_iter_events_is_complete(tmp_path):
    path = _write(tmp_path / 's.jsonl', 4)
    index = scan_index(path)
    assert [event.seq for event in iter_events(index)] == [0, 1, 2, 3]


def test_unknown_event_guard_is_not_relaxed(tmp_path):
    """索引路径**同样**拒绝未知且不可忽略的事件（不因为"不建对象"就放松守卫）。"""
    path = tmp_path / 's.jsonl'
    _write(path, 2)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({'seq': 9, 'type': 'mystery/event', 'data': {}}) + '\n')

    with pytest.raises(UnknownSessionEventError):
        scan_index(path)
    with pytest.raises(UnknownSessionEventError):
        load_events(path)      # 两条路径的判据必须一致


def test_ignorable_unknown_events_are_skipped_by_both_paths(tmp_path):
    """标了 `ignorable` 的未知事件：两条路径都跳过（不占索引位置，也不影响 seq）。"""
    path = _write(tmp_path / 's.jsonl', 2)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({'seq': 2, 'type': 'mystery/event', 'data': {},
                                 'ignorable': True}) + '\n')

    index = scan_index(path)
    assert len(index) == 2, '可忽略的未知事件不该进索引'
    assert len(load_events(path)) == 2, '整表读同样跳过它'


def test_index_records_surface_metadata_without_payloads(tmp_path):
    """索引要带够重建 **surface** 所需的顶层字段（`surface_op` / `shadowed`）。

    这是冷热分层的机制：投影（模型可见顺序）与"哪些区间被压缩遮蔽"都能只靠索引算出来，
    payload 只按需取——否则"冷区省内存"会在重建投影时被整表物化抵消。
    """
    path = _write(tmp_path / 's.jsonl', 4)
    session = Session(id='live')
    for event in load_events(path):
        session.adopt(event)
    first, last = session.surface[0], session.surface[-1]
    checkpoint = new_event(len(session.events), 'user/message',
                           create_user_message([TextBlock(text='<compacted-summary>摘要')]),
                           surface_op='replace', shadowed=(first, last))
    save_event(path, checkpoint)
    session.adopt(checkpoint)

    index = scan_index(path)
    events = load_events(path)
    assert list(index.surface_ops) == [event.surface_op for event in events]
    assert list(index.shadowed) == [event.shadowed for event in events]
    # 被遮蔽区间的两端 seq 拿得到 ⇒ 能只靠索引回答"模型现在看不到哪一段"
    assert index.shadowed[-1] == (first, last)
    # surface 事件就是"带 surface_op 的那些"，按日志序（投影可只靠索引重算）
    assert [seq for seq, op in zip(index.seqs, index.surface_ops, strict=True) if op] == \
        [event.seq for event in events if event.surface_op]


def test_missing_seq_and_mismatch_fail_closed(tmp_path):
    path = _write(tmp_path / 's.jsonl', 3)
    index = scan_index(path)

    with pytest.raises(KeyError):
        read_event_at(index, 999), '越界的 seq 必须报错，不能静默返回空'

    # 手工造一个"类型对不上"的索引 → 定点读必须报错（把索引与文件的不一致暴露出来）
    bad = type(index)(path=index.path, seqs=index.seqs, types=('wrong/type',) * len(index),
                      offsets=index.offsets, lengths=index.lengths)
    with pytest.raises(ValueError, match='mismatch'):
        read_event_at(bad, 0)
