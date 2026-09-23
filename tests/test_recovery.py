"""崩溃自愈：悬空工具调用补齐 + 修复痕迹 + 幂等（含 Web 入口级验证）。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**只搬家、不改断言**。）
"""
from __future__ import annotations

import json

from agent_demo.llm import (
    _to_wire_messages,
)
from agent_demo.persistence import load_events, save_event
from agent_demo.recovery import dangling_tool_calls, repair_dangling_tool_calls
from agent_demo.session import Session
from agent_demo.values import (
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    create_assistant_message,
    create_tool_result_message,
    create_user_message,
)


def _write_crash_log(path, *, with_trace=True):
    """造一条"跑到一半被 kill"的日志并写盘。

    turn 已经开了、模型请求了工具、`tool/call` 痕迹也落了……然后进程没了：
    没有 `tool/result`，也没有 `turn/end`。（`with_trace=False` 覆盖"连痕迹都
    没来得及落"的那半个窗口。）
    """
    session = Session(id='crashed')
    session.append('turn/start', {'turn': 1})
    session.append('user/message', create_user_message([TextBlock(text='read a.txt')]),
                   surface_op='append')
    session.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message(
            [ToolCallBlock(id='call-1', name='read_file', arguments='{"file_path": "a.txt"}')],
            'fake', 'fake-model'),
    }, surface_op='append')
    if with_trace:
        session.append('tool/call', {'turn': 1, 'step': 1, 'call_id': 'call-1',
                                     'name': 'read_file', 'arguments': '{"file_path": "a.txt"}'})
    for event in session.events:
        save_event(path, event)


def _replay(path) -> Session:
    session = Session(id='crashed')
    for event in load_events(path):
        session.adopt(event)
    return session


def _tool_pairing(session) -> tuple[list[str], list[str]]:
    """wire 上的判据：(assistant 请求的 call_id 序列, tool 回复的 call_id 序列)。

    两者不相等就是非法 wire——实测 DeepSeek 会回 400
    "An assistant message with 'tool_calls' must be followed by tool messages"。
    """
    wire = _to_wire_messages('', session.derive_messages())
    requested = [call['id'] for message in wire if message.get('tool_calls')
                 for call in message['tool_calls']]
    answered = [message['tool_call_id'] for message in wire if message.get('role') == 'tool']
    return requested, answered


def test_interrupted_run_makes_the_session_unusable_until_repaired(tmp_path):
    """先钉住"不修会怎样"：wire 上请求了 1 个调用、0 条回复（真模型会 400）。"""
    path = tmp_path / 'crashed.jsonl'
    _write_crash_log(path)

    session = _replay(path)
    assert _tool_pairing(session) == (['call-1'], [])          # 非法 wire
    assert [call.id for call in dangling_tool_calls(session)] == ['call-1']


def test_repair_makes_the_recovered_session_legal_again(tmp_path):
    """恢复即自愈：补一条 is_error 合成结果 + 一条修复痕迹，wire 重新合法。"""
    path = tmp_path / 'crashed.jsonl'
    _write_crash_log(path)

    session = _replay(path)
    session.bind_store(path)
    assert repair_dangling_tool_calls(session) == ('call-1',)

    # 记忆里现在有结果，且明确是失败（模型据此重发，而不是以为自己读过了）
    results = [block for message in session.derive_messages() for block in message.content
               if isinstance(block, ToolResultBlock)]
    assert len(results) == 1
    assert results[0].tool_call_id == 'call-1' and results[0].is_error
    assert 'interrupted' in results[0].content

    # 修复痕迹：日志里能一眼看出这些结果是合成的（真相不被抹掉）
    repaired = [event for event in session.events if event.type == 'session/repaired']
    assert len(repaired) == 1
    assert repaired[0].data == {'reason': 'dangling-tool-call', 'call_ids': ['call-1']}

    # wire 判据：请求与回复一一对应
    assert _tool_pairing(session) == (['call-1'], ['call-1'])

    # 修复已落盘：重新加载后依然一致（日志是唯一事实源）
    reloaded = [event.type for event in load_events(path)]
    assert reloaded.count('session/repaired') == 1
    assert reloaded.count('tool/result') == 1
    assert dangling_tool_calls(_replay(path)) == []


def test_repair_is_idempotent_and_leaves_healthy_sessions_alone(tmp_path):
    """幂等 + 不误伤：健康的会话一个事件都不写，修过的会话再修什么都不写。"""
    healthy = Session(id='healthy')
    healthy.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message(
            [ToolCallBlock(id='call-ok', name='read_file', arguments='{}')], 'fake', 'fake'),
    }, surface_op='append')
    healthy.append('tool/result', create_tool_result_message('call-ok', 'file body', False),
                   surface_op='append')
    before = [event.type for event in healthy.events]
    assert repair_dangling_tool_calls(healthy) == ()
    assert [event.type for event in healthy.events] == before

    path = tmp_path / 'crashed.jsonl'
    _write_crash_log(path, with_trace=False)     # 连 tool/call 痕迹都没落的窗口
    session = _replay(path)
    assert repair_dangling_tool_calls(session) == ('call-1',)
    after_first = [event.type for event in session.events]
    assert repair_dangling_tool_calls(session) == ()
    assert [event.type for event in session.events] == after_first


def test_web_opening_a_crash_damaged_session_repairs_it(tmp_path):
    """入口级验证：打开会话（/sessions/<id>/switch）时自愈，UI 拿到的是失败结果。"""
    from fastapi.testclient import TestClient

    from agent_demo import web

    sessions_dir = tmp_path / 'sess'
    web.init_web(tmp_path, fake=True, sessions_dir=sessions_dir)
    _write_crash_log(sessions_dir / 'crashed.jsonl')
    client = TestClient(web.app)

    switched = client.post('/sessions/crashed/switch').json()
    assert switched['id'] == 'crashed'

    # 落盘的日志里能看见修复（重开浏览器/重连也一致）
    event_types = [event.type for event in load_events(sessions_dir / 'crashed.jsonl')]
    assert 'session/repaired' in event_types
    assert event_types.count('tool/result') == 1

    # 前端拿到的是"失败的工具结果"，而不是一条永远转圈的 running 卡片
    history = json.dumps(client.get('/history').json(), ensure_ascii=False)
    assert 'no result was recorded' in history
