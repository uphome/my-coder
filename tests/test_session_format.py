"""日志格式锚点：会话头 + 方向感知拒绝 + 未知事件守卫（P0-3 的六条契约）。

背景：日志是唯一事实源，但**格式本身没有版本**——升级运行时、增删事件类型之后，
旧读取者会静默地按自己的理解重建一份**语义不同**的会话。各条断言对应
`DSH_0.2_GAP_AND_PLAN.md` §P0-3 验收表的一行。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from my_coder.state.session import Session
from my_coder.values.messages import (
    KNOWN_SESSION_EVENT_TYPES,
    SESSION_FORMAT_VERSION,
    TextBlock,
    create_assistant_message,
    create_user_message,
    new_session_header,
)
from my_coder.values.persistence import (
    SessionFormatUnsupportedError,
    UnknownSessionEventError,
    declared_version,
    load_events,
    read_header,
    save_event,
    save_header,
)


def _write_log(path: Path, events: list[tuple[str, object, str | None]]) -> None:
    """把一个事件序列写成合法日志（含会话头）。

    注意用一个 Session 贯穿全部 append——每步新建 Session 会让每条事件都从
    seq 0 开始，写出一份 seq 全撞的坏日志（本测试自己踩过）。
    """
    save_header(path, new_session_header('test-session'))
    session = Session(id='tmp')
    for type_, data, surface_op in events:
        save_event(path, session.append(type_, data, surface_op=surface_op))


def test_new_sessions_get_a_header_with_the_current_version(tmp_path):
    """新建会话的第一行是会话头（不是事件），带 format_version。"""
    path = tmp_path / 'new.jsonl'
    session = Session(id='new')
    session.bind_store(path)
    session.append('turn/start', {'turn': 1})

    first = path.read_text(encoding='utf-8').splitlines()[0]
    assert json.loads(first)['session'] is True, first
    assert declared_version(path) == SESSION_FORMAT_VERSION
    header = read_header(path)
    assert header is not None and header.id == 'new'
    # 头不是事件：读回来只有那一条事件
    assert [e.type for e in load_events(path)] == ['turn/start']


def test_migrating_a_legacy_file_without_a_header(tmp_path):
    """无版本头的老文件按 v0 正常读（本机制落地前的文件与 v1 事件形状相同）。"""
    path = tmp_path / 'legacy.jsonl'
    user = create_user_message([TextBlock(text='老会话')])
    path.write_text(
        json.dumps({
            'seq': 0, 'time': 1.0, 'type': 'user/message',
            'data': {'$message': {'id': user.id, 'role': 'user',
                                  'content': [{'$text': '老会话'}],
                                  'source': {'$user': ''}}},
            'surface_op': 'append', 'shadowed': None, 'ignorable': False,
        }, ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    assert read_header(path) is None
    assert declared_version(path) == 0          # v0 = 没有版本锚点的老文件
    events = load_events(path)                  # 不需要迁移：形状相同
    assert [e.type for e in events] == ['user/message']


def test_a_newer_log_is_refused_not_silently_rebuilt(tmp_path):
    """日志版本 > 读取者 → 拒绝重建，且报错指明升级方向与文件路径（不是"损坏"）。"""
    path = tmp_path / 'future.jsonl'
    save_header(path, new_session_header('future', version=SESSION_FORMAT_VERSION + 7))
    with pytest.raises(SessionFormatUnsupportedError) as excinfo:
        load_events(path)
    message = str(excinfo.value)
    assert 'newer runtime' in message and 'upgrade' in message
    assert str(path) in message
    assert excinfo.value.found == SESSION_FORMAT_VERSION + 7


def test_an_unknown_event_type_refuses_the_rebuild(tmp_path):
    """未知事件 + `ignorable=false` → 拒绝重建（默认"必读"，宁过度拒绝勿静默掏空）。"""
    path = tmp_path / 'unknown.jsonl'
    save_header(path, new_session_header('unknown'))
    session = Session(id='tmp')
    save_event(path, session.append('user/message', create_user_message([TextBlock(text='ok')]),
                                   surface_op='append'))
    path.write_text(
        path.read_text(encoding='utf-8')
        + json.dumps({'seq': 1, 'time': 1.0, 'type': 'future/thing', 'data': None,
                      'surface_op': None, 'shadowed': None, 'ignorable': False},
                     ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    with pytest.raises(UnknownSessionEventError) as excinfo:
        load_events(path)
    assert excinfo.value.event_type == 'future/thing'
    assert 'silently drop' in str(excinfo.value)


def test_an_ignorable_unknown_event_is_skipped_and_the_rest_rebuilds(tmp_path):
    """未知事件 + `ignorable=true` → 跳过它，其余正常重建（词汇增长不必 bump 版本）。"""
    path = tmp_path / 'ignorable.jsonl'
    save_header(path, new_session_header('ignorable'))
    session = Session(id='tmp')
    save_event(path, session.append('user/message', create_user_message([TextBlock(text='一')]),
                                    surface_op='append'))
    path.write_text(
        path.read_text(encoding='utf-8')
        + json.dumps({'seq': 1, 'time': 1.0, 'type': 'future/thing', 'data': None,
                      'surface_op': None, 'shadowed': None, 'ignorable': True},
                     ensure_ascii=False) + '\n'
        + json.dumps({'seq': 2, 'time': 1.0, 'type': 'turn/start', 'data': {'$dict': {'turn': 1}},
                      'surface_op': None, 'shadowed': None, 'ignorable': False},
                     ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    assert [e.type for e in load_events(path)] == ['user/message', 'turn/start']


def test_the_header_does_not_change_derived_messages(tmp_path):
    """有会话头时 `derive_messages()` 与加头之前**逐字节一致**（头不进投影）。"""
    events = [
        ('turn/start', {'turn': 1}, None),
        ('user/message', create_user_message([TextBlock(text='问')]), 'append'),
        ('assistant/message',
         {'message': create_assistant_message([TextBlock(text='答')], provider='p', model='m')},
         'append'),
    ]
    with_header = tmp_path / 'with.jsonl'
    _write_log(with_header, events)

    without = tmp_path / 'without.jsonl'
    session = Session(id='tmp')
    for type_, data, surface_op in events:
        save_event(without, session.append(type_, data, surface_op=surface_op))

    def derive(path: Path) -> list[str]:
        restored = Session(id='restored')
        for event in load_events(path):
            restored.adopt(event)
        return [f'{m.role}:{[b.text for b in m.content if b.type == "text"]}'
                for m in restored.derive_messages()]

    assert derive(with_header) == derive(without) == ['user:[\'问\']', 'assistant:[\'答\']']


def test_no_known_event_type_is_missing_from_the_closed_set():
    """闭集不许漏：`my_coder/` 里 append 的事件类型必须都在 `KNOWN_SESSION_EVENT_TYPES`。

    这条防的是"加了新事件类型却忘了登记"——那种漏网在**别人的**读取者那里才会炸
    （本仓库自己 append 走的不是 `load_events`），所以只能在写入侧自查。
    """
    import re

    found: set[str] = set()
    for path in Path('my_coder').rglob('*.py'):
        if '__pycache__' in path.parts:
            continue
        for literal in re.findall(r"['\"]([a-z][a-z0-9_]*/[a-z0-9_]+(?:/[a-z0-9_]+)*)['\"]",
                                  path.read_text(encoding='utf-8')):
            # 排除 HTTP 媒体类型与假模块名
            if literal in ('application/json', 'text/event-stream'):
                continue
            found.add(literal)
    missing = sorted(found - KNOWN_SESSION_EVENT_TYPES)
    assert not missing, (
        f'这些事件类型没有登记进 KNOWN_SESSION_EVENT_TYPES：{missing}——'
        '漏登记会让别的读取者把它们当"未知且不可忽略"而拒绝重建')
    unused = sorted(KNOWN_SESSION_EVENT_TYPES - found)
    assert not unused, f'闭集里有已不再使用的事件类型：{unused}（删掉，别留僵尸）'
