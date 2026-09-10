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
    h = client.get('/history').json()
    assert h['history'] == [] and h['todos'] == [] and 'context' in h  # 空会话

    resp = client.post('/chat', json={'message': 'hi'})
    assert resp.status_code == 200
    # SSE 帧：流式文本 + 工具调用 + 回合结束标记
    assert resp.text.startswith('data: ')
    assert '"type": "chunk"' in resp.text
    assert '"type": "tool_call"' in resp.text
    assert '"type": "turn_end"' in resp.text
    # 统一投影模型的协议：回合开始帧、真人发言帧（带 turn）、内容帧带 turn/step
    assert '"type": "turn_start"' in resp.text
    assert '"type": "user_message"' in resp.text
    assert '"type": "chunk", "turn": 1' in resp.text      # chunk 带 turn（前端按节点分块）
    assert '"type": "user_message", "text": "hi", "turn": 1' in resp.text

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
                                     json={'message': '首问', 'sid': 'web'}) as resp:
                assert resp.status_code == 200
                async for chunk in resp.aiter_text():
                    parts.append(chunk)
            return '\n'.join(parts)

        reader = asyncio.create_task(read_sse())
        await llm.started.wait()                      # 回合真的挂起在 LLM 里
        steer_resp = await client.post('/steer', json={'message': '停一下改方向', 'sid': 'web'})
        assert steer_resp.status_code == 200
        assert steer_resp.json()['queued'] == 'next-step'
        llm.release.set()                             # 放行：第一步完成，下一步轮到插队
        sse = await asyncio.wait_for(reader, timeout=10)

    # 插队回答沿原 SSE 流推回来（同回合，无第二个回合）
    assert '首轮回答' in sse
    assert '插队后回答' in sse
    assert sse.count('"type": "turn_end"') == 1       # 一个回合结束 = 插队没开新回合
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
