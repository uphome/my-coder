"""稀疏重放：跳过帧之后 `seq` 不再连续，`Session` 必须照样成立（issue #46 ③）。

**这条测试是补一个真事故的**：`derive_messages` 原来用 `self._log[seq]`（假设"下标即 seq"，
只在"重放一条不漏"时成立）。`Session.from_path(skip_types=…)` 跳帧之后 seq 变稀疏
（保留 3 与 10644、跳掉中间），于是打开一个帧密集的**老**日志会在取记忆时
`IndexError: list index out of range`——而服务正是用这条路径打开会话的。
"""

from __future__ import annotations

from my_coder.state.session import Session
from my_coder.values.messages import TextBlock, create_user_message
from my_coder.values.persistence import save_event, scan_index


def _log_with_frames(tmp_path):
    """造一份**帧夹在表面事件之间**的日志（这正是老日志的形状）。"""
    path = tmp_path / 's.jsonl'
    session = Session(id='live')
    session.append('turn/start', {'turn': 1})
    seq = 0
    for event in (session.events):
        save_event(path, event)
        seq = event.seq
    # 表面事件之后插几帧（用真实形状：`assistant/chunk` + 载荷），再插表面事件
    for offset in range(3):
        frame = Session(id='frame')
        frame.append('assistant/chunk', {'turn': 1, 'step': 1,
                                         'chunk': {'text': f'f{offset}', 'tool_calls': []}})
        save_path = path
        save_event(save_path, frame.events[-1])
    speaker = Session(id='more')
    speaker.append('user/message', create_user_message([TextBlock(text='第二句')]),
                   surface_op='append')
    save_event(path, speaker.events[-1])
    return path, seq


def test_from_path_skips_frames_and_memory_is_usable(tmp_path):
    path, _ = _log_with_frames(tmp_path)
    index = scan_index(path)
    assert len(index) > 4

    session = Session.from_path(path, 'sparse')
    types = [event.type for event in session.events]
    assert 'assistant/chunk' not in types, '帧必须被跳过'
    # seq 稀疏（中间有被跳过的帧）——这正是原来会炸的形状
    seqs = [event.seq for event in session.events]
    assert seqs != list(range(len(seqs))), '本用例必须造出稀疏 seq，否则测不到那个 bug'

    # **关键**：取记忆（内部按 seq 查 surface 事件）必须正常工作
    messages = session.derive_messages()
    texts = [block.text for message in messages for block in message.content
             if isinstance(block, TextBlock)]
    assert '第二句' in texts, f'记忆里应包含跳帧之后的表面事件，实际 {texts}'


def test_dense_replay_still_works(tmp_path):
    """不跳帧时行为不变（回归护栏）。"""
    path = tmp_path / 'd.jsonl'
    session = Session(id='live')
    session.append('user/message', create_user_message([TextBlock(text='甲')]),
                   surface_op='append')
    for event in session.events:
        save_event(path, event)

    replayed = Session.from_path(path, 'dense')
    assert [event.seq for event in replayed.events] == [event.seq for event in session.events]
    assert len(replayed.derive_messages()) == 1
