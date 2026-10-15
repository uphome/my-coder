"""同族 bug 的**穷举**护栏：所有读取路径在"整表 vs 跳帧"两种重放下必须一致。

为什么要有这一条（比单点测试更值）："位置 != seq"这类 bug 的共同特征是
**不会自己报错，只会悄悄给错数据**——本会话已经因此栽过三次（seq 分配、压缩取被压内容、
历史渲染）。单点测试只能覆盖想到的地方；这里把**所有**读取路径都跑一遍并逐条比对，
任何人以后把某条路径改回"按位置取"都会当场红。

两种形态的来源：#49 起服务打开会话走 `Session.from_path`（跳帧），而分析与测试常见的是
整表重放（`iter_events` + `adopt`）。两者**必须给出同一份"内容视图"**——因为帧是纯痕迹
（不在检索面里、内容也不是独有的）。
"""
from __future__ import annotations

import json

from my_coder.app.compaction import _shadowed_messages
from my_coder.app.recall import (
    build_turns,
    render_manifest,
    render_turn,
    shadowed_seqs,
)
from my_coder.state.session import Session
from my_coder.tools.todo import fold_todos
from my_coder.values.messages import (
    TRACE_FRAME_TYPES,
    TextBlock,
    create_assistant_message,
    create_tool_result_message,
    create_user_message,
)
from my_coder.values.persistence import iter_events, save_event, scan_index
from my_coder.web.payload import (
    context_payload,
    history_payloads,
    surface_with_seq,
    user_message_turns,
)


def _rich_log(tmp_path):
    """两轮 + 工具调用/结果 + 夹帧 + todo + inbox + 标题（老日志的真实形状）。"""
    path = tmp_path / 'rich.jsonl'
    session = Session(id='rich')

    def add(type_, data, **kwargs):
        session.append(type_, data, **kwargs)
        save_event(path, session.events[-1])
        return session.events[-1]

    add('turn/start', {'turn': 1})
    add('user/message', create_user_message([TextBlock(text='第一问')]), surface_op='append')
    add('assistant/chunk', {'turn': 1, 'step': 1, 'chunk': {'text': '流', 'tool_calls': []}})
    add('tool/call', {'turn': 1, 'step': 1, 'call_id': 'c1', 'name': 'read_file',
                     'arguments': {'path': 'README.md'}})
    add('tool/result', create_tool_result_message('c1', 'README 内容', is_error=False),
        surface_op='append')
    add('assistant/message', {'turn': 1, 'step': 1,
                              'message': create_assistant_message([TextBlock(text='第一答')])},
        surface_op='append')
    add('turn/end', {'turn': 1, 'reason': 'done'})
    add('assistant/chunk', {'turn': 1, 'step': 2, 'chunk': {'text': '流2', 'tool_calls': []}})
    add('todo/write', {'items': [{'text': '剩下的活', 'status': 'pending'}]}, ignorable=True)
    add('turn/start', {'turn': 2})
    add('user/message', create_user_message([TextBlock(text='第二问')]), surface_op='append')
    return path


def _both_forms(path):
    dense = Session(id='dense')
    for event in iter_events(scan_index(path)):
        dense.adopt(event)
    sparse = Session.from_path(path, 'sparse', skip_types=TRACE_FRAME_TYPES)
    assert len(sparse.events) < len(dense.events), '本用例必须造出跳帧形态'
    return dense, sparse


def _norm(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


# 每一条都是"读给人/给模型看"的路径——它们在两种形态下必须完全一致
CONSUMERS = {
    'derive_messages': lambda s: [str(m.content) for m in s.derive_messages()],
    'surface': lambda s: list(s.surface),
    'workspace': lambda s: s.workspace(),
    'history_payloads': history_payloads,
    'context_payload': context_payload,
    'user_message_turns': lambda s: sorted(user_message_turns(s).items()),
    'fold_todos': fold_todos,
    'build_turns': lambda s: [t.turn for t in build_turns(s)],
    'render_manifest': lambda s: render_manifest(build_turns(s)),
    'shadowed_seqs': lambda s: list(shadowed_seqs(s)),
    'render_turn(1)': lambda s: render_turn(s, 1),
    'render_turn(2)': lambda s: render_turn(s, 2),
    'surface_with_seq': lambda s: [(seq, str(m.content)) for seq, m in surface_with_seq(s)],
    # 压缩路径（本会话第二个同族 bug 的现场）：被压区间的模型可见消息
    '_shadowed_messages': lambda s: [str(m.content) for m in _shadowed_messages(s, 1, 12)],
}


def test_every_read_path_agrees_across_forms(tmp_path):
    """穷举护栏：上表每一条在整表重放与跳帧重放下必须给出同一份结果。"""
    path = _rich_log(tmp_path)
    dense, sparse = _both_forms(path)
    mismatched = []
    for name, consumer in CONSUMERS.items():
        dense_value = _norm(consumer(dense))
        sparse_value = _norm(consumer(sparse))
        if dense_value != sparse_value:
            mismatched.append(f'{name}:\n  整表 {dense_value[:200]}\n  跳帧 {sparse_value[:200]}')
    assert not mismatched, '跳帧改变了读取结果（同族 bug）：\n' + '\n'.join(mismatched)


def test_cursor_apis_are_positions_not_seqs(tmp_path):
    """`event_count`/`events_since` 是**位置游标**，不是 seq——两种形态**本来就该不同**。

    跳帧会话里事件更少（帧被跳过），所以这两个数字必然更小。**这不是 bug**：它们的唯一
    消费者（`recall.row_from_session` 的 L0 目录增量缓存）是"位置进、位置出"，自洽即可。
    把这条写下来，是为了避免以后有人把它误当 bug 去"修"成相同。
    """
    path = _rich_log(tmp_path)
    dense, sparse = _both_forms(path)

    assert sparse.event_count < dense.event_count, '跳帧会话的事件数本来就该更少'
    # 位置进、位置出：两种形态各自自洽（切片结果的长度 == 总数 - 起点）
    for session in (dense, sparse):
        assert len(session.events_since(1)) == session.event_count - 1
