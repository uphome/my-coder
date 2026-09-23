"""llm 层：wire 双向翻译 + FakeLlm + 思维链（痕迹，不进模型记忆）。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**断言与用例体一字未改**；
唯一差异是 26 处函数内冗余 import 被 ruff 的 F401/F811 删掉——那是拆分暴露出来的旧问题。）
"""
from __future__ import annotations

import pytest
from conftest import make_agent

from my_coder.capability.llm import FakeLlm, LlmRequest, _delta_reasoning, build_payload
from my_coder.values.messages import (
    TextBlock,
    ToolCallBlock,
    create_assistant_message,
    create_tool_result_message,
    create_user_message,
)


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
