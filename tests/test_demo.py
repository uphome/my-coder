"""架构测试：值层冻结、日志推导、inbox 重放、严格插值、工具循环、取消、持久化回放。"""

import asyncio
import json

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
    assert client.get('/history').json() == {'history': [], 'todos': []}  # 空会话

    resp = client.post('/chat', json={'message': 'hi'})
    assert resp.status_code == 200
    # SSE 帧：流式文本 + 工具调用 + 回合结束标记
    assert resp.text.startswith('data: ')
    assert '"type": "chunk"' in resp.text
    assert '"type": "tool_call"' in resp.text
    assert '"type": "turn_end"' in resp.text

    # 对话后历史可查（记忆 = 日志投影，Web 视角同样成立）
    payload = client.get('/history').json()
    assert payload['todos'] == []
    history = payload['history']
    assert history[0]['role'] == 'user'
    assert any(m['role'] == 'assistant' and m['text'] for m in history)


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
    assert client.get('/history').json() == {'history': [], 'todos': []}

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
async def test_todo_live_injection_across_steps(tmp_path):
    """todo 的 live 注入在真实 loop 生效：第一步规划 → 第二步请求的 system 含清单。

    FakeLlm 两步：第一步 todo_write（规划 2 项），第二步纯文本。两步是两次
    模型请求（loop 的 while：工具执行后回到顶部再请求）——第二步的
    request/header system 必须含第一步写的 todo（live 段每次 render 重新折叠）。
    """
    from argparse import Namespace

    from agent_demo.factory import build_agent
    from agent_demo.llm import FakeLlm
    from agent_demo.session import Session

    session = Session(id='todo-live')
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
    agent.llm = llm  # 替换成两步脚本
    agent.followup('do the multi-step work')
    await agent.when_idle()

    # 两次模型请求，system 各异：第二次必须带第一次规划的 todo 清单
    systems = [e.data['system'] for e in session.events if e.type == 'request/header']
    assert len(systems) == 2, f'expected 2 model requests, got {len(systems)}'
    assert 'step one' not in systems[0]          # 规划前：无清单
    assert 'Current todo list' in systems[1]      # 规划后：live 段注入
    assert 'step one=in_progress' in systems[1] or 'step one' in systems[1]
    assert 'step two' in systems[1]


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


def test_todo_fold_clears_on_turn_start(tmp_path):
    """todo = 当前回合的计划：turn/end 保留完成清单，下个 turn/start 清空。"""
    from agent_demo.session import Session
    from agent_demo.tools.todo import fold_todos

    s = Session(id='t')

    # 回合内：写清单 → 一直可见（无 turn/start 打断）
    s.append('turn/start', {'turn': 1})
    s.append('todo/write', {'todos': [
        {'content': 'a', 'status': 'completed'},
        {'content': 'b', 'status': 'completed'},
    ]})
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    folded = fold_todos(s)
    assert [t['content'] for t in folded] == ['a', 'b']  # 回合结束保留（收尾展示全勾）

    # 下个回合开始：清空——todo 不带进新任务
    s.append('turn/start', {'turn': 2})
    assert fold_todos(s) is None

    # 新回合里再规划 → 恢复
    s.append('todo/write', {'todos': [{'content': 'c', 'status': 'pending'}]})
    assert [t['content'] for t in fold_todos(s)] == ['c']

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
    assert [t['content'] for t in fold_todos(restored)] == ['c']


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
