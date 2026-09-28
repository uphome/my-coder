"""上下文召回（issue #3 的 M1）测试：L0 会话目录 / L1 用户话清单 / L2 回合明细。

判据都对着 `CONTEXT_BUDGET_DESIGN.md` §4.10 那张表——尤其是三条容易写错的：
1. **检索面含被遮蔽的事件**（压缩只改"看得见什么"，不改"存在什么"）；
2. **清单只收真人发言**（checkpoint 本身也是一条 `user/message`）；
3. **跨工作区不放行**（每个对话有自己的工作区，见 `agent.md` §10）。
"""
from __future__ import annotations

import json
from argparse import Namespace
from types import SimpleNamespace

import pytest

from my_coder.app.recall import (
    build_turns,
    render_manifest,
    render_session_index,
    render_turn,
    row_from_session,
    scan_sessions,
    session_index_text,
    shadowed_seqs,
)
from my_coder.capability.llm import FakeLlm
from my_coder.state.session import Session
from my_coder.tools import build_tools
from my_coder.values.messages import (
    TextBlock,
    ToolCallBlock,
    create_assistant_message,
    create_tool_result_message,
    create_user_message,
)


def _compacted_session(session_id: str = 'recall-a', workspace: str = '',
                       store=None) -> Session:
    """造一个"回合 1 已被压缩遮蔽、回合 2 有插队"的会话。

    `store` 给了就先 `bind_store` 再落事件——**顺序要紧**：`bind_store` 只是挂监听，
    之前 append 的事件不会补写进文件（测试里踩过：磁盘上只剩最后一条事件，
    于是"扫会话目录"看到的是一个没有用户话的空会话）。
    """
    session = Session(id=session_id)
    if store is not None:
        session.bind_store(store)
    if workspace:
        session.append('session/workspace', {'workspace': workspace, 'source': 'test'})
    session.append('session/title', {'title': '召回测试', 'source': 'test'})

    # 回合 1：探索 + 一次 bash（足迹素材）——随后被 checkpoint 遮蔽
    session.append('turn/start', {'turn': 1})
    session.append('step/start', {'turn': 1, 'step': 1})
    first = session.append('user/message', create_user_message([TextBlock(text='先探索这个项目')]),
                           surface_op='append')
    session.append('assistant/message', {
        'turn': 1, 'step': 1, 'message': create_assistant_message([
            TextBlock(text='探索完成：结论是 X。'),
            # 真实日志里工具调用**在 assistant 消息的块里**（`tool/call` 只是痕迹事件）；
            # 夹具必须照这个形状造，否则 L2 渲染会测出假象
            ToolCallBlock(id='call-1', name='bash', arguments=json.dumps({'command': 'pytest -q'})),
        ]),
    }, surface_op='append')
    session.append('tool/call', {
        'turn': 1, 'step': 1, 'call_id': 'call-1', 'name': 'bash',
        'arguments': json.dumps({'command': 'pytest -q'}),
    })
    last = session.append('tool/result', create_tool_result_message('call-1', '2 passed', False),
                          surface_op='append')
    session.append('turn/end', {'turn': 1, 'reason': 'completed'})

    # 回合 2：先落一条真实用户消息，再用 checkpoint 顶替回合 1 的 surface 区间
    session.append('turn/start', {'turn': 2})
    session.append('step/start', {'turn': 2, 'step': 1})
    session.append('user/message', create_user_message([TextBlock(text='继续')]), surface_op='append')
    checkpoint = ('This is an automatically generated checkpoint condensing an earlier span\n\n'
                  '<compacted-summary>\n## 主要请求\n- 探索\n</compacted-summary>')
    session.append('user/message', create_user_message([TextBlock(text=checkpoint)]),
                   surface_op='replace', shadowed=(first.seq, last.seq))
    session.append('user/message', create_user_message([TextBlock(text='插队：先停下来')]),
                   surface_op='append')
    session.append('assistant/message', {
        'turn': 2, 'step': 1, 'message': create_assistant_message([TextBlock(text='好，停下了。')]),
    }, surface_op='append')
    session.append('turn/end', {'turn': 2, 'reason': 'completed'})
    return session


def test_recall_surface_includes_shadowed_events():
    """检索面 = 曾经进过上下文的事件（含被 replace 遮蔽的）。"""
    session = _compacted_session()
    shadowed = shadowed_seqs(session)
    assert shadowed, '被遮蔽的 surface 事件必须仍在检索面里'
    turns = build_turns(session)
    assert turns[0].shadowed is True        # 回合 1 整体已折叠
    assert turns[1].shadowed is False       # 回合 2 还活着


def test_manifest_excludes_checkpoint_and_marks_flags():
    """清单只收真人发言；插队标来源；被遮蔽的保留并标 `[已压缩]`。"""
    session = _compacted_session()
    manifest = render_manifest(build_turns(session))
    assert 'automatically generated checkpoint' not in manifest   # 合成消息不进目录
    assert '[已压缩]' in manifest                                  # 但被折叠的**真人发言**要在
    assert '(插队) 插队：先停下来' in manifest
    assert '先探索这个项目' in manifest
    assert '探索完成：结论是 X。' in manifest                        # 结论摘录进了足迹


def test_manifest_footprint_comes_from_log():
    """足迹（step / 工具 / 文件 / 结局）从日志现算，并可选用命令原文。"""
    session = _compacted_session()
    turns = build_turns(session)
    turn1 = turns[0]
    assert turn1.steps == 1
    assert turn1.tools == ('bash',)
    assert turn1.commands == ('pytest -q',)
    assert turn1.outcome == 'completed'
    plain = render_manifest([turn1])
    assert '1 step · bash · — · completed' in plain
    with_cmd = render_manifest([turn1], with_commands=True)
    assert 'cmd:pytest -q' in with_cmd


def test_read_turn_renders_text_not_raw_jsonl():
    """L2 渲染成可读文本：没有 tagged 包装、没有 call_id 噪音。"""
    session = _compacted_session()
    text = render_turn(session, 1)
    assert '[turn 1 · step 1 · user] 先探索这个项目' in text
    assert 'tool_call bash({"command": "pytest -q"})' in text
    assert 'tool_result 2 passed' in text
    assert '$dict' not in text and '$message' not in text
    assert 'call-1' not in text                      # 内部坐标不外泄


def test_read_turn_bounds_output_and_reports_truncation():
    """超限要明说被截断（回合 p95 十万字符，绝不能无条件整段倒给模型）。"""
    session = _compacted_session()
    text = render_turn(session, 1, max_events=1)
    assert '被截断' in text


@pytest.mark.asyncio
async def test_recall_tools_declare_concurrency_and_offload(tmp_path):
    """两个工具都是纯读：声明 parallel（并发安全）+ offload（同步读盘，别堵事件循环）。"""
    registry = build_tools(workspace=tmp_path, sessions_dir=tmp_path)
    for name in ('session_manifest', 'read_turn'):
        spec = registry.get(name)
        assert spec.execution_mode == 'parallel', name
        assert spec.offload is True, name


@pytest.mark.asyncio
async def test_read_turn_tool_rejects_unknown_turn(tmp_path):
    """失败降级为结果：不存在的回合给 is_error，不炸循环。"""
    session = _compacted_session()
    registry = build_tools(workspace=tmp_path, sessions_dir=tmp_path)
    agent = SimpleNamespace(session=session)
    outcome = await registry.execute('read_turn', {'turn': 99}, agent)
    assert outcome.is_error and 'no turn 99' in outcome.content
    ok = await registry.execute('read_turn', {'turn': 1}, agent)
    assert not ok.is_error and '先探索这个项目' in ok.content


@pytest.mark.asyncio
async def test_manifest_tool_lists_current_session(tmp_path):
    """清单工具默认列当前会话，且不含 checkpoint 合成消息。"""
    session = _compacted_session()
    registry = build_tools(workspace=tmp_path, sessions_dir=tmp_path)
    agent = SimpleNamespace(session=session)
    outcome = await registry.execute('session_manifest', {}, agent)
    assert not outcome.is_error
    assert '先探索这个项目' in outcome.content
    assert 'automatically generated checkpoint' not in outcome.content


def test_session_index_filters_by_workspace(tmp_path):
    """L0 只列同一工作区的会话（跨工作区等于把别的项目念给模型）。"""
    sessions_dir = tmp_path / 'sess'
    sessions_dir.mkdir()
    _compacted_session('mine', workspace=str(tmp_path), store=sessions_dir / 'mine.jsonl')
    _compacted_session('other', workspace=str(tmp_path / 'elsewhere'),
                       store=sessions_dir / 'other.jsonl')

    rows = scan_sessions(sessions_dir, default_workspace='')
    assert {row.session_id for row in rows} == {'mine', 'other'}
    text = render_session_index(rows, current_id='mine', workspace=str(tmp_path))
    assert text is not None
    assert '[mine]' in text and '[other]' not in text
    assert '<context_sessions>' in text and '</context_sessions>' in text


def test_session_index_none_when_nothing_to_show(tmp_path):
    """没有可列的会话 → None（贡献者契约：无内容就不叠，不用空串占位）。"""
    assert render_session_index([], current_id='x', workspace='') is None


@pytest.mark.asyncio
async def test_cross_workspace_session_read_is_rejected(tmp_path):
    """跨工作区的会话不给读：错误里不泄漏对方的目录与内容。"""
    sessions_dir = tmp_path / 'sess'
    sessions_dir.mkdir()
    _compacted_session('other', workspace=str(tmp_path / 'elsewhere'),
                       store=sessions_dir / 'other.jsonl')

    current = _compacted_session('mine', workspace=str(tmp_path))
    registry = build_tools(workspace=tmp_path, sessions_dir=sessions_dir,
                           default_workspace=str(tmp_path))
    agent = SimpleNamespace(session=current)
    outcome = await registry.execute('session_manifest', {'session_id': 'other'}, agent)
    assert outcome.is_error and 'another workspace' in outcome.content
    assert 'elsewhere' not in outcome.content          # 不泄漏对方路径


@pytest.mark.asyncio
async def test_session_index_is_a_runtime_status_contributor(tmp_path):
    """L0 通过注册制叠给模型（issue #19 的通道），并进 request/header 审计。"""
    from my_coder.app.factory import build_agent

    sessions_dir = tmp_path / 'sess'
    sessions_dir.mkdir()
    session = Session(id='status-a')
    session.bind_store(sessions_dir / 'status-a.jsonl')
    session.append('session/workspace', {'workspace': str(tmp_path), 'source': 'test'})
    args = Namespace(fake=True, model='fake-model', workspace=tmp_path, hide_reasoning=False,
                     session='status-a', sessions=str(sessions_dir), prompt='x',
                     resume=False, verbose=False)
    agent = build_agent(session, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
    agent.llm = FakeLlm(script=[{'text': 'ok', 'finish_reason': 'stop'}])
    agent.followup('你好')
    await agent.when_idle()

    header = [e.data for e in session.events if e.type == 'request/header'][-1]
    index = header.get('runtime_status', {}).get('sessions')
    assert index, 'request/header.runtime_status["sessions"] 应记下这一轮模型看到的会话目录'
    assert 'status-a' in index and '<context_sessions>' in index
    # 状态栏是合成消息：不进 derive_messages（历史零污染）
    texts = [b.text for m in session.derive_messages() for b in m.content
             if isinstance(b, TextBlock)]
    assert not any('<context_sessions>' in text for text in texts)


def test_row_from_session_counts_from_memory():
    """当前会话未落盘时，L0 用内存里的日志兜底（不读盘）。"""
    session = _compacted_session('mem')
    row = row_from_session(session, default_workspace='')
    assert row.session_id == 'mem'
    assert row.turns == 2
    assert row.compactions == 0
    assert row.user_messages == 3        # 三条真人发言（含插队）；checkpoint 不算
    assert row.summary == '召回测试'        # 标题优先


# ---------- 审核（2026-09）查出的三个缺陷，各留一条回归 ----------

def test_checkpoint_is_not_counted_as_a_user_message(tmp_path):
    """审核发现：L0 的"用户话数"原先把 checkpoint 也算进去了。

    checkpoint 是 `surface_op='replace'` 写的 `user/message`（合成消息），而"清单只收
    真人发言"是我们自己定的规则——两边对不上时，模型会看到"74 条用户话"却只有 70 行清单。
    """
    sessions_dir = tmp_path / 'sess'
    sessions_dir.mkdir()
    session = _compacted_session('counted', workspace=str(tmp_path),
                                 store=sessions_dir / 'counted.jsonl')
    rows = scan_sessions(sessions_dir, default_workspace=str(tmp_path))
    row = next(r for r in rows if r.session_id == 'counted')
    assert row.user_messages == 3, '磁盘扫描：checkpoint 不算用户话'
    assert row_from_session(session).user_messages == 3, '内存兜底同理'


def test_session_index_uses_memory_for_the_current_session(tmp_path):
    """审核发现：L0 每请求会**重扫当前会话的整个日志**（它每请求都在变，缓存必失效）。

    修法是把当前会话排除在磁盘扫描之外、由内存投影。这条测试用一个**过期的磁盘副本**
    （只有 1 条用户话）反证：目录里读到的是内存里的 3 条，不是磁盘上的 1 条。
    """
    sessions_dir = tmp_path / 'sess'
    sessions_dir.mkdir()
    live = _compacted_session('live', workspace=str(tmp_path))
    (sessions_dir / 'live.jsonl').write_text(
        json.dumps({'seq': 0, 'time': 0.0, 'type': 'user/message',
                    'data': {'$message': {'id': 'x', 'role': 'user',
                                          'content': [{'$text': '过期副本'}]}},
                    'surface_op': 'append', 'shadowed': None, 'ignorable': False},
                   ensure_ascii=False) + '\n', encoding='utf-8')
    text = session_index_text(live, sessions_dir, default_workspace=str(tmp_path))
    assert text is not None
    assert '3 条用户话' in text, f'应以内存为准，实际：{text}'


@pytest.mark.asyncio
async def test_legacy_session_without_workspace_follows_host_default(tmp_path):
    """审核发现：跨工作区授权原先 fail-open（目标工作区判不出来就放行）。

    旧会话（日志里没有 `session/workspace`）按"跟随宿主默认"处理：当前会话也在默认
    工作区 → 允许；当前会话在**自定义**工作区 → 拒绝（它其实属于另一个目录）。
    """
    sessions_dir = tmp_path / 'sess'
    sessions_dir.mkdir()
    # 无 workspace 事件的旧会话（落盘时也不写 `session/workspace`）
    _compacted_session('legacy', store=sessions_dir / 'legacy.jsonl')

    custom = _compacted_session('custom', workspace=str(tmp_path / 'custom-ws'))
    registry = build_tools(workspace=tmp_path, sessions_dir=sessions_dir,
                           default_workspace=str(tmp_path))
    agent = SimpleNamespace(session=custom)
    denied = await registry.execute('session_manifest', {'session_id': 'legacy'}, agent)
    assert denied.is_error and 'another workspace' in denied.content

    # 当前会话就在宿主默认工作区 → 同一个"跟随默认"的旧会话应当放行
    inside = _compacted_session('inside', workspace=str(tmp_path))
    agent = SimpleNamespace(session=inside)
    allowed = await registry.execute('session_manifest', {'session_id': 'legacy'}, agent)
    assert not allowed.is_error


def test_read_turn_trace_scope_adds_reasoning():
    """`scope='trace'` 带上推理痕迹（默认不带）：痕迹能解释"当时为什么这么决定"。"""
    session = _compacted_session()
    session.append('turn/start', {'turn': 3})
    session.append('step/start', {'turn': 3, 'step': 1})
    session.append('assistant/reasoning', {'turn': 3, 'step': 1, 'reasoning': '因为 X 所以选 Y'})
    session.append('user/message', create_user_message([TextBlock(text='第三回合')]),
                   surface_op='append')
    assert 'reasoning' not in render_turn(session, 3)
    traced = render_turn(session, 3, scope='trace')
    assert '因为 X 所以选 Y' in traced and 'reasoning' in traced


# ---------- 第二轮自审（2026-09）查出的四个缺陷 + 一个文档/实现漂移 ----------

def test_scan_cache_does_not_leak_workspace_across_host_defaults(tmp_path):
    """缺陷：`_SCAN_CACHE` 的键只有 `(mtime_ns, size)`，而行里的 `workspace` 是
    "记录值 or 宿主默认值"——Web 宿主**每个 seat 有各自的默认工作区**，于是第二个
    seat 会命中第一个 seat 缓存的旧会话行，把别人的默认工作区当成自己的。

    后果不只是 L0 列错会话：跨工作区授权判据（`mine != theirs`）也建立在同一行上，
    错判就是**放行读另一个项目的会话**。
    """
    sessions_dir = tmp_path / 'sess'
    sessions_dir.mkdir()
    _compacted_session('legacy', store=sessions_dir / 'legacy.jsonl')   # 无 workspace 事件
    seat_a, seat_b = str(tmp_path / 'seat-a'), str(tmp_path / 'seat-b')

    row_a = next(r for r in scan_sessions(sessions_dir, seat_a) if r.session_id == 'legacy')
    row_b = next(r for r in scan_sessions(sessions_dir, seat_b) if r.session_id == 'legacy')
    assert row_a.workspace == seat_a
    assert row_b.workspace == seat_b, '第二个 seat 拿到了第一个 seat 的默认工作区'


def test_trace_scope_respects_the_step_filter():
    """缺陷：trace 分支写在 step 过滤**之前**，`step=2` 会把整回合所有步的推理都带出来
    （一步最多 600 字符 × 几十步）——模型点开一步，却拿到整回合的私密推理。"""
    session = Session(id='trace-steps')
    session.append('turn/start', {'turn': 1})
    for number in (1, 2):
        session.append('step/start', {'turn': 1, 'step': number})
        session.append('assistant/reasoning',
                       {'turn': 1, 'step': number, 'reasoning': f'第{number}步的想法'})
        session.append('user/message',
                       create_user_message([TextBlock(text=f'第{number}步的话')]),
                       surface_op='append')

    text = render_turn(session, 1, step=2, scope='trace')
    assert '第2步的想法' in text
    assert '第1步的想法' not in text, 'step 过滤必须同样作用于推理痕迹'
    assert '第1步的话' not in text


def test_trace_lines_count_toward_the_output_budget():
    """缺陷：trace 行既不进 `max_events` 也不进 `max_chars`——"L2 必须有界"被 reasoning
    旁路了（旧日志单步推理可上万字符）。"""
    session = Session(id='trace-budget')
    session.append('turn/start', {'turn': 1})
    for number in range(1, 6):
        session.append('step/start', {'turn': 1, 'step': number})
        session.append('assistant/reasoning',
                       {'turn': 1, 'step': number, 'reasoning': 'x' * 400})

    text = render_turn(session, 1, scope='trace', max_chars=1000)
    assert '被截断' in text, '痕迹超预算时同样要说被截断'
    assert len(text) < 3000, f'痕迹必须受字符上限约束，实际 {len(text)}'


def test_turn_header_lists_steps_and_truncation_names_them():
    """漂移：设计文档承诺"回合头 + 步骤目录"，实现只给了一行"用 step 精读"——却从不
    告诉模型有哪些 step，等于让它猜（旧日志 `step/start` 还可能整段缺失）。
    另：不存在的回合必须仍然渲染成空串，否则回合头会让工具把"没有这个回合"报成成功。"""
    session = Session(id='catalog')
    session.append('turn/start', {'turn': 1})
    for number in (1, 2, 3):
        session.append('step/start', {'turn': 1, 'step': number})
        session.append('user/message',
                       create_user_message([TextBlock(text=f'第{number}步的话')]),
                       surface_op='append')

    full = render_turn(session, 1)
    assert full.splitlines()[0].startswith('[turn 1 · 3 步 · 3 条事件]'), full.splitlines()[0]

    cut = render_turn(session, 1, max_events=1)
    assert '被截断' in cut and '1, 2, 3' in cut, f'截断提示要给可用 step，实际：{cut}'

    assert render_turn(session, 99) == '', '不存在的回合不许有害羞的回合头'
    assert render_turn(session, 1, step=9) == ''


@pytest.mark.asyncio
async def test_recall_tools_reject_mistyped_arguments(tmp_path):
    """缺陷：入参校验过松——`scope='traces'` 静默当 surface（模型以为读了推理、据此
    断言"当时没推理"）；`"false"` 是**真值**（`include_shadowed` 反而全都要）；
    `bool` 是 `int` 子类（`turn=true` 变成回合 1）。坏值一律降级成 is_error。"""
    session = _compacted_session()
    registry = build_tools(workspace=tmp_path, sessions_dir=tmp_path)
    agent = SimpleNamespace(session=session)
    for name, args in (
        ('read_turn', {'turn': 1, 'scope': 'traces'}),
        ('read_turn', {'turn': 1, 'scope': 0}),
        ('read_turn', {'turn': True}),
        ('read_turn', {'turn': 1, 'step': '1'}),
        ('session_manifest', {'include_shadowed': 'false'}),
        ('session_manifest', {'with_commands': 1}),
    ):
        outcome = await registry.execute(name, args, agent)
        assert outcome.is_error, f'{name}{args} 应当被拒：{outcome.content}'
    # 显式 `null` 是"没给"（与省略同义），不是坏值——这条边界也要钉住，
    # 否则"严格"会连正常的 `{"scope": null}` 一起拒掉
    plain = await registry.execute('read_turn', {'turn': 1, 'scope': None}, agent)
    assert not plain.is_error and '先探索这个项目' in plain.content
    # 边界：**缺** required 参数是注册表层的 ValueError（由 loop._run_one 降级成结果），
    # 与上面这些"值给歪了"是两层，别混在一起测
    with pytest.raises(ValueError, match='missing required'):
        await registry.execute('read_turn', {}, agent)


def test_live_counter_is_discarded_when_the_object_id_is_reused():
    """缺陷：`_LIVE_CACHE` 只按 `id(session)` 记，而 `id()` 在对象回收后**会被复用**
    ——一个已死会话的增量游标会被记到新会话名下，新会话的目录行凭空继承别人的计数。"""
    from my_coder.app import recall as recall_module

    session = _compacted_session('reused')
    recall_module._LIVE_CACHE[id(session)] = recall_module._LiveCounter(
        session_id='someone-else', indexed=0, user_messages=99, turns=7)
    row = row_from_session(session, default_workspace='')
    assert (row.user_messages, row.turns) == (3, 2), '身份对不上的游标必须重建'
