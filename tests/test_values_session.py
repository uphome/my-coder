"""值层与日志投影：surface 折叠、persistence 往返、resume、replace 遮蔽。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**断言与用例体一字未改**；
唯一差异是 26 处函数内冗余 import 被 ruff 的 F401/F811 删掉——那是拆分暴露出来的旧问题。）
"""
from __future__ import annotations

import pytest
from conftest import make_agent

from my_coder.state.session import Session
from my_coder.values.messages import TextBlock, create_assistant_message, create_user_message
from my_coder.values.persistence import load_events, save_event


def test_session_derive_only_surface_projects():
    session = Session(id='s')
    user = create_user_message([TextBlock(text='hello')])
    session.append('user/message', user, surface_op='append')
    session.append('todo/write', {'todos': []})
    session.append('assistant/chunk', {'chunk': {'text': 'hi'}})
    assistant = create_assistant_message([TextBlock(text='hi')], provider='fake', model='m')
    session.append('assistant/message', {'message': assistant}, surface_op='append')
    derived = session.derive_messages()
    assert [m.id for m in derived] == [user.id, assistant.id]


def test_surface_events_require_marker():
    session = Session(id='s')
    with pytest.raises(ValueError, match='requires surface_op'):
        session.append('user/message', create_user_message([TextBlock(text='x')]))
    with pytest.raises(ValueError, match='cannot carry surface_op'):
        session.append('turn/start', {'turn': 1}, surface_op='append')


def test_persistence_roundtrip(tmp_path):
    session = Session(id='s1')
    message = create_user_message([TextBlock(text='hello')])
    session.append('user/message', message, surface_op='append')
    path = tmp_path / 's1.jsonl'
    for event in session.events:
        save_event(path, event)

    restored = Session(id='s1')
    for event in load_events(path):
        restored.adopt(event)
    assert [m.id for m in restored.derive_messages()] == [message.id]


@pytest.mark.asyncio
async def test_resume_restores_inbox_and_last_turn(tmp_path):
    path = tmp_path / 'main.jsonl'
    session = Session(id='main')
    session.bind_store(path)
    agent, _ = make_agent([{'text': 'first answer', 'finish_reason': 'stop'}], session=session)
    agent.followup('first question')
    await agent.when_idle()

    restored = Session(id='main')
    for event in load_events(path):
        restored.adopt(event)
    agent2, _ = make_agent([{'text': 'second answer', 'finish_reason': 'stop'}], session=restored)
    assert agent2._last_turn == 1
    agent2.followup('second question')
    await agent2.when_idle()
    assert [e.type for e in restored.events].count('turn/start') == 2


def test_surface_replace_shadows_and_derives_in_place():
    """surface replace：遮蔽旧区间 + checkpoint 原位顶替 + 日志完整 + 重放一致。"""
    from my_coder.state.session import Session
    from my_coder.values.messages import TextBlock, create_assistant_message, create_user_message

    def user(text):
        return create_user_message([TextBlock(text=text)])

    def assistant(text):
        return {'message': create_assistant_message([TextBlock(text=text)])}

    s = Session(id='t')
    s.append('user/message', user('Q1'), surface_op='append')
    s.append('assistant/message', assistant('A1'), surface_op='append')
    s.append('user/message', user('Q2'), surface_op='append')
    s.append('assistant/message', assistant('A2'), surface_op='append')
    assert [m.content[0].text for m in s.derive_messages()] == ['Q1', 'A1', 'Q2', 'A2']

    # replace：遮蔽前两条（Q1/A1 = seq 0-1），checkpoint 原位顶替
    s.append('user/message', user('[checkpoint] early summary'),
             surface_op='replace', shadowed=(0, 1))
    assert s.surface == (4, 2, 3)          # checkpoint(4) 在原区间头部
    assert [m.content[0].text for m in s.derive_messages()] == ['[checkpoint] early summary', 'Q2', 'A2']

    # 日志 append-only：被遮蔽事件仍在（审计/恢复不丢）
    assert len(s.events) == 5
    assert [e.surface_op for e in s.events] == ['append', 'append', 'append', 'append', 'replace']
    assert s.events[4].shadowed == (0, 1)

    # 校验：非 surface 不能带 surface_op；replace 必须带 shadowed
    import pytest as _pytest
    with _pytest.raises(ValueError):
        s.append('turn/start', {'turn': 2}, surface_op='append')
    with _pytest.raises(ValueError):
        s.append('user/message', user('x'), surface_op='replace')  # 缺 shadowed
    with _pytest.raises(ValueError):
        s.append('user/message', user('x'), surface_op='append', shadowed=(0, 1))  # append 带 shadowed

    # 连续 replace（再压 Q2/A2）：新 checkpoint 继续原位
    s.append('user/message', user('[checkpoint2] full summary'),
             surface_op='replace', shadowed=(2, 3))
    assert [m.content[0].text for m in s.derive_messages()] == ['[checkpoint] early summary', '[checkpoint2] full summary']


def test_surface_replace_replays_identically(tmp_path):
    """resume：replace 遮蔽随日志重放重建，投影与压前一致。"""
    from my_coder.state.session import Session
    from my_coder.values.messages import TextBlock, create_user_message
    from my_coder.values.persistence import load_events, save_event

    s = Session(id='r')
    s.append('user/message', create_user_message([TextBlock(text='hi')]), surface_op='append')
    s.append('user/message', create_user_message([TextBlock(text='[cp] summarized')]),
             surface_op='replace', shadowed=(0, 0))

    path = tmp_path / 'r.jsonl'
    for e in s.events:
        save_event(path, e)
    restored = Session(id='r')
    for e in load_events(path):
        restored.adopt(e)

    assert restored.surface == s.surface
    assert [m.content[0].text for m in restored.derive_messages()] == ['[cp] summarized']
    # replace 的 shadowed 区间经 JSONL 往返后保留
    rep = [e for e in restored.events if e.surface_op == 'replace'][0]
    assert rep.shadowed == (0, 0)


def test_surface_replace_positional_not_numeric():
    """多次 replace 用位置语义：数值范围会误吞，位置定位才正确（防 surface 乱）。"""
    from my_coder.state.session import Session
    from my_coder.values.messages import TextBlock, create_assistant_message, create_user_message

    def user(text):
        return create_user_message([TextBlock(text=text)])

    def asst(text):
        return {'message': create_assistant_message([TextBlock(text=text)])}

    s = Session(id='pos')
    s.append('user/message', user('Q1'), surface_op='append')       # seq 0
    s.append('assistant/message', asst('A1'), surface_op='append')  # seq 1
    s.append('user/message', user('Q2'), surface_op='append')       # seq 2
    s.append('assistant/message', asst('A2'), surface_op='append')  # seq 3

    # 压缩 1：遮蔽 seq 0-1 → cp1(seq 4) 顶替，surface 变为 (4,2,3)——不再 seq 单调
    s.append('user/message', user('[cp1]'), surface_op='replace', shadowed=(0, 1))
    assert s.surface == (4, 2, 3)
    assert [m.content[0].text for m in s.derive_messages()] == ['[cp1]', 'Q2', 'A2']

    # 压缩 2：遮蔽 surface 连续段 [cp1(4) .. Q2(2)] → cp2(seq 5) 顶替
    # 数值范围 2..4 会误吞 A2(3)；位置语义只遮蔽 4 和 2，A2 必须保留
    s.append('user/message', user('[cp2]'), surface_op='replace', shadowed=(4, 2))
    assert s.surface == (5, 3)
    texts = [m.content[0].text for m in s.derive_messages()]
    assert texts == ['[cp2]', 'A2'], f'A2 must survive positional replace, got {texts}'

    # 压缩 3：整段再压（cp2 + A2）→ cp3(seq 6)，surface 只剩它
    s.append('user/message', user('[cp3]'), surface_op='replace', shadowed=(5, 3))
    assert s.surface == (6,)
    assert [m.content[0].text for m in s.derive_messages()] == ['[cp3]']

    # 日志 append-only：6 条原始全在
    assert len(s.events) == 7
    assert sum(1 for e in s.events if e.surface_op == 'replace') == 3


def test_surface_replace_positional_replays(tmp_path):
    """嵌套 replace 的重放一致性：adopt 重建出相同 surface 与派生消息。"""
    from my_coder.state.session import Session
    from my_coder.values.messages import TextBlock, create_assistant_message, create_user_message
    from my_coder.values.persistence import load_events, save_event

    def user(text):
        return create_user_message([TextBlock(text=text)])

    def asst(text):
        return {'message': create_assistant_message([TextBlock(text=text)])}

    s = Session(id='rp')
    s.append('user/message', user('Q1'), surface_op='append')       # seq 0
    s.append('assistant/message', asst('A1'), surface_op='append')  # seq 1
    s.append('user/message', user('Q2'), surface_op='append')       # seq 2
    s.append('assistant/message', asst('A2'), surface_op='append')  # seq 3
    s.append('user/message', user('[cp1]'), surface_op='replace', shadowed=(0, 1))   # seq 4
    s.append('user/message', user('[cp2]'), surface_op='replace', shadowed=(4, 2))   # seq 5

    path = tmp_path / 'rp.jsonl'
    for e in s.events:
        save_event(path, e)
    restored = Session(id='rp')
    for e in load_events(path):
        restored.adopt(e)

    assert restored.surface == s.surface == (5, 3)
    assert [m.content[0].text for m in restored.derive_messages()] == ['[cp2]', 'A2']
