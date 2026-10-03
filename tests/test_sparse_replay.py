"""稀疏重放：跳过帧之后 `seq` 不再连续，`Session` 必须照样成立（issue #46 ③）。

**这条测试是补一个真事故的**：`derive_messages` 原来用 `self._log[seq]`（假设"下标即 seq"，
只在"重放一条不漏"时成立）。`Session.from_path(skip_types=…)` 跳帧之后 seq 变稀疏
（保留 3 与 10644、跳掉中间），于是打开一个帧密集的**老**日志会在取记忆时
`IndexError: list index out of range`——而服务正是用这条路径打开会话的。

> 写这份 fixture 时踩过一个坑，记下来：**同一个文件里的事件必须来自同一个 `Session`**。
> 我先用三个独立 `Session` 生成事件再写进同一文件，结果**seq 重复**（0,0,0,0）——那不是真实
> 日志的形状，还让"按 seq 取事件"的行为变得没有意义（这张测试因此一度假绿）。
"""

from __future__ import annotations

from my_coder.state.session import Session
from my_coder.values.messages import TextBlock, create_user_message
from my_coder.values.persistence import save_event, scan_index


def _build(path, session: Session, type_: str, data, **kwargs) -> None:
    """在**同一个会话**上追加一条事件并落盘（seq 因此是真实递增的）。"""
    session.append(type_, data, **kwargs)
    save_event(path, session.events[-1])


def _log_with_frames(tmp_path):
    """造一份**帧夹在表面事件之间**的日志（老日志的真实形状）。"""
    path = tmp_path / 's.jsonl'
    session = Session(id='live')
    _build(path, session, 'turn/start', {'turn': 1})
    _build(path, session, 'user/message',
           create_user_message([TextBlock(text='第一句')]), surface_op='append')
    for offset in range(3):
        _build(path, session, 'assistant/chunk',
               {'turn': 1, 'step': 1, 'chunk': {'text': f'f{offset}', 'tool_calls': []}})
    _build(path, session, 'user/message',
           create_user_message([TextBlock(text='第二句')]), surface_op='append')
    return path, session


def test_from_path_skips_frames_and_memory_is_usable(tmp_path):
    path, _ = _log_with_frames(tmp_path)
    # 不过滤时索引里是全部 6 条；**过滤后**才是 3 条非帧事件（turn/start + 两条 user）
    assert len(scan_index(path)) == 6
    assert len(scan_index(path, skip_types=('assistant/chunk',))) == 3

    session = Session.from_path(path, 'sparse')
    types = [event.type for event in session.events]
    assert 'assistant/chunk' not in types, '帧必须被跳过'

    seqs = [event.seq for event in session.events]
    assert seqs != list(range(len(seqs))), '本用例必须造出稀疏 seq，否则测不到那个 bug'

    # **关键**：取记忆（内部按 seq 查 surface 事件）必须正常工作
    texts = [block.text for message in session.derive_messages()
             for block in message.content if isinstance(block, TextBlock)]
    assert texts == ['第一句', '第二句'], f'记忆应包含跳帧前后的表面事件，实际 {texts}'


def test_dense_replay_still_works(tmp_path):
    """不跳帧时行为不变（回归护栏）。"""
    path = tmp_path / 'd.jsonl'
    session = Session(id='dense')
    _build(path, session, 'user/message',
           create_user_message([TextBlock(text='甲')]), surface_op='append')

    replayed = Session.from_path(path, 'dense')
    assert [event.seq for event in replayed.events] == [event.seq for event in session.events]
    assert len(replayed.derive_messages()) == 1
