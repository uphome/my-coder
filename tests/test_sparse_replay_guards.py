"""稀疏重放的两条**回归护栏**（都对应真事故）。

1. 🔴 行首类型正则原来**没有锚定**：payload 里出现 `"type": "assistant/chunk"` 就会把一条
   **真事件**误判成"要跳过的帧"而整行丢掉 ⇒ 静默少读。锚定到信封之后不可能再发生。
2. 🟠 `by_seq` 在**稠密**会话里必须就是 `_log` 里那个对象（不额外建 seq→事件 的映射），
   否则每个事件都被存两份，与"省内存"的初衷相反。
3. 稀疏重放（跳帧）与整表重放在**带压缩**的日志上必须给出同一份记忆。
"""

from __future__ import annotations

from my_coder.state.session import Session
from my_coder.values.messages import TextBlock, create_user_message
from my_coder.values.persistence import iter_events, save_event, scan_index


def _build(path, session: Session, type_: str, data, **kwargs) -> None:
    session.append(type_, data, **kwargs)
    save_event(path, session.events[-1])


def test_payload_text_that_looks_like_a_frame_is_not_skipped(tmp_path):
    path = tmp_path / 'trap.jsonl'
    session = Session(id='trap')
    _build(path, session, 'turn/start', {'turn': 1})
    trap = ('{"seq": 1, "time": 0.0, "type": "assistant/chunk", "data": {}}'
            '  ← 用户把日志片段贴进了对话')
    _build(path, session, 'user/message',
           create_user_message([TextBlock(text=trap)]), surface_op='append')

    replayed = Session.from_path(path, 'trap')
    texts = [block.text for message in replayed.derive_messages()
             for block in message.content if isinstance(block, TextBlock)]
    assert any('贴进了对话' in text for text in texts), \
        f'含"像帧的文本"的真实 user/message 必须保留，实际 {texts}'


def test_by_seq_is_the_same_object_when_dense(tmp_path):
    path = tmp_path / 'dense.jsonl'
    session = Session(id='dense')
    _build(path, session, 'user/message',
           create_user_message([TextBlock(text='甲')]), surface_op='append')

    replayed = Session.from_path(path, 'dense')
    assert replayed.by_seq(replayed.events[0].seq) is replayed.events[0]
    assert not getattr(replayed, '_sparse', {}), '稠密会话不该为每条事件再存一份映射'


def test_compaction_plus_frame_skip_matches_dense_replay(tmp_path):
    path = tmp_path / 'compact.jsonl'
    session = Session(id='compact')
    _build(path, session, 'turn/start', {'turn': 1})
    _build(path, session, 'user/message',
           create_user_message([TextBlock(text='问题')]), surface_op='append')
    _build(path, session, 'assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_user_message([TextBlock(text='回答')]),
    }, surface_op='append')
    _build(path, session, 'assistant/chunk',
           {'turn': 1, 'step': 1, 'chunk': {'text': '帧', 'tool_calls': []}})
    first, last = session.surface[0], session.surface[-1]
    _build(path, session, 'user/message',
           create_user_message([TextBlock(text='<compacted-summary>摘要')]),
           surface_op='replace', shadowed=(first, last))

    dense = Session(id='dense')
    for event in iter_events(scan_index(path)):
        dense.adopt(event)
    sparse = Session.from_path(path, 'sparse')

    assert [str(m.content) for m in sparse.derive_messages()] == \
        [str(m.content) for m in dense.derive_messages()], '跳帧不得改变记忆'
