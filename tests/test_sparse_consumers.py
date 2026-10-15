"""跨形态一致性：**跳帧重放与整表重放，各条读取路径必须给出同一份结果**（issue #54 同族）。

事故现场（两个同族 bug，都是"用 seq 去索引 tuple"）：
- `app/compaction.py::_shadowed_messages`：压缩算"被压内容"时 `session.events[seq]`；
- `web/payload.py::surface_with_seq`：历史渲染 `events[seq]`。

稠密会话里"位置 == seq"所以碰巧对；但**服务打开会话走跳帧重放**（`Session.from_path`
跳过流式帧），那里 seq 稀疏（保留 3 与 10644、跳掉中间）⇒ 要么 `IndexError`（历史/压缩整个
500），要么**静默取到错的事件** ⇒ 渲染/压缩结果**看起来正常但是错的**。

本文件把"两条路径在两种形态下结果相同"钉住——它们不会自己报错，只会悄悄给错数据。
"""
from __future__ import annotations

from my_coder.app.compaction import _shadowed_messages
from my_coder.state.session import Session
from my_coder.values.messages import (
    TRACE_FRAME_TYPES,
    TextBlock,
    create_user_message,
)
from my_coder.values.persistence import iter_events, save_event, scan_index
from my_coder.web.payload import surface_with_seq


def _frame_heavy_log(tmp_path):
    """两轮对话 + 中间夹帧 + 一次压缩（老日志的真实形状），返回 (path, 压缩区间)。"""
    path = tmp_path / 's.jsonl'
    session = Session(id='live')

    def add(type_, data, **kwargs):
        session.append(type_, data, **kwargs)
        save_event(path, session.events[-1])
        return session.events[-1]

    add('turn/start', {'turn': 1})
    first = add('user/message', create_user_message([TextBlock(text='第一问')]),
                surface_op='append')
    add('assistant/chunk', {'turn': 1, 'step': 1, 'chunk': {'text': '流', 'tool_calls': []}})
    add('assistant/message', {'turn': 1, 'step': 1,
                              'message': create_user_message([TextBlock(text='第一答')])},
        surface_op='append')
    add('turn/end', {'turn': 1, 'reason': 'done'})
    add('assistant/chunk', {'turn': 1, 'step': 2, 'chunk': {'text': '流2', 'tool_calls': []}})
    add('turn/start', {'turn': 2})
    last = add('user/message', create_user_message([TextBlock(text='第二问')]),
               surface_op='append')
    return path, (first.seq, last.seq)


def _both_forms(path):
    """同一份日志的两种重放：整表（含帧）与跳帧。"""
    dense = Session(id='dense')
    for event in iter_events(scan_index(path)):
        dense.adopt(event)
    sparse = Session.from_path(path, 'sparse', skip_types=TRACE_FRAME_TYPES)
    assert len(sparse.events) < len(dense.events), '本用例必须造出"跳帧"的稀疏形态'
    return dense, sparse


def test_surface_with_seq_matches_across_forms(tmp_path):
    """历史渲染的取数（`/history` 用）：两种形态必须逐条相同（seq 也相同）。"""
    path, _ = _frame_heavy_log(tmp_path)
    dense, sparse = _both_forms(path)

    dense_rows = [(seq, str(message.content)) for seq, message in surface_with_seq(dense)]
    sparse_rows = [(seq, str(message.content)) for seq, message in surface_with_seq(sparse)]
    assert sparse_rows == dense_rows, f'跳帧后历史取数变了：\n{dense_rows}\n{sparse_rows}'


def test_shadowed_messages_matches_across_forms(tmp_path):
    """压缩算"被压内容"（`_shadowed_messages` 用）：两种形态必须一致，且不许抛错。"""
    path, (start_seq, end_seq) = _frame_heavy_log(tmp_path)
    dense, sparse = _both_forms(path)

    dense_msgs = [str(message.content) for message in
                  _shadowed_messages(dense, start_seq, end_seq)]
    sparse_msgs = [str(message.content) for message in
                   _shadowed_messages(sparse, start_seq, end_seq)]
    assert sparse_msgs == dense_msgs, f'跳帧后被压内容变了：\n{dense_msgs}\n{sparse_msgs}'
    assert sparse_msgs, '这个区间里应当有被压消息（否则用例没测到东西）'


def test_history_payloads_matches_across_forms(tmp_path):
    """端到端：`history_payloads`（`/history` 的返回体）两种形态必须一致。"""
    from my_coder.web.payload import history_payloads

    path, _ = _frame_heavy_log(tmp_path)
    dense, sparse = _both_forms(path)
    assert history_payloads(sparse) == history_payloads(dense), '跳帧改变了历史返回体'
