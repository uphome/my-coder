"""seq 连续性：**跳帧重放之后追加的事件必须拿到没被用过的 seq**（真事故的回归测试）。

事故现场：`append` 原来用 `new_event(len(self._log), …)` 分配 seq。当会话是**跳帧重放**
打开的（`Session.from_path` 跳过流式帧），`len(_log)` 只等于"留下来的事件数"（4,661），
而文件里真实的最后一个 seq 是 425,984 ⇒ 之后每条新事件都拿到**已经被占用**的 seq。

实测损伤：`web-1788604766.jsonl` 出现 **957 个重复 seq**（从 4,661 起）——日志契约
（seq 唯一且递增）被破坏，连带 `read_event_at` 的二分、surface 重建、压缩选区全部可能错位。

判据（本文件钉住）：**任何**会话在追加事件后，其 `events` 的 seq 必须**严格递增**，
且新 seq **大于文件里出现过的最大 seq**。
"""
from __future__ import annotations

from my_coder.state.session import Session
from my_coder.values.messages import TRACE_FRAME_TYPES, TextBlock, create_user_message
from my_coder.values.persistence import iter_events, save_event, scan_index


def _log_with_frames(path, frames: int = 3) -> int:
    """造一份"帧夹在中间"的日志，返回文件里出现过的最大 seq。"""
    session = Session(id='live')
    session.append('user/message', create_user_message([TextBlock(text='甲')]),
                   surface_op='append')
    save_event(path, session.events[-1])
    for offset in range(frames):
        session.append('assistant/chunk', {'turn': 1, 'step': 1,
                                           'chunk': {'text': f'f{offset}', 'tool_calls': []}})
        save_event(path, session.events[-1])
    session.append('user/message', create_user_message([TextBlock(text='乙')]),
                   surface_op='append')
    save_event(path, session.events[-1])
    return max(event.seq for event in iter_events(scan_index(path)))


def test_append_after_frame_skipping_does_not_reuse_seq(tmp_path):
    """事故的直接回归：跳帧重放后 append 的 seq 必须 > 文件里的最大 seq。"""
    path = tmp_path / 's.jsonl'
    highest = _log_with_frames(path)

    replayed = Session.from_path(path, 'sparse', skip_types=TRACE_FRAME_TYPES)
    assert len(replayed.events) == 2, '帧应被跳过（索引里只剩两条 user/message）'

    replayed.bind_store(path)
    added = replayed.append('user/message', create_user_message([TextBlock(text='丙')]),
                            surface_op='append')
    assert added.seq > highest, f'新 seq 必须大于文件里的最大 seq（{highest}），实际 {added.seq}'


def test_replayed_file_keeps_strictly_increasing_seqs(tmp_path):
    """继续用这个会话写几条，再整表重放：seq 必须严格递增（不许出现重复）。"""
    path = tmp_path / 's.jsonl'
    _log_with_frames(path)
    live = Session.from_path(path, 'sparse', skip_types=TRACE_FRAME_TYPES)
    live.bind_store(path)
    for index in range(3):
        live.append('user/message', create_user_message([TextBlock(text=f'续{index}')]),
                    surface_op='append')

    seqs = [event.seq for event in iter_events(scan_index(path))]
    assert len(seqs) == len(set(seqs)), f'seq 重复了：{sorted(seqs)}'
    assert all(b > a for a, b in zip(seqs, seqs[1:], strict=False)), f'seq 非严格递增：{seqs}'
