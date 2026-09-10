"""架构测试：值层冻结、日志推导、inbox 重放、严格插值、工具循环、取消、持久化回放。"""

import asyncio
import json

import httpx
import pytest

from agent_demo.agent import Agent
from agent_demo.hooks import Hooks, PreStepContext, RequestErrorContext
from agent_demo.inbox import Inbox
from agent_demo.llm import (
    FakeLlm,
    LlmRequest,
    StreamChunk,
    _delta_reasoning,
    build_payload,
)
from agent_demo.persistence import load_events, save_event
from agent_demo.prompt import PromptRegistry
from agent_demo.registry import ToolOutcome, ToolRegistry, ToolSpec
from agent_demo.session import Session
from agent_demo.tools import build_tools
from agent_demo.values import (
    TextBlock,
    ToolCallBlock,
    create_assistant_message,
    create_tool_result_message,
    create_user_message,
)


def make_agent(script, session=None, hooks=None):
    session = session if session is not None else Session(id='test')
    llm = FakeLlm(script=script, provider='fake', model='fake-model')
    return Agent(
        session=session,
        llm=llm,
        prompt=PromptRegistry(),
        tools=ToolRegistry(),
        options={'provider': 'fake', 'model': 'fake-model'},
        hooks=hooks,
    ), session


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


def test_inbox_durable_first_then_replay():
    session = Session(id='s')
    inbox = Inbox(session)
    message = create_user_message([TextBlock(text='x')])
    inbox.append('next-turn', message)
    assert session.events[-1].type == 'agent/inbox/spliced'
    assert session.events[-1].data['inserted'][0].id == message.id
    replayed = Inbox(session)
    assert [m.id for m in replayed.next_turn] == [message.id]


def test_inbox_claim_takes_next_step_batch_then_one_turn():
    session = Session(id='s')
    inbox = Inbox(session)
    turn_msg = create_user_message([TextBlock(text='turn')])
    step_a = create_user_message([TextBlock(text='a')])
    step_b = create_user_message([TextBlock(text='b')])
    inbox.append('next-turn', turn_msg)
    inbox.append('next-step', step_a)
    inbox.append('next-step', step_b)
    claimed = inbox.claim('next-turn', 1)
    assert [m.id for m in claimed] == [step_a.id, step_b.id, turn_msg.id]
    assert not inbox.has_pending


def test_inbox_rejects_duplicate_identity():
    session = Session(id='s')
    inbox = Inbox(session)
    message = create_user_message([TextBlock(text='x')])
    inbox.append('next-turn', message)
    with pytest.raises(ValueError, match='already pending'):
        inbox.append('next-step', message)


def test_inbox_remove_is_durable_and_replays():
    """撤回（队列区 × 按钮）：撤回同样先落 spliced 事件——重放不会复活它。

    未 claim 的消息没有 surface，撤回后日志里只剩 spliced（outcome=canceled）
    这一条痕迹；它不进模型记忆（surface 才进），所以撤回是干净的。
    """
    session = Session(id='s')
    inbox = Inbox(session)
    keep = create_user_message([TextBlock(text='keep')])
    drop = create_user_message([TextBlock(text='drop')])
    inbox.append('next-turn', keep)
    inbox.append('next-step', drop)

    assert inbox.remove(drop.id) is True
    assert [m.id for m in inbox.next_step] == []
    assert [m.id for m in inbox.next_turn] == [keep.id]
    last = session.events[-1]
    assert last.type == 'agent/inbox/spliced'
    assert last.data['removed_count'] == 1 and last.data['outcome'] == 'canceled'
    # 认领过的（不存在于任何队列）撤不回来：返回 False，不改日志
    assert inbox.remove(drop.id) is False
    splices = [e for e in session.events if e.type == 'agent/inbox/spliced']
    assert len(splices) == 3      # 2 次入队 + 1 次撤回；失败的撤回不落事件

    # 重放：队列原地复活成撤回后的样子（撤回不是"内存里删掉"）
    replayed = Inbox(session)
    assert [m.id for m in replayed.next_turn] == [keep.id]
    assert [m.id for m in replayed.next_step] == []


def test_interpolation_strict():
    registry = PromptRegistry()
    registry.section('persona', 0, 'You run on {{model}} in {{cwd}}.')
    registry.variable('model', lambda ctx: 'deepseek-chat')
    registry.variable('cwd', lambda ctx: '/tmp')
    assert registry.render(registry.assemble({})) == 'You run on deepseek-chat in /tmp.'

    bad = PromptRegistry()
    bad.section('p', 0, '{{nope}}')
    with pytest.raises(ValueError, match='unknown prompt variable'):
        bad.render(bad.assemble({}))


def test_interpolation_keeps_literal_braces():
    registry = PromptRegistry()
    registry.section('p', 0, 'a { b and an unclosed {{ brace')
    assert registry.render(registry.assemble({})) == 'a { b and an unclosed {{ brace'


def test_interpolation_rejects_nested_braces():
    registry = PromptRegistry()
    registry.section('p', 0, '{{{model}}}')
    registry.variable('model', lambda ctx: 'M')
    with pytest.raises(ValueError, match='malformed prompt variable'):
        registry.render(registry.assemble({}))


@pytest.mark.asyncio
async def test_tool_loop_fake():
    called = []

    async def read_file(args, agent, signal):
        called.append(args)
        return ToolOutcome(content='line 1\nline 2')

    session = Session(id='s')
    tools = ToolRegistry()
    tools.register(ToolSpec(
        name='read_file',
        description='Read a file.',
        parameters={
            'type': 'object',
            'properties': {'file_path': {'type': 'string'}},
            'required': ['file_path'],
        },
        execute=read_file,
    ))
    llm = FakeLlm(script=[
        {
            'tool_calls': [{'id': 'call1', 'name': 'read_file', 'arguments': json.dumps({'file_path': 'a.txt'})}],
            'finish_reason': 'tool_calls',
        },
        {'text': 'The file says: line 1 line 2', 'finish_reason': 'stop'},
    ])
    agent = Agent(
        session=session, llm=llm, prompt=PromptRegistry(), tools=tools,
        options={'provider': 'fake', 'model': 'fake-model'},
    )
    agent.followup('read a.txt')
    await agent.when_idle()

    assert called == [{'file_path': 'a.txt'}]
    types = [e.type for e in session.events]
    assert types.count('tool/call') == 1
    assert 'tool/result' in types
    assert session.events[-1].type == 'turn/end'
    assert session.events[-1].data['reason'] == 'completed'
    assert [m.role for m in session.derive_messages()] == ['user', 'assistant', 'user', 'assistant']
    assert agent.status == 'idle'


@pytest.mark.asyncio
async def test_pre_step_hook_rewrites_and_rejects():
    hooks = Hooks()

    async def rewrite(ctx: PreStepContext, default):
        messages = await default()
        if not messages:
            return None
        return [create_user_message([TextBlock(text='rewritten')])]

    hooks.pre_step = rewrite
    agent, session = make_agent([{'text': 'done', 'finish_reason': 'stop'}], hooks=hooks)
    agent.followup('original')
    await agent.when_idle()
    user_events = [e for e in session.events if e.type == 'user/message']
    assert len(user_events) == 1
    assert user_events[0].data.content[0].text == 'rewritten'

    hooks.pre_step = None

    async def reject(ctx, default):
        return None

    hooks.pre_step = reject
    agent, session = make_agent([{'text': 'never', 'finish_reason': 'stop'}], hooks=hooks)
    agent.followup('original')
    await agent.when_idle()
    assert session.events[-1].data['reason'] == 'blocked'


@pytest.mark.asyncio
async def test_request_error_hook_retries():
    hooks = Hooks()

    async def on_error(ctx: RequestErrorContext) -> str:
        return 'retry' if ctx.code == 'RATE_LIMIT' else 'throw'

    hooks.request_error = on_error
    agent, session = make_agent(
        [
            {'error': {'code': 'RATE_LIMIT', 'message': '429'}},
            {'text': 'recovered', 'finish_reason': 'stop'},
        ],
        hooks=hooks,
    )
    agent.followup('go')
    await agent.when_idle()
    assert [e.type for e in session.events].count('request/header') == 2
    assert session.events[-1].data['reason'] == 'completed'
    final = [m for m in session.derive_messages() if m.role == 'assistant'][-1]
    assert final.content[0].text == 'recovered'


@pytest.mark.asyncio
async def test_cancel_aborts_turn():
    session = Session(id='s')

    class SlowLlm(FakeLlm):
        async def stream(self, request, signal=None):
            yield StreamChunk(text='slow ')
            await asyncio.sleep(5)
            yield StreamChunk(text='finished', finish_reason='stop')

    agent = Agent(
        session=session, llm=SlowLlm([{}]), prompt=PromptRegistry(), tools=ToolRegistry(),
        options={'provider': 'fake', 'model': 'fake-model'},
    )
    agent.followup('go')
    await asyncio.sleep(0.05)
    agent.cancel()
    await agent.when_idle()
    assert session.events[-1].type == 'turn/end'
    assert session.events[-1].data['reason'] == 'aborted'
    assert agent.status == 'idle'
    assert not agent.inbox.has_pending


def test_wire_payload_uses_openai_function_wrapper():
    user = create_user_message([TextBlock(text='hi')])
    assistant = create_assistant_message(
        [ToolCallBlock(id='c1', name='read_file', arguments='{"file_path":"a.txt"}')],
        provider='deepseek', model='deepseek-chat',
    )
    result = create_tool_result_message('c1', 'content', False)
    request = LlmRequest(
        provider='deepseek',
        model='deepseek-chat',
        system='sys',
        messages=(user, assistant, result),
        tools=({'name': 'read_file', 'description': 'd', 'parameters': {'type': 'object'}},),
    )
    payload = build_payload(request, 'fallback')
    assert payload['tools'] == [{
        'type': 'function',
        'function': {'name': 'read_file', 'description': 'd', 'parameters': {'type': 'object'}},
    }]
    assert payload['tool_choice'] == 'auto'
    wire = payload['messages']
    assert wire[0] == {'role': 'system', 'content': 'sys'}
    assert wire[1] == {'role': 'user', 'content': 'hi'}
    assert wire[2]['role'] == 'assistant'
    assert wire[2]['tool_calls'][0]['function']['name'] == 'read_file'
    assert wire[3] == {'role': 'tool', 'tool_call_id': 'c1', 'content': 'content'}


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


@pytest.mark.asyncio
async def test_read_file_pages_with_line_numbers(tmp_path):
    registry = build_tools(workspace=tmp_path)
    path = tmp_path / 'code.py'
    path.write_text(''.join(f'line {i}\n' for i in range(1, 6)), encoding='utf-8')

    # 默认：全部行 + 行号，窗口没截断时没有提示行
    full = await registry.execute('read_file', {'file_path': str(path)}, None)
    assert full.is_error is False
    assert full.content == (
        '   1: line 1\n   2: line 2\n   3: line 3\n   4: line 4\n   5: line 5'
    )

    # offset + limit 组合：从第 3 行开始只给 2 行，且必须告知后面还有
    paged = await registry.execute(
        'read_file', {'file_path': str(path), 'offset': 3, 'limit': 2}, None)
    assert paged.content == (
        '   3: line 3\n   4: line 4\n'
        '(file has 5 lines; showing lines 3-4; increase offset to continue)'
    )

    # 窗口恰好覆盖到文件末尾：没有截断提示
    tail = await registry.execute(
        'read_file', {'file_path': str(path), 'offset': 5, 'limit': 10}, None)
    assert tail.content == '   5: line 5'

    # line_numbers=false：裸行输出（读文档/日志省 token）；JSON 布尔和字符串写法都接受
    plain = await registry.execute(
        'read_file', {'file_path': str(path), 'line_numbers': False}, None)
    assert plain.content == 'line 1\nline 2\nline 3\nline 4\nline 5'
    plain_str = await registry.execute(
        'read_file', {'file_path': str(path), 'line_numbers': 'false'}, None)
    assert plain_str.content == 'line 1\nline 2\nline 3\nline 4\nline 5'


@pytest.mark.asyncio
async def test_read_file_errors_are_results(tmp_path):
    registry = build_tools(workspace=tmp_path)
    path = tmp_path / 'code.py'
    path.write_text('a\nb\nc\n', encoding='utf-8')

    missing = await registry.execute('read_file', {'file_path': str(tmp_path / 'nope.txt')}, None)
    assert missing.is_error and 'file not found' in missing.content

    oob = await registry.execute('read_file', {'file_path': str(path), 'offset': 10}, None)
    assert oob.is_error and 'file has 3 lines, offset 10 out of range' in oob.content

    bad = await registry.execute('read_file', {'file_path': str(path), 'offset': 'abc'}, None)
    assert bad.is_error and 'integers' in bad.content

    zero = await registry.execute('read_file', {'file_path': str(path), 'limit': 0}, None)
    assert zero.is_error and 'limit must be >= 1' in zero.content

    weird = await registry.execute(
        'read_file', {'file_path': str(path), 'line_numbers': 'maybe'}, None)
    assert weird.is_error and 'line_numbers must be a boolean' in weird.content

    empty = tmp_path / 'empty.txt'
    empty.write_text('', encoding='utf-8')
    result = await registry.execute('read_file', {'file_path': str(empty)}, None)
    assert result.is_error is False and result.content == '(empty file)'


def test_delta_reasoning_unifies_common_fields():
    assert _delta_reasoning({'reasoning_content': 'a'}) == 'a'
    assert _delta_reasoning({'reasoning': 'b'}) == 'b'
    assert _delta_reasoning({'thinking': 'c'}) == 'c'
    assert _delta_reasoning({'content': 'not reasoning'}) == ''
    assert _delta_reasoning({'reasoning': '', 'thinking': 'd'}) == 'd'


@pytest.mark.asyncio
async def test_fake_llm_yields_reasoning():
    llm = FakeLlm([{'reasoning': '先思考', 'text': '再回答', 'finish_reason': 'stop'}])
    chunks = [chunk async for chunk in llm.stream(LlmRequest())]
    assert ''.join(chunk.reasoning for chunk in chunks) == '先思考'
    assert ''.join(chunk.text for chunk in chunks) == '再回答'


@pytest.mark.asyncio
async def test_reasoning_is_trace_only_not_in_model_memory():
    agent, session = make_agent([
        {
            'reasoning': '这是内部思考，不应该进入模型记忆。',
            'text': '这是正式回答。',
            'finish_reason': 'stop',
        },
    ])
    agent.followup('请思考后回答')
    await agent.when_idle()

    types = [e.type for e in session.events]
    assert 'assistant/reasoning/chunk' in types
    assert 'assistant/reasoning' in types
    # 思维链事件不是 surface 事件，不会出现在 derive_messages 里
    derived_texts = []
    for message in session.derive_messages():
        for block in message.content:
            if block.type == 'text':
                derived_texts.append(block.text)
    assert '这是内部思考，不应该进入模型记忆。' not in derived_texts
    assert '这是正式回答。' in derived_texts
    # 日志中完整思维链确实被记录下来
    reasoning_events = [e for e in session.events if e.type == 'assistant/reasoning']
    assert reasoning_events and reasoning_events[-1].data['reasoning'] == '这是内部思考，不应该进入模型记忆。'


@pytest.mark.asyncio
async def test_grep_matches_and_groups(tmp_path):
    registry = build_tools(workspace=tmp_path)
    (tmp_path / 'a.py').write_text('def foo():\n    return 1\n', encoding='utf-8')
    (tmp_path / 'b.py').write_text('x = foo()\n', encoding='utf-8')
    (tmp_path / 'notes.md').write_text('nothing here\n', encoding='utf-8')

    out = await registry.execute('grep', {'pattern': 'foo', 'path': str(tmp_path)}, None)
    assert out.is_error is False
    assert out.content.startswith('Found 2 matches')
    assert 'a.py\nLine 1: def foo():' in out.content
    assert 'b.py\nLine 1: x = foo()' in out.content
    assert 'notes.md' not in out.content  # 没命中的文件不出现在分组里

    # include 过滤：只搜 *.py
    only_py = await registry.execute(
        'grep', {'pattern': 'foo', 'path': str(tmp_path), 'include': '*.py'}, None)
    assert 'notes.md' not in only_py.content

    # 大小写敏感（对齐 harness：不公开大小写参数）
    case = await registry.execute('grep', {'pattern': 'FOO', 'path': str(tmp_path)}, None)
    assert case.content == '(no matches)'

    # 坏正则 / 坏 include / 路径不存在：一律降级为 is_error
    bad = await registry.execute('grep', {'pattern': '(unclosed', 'path': str(tmp_path)}, None)
    assert bad.is_error and 'invalid regex' in bad.content
    bad_inc = await registry.execute(
        'grep', {'pattern': 'foo', 'path': str(tmp_path), 'include': '*.py,*.md'}, None)
    assert bad_inc.is_error and 'one positive glob' in bad_inc.content
    missing = await registry.execute('grep', {'pattern': 'foo', 'path': str(tmp_path / 'nope')}, None)
    assert missing.is_error and 'not found' in missing.content

    # path 可以是单个文件（对齐 harness：grep 目标是文件或目录）
    single_file = await registry.execute(
        'grep', {'pattern': 'foo', 'path': str(tmp_path / 'a.py')}, None)
    assert single_file.content.startswith('Found 1 match')
    assert 'a.py\nLine 1' in single_file.content


@pytest.mark.asyncio
async def test_grep_skips_hidden_and_truncates(tmp_path):
    registry = build_tools(workspace=tmp_path)
    (tmp_path / '.hidden.py').write_text('secret = 1\n', encoding='utf-8')
    git = tmp_path / '.git'
    git.mkdir()
    (git / 'config').write_text('secret = 2\n', encoding='utf-8')
    big = tmp_path / 'big.py'
    big.write_text(''.join(f'match {i}\n' for i in range(260)), encoding='utf-8')

    # 隐藏条目（. 开头目录/文件）不进搜索结果
    hidden = await registry.execute('grep', {'pattern': 'secret', 'path': str(tmp_path)}, None)
    assert hidden.content == '(no matches)'

    # 超过上限：头部 Found 250 of 260 + 截断页脚（模型必须知道还有更多）
    trunc = await registry.execute('grep', {'pattern': 'match', 'path': str(tmp_path)}, None)
    assert trunc.content.startswith('Found 250 of 260 matches')
    assert 'narrow pattern' in trunc.content


@pytest.mark.asyncio
async def test_glob_recursive_and_skip_hidden(tmp_path):
    registry = build_tools(workspace=tmp_path)
    (tmp_path / 'a.py').write_text('x\n', encoding='utf-8')
    tests = tmp_path / 'tests'
    tests.mkdir()
    (tests / 'test_a.py').write_text('x\n', encoding='utf-8')
    (tests / 'test_b.txt').write_text('x\n', encoding='utf-8')
    hidden = tmp_path / '.hidden'
    hidden.mkdir()
    (hidden / 'h.py').write_text('x\n', encoding='utf-8')

    out = await registry.execute('glob', {'pattern': '**/*.py', 'path': str(tmp_path)}, None)
    assert out.is_error is False
    # 递归匹配 + 相对路径 + 排序输出 + 隐藏目录跳过
    assert out.content.splitlines() == ['a.py', 'tests/test_a.py']
    assert '.hidden' not in out.content

    no_match = await registry.execute('glob', {'pattern': '**/*.rs', 'path': str(tmp_path)}, None)
    assert no_match.content == '(no paths match)'

    # 畸形模式（如 [z-a]）：3.13 的 pathlib 宽容处理（按字面量，不抛错）——
    # 与 rg 的 invalid-regex 行为不同；工具如实返回空结果，模型自己会修正。
    # executor 仍保留 re.error/ValueError 兜底（防御其他平台/版本的异常）。
    bad = await registry.execute('glob', {'pattern': '[z-a]', 'path': str(tmp_path)}, None)
    assert bad.is_error is False and bad.content == '(no paths match)'

    # 超过上限：截断页脚
    many = tmp_path / 'many'
    many.mkdir()
    for i in range(105):
        (many / f'f{i:03}.txt').write_text('x\n', encoding='utf-8')
    trunc = await registry.execute('glob', {'pattern': 'many/**', 'path': str(tmp_path)}, None)
    assert trunc.content.endswith('(Showing 100 of 105 paths; narrow the pattern to see more.)')


@pytest.mark.asyncio
async def test_workspace_boundary_blocks_escape(tmp_path):
    registry = build_tools(workspace=tmp_path)
    outside = tmp_path.parent / 'outside.txt'
    outside.write_text('secret\n', encoding='utf-8')

    # 绝对路径越界 → is_error（模型看到原因能自己改正）
    denied = await registry.execute('read_file', {'file_path': str(outside)}, None)
    assert denied.is_error and 'outside workspace' in denied.content

    # 相对路径 .. 逃逸 → is_error（resolve 折叠后前缀不符）
    escaped = await registry.execute('read_file', {'file_path': '../outside.txt'}, None)
    assert escaped.is_error and 'outside workspace' in escaped.content

    # 越界写入不落盘
    write = await registry.execute('write_file', {'file_path': str(outside), 'content': 'x'}, None)
    assert write.is_error and 'outside workspace' in write.content
    assert outside.read_text(encoding='utf-8') == 'secret\n'

    # grep / glob 的 path 越界同样拒绝
    grep = await registry.execute('grep', {'pattern': 'x', 'path': str(outside.parent)}, None)
    assert grep.is_error and 'outside workspace' in grep.content
    glb = await registry.execute('glob', {'pattern': '**', 'path': str(outside.parent)}, None)
    assert glb.is_error and 'outside workspace' in glb.content

    # workspace 内一切正常
    inner = tmp_path / 'inner.txt'
    inner.write_text('ok\n', encoding='utf-8')
    ok = await registry.execute('read_file', {'file_path': str(inner)}, None)
    assert ok.is_error is False and ok.content == '   1: ok'


def test_build_tools_requires_explicit_workspace():
    # 安全边界必须显式声明：不传 workspace 直接抛错（严格校验哲学）
    with pytest.raises(ValueError, match='workspace'):
        build_tools(None)


@pytest.mark.asyncio
async def test_edit_replaces_exact_string(tmp_path):
    registry = build_tools(workspace=tmp_path)
    path = tmp_path / 'code.py'
    path.write_text('def foo():\n    return 1\n', encoding='utf-8')

    out = await registry.execute(
        'edit',
        {'file_path': str(path), 'old_string': '    return 1', 'new_string': '    return 2'},
        None)
    assert out.is_error is False
    assert out.content == f'edited {path} (replaced at line 2)'
    assert path.read_text(encoding='utf-8') == 'def foo():\n    return 2\n'

    # new_string 缺省 = 删除片段（对齐 harness）
    out2 = await registry.execute(
        'edit', {'file_path': str(path), 'old_string': 'def foo():\n'}, None)
    assert out2.is_error is False
    assert path.read_text(encoding='utf-8') == '    return 2\n'


@pytest.mark.asyncio
async def test_edit_requires_unique_verbatim_match(tmp_path):
    registry = build_tools(workspace=tmp_path)
    path = tmp_path / 'code.py'
    path.write_text('x = 1\ny = x\nx = 2\n', encoding='utf-8')

    # 零匹配（字面量、空白敏感）：提示模型检查空白/缩进
    missing = await registry.execute(
        'edit', {'file_path': str(path), 'old_string': 'x=1'}, None)
    assert missing.is_error and 'did not appear verbatim' in missing.content

    # 多匹配：报所有出现行号，拒绝替换——改错位置比拒绝更危险（宁炸勿静默）
    ambiguous = await registry.execute(
        'edit', {'file_path': str(path), 'old_string': 'x = ', 'new_string': 'z = '}, None)
    assert ambiguous.is_error and 'appears 2 times' in ambiguous.content
    assert 'lines 1, 3' in ambiguous.content
    assert path.read_text(encoding='utf-8') == 'x = 1\ny = x\nx = 2\n'  # 文件未被改动

    # 空 old_string 拒绝（严格校验）
    empty = await registry.execute(
        'edit', {'file_path': str(path), 'old_string': '', 'new_string': 'z'}, None)
    assert empty.is_error and 'must not be empty' in empty.content

    # 文件不存在 / 越界路径（沙箱自动继承）
    missing_file = await registry.execute(
        'edit', {'file_path': str(tmp_path / 'nope.py'), 'old_string': 'x'}, None)
    assert missing_file.is_error and 'file not found' in missing_file.content
    outside = tmp_path.parent / 'outside.txt'
    outside.write_text('x\n', encoding='utf-8')
    denied = await registry.execute(
        'edit', {'file_path': str(outside), 'old_string': 'x'}, None)
    assert denied.is_error and 'outside workspace' in denied.content


@pytest.mark.asyncio
async def test_bash_runs_and_reports_exit_code(tmp_path):
    registry = build_tools(workspace=tmp_path)

    ok = await registry.execute('bash', {'command': 'echo hello'}, None)
    assert ok.is_error is False
    assert ok.content.strip() == 'hello'

    # 退出码非 0 → is_error + [exit code: N] 前缀（模型看到结果自己修）
    fail = await registry.execute('bash', {'command': 'exit 3'}, None)
    assert fail.is_error
    assert fail.content.startswith('[exit code: 3]')

    # 成功但无输出 → (no output)
    empty = await registry.execute('bash', {'command': 'cd .'}, None)
    assert empty.is_error is False and empty.content == '(no output)'

    # 空命令拒绝
    blank = await registry.execute('bash', {'command': '   '}, None)
    assert blank.is_error and 'must not be empty' in blank.content


@pytest.mark.asyncio
async def test_bash_cwd_and_truncation(tmp_path):
    registry = build_tools(workspace=tmp_path)
    sub = tmp_path / 'sub'
    sub.mkdir()
    (sub / 'probe.txt').write_text('x\n', encoding='utf-8')

    # cwd 生效：命令在指定目录下跑
    listed = await registry.execute(
        'bash', {'command': 'python -c "import os; print(sorted(os.listdir()))"', 'cwd': str(sub)}, None)
    assert listed.is_error is False and 'probe.txt' in listed.content

    # cwd 越界 → is_error（沙箱继承）
    denied = await registry.execute('bash', {'command': 'echo hi', 'cwd': str(tmp_path.parent)}, None)
    assert denied.is_error and 'outside workspace' in denied.content

    # 大输出截断 + 重定向导航提示
    big = await registry.execute('bash', {'command': 'python -c "print(\'x\' * 20000)"'}, None)
    assert big.is_error is False
    assert big.content.startswith('x' * 100)
    assert 'truncated at 8000 chars' in big.content
    assert 'redirect to a file' in big.content


@pytest.mark.asyncio
async def test_bash_timeout_kills_process(tmp_path):
    # 注入短超时：wait_for 取消 → executor kill 子进程 → TimeoutError → is_error
    registry = build_tools(workspace=tmp_path, bash_timeout_s=0.3)
    slow = await registry.execute('bash', {'command': 'python -c "import time; time.sleep(5)"'}, None)
    assert slow.is_error and 'timed out' in slow.content


@pytest.mark.asyncio
async def test_approval_gate_approves_and_skips(tmp_path):
    # 批准路径：确认钩子返回 True → 工具执行、文件写入、无 skipped
    decisions = []
    approve_hooks = Hooks()

    async def approve(name, args):
        decisions.append(name)
        return True

    approve_hooks.approval = approve
    agent = Agent(
        session=Session(id='s'),
        llm=FakeLlm(script=[
            {
                'tool_calls': [{'id': 'c1', 'name': 'write_file',
                                'arguments': json.dumps({'file_path': 'out.txt', 'content': 'hi'})}],
                'finish_reason': 'tool_calls',
            },
            {'text': 'done', 'finish_reason': 'stop'},
        ]),
        prompt=PromptRegistry(), tools=build_tools(workspace=tmp_path),
        options={'provider': 'fake', 'model': 'fake-model'}, hooks=approve_hooks,
    )
    agent.followup('write a file')
    await agent.when_idle()
    assert decisions == ['write_file']
    assert (tmp_path / 'out.txt').read_text(encoding='utf-8') == 'hi'
    assert not [e for e in agent.session.events if e.type == 'tool/skipped']

    # 拒绝路径：确认钩子返回 False → tool/skipped + is_error result + 文件未写入 + 循环正常收尾
    reject_hooks = Hooks()

    async def reject(name, args):
        return False

    reject_hooks.approval = reject
    agent2 = Agent(
        session=Session(id='s2'),
        llm=FakeLlm(script=[
            {
                'tool_calls': [{'id': 'c1', 'name': 'write_file',
                                'arguments': json.dumps({'file_path': 'evil.txt', 'content': 'evil'})}],
                'finish_reason': 'tool_calls',
            },
            {'text': 'ok, skipped', 'finish_reason': 'stop'},
        ]),
        prompt=PromptRegistry(), tools=build_tools(workspace=tmp_path),
        options={'provider': 'fake', 'model': 'fake-model'}, hooks=reject_hooks,
    )
    agent2.followup('write a file')
    await agent2.when_idle()
    events = agent2.session.events
    skipped = [e for e in events if e.type == 'tool/skipped']
    assert skipped and skipped[0].data['reason'] == 'not-approved'
    results = [e for e in events if e.type == 'tool/result']
    assert results and 'skipped' in str(results[0].data)
    assert not (tmp_path / 'evil.txt').exists()  # 拒绝后文件未写入
    assert events[-1].data['reason'] == 'completed'  # 循环正常完成，不是炸掉


def test_web_chat_streams_events(tmp_path):
    from fastapi.testclient import TestClient

    from agent_demo import web_app

    # SSE 流全链路（--fake 离线验证；sessions_dir 隔离，不污染真实会话）
    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web_app.app)

    assert client.get('/').status_code == 200          # 页面可访问
    h = client.get('/history').json()
    assert h['history'] == [] and h['todos'] == [] and 'context' in h  # 空会话

    resp = client.post('/chat', json={'message': 'hi'})
    assert resp.status_code == 200
    # SSE 帧：流式文本 + 工具调用 + 回合结束标记
    assert resp.text.startswith('data: ')
    assert '"type": "chunk"' in resp.text
    assert '"type": "tool_call"' in resp.text
    assert '"type": "turn_end"' in resp.text
    # 统一投影模型的协议：回合开始帧、真人发言帧（带 turn+message_id）、
    # 请求边界帧（issue #4 ③）、内容帧带 turn/step
    assert '"type": "turn_start"' in resp.text
    assert '"type": "user_message"' in resp.text
    assert '"type": "chunk", "turn": 1' in resp.text      # chunk 带 turn（前端按节点分块）
    assert '"type": "request_start", "turn": 1' in resp.text  # 请求边界（按请求分块）
    user_frame = [f for f in resp.text.split('data: ') if '"user_message"' in f]
    assert user_frame, 'user_message frame missing'
    frame = json.loads(user_frame[0].split('\n\n')[0])
    assert frame['text'] == 'hi'
    assert frame['turn'] == 1
    assert frame['message_id'], 'user_message frame must carry message_id (乐观气泡认领用)'

    # 对话后历史可查（记忆 = 日志投影，Web 视角同样成立）
    payload = client.get('/history').json()
    assert payload['todos'] == []
    history = payload['history']
    assert history[0]['role'] == 'user'
    assert any(m['role'] == 'assistant' and m['text'] for m in history)


def test_history_projects_reasoning_per_request(tmp_path):
    """issue #4：/history 把思维链（痕迹）投影到对应 assistant 消息上。

    UI 是日志的投影——思维链虽不回灌模型，但历史/刷新后深度思考块必须能
    重建。配对规则：assistant/reasoning 紧跟在它的 assistant/message 之前，
    按 seq 顺序 buffer 配对；同一步工具循环的多次请求各配一份（不合并）。
    """
    from fastapi.testclient import TestClient

    from agent_demo import web_app

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web_app.app)
    session = web_app._session

    # 手工落一轮：两次请求（同 turn/step，模拟工具循环），各带思维链
    session.append('turn/start', {'turn': 1})
    session.append('user/message', create_user_message([TextBlock(text='调查一下')]),
                   surface_op='append')
    session.append('step/start', {'turn': 1, 'step': 1})
    # 第一次请求：reasoning + assistant/message（带 tool_call）
    session.append('assistant/reasoning', {'turn': 1, 'step': 1, 'reasoning': '先读文件'})
    session.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message(
            [ToolCallBlock(id='c1', name='read_file', arguments='{}')],
            provider='fake', model='m'),
    }, surface_op='append')
    session.append('tool/call', {'turn': 1, 'step': 1, 'call_id': 'c1',
                                 'name': 'read_file', 'arguments': '{}'})
    session.append('tool/result', create_tool_result_message('c1', '内容', False), surface_op='append')
    # 第二次请求：另一份 reasoning + 纯文本回答
    session.append('assistant/reasoning', {'turn': 1, 'step': 1, 'reasoning': '看完了，可以总结'})
    session.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message([TextBlock(text='总结如下')],
                                            provider='fake', model='m'),
    }, surface_op='append')
    session.append('step/end', {'turn': 1, 'step': 1})
    session.append('turn/end', {'turn': 1, 'reason': 'completed'})

    history = client.get('/history').json()['history']
    assistants = [m for m in history if m['role'] == 'assistant']
    assert len(assistants) == 2
    # 每条 assistant 各自带自己那次的思维链（同一步两次请求不合并）
    assert assistants[0]['reasoning'] == '先读文件'
    assert assistants[1]['reasoning'] == '看完了，可以总结'
    # 无思维链的消息不出现该字段
    user_msgs = [m for m in history if m['role'] == 'user']
    assert all('reasoning' not in m for m in user_msgs)


def test_web_session_management(tmp_path):
    from fastapi.testclient import TestClient

    from agent_demo import web_app

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web_app.app)

    # 隔离目录初始化即含空的 web 会话（"会话存在 = 有文件"）；
    # 聊天后摘要更新为首条用户消息
    initial = client.get('/sessions').json()
    assert [s['id'] for s in initial] == ['web']
    assert initial[0]['summary'] == '(empty)'
    client.post('/chat', json={'message': 'hello web'})
    items = client.get('/sessions').json()
    assert items[0]['summary'].startswith('hello web')
    assert items[0]['events'] > 0

    # 新建会话并切换（空历史）
    fresh = client.post('/sessions/new').json()
    assert fresh['id'] != 'web' and fresh['history'] == []
    h = client.get('/history').json()
    assert h['history'] == [] and h['todos'] == [] and 'context' in h

    # 切回 web 会话：历史还原（恢复 = 重放）
    back = client.post(f"/sessions/{fresh['id']}/switch").json()
    assert back['id'] == fresh['id']
    switched = client.post('/sessions/web/switch').json()
    assert switched['id'] == 'web'
    assert switched['history'][0]['role'] == 'user'
    assert switched['history'][0]['text'] == 'hello web'

    # 非法/不存在会话 id：拒绝而不是穿路径
    assert client.post('/sessions/%2e%2e%2fswitch').status_code in (400, 404)
    assert client.post('/sessions/no-such-session/switch').status_code == 404

    # 删除：非当前会话可删；当前会话拒绝（先切换走再删）
    fresh2 = client.post('/sessions/new').json()
    assert fresh2['id'] != 'web'
    client.post('/sessions/web/switch')   # new 已切到 fresh2，先切回 web
    assert client.post(f"/sessions/{fresh2['id']}/delete").status_code == 200
    assert all(s['id'] != fresh2['id'] for s in client.get('/sessions').json())
    assert client.post('/sessions/web/delete').status_code == 400  # 当前会话
    assert client.post('/sessions/%2e%2e%2fdelete').status_code in (400, 404)

def test_web_session_title_endpoint(tmp_path):
    """手动改名：append session/title（user）→ 列表 summary 以标题优先，重放可恢复。"""
    from fastapi.testclient import TestClient

    from agent_demo import web_app

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web_app.app)

    # 聊天产生内容 → fallback 摘要为首条消息
    client.post('/chat', json={'message': 'hello rename me'})
    items = client.get('/sessions').json()
    assert items[0]['summary'].startswith('hello rename me')
    assert items[0]['title'] is None

    # 改名（非当前会话也可以：先新建一个并切走，再改回 web 的名字）
    fresh = client.post('/sessions/new').json()
    resp = client.post(f"/sessions/{fresh['id']}/title", json={'title': '我的自定义名字'})
    assert resp.status_code == 200
    assert resp.json()['source'] == 'user'
    by_id = {s['id']: s for s in client.get('/sessions').json()}
    assert by_id[fresh['id']]['summary'] == '我的自定义名字'
    assert by_id[fresh['id']]['title_source'] == 'user'

    # 标题 = 日志投影：切回该会话后，日志里存在 session/title 事件（可重放）
    switched = client.post(f"/sessions/{fresh['id']}/switch").json()
    assert switched['id'] == fresh['id']
    log = (tmp_path / 'sess' / f"{fresh['id']}.jsonl").read_text(encoding='utf-8')
    assert '"type": "session/title"' in log
    assert '"source": "user"' in log

    # 空标题拒绝
    assert client.post(f"/sessions/{fresh['id']}/title", json={'title': '   '}).status_code == 400


def test_auto_title_trigger_conditions(tmp_path):
    """自动起名只在 真模型 + 无标题 + 首条消息 时触发（fake 一律跳过）。"""
    from agent_demo import web_app
    from agent_demo.session import Session

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    s = Session(id='x')
    # fake 模式：不自动起名（没有真模型可调）
    assert web_app._should_auto_title(s) is False

    # 已有标题：不重复起名
    from argparse import Namespace
    web_app._args = Namespace(
        fake=False, model='m', workspace=tmp_path, hide_reasoning=False,
        session='x', sessions=str(tmp_path / 'sess'), prompt='', resume=False, verbose=False,
    )
    s.append('session/title', {'title': 't', 'source': 'user'})
    assert web_app._should_auto_title(s) is False

    # 已有用户消息（resume 继续对话）：不再自动起名（标题应基于第一条）
    s2 = Session(id='x')
    s2.append('user/message', create_user_message([TextBlock(text='hi')]), surface_op='append')
    assert web_app._should_auto_title(s2) is False

    # 干净会话 + 真模型：触发
    s3 = Session(id='x')
    assert web_app._should_auto_title(s3) is True


def test_session_title_event_is_trace_not_surface(tmp_path):
    """session/title 是痕迹事件：不进模型记忆（derive_messages），但重放保留。"""
    from agent_demo import web_app
    from agent_demo.persistence import save_event
    from agent_demo.session import Session

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    s = Session(id='t')
    s.append('session/title', {'title': '我的标题', 'source': 'user'})
    assert [e.type for e in s.events] == ['session/title']
    assert s.derive_messages() == []  # 不污染模型可见消息

    # 持久化 → 新会话重放（adopt）仍能看到标题事件
    path = tmp_path / 'sess' / 't.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    for e in s.events:
        save_event(path, e)
    restored = Session(id='t')
    for e in load_events(path):
        restored.adopt(e)
    titles = [e for e in restored.events if e.type == 'session/title']
    assert titles and titles[-1].data['title'] == '我的标题'

def test_auto_title_rejects_verbatim_copy():
    """自动起名逐字复读首条消息 → 判定为失败（不落 auto 事件，退回 fallback）。"""
    from agent_demo import web_app
    assert web_app._is_verbatim_copy('你好', '你好') is True
    assert web_app._is_verbatim_copy('总结README', '请帮我总结README') is True   # 子串
    assert web_app._is_verbatim_copy('代码审查', '请帮我审查这段代码') is False  # 概括 ≠ 复读
    assert web_app._is_verbatim_copy('问候', '你好') is False

@pytest.mark.asyncio
async def test_identity_prompt_is_neutral(tmp_path):
    """agent 身份 = 中性编码助手：request/header 的 system 里不含品牌/血缘词。

    之前 identity 写的是 'powered by DeepSeek Harness (Python demo)'，模型
    会照抄自我介绍——作品集项目不该把功劳归给被复刻对象，锁住文案防回归。
    """
    from argparse import Namespace

    from agent_demo import factory
    from agent_demo.session import Session

    args = Namespace(fake=True, model='fake-model', workspace=tmp_path, hide_reasoning=False,
                     session='id', sessions=str(tmp_path), prompt='x', resume=False, verbose=False)
    session = Session(id='id')
    agent = factory.build_agent(session, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
    agent.followup('hi')
    await agent.when_idle()
    headers = [e.data for e in session.events if e.type == 'request/header']
    assert headers, 'expected a request/header event'
    system = headers[0]['system']
    assert 'coding agent' in system
    # {{model}} 变量已渲染成实际模型名，且身份即模型名（不许自称别的模型）
    assert 'fake-model' in system
    for banned in ('DeepSeek Harness', 'powered by', 'Python demo',
                   'Claude', 'Anthropic', 'GPT', 'OpenAI'):
        assert banned not in system, f'identity must not mention {banned!r}'

def test_todo_write_folds_and_injects_into_prompt(tmp_path):
    """todo_write 全链路：写整表 → 折叠读回 → 作为 live 段注入下次请求的 system。"""
    import asyncio

    from agent_demo.prompt import PromptRegistry
    from agent_demo.registry import ToolSpec
    from agent_demo.session import Session
    from agent_demo.tools import build_tools
    from agent_demo.tools.todo import fold_todos

    session = Session(id='todo-test')
    registry = build_tools(tmp_path)
    spec: ToolSpec = registry._tools['todo_write']

    class FakeAgent:  # todo_write executor 需要 agent.session 落日志
        def __init__(self, s): self.session = s
    agent = FakeAgent(session)

    # 1) 规划 3 步：一步 in_progress
    out = asyncio.run(spec.execute({'todos': [
        {'content': '读代码', 'status': 'in_progress'},
        {'content': '写修复', 'status': 'pending'},
        {'content': '跑测试', 'status': 'pending'},
    ]}, agent, None))
    assert out.is_error is False and '2 pending, 1 in progress' in out.content

    # 2) 折叠读回 = 最后一次 todo/write 快照
    folded = fold_todos(session)
    assert [t['content'] for t in folded] == ['读代码', '写修复', '跑测试']
    assert folded[0]['status'] == 'in_progress'

    # 3) 事件是痕迹（非 surface）：不进模型记忆，但可重放
    assert [e.type for e in session.events] == ['todo/write']
    assert session.derive_messages() == []

    # 4) prompt live 段注入：assemble 一次，两次 render 拿到两次新鲜清单
    prompt = PromptRegistry()
    prompt.section('todo:state', 0, lambda ctx: _fmt_todos(ctx['agent']))
    prompt.section('identity', -10, 'static preamble')
    prompt.variable('x', lambda ctx: 'v')
    from agent_demo.tools.todo import fold_todos as _fold
    def _fmt_todos(agent):
        todos = _fold(agent.session)
        if not todos:
            return ''
        return 'TODOS: ' + '; '.join(f"{t['content']}={t['status']}" for t in todos)
    assembly = prompt.assemble({'agent': type('A', (), {'session': session})()})
    assert 'TODOS: 读代码=in_progress; 写修复=pending; 跑测试=pending' in prompt.render(assembly, {'agent': type('A', (), {'session': session})()})

    # 5) 更新清单（完成一项）→ 下次 render 看到新状态（live 段不锁死在快照）
    asyncio.run(spec.execute({'todos': [
        {'content': '读代码', 'status': 'completed'},
        {'content': '写修复', 'status': 'in_progress'},
    ]}, agent, None))
    rendered = prompt.render(assembly, {'agent': type('A', (), {'session': session})()})
    assert '读代码=completed' in rendered and '写修复=in_progress' in rendered
    assert '跑测试' not in rendered


def test_todo_write_rejects_bad_inputs(tmp_path):
    """todo 校验降级为 is_error（不炸循环）：空 content / 重复 / 多 in_progress。"""
    import asyncio

    from agent_demo.tools import build_tools

    registry = build_tools(tmp_path)
    spec = registry._tools['todo_write']
    cases = [
        [{'content': '  ', 'status': 'pending'}],                 # 空 content
        [{'content': 'a', 'status': 'pending'}, {'content': 'a', 'status': 'completed'}],  # 重复
        [{'content': 'a', 'status': 'in_progress'}, {'content': 'b', 'status': 'in_progress'}],  # 双活动
    ]
    for todos in cases:
        out = asyncio.run(spec.execute({'todos': todos}, None, None))
        assert out.is_error is True, f'should reject {todos}'


@pytest.mark.asyncio
async def test_todo_status_bar_in_messages(tmp_path):
    """方案 A：todo 状态栏从 system 迁到 messages 末尾（合成 user 消息）。

    FakeLlm 两步：第一步 todo_write 规划 2 项，第二步纯文本。断言：
    - 第一步请求（规划前）：无状态栏（fold 无清单）
    - 第二步请求（规划后）：request/header 记了 todo_status（审计字段），
      且 system **不含** todo 清单（system 全静态）
    - 状态栏 XML 含两项与状态
    """
    from argparse import Namespace

    from agent_demo.factory import build_agent
    from agent_demo.llm import FakeLlm
    from agent_demo.session import Session

    session = Session(id='todo-status')
    args = Namespace(fake=True, model='fake-model', workspace=tmp_path, hide_reasoning=False,
                     session='id', sessions=str(tmp_path), prompt='x', resume=False, verbose=False)
    llm = FakeLlm(script=[
        {
            'tool_calls': [{'id': 't1', 'name': 'todo_write', 'arguments': json.dumps({
                'todos': [
                    {'content': 'step one', 'status': 'in_progress'},
                    {'content': 'step two', 'status': 'pending'},
                ]})}],
            'finish_reason': 'tool_calls',
        },
        {'text': 'planned done', 'finish_reason': 'stop'},
    ])
    agent = build_agent(session, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
    agent.llm = llm
    agent.followup('do the multi-step work')
    await agent.when_idle()

    headers = [e.data for e in session.events if e.type == 'request/header']
    assert len(headers) == 2, f'expected 2 model requests, got {len(headers)}'

    # 规划前：无状态栏
    assert 'todo_status' not in headers[0]
    # system 里不再有 todo 清单（方案 A：system 全静态）
    assert 'step one' not in headers[0]['system']
    assert 'todo:state' not in headers[0]['system']

    # 规划后：audit 字段带 XML 状态栏；system 仍不含清单
    status = headers[1].get('todo_status')
    assert status is not None, 'second request must carry todo_status audit field'
    assert status.startswith('<todo_status>') and status.endswith('</todo_status>')
    assert '[in_progress] step one' in status
    assert '[pending] step two' in status
    assert 'step one' not in headers[1]['system']      # system 保持静态

    # 每轮都叠（第二轮也带了）；且 derive_messages 里没有状态栏（历史零污染）
    derived_texts = []
    for message in session.derive_messages():
        for block in message.content:
            if getattr(block, 'type', '') == 'text':
                derived_texts.append(block.text)
    assert not any('todo_status' in text for text in derived_texts)


def test_todo_status_bar_absent_cases(tmp_path):
    """build_todo_status 的不叠条件：无清单 / 全 completed → None。"""
    from agent_demo.session import Session
    from agent_demo.tools.todo import build_todo_status

    session = Session(id='status-absent')
    assert build_todo_status(session) is None          # 从未写过

    session.append('todo/write', {'todos': [
        {'content': 'a', 'status': 'pending'},
        {'content': 'b', 'status': 'in_progress'},
    ]})
    status = build_todo_status(session)
    assert status is not None
    assert '<todo_status>' in status and '1. [pending] a' in status

    # 全部 completed → 收尾，状态栏关闭
    session.append('todo/write', {'todos': [
        {'content': 'a', 'status': 'completed'},
        {'content': 'b', 'status': 'completed'},
    ]})
    assert build_todo_status(session) is None

    # 空清单 → 也不叠
    session.append('todo/write', {'todos': []})
    assert build_todo_status(session) is None


def test_skill_catalog_scan_and_format(tmp_path, capsys):
    """技能目录扫描/格式化（AGENTS.md 约定：目录只放 name+description+路径）。

    - scan_skills：解析 skills/*.md 的 frontmatter；坏技能跳过并打诊断
    - format_catalog：纯文本目录行，路径相对 workspace 且正斜杠
    - 正文绝不进目录（正文由模型 read_file 按需读，见下一测试）
    """
    from agent_demo.skills import format_catalog, scan_skills

    skills_dir = tmp_path / 'skills'
    skills_dir.mkdir()
    (skills_dir / 'gh-issue.md').write_text(
        '---\nname: gh-issue\ndescription: 处理 GitHub issue：读全文、独立验证\n'
        '---\n# 正文\n这是一段不该出现在目录的技能正文。\n',
        encoding='utf-8',
    )
    # 描述含成对引号 → 应剥离引号保留内容
    (skills_dir / 'quoted.md').write_text(
        '---\nname: quoted\ndescription: "任务匹配时使用，含:冒号"\n---\n正文\n',
        encoding='utf-8',
    )
    # 坏技能（无 description）：应被 scan 跳过 + stderr 诊断，不进目录
    (skills_dir / 'broken.md').write_text(
        '---\nname: broken\n---\n没有 description。\n', encoding='utf-8')
    # 非法技能名（文件名中文且无 name）：跳过 + 诊断
    (skills_dir / '处理问题.md').write_text(
        '---\ndescription: 中文文件名技能\n---\n正文\n', encoding='utf-8')
    # 非技能 md（无 frontmatter）：静默跳过（不算坏，不打诊断）
    (skills_dir / 'README.md').write_text('目录说明，不是技能。\n', encoding='utf-8')

    skills = scan_skills(skills_dir)
    names = [s.name for s in skills]
    assert names == ['gh-issue', 'quoted'], names
    # 引号剥离 + 值内冒号保留
    quoted = next(s for s in skills if s.name == 'quoted')
    assert quoted.description == '任务匹配时使用，含:冒号'
    gh = next(s for s in skills if s.name == 'gh-issue')
    assert gh.description == '处理 GitHub issue：读全文、独立验证'

    # 诊断：broken 与中文名各一条 [skill] skipped（README 静默）
    err = capsys.readouterr().err
    assert '[skill] skipped broken.md' in err
    assert '[skill] skipped 处理问题.md' in err
    assert 'README' not in err

    catalog = format_catalog(skills, tmp_path)
    assert 'gh-issue' in catalog
    assert 'skills/gh-issue.md' in catalog          # 相对 workspace 路径（正斜杠）
    assert '处理 GitHub issue' in catalog
    assert '不该出现在目录' not in catalog           # 正文不进目录
    assert 'broken' not in catalog                    # 坏技能被跳过
    assert 'README' not in catalog

    # 无技能目录/空目录 → 空目录文本（render 自动省略，零 token）
    assert format_catalog([], tmp_path) == ''
    assert scan_skills(tmp_path / 'no-such-dir') == []


@pytest.mark.asyncio
async def test_skill_catalog_injected_into_system(tmp_path):
    """技能目录静态注入 system（端到端：build_agent → 一步请求）。

    - request/header 的 system 含目录（name/description/路径），随 system 落
      日志可重建
    - 正文不进 system——正文只在模型 read_file 后作为 tool/result 进上下文
    - 目录在 todo:state 等动态段之前（order 95 < 100，缓存稳定前缀）
    """
    from argparse import Namespace

    from agent_demo.factory import build_agent
    from agent_demo.llm import FakeLlm
    from agent_demo.session import Session

    skills_dir = tmp_path / 'skills'
    skills_dir.mkdir()
    (skills_dir / 'gh-issue.md').write_text(
        '---\nname: gh-issue\ndescription: 处理 GitHub issue（读全文、独立验证）\n'
        '---\n# 正文\n秘密技能正文，绝不该进 system。\n',
        encoding='utf-8',
    )

    session = Session(id='skill-cat')
    args = Namespace(fake=True, model='fake-model', workspace=tmp_path,
                     hide_reasoning=False, session='id', sessions=str(tmp_path),
                     prompt='x', resume=False, verbose=False)
    llm = FakeLlm(script=[{'text': 'done', 'finish_reason': 'stop'}])
    agent = build_agent(session, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
    agent.llm = llm
    agent.followup('hi')
    await agent.when_idle()

    header = session.request_header()
    assert header is not None and 'system' in header
    system = header['system']
    assert '可用技能' in system
    assert 'gh-issue' in system
    assert 'skills/gh-issue.md' in system
    assert '秘密技能正文' not in system              # 正文始终不进 system

    # 目录段在工具提示段之前（todo 为空时无 todo 段，取 bash 提示为序界）
    assert system.index('可用技能') < system.index('Use bash to verify')


def test_web_skill_catalog_survives_reload(tmp_path):
    """技能目录可重建：新会话（reload）重新扫描，目录照旧注入。

    目录是磁盘文件的静态投影——会话重建不丢（与 todo 的 fold 同理，
    都是"从持久状态折叠"，只是来源是文件而非日志）。
    """
    from argparse import Namespace

    from agent_demo.factory import build_agent
    from agent_demo.session import Session

    skills_dir = tmp_path / 'skills'
    skills_dir.mkdir()
    (skills_dir / 'gh-issue.md').write_text(
        '---\nname: gh-issue\ndescription: 处理 GitHub issue\n'
        '---\n正文\n', encoding='utf-8')

    def system_of():
        args = Namespace(fake=True, model='fake-model', workspace=tmp_path,
                         hide_reasoning=False, session='id', sessions=str(tmp_path),
                         prompt='x', resume=False, verbose=False)
        s = Session(id='reload')
        a = build_agent(s, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
        return a.prompt.render(a.prompt.assemble(ctx={'agent': a}), ctx={'agent': a})

    first = system_of()
    assert 'gh-issue' in first
    # 第二个会话重建 → 目录仍在（来源是磁盘文件，与日志无关）
    assert system_of() == first


def test_web_todo_dock_payloads(tmp_path):
    """todo dock 的数据通道：SSE 帧 todo_update + /history 附带 todos + 会话切换恢复。"""
    from fastapi.testclient import TestClient

    from agent_demo import web_app

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web_app.app)

    # fake 会话默认脚本不含 todo_write；先直接往日志写 todo，模拟已有清单
    s = web_app._session
    s.append('todo/write', {'todos': [
        {'content': '读代码', 'status': 'completed'},
        {'content': '写修复', 'status': 'in_progress'},
        {'content': '跑测试', 'status': 'pending'},
    ]})

    # /history 附带当前 todos 投影
    payload = client.get('/history').json()
    assert [t['content'] for t in payload['todos']] == ['读代码', '写修复', '跑测试']
    assert payload['todos'][1]['status'] == 'in_progress'

    # 会话切换也带 todos（dock 在换会话时恢复）
    fresh = client.post('/sessions/new').json()
    assert fresh['todos'] == []
    switched = client.post('/sessions/web/switch').json()
    assert [t['content'] for t in switched['todos']] == ['读代码', '写修复', '跑测试']


def test_todo_fold_persists_across_turns(tmp_path):
    """todo = 跨回合任务清单（2026-09 语义变更）：turn/start 不再清空。

    一旦 todo_write 建立清单就持续（后续回合继续更新同一份），直到模型
    把全部项标 completed（前端据此短暂展示后自动隐藏 dock）。
    """
    from agent_demo.session import Session
    from agent_demo.tools.todo import all_completed, fold_todos

    s = Session(id='t')

    # 回合 1：写清单
    s.append('turn/start', {'turn': 1})
    s.append('todo/write', {'todos': [
        {'content': 'a', 'status': 'completed'},
        {'content': 'b', 'status': 'pending'},
    ]})
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    folded = fold_todos(s)
    assert [t['content'] for t in folded] == ['a', 'b']
    assert all_completed(folded) is False

    # 回合 2 开始：**不再清空**——清单跨回合持续（b 还在 pending）
    s.append('turn/start', {'turn': 2})
    assert [t['content'] for t in fold_todos(s)] == ['a', 'b']

    # 回合 2 里继续更新同一份清单
    s.append('todo/write', {'todos': [
        {'content': 'a', 'status': 'completed'},
        {'content': 'b', 'status': 'completed'},
    ]})
    all_done = fold_todos(s)
    assert [t['content'] for t in all_done] == ['a', 'b']
    assert all_completed(all_done) is True      # 全 completed → 前端收尾隐藏

    # 回合 3：全 completed 清单仍折叠出来（dock 由前端判定全勾后隐藏，
    # 折叠本身保留最后快照——resume 重放一致）
    s.append('turn/start', {'turn': 3})
    assert all_completed(fold_todos(s)) is True

    # resume 重放语义一致：adopt 同样的序列得到同样的折叠
    from agent_demo.persistence import save_event
    from agent_demo.session import Session as S2
    path = tmp_path / 'turn.jsonl'
    for e in s.events:
        save_event(path, e)
    restored = S2(id='t')
    from agent_demo.persistence import load_events
    for e in load_events(path):
        restored.adopt(e)
    assert all_completed(fold_todos(restored)) is True


def test_surface_replace_shadows_and_derives_in_place():
    """surface replace：遮蔽旧区间 + checkpoint 原位顶替 + 日志完整 + 重放一致。"""
    from agent_demo.session import Session
    from agent_demo.values import TextBlock, create_assistant_message, create_user_message

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
    from agent_demo.persistence import load_events, save_event
    from agent_demo.session import Session
    from agent_demo.values import TextBlock, create_user_message

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
    from agent_demo.session import Session
    from agent_demo.values import TextBlock, create_assistant_message, create_user_message

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
    from agent_demo.persistence import load_events, save_event
    from agent_demo.session import Session
    from agent_demo.values import TextBlock, create_assistant_message, create_user_message

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


@pytest.mark.asyncio
async def test_compaction_selection_and_file_ops():
    """选区：保留最近 N 回合；文件提取：read/write/edit 归类。"""
    from agent_demo.compaction import extract_file_ops, select_compact_range
    from agent_demo.session import Session
    from agent_demo.values import TextBlock, ToolCallBlock, create_assistant_message, create_user_message

    def user(text):
        return create_user_message([TextBlock(text=text)])

    def asst(text='', calls=()):
        return {'message': create_assistant_message([TextBlock(text=text)] + list(calls))}

    s = Session(id='c')
    s.append('turn/start', {'turn': 1})
    s.append('user/message', user('Q1'), surface_op='append')
    s.append('assistant/message', asst('A1'), surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    s.append('turn/start', {'turn': 2})
    s.append('user/message', user('Q2'), surface_op='append')
    s.append('assistant/message', asst('', [ToolCallBlock(id='c1', name='read_file', arguments='{"file_path": "a.py"}')]),
             surface_op='append')
    s.append('tool/call', {'call_id': 'c1', 'name': 'read_file', 'arguments': '{"file_path": "a.py"}'})
    s.append('assistant/message', asst('', [ToolCallBlock(id='c2', name='edit', arguments='{"file_path": "b.py"}')]),
             surface_op='append')
    s.append('tool/call', {'call_id': 'c2', 'name': 'edit', 'arguments': '{"file_path": "b.py"}'})
    s.append('tool/result', __import__('agent_demo.values', fromlist=['create_tool_result_message']).create_tool_result_message('c1', 'ok', False), surface_op='append')
    s.append('turn/end', {'turn': 2, 'reason': 'completed'})
    s.append('turn/start', {'turn': 3})
    s.append('user/message', user('Q3'), surface_op='append')

    # keep=2 → 压掉 turn1（Q1/A1 在最前）
    rng = select_compact_range(s, keep_turns=2)
    assert rng is not None
    start, end = rng
    assert (start, end) == (s.surface[0], s.surface[1])  # Q1, A1
    texts = []
    for m in s.derive_messages():
        for b in m.content:
            if getattr(b, 'type', '') == 'text':
                texts.append(b.text)
    assert texts[:2] == ['Q1', 'A1']

    # keep=3 → 无可压
    assert select_compact_range(s, keep_turns=3) is None

    # 文件提取：turn2 区间（Q2..tool result）含 read a.py + edit b.py
    rng2 = select_compact_range(s, keep_turns=1)
    assert rng2 is not None
    ops = extract_file_ops(s, *rng2)
    assert ops.read == frozenset({'a.py'})
    assert ops.edited == frozenset({'b.py'})
    assert ops.written == frozenset()


@pytest.mark.asyncio
async def test_compaction_transaction_fake_llm(tmp_path):
    """整链路：start → summary → replace(checkpoint) → end，摘要替换旧回合。"""
    from agent_demo.compaction import run_compaction
    from agent_demo.session import Session
    from agent_demo.values import TextBlock, create_assistant_message, create_user_message

    class FakeCompactorLlm:
        def __init__(self, summary):
            self._summary = summary
            self.seen_requests: list = []

        async def stream(self, request, signal=None):
            from agent_demo.llm import StreamChunk
            self.seen_requests.append(request)
            yield StreamChunk(text=self._summary, finish_reason='stop')

    def user(text):
        return create_user_message([TextBlock(text=text)])

    def asst(text):
        return {'message': create_assistant_message([TextBlock(text=text)])}

    s = Session(id='tc')
    s.append('turn/start', {'turn': 1})
    s.append('user/message', user('请做一个非常非常长的任务说明，内容足以超过摘要长度以便触发压缩检查'), surface_op='append')
    s.append('assistant/message', asst('完成了第一步的详细描述，这里还有很多后续内容要继续展开说明以便填充足够长度'), surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    s.append('turn/start', {'turn': 2})
    s.append('user/message', user('继续'), surface_op='append')

    llm = FakeCompactorLlm('[checkpoint] 早期任务的简短摘要')
    ok = await run_compaction(s, llm, keep_turns=1)
    assert ok is True

    # 四事件齐全 + checkpoint replace
    types = [e.type for e in s.events]
    assert types.count('compaction/start') == 1
    assert types.count('compaction/summary') == 1
    assert types.count('compaction/end') == 1
    replace_events = [e for e in s.events if e.surface_op == 'replace']
    assert len(replace_events) == 1
    cp = replace_events[0]
    # 遮蔽的是原始 surface 里 turn1 的两条消息（Q1/A1，seq 1 和 2）
    assert cp.shadowed == (1, 2)

    # derive 只剩 checkpoint + turn2 的 Q2
    texts = [m.content[0].text for m in s.derive_messages()]
    assert 'automatically generated checkpoint' in texts[0]      # preamble
    assert '<compacted-summary>' in texts[0]                      # 标签包裹
    assert '[checkpoint] 早期任务的简短摘要' in texts[0]           # 模型摘要本体
    assert texts[-1] == '继续'

    # summary 审计事件存模型产出本体（不含 preamble/标签包装）
    summary_evt = [e for e in s.events if e.type == 'compaction/summary'][0]
    assert summary_evt.data['summary'].startswith('[checkpoint]')

    # summary 事件含审计字段
    summary_evt = [e for e in s.events if e.type == 'compaction/summary'][0]
    assert 'range' in summary_evt.data
    assert 'file_ops' in summary_evt.data

    # 摘要请求保留 thinking（默认开，v4）——压缩要提炼取舍，思维链有助质量；
    # max_tokens 给足（8k），让"思考 + 正文"都放得下（曾设 600 被思维链吃光 →
    # content 空 → empty summary：真 bug，曾让手动压缩多次失败）
    req = llm.seen_requests[0]
    assert req.thinking is not False
    assert req.max_tokens is not None and req.max_tokens >= 8192


@pytest.mark.asyncio
async def test_compaction_failure_degrades():
    """失败降级：空摘要 / 摘要不够小 → 落 end{error}，日志完好、无 replace。"""
    from agent_demo.compaction import run_compaction
    from agent_demo.session import Session
    from agent_demo.values import TextBlock, create_assistant_message, create_user_message

    def user(text):
        return create_user_message([TextBlock(text=text)])

    def asst(text):
        return {'message': create_assistant_message([TextBlock(text=text)])}

    def fresh():
        s = Session(id='f')
        s.append('turn/start', {'turn': 1})
        s.append('user/message', user('很长的原始内容' * 30), surface_op='append')
        s.append('assistant/message', asst('回复内容' * 30), surface_op='append')
        s.append('turn/end', {'turn': 1, 'reason': 'completed'})
        s.append('turn/start', {'turn': 2})
        s.append('user/message', user('继续'), surface_op='append')
        return s

    class StubLlm:
        def __init__(self, text): self._text = text
        async def stream(self, request, signal=None):
            from agent_demo.llm import StreamChunk
            yield StreamChunk(text=self._text, finish_reason='stop')

    # 空摘要
    s = fresh()
    assert await run_compaction(s, StubLlm(''), keep_turns=1) is False
    ends = [e for e in s.events if e.type == 'compaction/end']
    assert ends and 'error' in ends[-1].data
    assert not any(e.surface_op == 'replace' for e in s.events)  # 没提交
    assert [m.content[0].text for m in s.derive_messages()][0].startswith('很长的原始内容')  # 原样

    # 摘要不小于原文（模型偷懒抄回去）→ 拒绝提交
    s2 = fresh()
    assert await run_compaction(s2, StubLlm('模型偷懒复读的摘要内容' * 100), keep_turns=1) is False
    assert not any(e.surface_op == 'replace' for e in s2.events)


@pytest.mark.asyncio
async def test_auto_compaction_fires_on_threshold(tmp_path):
    """自动压缩：turn/end 后上下文超阈值 → 触发压缩；低于阈值不触发。"""
    from agent_demo.compaction import wire_auto_compaction
    from agent_demo.session import Session
    from agent_demo.values import TextBlock, create_assistant_message, create_user_message

    def user(text):
        return create_user_message([TextBlock(text=text)])

    def asst(text):
        return {'message': create_assistant_message([TextBlock(text=text)])}

    class StubLlm:
        async def stream(self, request, signal=None):
            from agent_demo.llm import StreamChunk
            yield StreamChunk(text='## 主要请求\n- 总结\n## 下一步\n1. 无', finish_reason='stop')

    def build():
        s = Session(id='a')
        long_txt = '任务：全面重构工具并补充边界测试覆盖空文件越界非法输入等场景。' * 20
        s.append('turn/start', {'turn': 1})
        s.append('user/message', user(long_txt), surface_op='append')
        s.append('assistant/message', asst('收到，先读实现再分步重构。' * 8), surface_op='append')
        s.append('turn/end', {'turn': 1, 'reason': 'completed'})
        s.append('turn/start', {'turn': 2})
        s.append('user/message', user('继续'), surface_op='append')
        return s

    # 超阈值（估算 ~几百）→ turn2 结束触发压缩
    s = build()
    agent = type('A', (), {'session': s, 'llm': StubLlm(), 'options': {'model': 'm'}})()
    unsub = wire_auto_compaction(agent, max_tokens=100, keep_turns=1)
    s.append('assistant/message', asst('继续推进。' * 3), surface_op='append')
    s.append('turn/end', {'turn': 2, 'reason': 'completed'})
    await asyncio.sleep(0.3)
    unsub()
    assert any(e.type == 'compaction/summary' for e in s.events)
    assert any(e.type == 'compaction/end' for e in s.events)
    assert any(e.type == 'turn/end' for e in s.events)

    # 低于阈值 → 不触发
    s2 = Session(id='a2')
    s2.append('turn/start', {'turn': 1})
    s2.append('user/message', user('hi'), surface_op='append')
    s2.append('assistant/message', asst('hello'), surface_op='append')
    agent2 = type('A', (), {'session': s2, 'llm': StubLlm(), 'options': {'model': 'm'}})()
    unsub2 = wire_auto_compaction(agent2, max_tokens=100000, keep_turns=1)
    s2.append('turn/end', {'turn': 1, 'reason': 'completed'})
    await asyncio.sleep(0.2)
    unsub2()
    assert not any(e.type == 'compaction/start' for e in s2.events)


def test_build_agent_wires_auto_compaction_by_default(tmp_path):
    """真实模式默认挂阈值压缩（0.5M）+ 溢出恢复；fake 模式不挂；0 关闭阈值压缩。"""
    import os
    from argparse import Namespace

    from agent_demo.constants import DEFAULT_COMPACT_TOKENS
    from agent_demo.factory import build_agent
    from agent_demo.session import Session
    os.environ['DEEPSEEK_API_KEY'] = 'sk-placeholder'  # build_agent 只构造 llm 不连接
    import agent_demo.compaction as comp  # factory 函数体内 import 会实时取这里，mock 生效
    wired = []
    orig_wire = comp.wire_auto_compaction
    orig_overflow = comp.wire_overflow_recovery
    # 观察接线调用（不真正挂，避免副作用）
    comp.wire_auto_compaction = lambda agent, **kw: wired.append(('auto', kw)) or object()
    comp.wire_overflow_recovery = lambda agent, **kw: wired.append(('overflow', kw))
    try:
        args = Namespace(fake=False, model='m', workspace=tmp_path, hide_reasoning=False,
                         session='x', sessions=str(tmp_path), prompt='', resume=False, verbose=False)
        session = Session(id='x')
        build_agent(session, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
        kinds = [k for k, _ in wired]
        assert kinds == ['overflow', 'auto']  # 溢出恢复恒挂 + 阈值默认挂
        auto_kw = dict(wired[1][1])
        assert auto_kw['max_tokens'] == DEFAULT_COMPACT_TOKENS

        # 显式 0 → 阈值压缩关，但溢出恢复仍在（错误兜底不依赖阈值开关）
        wired.clear()
        args2 = Namespace(fake=False, model='m', workspace=tmp_path, hide_reasoning=False,
                          session='x', sessions=str(tmp_path), prompt='', resume=False, verbose=False,
                          compact_at=0)
        build_agent(Session(id='x2'), args2, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
        assert [k for k, _ in wired] == ['overflow']

        # fake 模式 → 都不挂（脚本 llm 不能真摘要）
        wired.clear()
        args3 = Namespace(fake=True, model='m', workspace=tmp_path, hide_reasoning=False,
                          session='x', sessions=str(tmp_path), prompt='', resume=False, verbose=False)
        build_agent(Session(id='x3'), args3, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
        assert wired == []
    finally:
        comp.wire_auto_compaction = orig_wire
        comp.wire_overflow_recovery = orig_overflow


def test_cache_hit_rate_from_real_usage():
    """真实 usage 拆分缓存命中率：hit/(hit+miss)；无 usage → None。"""
    from agent_demo.compaction import cache_hit_rate, estimate_context_tokens, last_prompt_usage
    from agent_demo.session import Session
    from agent_demo.values import TextBlock, create_assistant_message

    s = Session(id='t')
    s.append('turn/start', {'turn': 1})
    # 模拟真实 provider 的 usage：prompt_tokens = hit + miss（DeepSeek 加性拆分）
    usage = {
        'prompt_tokens': 1000,
        'prompt_cache_hit_tokens': 700,
        'prompt_cache_miss_tokens': 300,
    }
    s.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message([TextBlock(text='ok')]),
        'usage': usage,
    }, surface_op='append')
    assert last_prompt_usage(s) == usage
    assert estimate_context_tokens(s) == 1000          # 真实 usage 优先
    assert cache_hit_rate(s) == 0.7                    # 700 / (700+300)
    assert cache_hit_rate(s) is not None and 0 <= cache_hit_rate(s) <= 1

    # 无 usage（fake 模式 / 最新 assistant 无 usage）→ 无真实测量
    s2 = Session(id='t2')
    s2.append('turn/start', {'turn': 1})
    s2.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message([TextBlock(text='x' * 200)]),
    }, surface_op='append')
    assert last_prompt_usage(s2) is None
    assert cache_hit_rate(s2) is None
    assert estimate_context_tokens(s2) == 100           # 兜底字符估算 200//2

    # usage 缺 cache 拆分字段 → 命中率 None（不是 0——别假装测量过）
    s3 = Session(id='t3')
    s3.append('turn/start', {'turn': 1})
    s3.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message([TextBlock(text='ok')]),
        'usage': {'prompt_tokens': 50, 'completion_tokens': 5, 'total_tokens': 55},
    }, surface_op='append')
    assert cache_hit_rate(s3) is None


def test_context_overflow_detection():
    """溢出错误识别：HTTP_ERROR + 特征串为真，其他为假。"""
    from agent_demo.compaction import _is_context_overflow
    assert _is_context_overflow('HTTP_ERROR', '400: maximum context length exceeded') is True
    assert _is_context_overflow('HTTP_ERROR', 'input is too long for the model') is True
    assert _is_context_overflow('HTTP_ERROR', '上下文长度超过限制') is True
    assert _is_context_overflow('HTTP_ERROR', 'rate limit reached') is False
    assert _is_context_overflow('RATE_LIMIT', 'context length') is False  # 非 HTTP_ERROR code
    assert _is_context_overflow('HTTP_ERROR', '') is False


@pytest.mark.asyncio
async def test_overflow_recovery_compacts_and_retries(tmp_path):
    """溢出恢复：模型报上下文过长 → 压缩旧回合 → retry 成功。"""
    import os
    os.environ['DEEPSEEK_API_KEY'] = 'sk-placeholder'
    from argparse import Namespace

    from agent_demo.factory import build_agent
    from agent_demo.session import Session
    from agent_demo.values import TextBlock, create_assistant_message, create_user_message

    class OverflowLlm:
        def __init__(self):
            self.main_steps = [
                {'error': {'code': 'HTTP_ERROR', 'message': 'maximum context length exceeded (1M)'}},
                {'text': '压缩后重试成功', 'finish_reason': 'stop'},
            ]

        async def stream(self, request, signal=None):
            from agent_demo.llm import LlmError, StreamChunk
            if 'compaction engine' in (request.system or ''):
                yield StreamChunk(text='## 主要请求\n- 压缩历史\n## 下一步\n1. 继续', finish_reason='stop')
                return
            step = self.main_steps.pop(0)
            if 'error' in step:
                raise LlmError(step['error']['code'], step['error']['message'])
            yield StreamChunk(text=step['text'], finish_reason='stop')

    s = Session(id='ov')
    def user(t): return create_user_message([TextBlock(text=t)])
    def asst(t): return {'message': create_assistant_message([TextBlock(text=t)])}
    s.append('turn/start', {'turn': 1})
    s.append('user/message', user('历史任务内容' * 30), surface_op='append')
    s.append('assistant/message', asst('历史回答' * 30), surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    s.append('turn/start', {'turn': 2})
    s.append('user/message', user('继续'), surface_op='append')

    args = Namespace(fake=False, model='m', workspace=tmp_path, hide_reasoning=False,
                     session='ov', sessions=str(tmp_path), prompt='', resume=False, verbose=False)
    agent = build_agent(s, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
    agent.llm = OverflowLlm()
    agent.followup('继续')
    await agent.when_idle()

    # 溢出触发压缩（四事件）+ retry 成功
    assert any(e.type == 'compaction/summary' for e in s.events)
    assert any(e.type == 'compaction/end' for e in s.events)
    assert s.events[-1].data['reason'] == 'completed'
    texts = [m.content[0].text for m in s.derive_messages() if m.content and m.content[0].type == 'text']
    assert any('<compacted-summary>' in t for t in texts)
    assert texts[-1] == '压缩后重试成功'


def test_web_checkpoint_role_and_context_payload(tmp_path):
    """checkpoint 消息标记 role=checkpoint；history/会话响应带 context。"""
    from fastapi.testclient import TestClient

    from agent_demo import web_app
    from agent_demo.values import TextBlock, create_user_message

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web_app.app)

    # 直接往当前会话写 checkpoint 形态的 user/message（带 compacted-summary 标签）
    s = web_app._session
    s.append('turn/start', {'turn': 1})
    s.append('user/message', create_user_message([TextBlock(text='Q1')]), surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    s.append('turn/start', {'turn': 2})
    cp_text = ('This is an automatically generated checkpoint…\n\n'
               '<compacted-summary>\n## 主要请求\n- 重构\n## 下一步\n1. 测试\n</compacted-summary>')
    s.append('user/message', create_user_message([TextBlock(text=cp_text)]),
             surface_op='replace', shadowed=(1, 1))
    s.append('user/message', create_user_message([TextBlock(text='继续')]), surface_op='append')

    hist = client.get('/history').json()
    roles = [m['role'] for m in hist['history']]
    assert roles == ['checkpoint', 'user']          # checkpoint 被标记，后续 user 正常
    assert hist['context'] is not None              # 上下文 payload 存在
    assert 'window' in hist['context'] and 'percent' in hist['context']
    # fake 模式无真实 usage → 不带 session 累计账（前端隐藏命中/消耗标签）
    assert 'session' not in hist['context']

    # 切换也带 context
    fresh = client.post('/sessions/new').json()
    assert 'context' in fresh


def test_web_context_session_totals_accumulate(tmp_path):
    """真实 usage 多条 → 会话级累计账（消耗 token 求和、缓存命中率 token 加权）。"""
    from fastapi.testclient import TestClient

    from agent_demo import web_app
    from agent_demo.values import TextBlock, create_assistant_message, create_user_message

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web_app.app)
    s = web_app._session
    s.append('turn/start', {'turn': 1})
    s.append('user/message', create_user_message([TextBlock(text='Q1')]), surface_op='append')
    # 两次真实请求：usage 必须各自落账，命中率是 Σ 比值而非单次、也非简单平均
    s.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message([TextBlock(text='A1')]),
        'usage': {'prompt_tokens': 2000, 'completion_tokens': 300,
                  'prompt_cache_hit_tokens': 1500, 'prompt_cache_miss_tokens': 500},
    }, surface_op='append')
    s.append('assistant/message', {
        'turn': 1, 'step': 2,
        'message': create_assistant_message([TextBlock(text='A2')]),
        'usage': {'prompt_tokens': 1000, 'completion_tokens': 100,
                  'prompt_cache_hit_tokens': 200, 'prompt_cache_miss_tokens': 800},
    }, surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})

    hist = client.get('/history').json()
    ctx = hist['context']
    # 快照仍取最后一条真实 usage（圆环语义不变）
    assert ctx['used'] == 1000
    assert ctx['percent'] == round(1000 * 100 / web_app.MODEL_CONTEXT_WINDOW)
    # 会话级累计账：消耗 = Σ input + Σ output；命中率 = Σhit / Σ(hit+miss)
    session = ctx['session']
    assert session['input_tokens'] == 3000            # 2000 + 1000
    assert session['output_tokens'] == 400            # 300 + 100
    assert session['total_tokens'] == 3400            # 3000 + 400
    assert session['cache_hit_pct'] == 57             # (1500+200)/(2000+1000) = 56.67 → 57%
    # 简单平均会得 (75% + 20%)/2 = 47.5 —— 断言拒绝该口径
    assert session['cache_hit_pct'] != 48

    # 会话内某些请求没报缓存拆分 → 该请求不计入命中统计（但消耗照记）
    s2 = client.post('/sessions/new').json()
    sid = s2['id']
    client.post(f'/sessions/{sid}/switch')
    s = web_app._session
    s.append('turn/start', {'turn': 1})
    s.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message([TextBlock(text='no-cache-field')]),
        'usage': {'prompt_tokens': 500, 'completion_tokens': 50},
    }, surface_op='append')
    s.append('assistant/message', {
        'turn': 1, 'step': 2,
        'message': create_assistant_message([TextBlock(text='with-cache')]),
        'usage': {'prompt_tokens': 300, 'completion_tokens': 30,
                  'prompt_cache_hit_tokens': 90, 'prompt_cache_miss_tokens': 210},
    }, surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    ctx2 = client.get('/history').json()['context']
    assert ctx2['session']['total_tokens'] == 880      # 消耗照记全部：500+300+50+30
    assert ctx2['session']['cache_hit_pct'] == 30      # 命中只统计报了拆分的：90/300


def test_web_manual_compact_endpoint(tmp_path):
    """手动压缩 POST /compact：fake 拒绝；真实模式压缩旧回合落 checkpoint。"""
    import os
    os.environ['DEEPSEEK_API_KEY'] = 'sk-placeholder'
    from fastapi.testclient import TestClient

    from agent_demo import web_app
    from agent_demo.values import TextBlock, create_assistant_message, create_user_message

    # fake 模式：脚本模型不能生成摘要 → 400 拒绝
    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web_app.app)
    assert client.post('/compact').status_code == 400

    # 真实模式 + stub llm：跑一轮完整旧回合，手动压缩应落 checkpoint
    web_app.init_web(tmp_path, fake=False, sessions_dir=tmp_path / 'sess2',
                     model='deepseek-v4-flash')
    s = web_app._session
    agent = web_app._agent

    class CompactLlm:
        async def stream(self, request, signal=None):
            from agent_demo.llm import StreamChunk
            if 'compaction engine' in (request.system or ''):
                yield StreamChunk(text='## 主要请求\n- 压历史\n## 下一步\n1. 继续', finish_reason='stop')
                return
            yield StreamChunk(text='回答', finish_reason='stop')
    agent.llm = CompactLlm()

    def user(t): return create_user_message([TextBlock(text=t)])
    s.append('turn/start', {'turn': 1})
    s.append('user/message', user('历史任务' * 50), surface_op='append')
    s.append('assistant/message', {'turn': 1, 'step': 1,
             'message': create_assistant_message([TextBlock(text='旧回答' * 50)])},
             surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    s.append('turn/start', {'turn': 2})
    s.append('user/message', user('新任务'), surface_op='append')

    resp = client.post('/compact')
    body = resp.json()
    assert resp.status_code == 200
    assert body['compacted'] is True
    assert any(e.type == 'compaction/summary' for e in s.events)
    assert any(e.type == 'compaction/end' for e in s.events)
    # checkpoint 进了模型可见历史，且新回合消息保留
    texts = [m.content[0].text for m in s.derive_messages()
             if m.content and getattr(m.content[0], 'type', '') == 'text']
    assert any('<compacted-summary>' in t for t in texts)
    assert '新任务' in texts[-1]

    # 无可压旧回合 → compacted False + reason（前面已全压完，只剩新回合）
    body2 = client.post('/compact').json()
    assert body2['compacted'] is False
    assert body2['reason']


def test_web_sessions_run_in_parallel_isolated(tmp_path):
    """Web 并发隔离：两会话各自跑一轮 chat，事件/审批/焦点互不踩。

    旧实现是全局单例（_session/_agent/_active_queue）：两会话同时跑时
    B 会覆盖 A 的指针与 SSE 队列。seat 化后每个 sid 一个
    {session, agent, queue}——验证：/chat 带 sid 路由到各自 seat、
    消息只进自己的日志、焦点别名随切换走、agent 实例彼此不同。
    """
    from fastapi.testclient import TestClient

    from agent_demo import web_app

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web_app.app)
    assert _id_of(client) == 'web'

    # 建第二个会话（new 返回描述并切为焦点）
    b = client.post('/sessions/new').json()
    assert b['id'] != 'web'

    # 两个会话各自发一轮消息（fake llm 离线脚本，能跑完一个回合）
    ra = client.post('/chat', json={'message': '任务甲：先读 A', 'sid': 'web'})
    rb = client.post('/chat', json={'message': '任务乙：先读 B', 'sid': b['id']})
    assert ra.status_code == 200 and rb.status_code == 200
    assert '"type": "turn_end"' in ra.text and '"type": "turn_end"' in rb.text

    # 焦点 = 最后切换/操作的会话（兼容旧路由无 sid 语义）
    assert web_app._current_sid == b['id']
    assert web_app._session.id == b['id']

    # 两会话的 agent 是不同实例，各自日志只有自己的消息
    seat_a = web_app._seats['web']
    seat_b = web_app._seats[b['id']]
    assert seat_a.agent is not seat_b.agent          # 独立 agent
    assert seat_a.session is not seat_b.session      # 独立事件日志

    def user_texts(seat):
        return [m.content[0].text for m in seat.session.derive_messages()
                if m.content and getattr(m.content[0], 'type', '') == 'text'
                and m.role == 'user']

    assert user_texts(seat_a) == ['任务甲：先读 A']   # A 的日志只有甲
    assert user_texts(seat_b) == ['任务乙：先读 B']   # B 的日志只有乙

    # /history 带 sid 各取各的（不带 = 焦点 b）
    ha = client.get('/history', params={'sid': 'web'}).json()
    hb = client.get('/history').json()
    assert any('任务甲' in (m.get('text') or '') for m in ha['history'])
    assert any('任务乙' in (m.get('text') or '') for m in hb['history'])

    # 切回 A：seat 复用（同实例，agent 事件日志延续）——不重建销毁
    switched = client.post('/sessions/web/switch').json()
    assert web_app._session.id == 'web'
    assert web_app._seats['web'] is seat_a             # 复用而非重建
    assert any('任务甲' in (m.get('text') or '') for m in switched['history'])


def _id_of(client) -> str:
    """当前焦点会话 id（init 后默认 'web'）。"""
    from agent_demo import web_app
    return web_app._current_sid


@pytest.mark.asyncio
async def test_steer_inserts_and_runs_as_next_turn():
    """steer 插队：消息进 next-step，回合结束后立即作为下一轮首条被消费。

    不苛求抓到 running 窗口（fake llm 太快）；重点是 next-step 里的消息
    绝不会丢——when_idle 收敛后 inbox.has_pending 为假、两条用户消息都
    进了模型记忆。
    """
    from agent_demo.agent import Agent
    from agent_demo.prompt import PromptRegistry
    from agent_demo.registry import ToolRegistry
    from agent_demo.session import Session

    session = Session(id='s')
    agent = Agent(
        session=session, llm=FakeLlm([{}]), prompt=PromptRegistry(), tools=ToolRegistry(),
        options={'provider': 'fake', 'model': 'fake-model'},
    )
    agent.followup('第一问')
    agent.steer('插队指令')      # 可能回合已完 → 进 next-step，下轮消费
    await agent.when_idle()

    # 两条用户消息都进了模型记忆，插队的那条也在
    users = [m.content[0].text for m in session.derive_messages()
             if m.content and getattr(m.content[0], 'type', '') == 'text' and m.role == 'user']
    assert '第一问' in users and '插队指令' in users
    assert agent.inbox.has_pending is False
    assert agent.status == 'idle'


@pytest.mark.asyncio
async def test_steer_while_running_becomes_next_step_of_same_turn():
    """steer 在回合进行中入队 → next-step：同回合内被消费（不新开回合）。

    验证方式不靠 sleep 抓窗口（脆弱），用事件日志的事实：
    - 全程只有一次 turn/start（steer 没触发新回合）
    - 出现 ≥2 次 step/start（steer 消息作为本回合的下一步被处理）
    - 插队文本在模型可见记忆里
    """
    from agent_demo.agent import Agent
    from agent_demo.llm import StreamChunk
    from agent_demo.prompt import PromptRegistry
    from agent_demo.registry import ToolRegistry
    from agent_demo.session import Session

    class HoldingLlm:
        """第一步：stream 挂起（等 steer 入队窗口）再 yield 文本。

        用 asyncio.Event 精确控制：stream 一进来就置 started 并等 release，
        测试在 release 前完成 steer——保证消息入队时回合仍在进行。
        """
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def stream(self, request, signal=None):
            self.started.set()
            await self.release.wait()
            yield StreamChunk(text='done', finish_reason='stop')

    session = Session(id='s')
    llm = HoldingLlm()
    agent = Agent(
        session=session, llm=llm, prompt=PromptRegistry(), tools=ToolRegistry(),
        options={'provider': 'fake', 'model': 'fake-model'},
    )
    agent.followup('做任务')
    await llm.started.wait()            # 回合确实在跑（driver 挂起在 stream 里）
    assert agent.status == 'running'
    agent.steer('改个方向')             # 进行中插队 → next-step
    llm.release.set()                   # 放行：第一步完成，下一步轮到 steer 消息
    await agent.when_idle()

    turns = [e.data['turn'] for e in session.events if e.type == 'turn/start']
    steps = [e for e in session.events if e.type == 'step/start']
    assert len(turns) == 1                      # 没开新回合
    assert len(steps) >= 2                      # steer 是同一回合的下一步
    users = [m.content[0].text for m in session.derive_messages()
             if m.content and getattr(m.content[0], 'type', '') == 'text' and m.role == 'user']
    assert users == ['做任务', '改个方向']       # 两条都消费，顺序正确
    assert agent.status == 'idle'


@pytest.mark.asyncio
async def test_steer_absorbed_at_next_request_inside_tool_loop():
    """插队在**下一次模型请求**就被吸收——step 粒度 = 一次请求。

    回归（真实日志实测）：step 曾是"整段工具循环"，`_run_step` 内部反复
    模型↔工具，只有模型最终给出纯文本才回到外层 claim。于是插队消息在
    next-step 里躺了 2 分 40 秒（50+ 次工具调用），最后被 cancel 清掉
    （outcome='canceled'）——模型从头到尾没见过它，用户看到的就是
    "插入信息不起作用"。

    现在一步只发一次请求，claim 发生在每次请求**之前**，所以断言：
    - 第 1 次请求的 messages 里没有插队文本（那时还没插队）
    - 第 2 次请求（工具循环仍在继续）的 messages 里**已经有**它
    - 日志里 step 数与请求数一一对应
    """
    from agent_demo.llm import ToolCallDelta

    calls = []

    async def read_file(args, agent, signal):  # noqa: ARG001
        calls.append(args)
        return ToolOutcome(content='line 1')

    tools = ToolRegistry()
    tools.register(ToolSpec(
        name='read_file',
        description='Read a file.',
        parameters={'type': 'object', 'properties': {'file_path': {'type': 'string'}},
                    'required': ['file_path']},
        execute=read_file,
    ))

    class LoopLlm:
        """前三轮都要求调用工具；每轮记下它这次请求收到的 user 文本。"""

        def __init__(self):
            self.seen: list[list[str]] = []
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def stream(self, request, signal=None):  # noqa: ARG002
            self.seen.append([m.content[0].text for m in request.messages
                              if m.role == 'user' and m.content
                              and getattr(m.content[0], 'type', '') == 'text'])
            index = len(self.seen)
            if index == 1:
                self.entered.set()
                await self.release.wait()   # 卡住第 1 轮：给测试留插队窗口
            if index <= 3:                  # 三轮工具循环，回合一直没收尾
                yield StreamChunk(tool_calls=(ToolCallDelta(
                    index=0, id=f'c{index}', name='read_file',
                    arguments=json.dumps({'file_path': 'a.txt'})),))
                yield StreamChunk(finish_reason='tool_calls')
            else:
                yield StreamChunk(text='done', finish_reason='stop')

    session = Session(id='s')
    llm = LoopLlm()
    agent = Agent(session=session, llm=llm, prompt=PromptRegistry(), tools=tools,
                  options={'provider': 'fake', 'model': 'fake-model'})
    agent.followup('做一个长任务')
    driver = asyncio.create_task(agent.when_idle())
    await llm.entered.wait()
    agent.steer('插队：改方向')          # 工具循环还在第 1 轮里
    llm.release.set()
    await asyncio.wait_for(driver, timeout=5)

    assert len(llm.seen) == 4, llm.seen
    assert not any('插队' in text for text in llm.seen[0])
    assert any('插队：改方向' in text for text in llm.seen[1]), llm.seen
    # 它不是等工具循环跑完才被看到的：第 2、3 轮仍在调用工具
    assert len(calls) == 3
    # 一步 = 一次请求：step/start 与模型请求数一一对应
    assert len([e for e in session.events if e.type == 'step/start']) == len(llm.seen)
    # durable 落地：插队的 user/message 排在第 2 次请求的 header 之前
    claimed_seq = next(e.seq for e in session.events
                       if e.type == 'user/message' and e.data.content[0].text == '插队：改方向')
    headers = [e.seq for e in session.events if e.type == 'request/header']
    assert claimed_seq < headers[1], (claimed_seq, headers)
    assert session.events[-1].type == 'turn/end'
    assert session.events[-1].data['reason'] == 'completed'


def test_web_queue_actions_endpoint(tmp_path):
    """POST /queue/update：队列项操作（对齐 DSH 的 updateQueue(itemId, action)）。

    三种动作与两个语义要守住：
    - remove：队列区快照为空，且日志里没有产生 user/message surface——
      这条消息从未成为模型可见的输入（"没有状态不进日志"的逆向：撤回
      只留 spliced 痕迹）
    - edit：就地改写还没 claim 的消息（同 id 换文案），同样不产生 surface
    - steer：next-turn → next-step 的搬家（空闲时拒绝：没有"下一步"）
    - 已 claim（已进消息流）的操作返回 ok=False + queue-item-not-found，
      这是并发下的**正常收敛**而不是错误（HTTP 仍 200）
    """
    from fastapi.testclient import TestClient

    from agent_demo import web_app

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web_app.app)
    seat = web_app._seats['web']

    def update(item_id, action):
        resp = client.post('/queue/update',
                           json={'item_id': item_id, 'action': action, 'sid': 'web'})
        assert resp.status_code == 200, resp.text
        return resp.json()

    # 直接入队一条（wakeup=False：不真跑回合）：模拟"已发出、还没轮到"
    message = create_user_message([TextBlock(text='这条还没轮到')])
    seat.agent.send(message, 'next-step', wakeup=False)
    queued_id = message.id
    assert [r['id'] for r in web_app._queue_rows(seat.agent)] == [queued_id]

    # /history 与 /steer 一样带 queue（刷新页面也能恢复队列区）
    hist = client.get('/history').json()
    assert [(r['id'], r['placement']) for r in hist['queue']] == [(queued_id, 'steering')]
    # 会话切换也带 queue
    client.post('/sessions/new')
    switched = client.post('/sessions/web/switch').json()
    assert [r['id'] for r in switched['queue']] == [queued_id]

    resp = client.post('/queue/update',
                       json={'item_id': queued_id, 'action': {'kind': 'remove'}, 'sid': 'web'})
    assert resp.json() == {'ok': True, 'code': 'ok', 'sid': 'web', 'queue': []}
    assert not seat.agent.inbox.has_pending
    assert not [e for e in seat.session.events if e.type == 'user/message']

    # 已认领/已消失的项：ok=False + 并发收敛码（HTTP 仍 200）
    gone = update(queued_id, {'kind': 'remove'})
    assert gone['ok'] is False and gone['code'] == 'queue-item-not-found'
    # 校验：缺 item_id / 动作词表外 / 空编辑 / 会话不存在
    assert client.post('/queue/update', json={'action': {'kind': 'remove'}}).status_code == 400
    assert client.post('/queue/update',
                       json={'item_id': 'x', 'action': {'kind': 'nope'}}).status_code == 400
    assert client.post('/queue/update',
                       json={'item_id': 'x', 'action': {'kind': 'edit', 'text': ' '}}).status_code == 400
    assert client.post('/queue/update',
                       json={'item_id': 'x', 'action': {'kind': 'remove'},
                             'sid': 'nope'}).status_code == 404


def test_inbox_queue_actions_edit_and_promote():
    """状态层的三个队列动作：edit（同 id 换文案）/ promote（搬家）/ remove。

    edit 的安全性来自"还没 claim 就没有 surface"：改的只是将要成为模型输入的
    内容，日志里只有 spliced 痕迹。promote 是 next-turn → next-step 的搬家，
    两步各自落账、可重放。
    """
    session = Session(id='s')
    inbox = Inbox(session)
    queued = create_user_message([TextBlock(text='先写个草稿')])
    inbox.append('next-turn', queued)

    assert inbox.edit(queued.id, '改成：直接给结论') is True
    items = inbox.queued_items()
    assert items[0].id == queued.id                      # 身份不变（前端那行不闪）
    assert items[0].message.content[0].text == '改成：直接给结论'
    # 一次原子改动（replace 而不是"删+插"两条事件）
    splices = [e for e in session.events if e.type == 'agent/inbox/spliced']
    assert len(splices) == 2 and splices[-1].data['removed_count'] == 1
    assert splices[-1].data['inserted'][0].id == queued.id

    assert inbox.promote(queued.id) is True              # queued → steering
    assert [i.placement for i in inbox.queued_items()] == ['steering']
    assert inbox.promote(queued.id) is False             # 已经在 next-step：幂等无变化
    # 搬家 = 两次 splice（先摘后插）；摘除那步 discard=False：这不是丢弃，
    # 不该标 outcome='canceled'（那个标记专给撤回）
    moves = [e for e in session.events if e.type == 'agent/inbox/spliced'][2:]
    assert len(moves) == 2
    assert moves[0].data['target'] == 'next-turn' and moves[0].data['removed_count'] == 1
    assert moves[0].data.get('outcome') is None
    assert moves[1].data['target'] == 'next-step' and moves[1].data['inserted'][0].id == queued.id

    replayed = Inbox(session).queued_items()             # 重放：编辑与搬家都在
    assert replayed == inbox.queued_items()
    assert replayed[0].message.content[0].text == '改成：直接给结论'

    assert inbox.edit('nope', 'x') is False
    assert inbox.promote('nope') is False


def test_inbox_queued_items_is_a_session_projection():
    """队列投影在**状态层**：Inbox.queued_items() 与 derive_messages() 并列。

    为什么这条要单独测（架构回归）：投影曾被放在 web_app 里自己重放
    agent/inbox/spliced——同一事件类型两份折叠（Inbox._apply 一份、web 一份）
    必然分叉，而且投影绑死在 Web 宿主上（CLI/测试拿不到）。现在只有一份：
    _state 本身就是重放结果，queued_items() 只是给它贴上 placement 语义。

    断言三件事：
    - placement 映射：next-turn→queued、next-step→steering，顺序固定
    - 撤回/拼接后投影跟着变（走的是同一份折叠）
    - **换个 Inbox 重放同一段日志，投影逐字段相同**（可重建 ⟺ 模型可见的
      同款保证；resume 后队列区不会变形）
    """
    session = Session(id='s')
    inbox = Inbox(session)
    steer_one = create_user_message([TextBlock(text='插队一')])
    queued_two = create_user_message([TextBlock(text='排队二')])
    steer_three = create_user_message([TextBlock(text='插队三')])
    inbox.append('next-step', steer_one)
    inbox.append('next-turn', queued_two)
    inbox.append('next-step', steer_three)

    items = inbox.queued_items()
    assert [(i.placement, i.id) for i in items] == [
        ('queued', queued_two.id), ('steering', steer_one.id), ('steering', steer_three.id)]
    assert items[0].message is queued_two          # 值对象持有本体，不是副本
    assert items[1].id == steer_one.id             # id 直接取自 message

    inbox.remove(steer_one.id)
    assert [i.id for i in inbox.queued_items()] == [queued_two.id, steer_three.id]

    replayed = Inbox(session).queued_items()       # 日志重放 → 同一投影
    assert replayed == inbox.queued_items()        # frozen dataclass：逐字段相等


def test_web_queue_rows_serialize_state_projection(tmp_path):
    """web 层只做序列化：_queue_rows 把状态层的投影摊平成前端 JSON。

    注意入队走 wakeup=False：这里只测投影，不真跑回合（sync 测试里
    没有事件循环，_wake 会拉不起 driver）。
    """
    from agent_demo import web_app

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    session, agent = web_app._session, web_app._agent
    assert session is not None and agent is not None

    first = create_user_message([TextBlock(text='插队一')])
    agent.send(first, 'next-step', wakeup=False)
    second = create_user_message([TextBlock(text='排队二')])
    agent.send(second, 'next-turn', wakeup=False)      # next-turn = 'queued'
    rows = web_app._queue_rows(agent)
    assert [(r['text'], r['placement']) for r in rows] == [
        ('排队二', 'queued'), ('插队一', 'steering')]

    agent.unqueue(first.id)
    assert [(r['text'], r['placement']) for r in web_app._queue_rows(agent)] == [
        ('排队二', 'queued')]
    # 序列化 = 投影的镜像（id 集合一致，一一对应）
    assert [r['id'] for r in web_app._queue_rows(agent)] == [
        item.id for item in agent.inbox.queued_items()]


def test_web_steer_requires_active_stream(tmp_path):
    """POST /steer：无活跃对话流（idle）时 409 拒绝；提示用 /chat 开回合。"""
    from fastapi.testclient import TestClient

    from agent_demo import web_app

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web_app.app)

    # 会话刚初始化，无活跃 SSE 流 → 409
    resp = client.post('/steer', json={'message': 'hi'})
    assert resp.status_code == 409
    assert '活跃对话流' in resp.json()['detail']

    # fake llm 回合很快结束，chat 结束后流已关 → 再 steer 仍 409
    client.post('/chat', json={'message': 'hi'})
    resp2 = client.post('/steer', json={'message': 'again'})
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_web_steer_interrupts_open_sse_stream(tmp_path):
    """Web 端到端打断：/chat 的 SSE 流还开着时 POST /steer → 插队回答沿原流推回。

    这是"运行中可打断对话"的关键链路，同步 TestClient 测不了（post 阻塞到
    回合结束，无法中途发 /steer），故用 httpx.AsyncClient + ASGITransport
    并发两个请求：一个开着 SSE（agent 卡在可控挂起 LLM 上），一个 /steer。
    断言：插队消息在同一回合内被消费（无第二个 turn/start），回答沿原流推送。
    """
    import httpx

    from agent_demo import web_app
    from agent_demo.llm import StreamChunk

    class HoldLlm:
        """第一次 stream 挂起（started 置位等 release）；放行后给第一轮回答。

        steer 在挂起期间入队 → 第一轮 step 结束后，第二步立即轮到插队消息，
        第二次 stream 返回插队后的回答。
        """
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def stream(self, request, signal=None):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await self.release.wait()
                yield StreamChunk(text='首轮回答', finish_reason='stop')
            else:
                yield StreamChunk(text='插队后回答', finish_reason='stop')

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    seat = web_app._seats['web']
    llm = HoldLlm()
    seat.agent.llm = llm

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        async def read_sse() -> str:
            parts = []
            async with client.stream('POST', '/chat',
                                     json={'message': '首问', 'sid': 'web',
                                           'request_id': 'rpc-first'}) as resp:
                assert resp.status_code == 200
                async for chunk in resp.aiter_text():
                    parts.append(chunk)
            return '\n'.join(parts)

        reader = asyncio.create_task(read_sse())
        await llm.started.wait()                      # 回合真的挂起在 LLM 里
        steer_resp = await client.post(
            '/steer', json={'message': '停一下改方向', 'sid': 'web',
                            'request_id': 'rpc-steer'})
        assert steer_resp.status_code == 200
        assert steer_resp.json()['queued'] == 'next-step'
        # 返回 message_id + 队列快照：前端据此在消息流尾部画"待处理插队"气泡
        steer_id = steer_resp.json().get('message_id')
        assert steer_id, 'steer must return the queued message id'
        queued = steer_resp.json()['queue']
        # rpc_id 随消息 source 一起投影出来：前端靠它把本地回显原子换成真身
        assert [(r['id'], r['text'], r['placement'], r['rpc_id']) for r in queued] == [
            (steer_id, '停一下改方向', 'steering', 'rpc-steer')]
        llm.release.set()                             # 放行：第一步完成，下一步轮到插队
        sse = await asyncio.wait_for(reader, timeout=10)

    # 插队回答沿原 SSE 流推回来（同回合，无第二个回合）
    assert '首轮回答' in sse
    assert '插队后回答' in sse
    assert sse.count('"type": "turn_end"') == 1       # 一个回合结束 = 插队没开新回合
    # 认领链路：插队消息的 user_message 帧带 /steer 返回的同一个 message_id
    # 与提交身份 rpc_id（前端据此在**同一次渲染**里把本地回显换成真身）
    steer_frames = [f for f in sse.split('data: ')
                    if '"user_message"' in f and '停一下改方向' in f]
    assert steer_frames, 'steer message must be pushed as a user_message frame'
    steer_frame = json.loads(steer_frames[0].split('\n\n')[0])
    assert steer_frame['message_id'] == steer_id
    assert steer_frame['rpc_id'] == 'rpc-steer'
    # 首问那条也带自己的提交身份（idle 发送的回显同样要能交接）
    first_frames = [f for f in sse.split('data: ')
                    if '"user_message"' in f and '首问' in f]
    assert json.loads(first_frames[0].split('\n\n')[0])['rpc_id'] == 'rpc-first'
    # durable 消息 source 上落了提交身份（重放/历史都能认出来）
    sources = {e.data.content[0].text: e.data.source
               for e in seat.session.events if e.type == 'user/message'}
    assert sources['首问'].rpc_id == 'rpc-first'
    assert sources['停一下改方向'].rpc_id == 'rpc-steer'
    # 队列区通道：入队（spliced）推一条含插队消息的快照，claim 后再推一条空快照
    queue_frames = [json.loads(f.split('\n\n')[0]) for f in sse.split('data: ')
                    if '"queue_update"' in f]
    assert queue_frames, 'inbox splice must be pushed as queue_update frames'
    assert any(f['queue'] and f['queue'][0]['id'] == steer_id for f in queue_frames)
    assert queue_frames[-1]['queue'] == []            # 认领后队列区清空
    # 事件日志：全程只有一次 turn/start，两条 user 消息都在
    turns = [e for e in seat.session.events if e.type == 'turn/start']
    assert len(turns) == 1
    users = [e.data for e in seat.session.events if e.type == 'user/message']
    texts = []
    for u in users:
        for block in getattr(u, 'content', ()):
            if getattr(block, 'type', '') == 'text':
                texts.append(block.text)
    assert texts == ['首问', '停一下改方向']


@pytest.mark.asyncio
async def test_web_sse_disconnect_cancels_agent(tmp_path):
    """SSE 客户端断开（停止/关页）必须取消 agent，否则回合永不收敛。

    回归：sse_stream 的 finally 曾只 task.cancel()（run_agent），而
    when_idle 用 asyncio.shield 保护 driver——run_agent 被取消只是让
    when_idle 返回，正在跑的 driver（回合）继续执行、永不结束：
    前端 busy 复位后新消息走 /chat 全堵在 next-turn 排队，新回合永远
    开不了。修复：断开时 agent.cancel() 直达 driver（记 turn/end aborted）。

    可控挂起 LLM：流开着、agent 卡在等待 → 关流（asyncio 取消读取）→
    等 agent 收敛 → 断言回合被 abort、agent 回 idle、inbox 被清。
    """
    import httpx

    from agent_demo import web_app
    from agent_demo.llm import StreamChunk

    class HoldLlm:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def stream(self, request, signal=None):
            self.started.set()
            await self.release.wait()          # 一直挂到测试放行/取消
            yield StreamChunk(text='never', finish_reason='stop')

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    seat = web_app._seats['web']
    seat.agent.llm = HoldLlm()

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        async def read_sse() -> None:
            async with client.stream('POST', '/chat',
                                     json={'message': '首问', 'sid': 'web'}) as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_text():
                    pass

        reader = asyncio.create_task(read_sse())
        await seat.agent.llm.started.wait()    # 回合确实挂起（agent running）
        assert seat.agent.status == 'running'
        reader.cancel()                        # 模拟客户端断开（点停止/关页）
        try:
            await reader
        except asyncio.CancelledError:
            pass
        # 断开后允许 run_agent 收尾（cancel 传播 + DONE 落队列）
        await asyncio.sleep(0.3)
        # agent 必须回 idle（driver 被 agent.cancel() 打断，不是被 shield 留着）
        assert seat.agent.status == 'idle', 'SSE 断开后 agent 必须被取消回 idle'
        # 回合记 aborted（不是 completed——被打断，不是自然结束）
        ends = [e.data['reason'] for e in seat.session.events
                if e.type == 'turn/end']
        assert ends == ['aborted'], f'expected aborted turn, got {ends}'
        # inbox 被清空（cancel 默认清队列）：无幽灵消息等下次执行
        assert not seat.agent.inbox.has_pending

def test_cli_repl_runs_multiple_turns(tmp_path):
    """CLI REPL：无 prompt 启动 → 多轮输入各自开回合，/exit 退出。

    用 subprocess 真跑 CLI（管道喂 stdin），最接近真实交互；fake llm
    离线跑通。断言：退出后会话日志里有两个回合、两条 user 消息。
    """
    import subprocess
    import sys
    from pathlib import Path

    sessions_dir = tmp_path / 'sess'
    sessions_dir.mkdir(exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, '-m', 'agent_demo.cli', '--fake',
         '--workspace', str(tmp_path), '--session', 'repl-test',
         '--sessions', str(sessions_dir)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=Path(__file__).resolve().parent.parent,  # 仓库根（agent_demo 可导入）
    )
    out, err = proc.communicate(
        input='第一轮：读 README\n第二轮：列文件\n/exit\n'.encode(),
        timeout=60,
    )
    assert proc.returncode == 0, f'REPL exit {proc.returncode}\n{err.decode(errors="replace")}'
    # 提示符出现多次 = 多轮
    assert out.decode(errors='replace').count('agent> ') >= 3   # 两轮 + 首提示 + 结尾

    # 会话日志落盘：两个回合、两条 user 消息
    log = (sessions_dir / 'repl-test.jsonl')
    assert log.exists()
    from agent_demo.persistence import load_events
    events = list(load_events(log))
    turns = [e for e in events if e.type == 'turn/start']
    assert len(turns) >= 2
    texts = []
    for e in events:
        if e.type != 'user/message':
            continue
        msg = e.data
        for block in getattr(msg, 'content', ()):
            if getattr(block, 'type', '') == 'text':
                texts.append(block.text)
    assert '第一轮：读 README' in texts and '第二轮：列文件' in texts


def test_web_history_marks_same_turn_steer(tmp_path):
    """历史渲染的回合归属：同回合插队（steer）的 user 消息 turn 相同。

    重开会话时前端靠 user 消息的 turn 决定画不画回合分隔线——若两条
    user 同 turn（首问 + 插队），不画；若跨回合（followup 开新回合），
    画。回归：之前 steer 消息在刷新后被渲染成独立"回合 N"（缺 turn 信息）。
    """
    from fastapi.testclient import TestClient

    from agent_demo import web_app
    from agent_demo.values import TextBlock, create_assistant_message, create_user_message

    web_app.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web_app.app)
    s = web_app._session

    # turn 1：首问 → 模型答 → 插队（同回合第二条 user）→ 模型答
    def user(t): return create_user_message([TextBlock(text=t)])
    s.append('turn/start', {'turn': 1})
    s.append('user/message', user('首问：读 README'), surface_op='append')
    s.append('assistant/message', {'turn': 1, 'step': 1,
             'message': create_assistant_message([TextBlock(text='A1')])},
             surface_op='append')
    s.append('user/message', user('插队：先别读，列文件'), surface_op='append')  # steer 同回合
    s.append('assistant/message', {'turn': 1, 'step': 2,
             'message': create_assistant_message([TextBlock(text='A2')])},
             surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    # turn 2：新回合
    s.append('turn/start', {'turn': 2})
    s.append('user/message', user('第二轮问题'), surface_op='append')
    s.append('turn/end', {'turn': 2, 'reason': 'completed'})

    hist = client.get('/history').json()['history']
    users = [(m['text'], m.get('turn')) for m in hist if m['role'] == 'user']
    assert [t for _, t in users] == [1, 1, 2]
    assert users[0][0].startswith('首问') and users[1][0].startswith('插队')
    # 同回合两条 user 的 turn 相同（前端据此不画分隔线）；新回合不同
    turns = [t for _, t in users]
    assert turns[0] == turns[1] and turns[1] != turns[2]


# ============================================================
# web_search —— DeepSeek 官方原生搜索（Anthropic 兼容 Messages 端点）
#
# 搜索由服务端 `web_search_20250305` 工具执行，我们只解析结构化块
# （web_search_tool_result → web_search_result），绝不抓网页、绝不去
# text 正文里抠 URL。夹具剪裁自真实响应（page_age 实测为 null）。
# ============================================================

# 夹具：含重复 URL（验证去重）、空 title（验证 label 退化到 hostname）、
# 一条非 web_search_result 项（验证被跳过）。
_WEB_SEARCH_FIXTURE = {
    'type': 'message',
    'model': 'deepseek-v4-flash',
    'stop_reason': 'end_turn',
    'content': [
        {'type': 'thinking', 'thinking': '先搜一下', 'signature': 'sig'},
        {'type': 'server_tool_use', 'id': 'call_1', 'name': 'web_search',
         'input': {'query': 'deepseek-harness 架构'}},
        {'type': 'web_search_tool_result', 'tool_use_id': 'call_1', 'content': [
            {'type': 'web_search_result', 'title': 'Harness 架构（中文）',
             'url': 'https://example.com/a', 'page_age': None, 'encrypted_content': 'xxx'},
            {'type': 'web_search_result', 'title': '',
             'url': 'https://example.com/b', 'page_age': None, 'encrypted_content': 'yyy'},
            {'type': 'web_search_result', 'title': '重复 URL 应被丢弃',
             'url': 'https://example.com/a', 'page_age': None},
            {'type': 'web_search_result', 'title': '空 url 应被丢弃',
             'url': '', 'page_age': None},
        ]},
        {'type': 'text', 'text': '以下是整理后的答复。'},
    ],
    'usage': {'server_tool_use': {'web_search_requests': 1}},
}


def _web_search_backend(monkey_env='test-key'):
    """造一个 httpx.MockTransport 后端（喂夹具，记录请求体），不碰网络。"""
    from agent_demo.tools import web_search as ws

    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append({
            'url': str(request.url),
            'headers': dict(request.headers),
            'body': json.loads(request.content),
        })
        return httpx.Response(200, json=_WEB_SEARCH_FIXTURE)

    async def backend(query: str, max_results: int):
        return await ws.deepseek_search_backend(
            query, max_results,
            transport=httpx.MockTransport(handler), api_key=monkey_env,
        )

    return backend, requests


def test_web_search_parses_structured_blocks_only():
    """只认结构化块：去重 / 空 url 丢弃 / title 缺失 / page_age=null 都不炸。"""
    from agent_demo.tools import web_search as ws

    results = ws.parse_search_response(_WEB_SEARCH_FIXTURE)
    assert [r.url for r in results] == ['https://example.com/a', 'https://example.com/b']
    assert results[0].title == 'Harness 架构（中文）'
    assert results[1].title == ''            # 缺 title 不是错误
    assert results[0].published_at == ''     # page_age 实测为 null → 空
    assert results[0].snippet == ''          # 实测 text 块没有 citations → snippet 空

    # 空 title 的 label 退化到 hostname；`No results found.` 只在真的空列表时出现
    output = ws.format_search_output(results)
    assert output.startswith(ws.EXTERNAL_WEB_CONTENT_NOTICE)
    assert '- [Harness 架构（中文）](https://example.com/a)' in output
    assert '- [example.com](https://example.com/b)' in output
    assert output.endswith(ws.CITE_INSTRUCTION)
    assert 'No results found.' not in output

    empty = ws.format_search_output([])
    assert 'No results found.' in empty and 'Sources:' not in empty


def test_web_search_citation_snippet_when_present():
    """text 块的 citations 提供 snippet（DSH 的 citationSnippets 语义；首次出现者胜）。"""
    from agent_demo.tools import web_search as ws

    payload = {
        'content': [
            {'type': 'web_search_tool_result', 'content': [
                {'type': 'web_search_result', 'title': 'T', 'url': 'https://x.test/1', 'page_age': '2026-08-13'},
            ]},
            {'type': 'text', 'text': '正文', 'citations': [
                {'url': 'https://x.test/1', 'cited_text': '第一次的摘录'},
                {'url': 'https://x.test/1', 'cited_text': '应被忽略'},
            ]},
        ],
    }
    results = ws.parse_search_response(payload)
    assert results[0].snippet == '第一次的摘录'
    assert results[0].published_at == '2026-08-13'
    assert '(2026-08-13)' in ws.format_search_output(results)


def test_web_search_no_result_block_is_error_not_empty():
    """没触发原生搜索 → 响亮失败（WEB_PROVIDER_ERROR），不退化成"没找到"。"""
    from agent_demo.tools import web_search as ws

    for payload in (
        {'content': [{'type': 'text', 'text': '我直接回答了，没搜索'}]},
        {'content': []},
        {},
    ):
        with pytest.raises(ws.WebSearchError) as excinfo:
            ws.parse_search_response(payload)
        assert excinfo.value.code == 'WEB_PROVIDER_ERROR'
        assert 'web_search_tool_result' in excinfo.value.message


@pytest.mark.asyncio
async def test_web_search_backend_request_shape_and_failures(monkeypatch):
    """默认后端：请求体/头照 DSH；缺 key / HTTP 非 200 / 响应不可解析都结构化失败。"""
    from agent_demo.tools import web_search as ws

    backend, requests = _web_search_backend()
    results = await backend('deepseek-harness 架构', 3)
    # 夹具里只有 a / b 是唯一且非空的 URL（重复项与空 url 被丢弃）
    assert [r.url for r in results] == ['https://example.com/a', 'https://example.com/b']

    sent = requests[0]
    assert sent['url'] == 'https://api.deepseek.com/anthropic/v1/messages'
    assert sent['headers']['x-api-key'] == 'test-key'
    assert sent['headers']['anthropic-version'] == '2023-06-01'
    assert sent['body']['model'] == 'deepseek-v4-flash'
    assert sent['body']['max_tokens'] == 4096
    assert sent['body']['tools'] == [
        {'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 5}]
    assert sent['body']['messages'][0]['content'][0]['text'] == (
        'Perform a web search for the query: deepseek-harness 架构')

    # 缺 key → WEB_PROVIDER_CREDENTIAL_MISSING（不抛穿，交给包装层降级）
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    with pytest.raises(ws.WebSearchError) as excinfo:
        await ws.deepseek_search_backend('q', 3, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert excinfo.value.code == 'WEB_PROVIDER_CREDENTIAL_MISSING'

    # HTTP 非 200 → 带上状态码与 detail
    def http_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={'error': {'message': 'rate limited'}})

    with pytest.raises(ws.WebSearchError) as excinfo:
        await ws.deepseek_search_backend('q', 3, transport=httpx.MockTransport(http_error), api_key='k')
    assert excinfo.value.code == 'WEB_PROVIDER_HTTP_ERROR'
    assert '429' in excinfo.value.message and 'rate limited' in excinfo.value.message

    # 响应体不是 JSON → WEB_PROVIDER_ERROR（不炸）
    with pytest.raises(ws.WebSearchError) as excinfo:
        await ws.deepseek_search_backend(
            'q', 3, transport=httpx.MockTransport(lambda r: httpx.Response(200, text='not json')), api_key='k')
    assert excinfo.value.code == 'WEB_PROVIDER_ERROR'


def test_web_search_query_validation():
    """queries 校验：空数组 / 超限 / 空白项 → is_error；重复项折叠。"""
    from agent_demo.tools import web_search as ws

    assert ws.parse_query_args(['a', ' b ', 'a'], 4) == ['a', 'b']   # 折叠 + strip
    for bad in ([], ['  '], ['a'] * 5, 'a', [1]):
        with pytest.raises(ws.WebSearchError) as excinfo:
            ws.parse_query_args(bad, 4)
        assert excinfo.value.code == 'INVALID_QUERIES'


@pytest.mark.asyncio
async def test_web_search_tool_multi_query_merge_and_trace(tmp_path):
    """工具层：多 query 逐条搜索、按 url 去重合并、派发前落 web/search 痕迹（无 key）。

    DSH 的 mergeSearchResults 是 round-robin；这里按 query 顺序拼接（先到先得），
    超上限截断。痕迹事件对齐 DSH 的 web/deepseek-search-llm-request：
    记 query/endpoint/model/max_uses，**绝不含 key**。
    """
    from agent_demo.registry import ToolRegistry
    from agent_demo.session import Session
    from agent_demo.tools import web_search as ws

    backend, requests = _web_search_backend()
    registry = ToolRegistry()
    ws.register(registry, backend=backend, max_results=3)
    spec = registry._tools['web_search']

    session = Session(id='web-search-test')
    agent = type('A', (), {'session': session})()

    first = await spec.execute({'queries': ['q1', 'q2']}, agent, None)
    assert first.is_error is False
    # 两条 query 各发一次请求；结果按 url 去重（夹具里 a 重复）→ 只剩 a/b，未到上限
    assert len(requests) == 2
    assert first.content.count('- [') == 2
    assert 'https://example.com/a' in first.content and 'https://example.com/b' in first.content

    # 痕迹事件：派发前落、不含 key
    traces = [e for e in session.events if e.type == 'web/search']
    assert [t.data['query'] for t in traces] == ['q1', 'q2']
    assert traces[0].data['endpoint'] == 'https://api.deepseek.com/anthropic/v1/messages'
    assert traces[0].data['model'] == 'deepseek-v4-flash'
    assert traces[0].data['max_uses'] == 5
    assert 'key' not in json.dumps(traces[0].data, ensure_ascii=False).lower()
    # 痕迹不是 surface：不进模型记忆（不变式②）
    assert session.derive_messages() == []
    # 请求顺序：痕迹先落，再发请求（派发前记账）
    assert session.events.index(traces[0]) < session.events.index(traces[1])


@pytest.mark.asyncio
async def test_web_search_tool_degrades_to_is_error(tmp_path):
    """工具包装层：坏入参 / 后端失败都返回 is_error 的 ToolOutcome，绝不抛异常。"""
    from agent_demo.registry import ToolRegistry
    from agent_demo.session import Session
    from agent_demo.tools import web_search as ws

    # 坏参数：空 queries（schema 外的话直接拒绝）
    registry = ToolRegistry()
    ws.register(registry, backend=_web_search_backend()[0])
    spec = registry._tools['web_search']
    session = Session(id='degrade')
    agent = type('A', (), {'session': session})()

    out = await spec.execute({'queries': []}, agent, None)
    assert out.is_error is True and 'at least one query' in out.content

    out = await spec.execute({'queries': ['a', 'b', 'c', 'd', 'e']}, agent, None)
    assert out.is_error is True and 'at most 4' in out.content

    out = await spec.execute({'queries': ['ok', '   ']}, agent, None)
    assert out.is_error is True

    # 后端结构化失败（缺 key）→ is_error，且带上 code 供模型/诊断识别
    async def no_key_backend(query, max_results):
        raise ws.WebSearchError('WEB_PROVIDER_CREDENTIAL_MISSING', 'no key')

    registry2 = ToolRegistry()
    ws.register(registry2, backend=no_key_backend)
    out = await registry2._tools['web_search'].execute({'queries': ['q']}, None, None)
    assert out.is_error is True and 'WEB_PROVIDER_CREDENTIAL_MISSING' in out.content

    # 后端抛别的异常也不穿：一律降级为 is_error 结果（不变式⑤）
    async def boom(query, max_results):
        raise RuntimeError('boom')

    registry3 = ToolRegistry()
    ws.register(registry3, backend=boom)
    out = await registry3._tools['web_search'].execute({'queries': ['q']}, None, None)
    assert out.is_error is True and 'boom' in out.content
