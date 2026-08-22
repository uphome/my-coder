"""架构测试：值层冻结、日志推导、inbox 重放、严格插值、工具循环、取消、持久化回放。"""

import asyncio
import json

import pytest

from agent import Agent
from hooks import Hooks, PreStepContext, RequestErrorContext
from inbox import Inbox
from llm import FakeLlm, LlmRequest, StreamChunk, build_payload, _delta_reasoning, _to_wire_messages
from main import build_tools
from persistence import load_events, save_event
from prompt import PromptRegistry
from session import Session
from tools import ToolOutcome, ToolRegistry, ToolSpec
from values import (
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
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
    registry = build_tools()
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
    registry = build_tools()
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
    registry = build_tools()
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
    registry = build_tools()
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
    registry = build_tools()
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
