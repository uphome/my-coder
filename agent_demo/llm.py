"""能力层：LLM 客户端——OpenAI 兼容流式 + 可脚本化假模型。

LlmError 是结构化的模型失败（code + message），循环的 request_error
钩子按它决定 retry 或放弃——错误策略与循环本体解耦。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx


class LlmError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LlmRequest:
    provider: str = ''
    model: str = ''
    system: str = ''
    messages: tuple = ()
    tools: tuple = ()
    max_tokens: int | None = None


@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    id: str | None = None
    name: str | None = None
    arguments: str = ''


@dataclass(frozen=True)
class StreamChunk:
    """一次流增量：text 增量 / 思维链增量 / 工具调用片段 / 结束原因 / usage。"""
    text: str = ''
    reasoning: str = ''
    tool_calls: tuple[ToolCallDelta, ...] = ()
    finish_reason: str | None = None
    usage: dict | None = None


class OpenAiCompatibleLlm:
    """httpx 直连 /chat/completions 的 SSE 流式客户端（DeepSeek 官方 API 兼容）。"""

    def __init__(self, base_url: str, api_key: str, model: str, provider: str = 'openai-compatible') -> None:
        self._base_url = base_url.rstrip('/')
        self._api_key = api_key
        self.model = model
        self.provider = provider
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))

    async def stream(self, request: LlmRequest, signal=None) -> AsyncIterator[StreamChunk]:
        payload = build_payload(request, self.model)
        try:
            async with self._client.stream(
                'POST',
                f'{self._base_url}/chat/completions',
                headers={'Authorization': f'Bearer {self._api_key}'},
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode(errors='replace')
                    raise LlmError('HTTP_ERROR', f'{response.status_code}: {body[:500]}')
                index_by_id: dict[int, str] = {}
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        break
                    try:
                        frame = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = frame.get('choices') or []
                    if not choices:
                        continue
                    delta = choices[0].get('delta') or {}
                    yield StreamChunk(
                        text=delta.get('content') or '',
                        reasoning=_delta_reasoning(delta),
                        tool_calls=tuple(_tool_call_deltas(delta.get('tool_calls'), index_by_id)),
                        finish_reason=choices[0].get('finish_reason'),
                        usage=frame.get('usage'),
                    )
        except httpx.HTTPError as error:
            raise LlmError('TRANSPORT', str(error)) from error

    async def aclose(self) -> None:
        await self._client.aclose()


def build_payload(request: LlmRequest, default_model: str) -> dict:
    """纯函数构造 wire payload——OpenAI/DeepSeek 的 tools 需要 function 包装格式。"""
    payload = {
        'model': request.model or default_model,
        'messages': _to_wire_messages(request.system, request.messages),
        'stream': True,
    }
    if request.tools:
        payload['tools'] = [
            {
                'type': 'function',
                'function': {
                    'name': tool['name'],
                    'description': tool['description'],
                    'parameters': tool['parameters'],
                },
            }
            for tool in request.tools
        ]
        payload['tool_choice'] = 'auto'
    if request.max_tokens:
        payload['max_tokens'] = request.max_tokens
    return payload


def _delta_reasoning(delta: dict) -> str:
    """统一提取各家流式接口里的思维链字段。

    目前兼容三种常见命名：
    - reasoning_content（DeepSeek 等）
    - reasoning（部分 OpenAI 兼容服务）
    - thinking（部分模型/代理使用）
    """
    for key in ('reasoning_content', 'reasoning', 'thinking'):
        value = delta.get(key)
        if value:
            return value
    return ''


def _tool_call_deltas(fragments, index_by_id: dict[int, str]) -> list[ToolCallDelta]:
    if not fragments:
        return []
    out = []
    for fragment in fragments:
        index = fragment.get('index', 0)
        if fragment.get('id') and index not in index_by_id:
            index_by_id[index] = fragment['id']
        function = fragment.get('function') or {}
        out.append(ToolCallDelta(
            index=index,
            id=fragment.get('id') or index_by_id.get(index),
            name=function.get('name'),
            arguments=function.get('arguments') or '',
        ))
    return out


def _text_of(message) -> str:
    return ''.join(block.text for block in message.content if block.type == 'text')


def _assistant_to_wire(message) -> dict:
    tool_calls = [block for block in message.content if block.type == 'tool-call']
    if tool_calls:
        return {
            'role': 'assistant',
            'content': None,
            'tool_calls': [
                {'id': block.id, 'type': 'function', 'function': {'name': block.name, 'arguments': block.arguments}}
                for block in tool_calls
            ],
        }
    return {'role': 'assistant', 'content': _text_of(message)}


def _user_to_wire(message) -> list[dict]:
    results = [block for block in message.content if block.type == 'tool-result']
    if results and len(results) == len(message.content):
        return [
            {'role': 'tool', 'tool_call_id': block.tool_call_id, 'content': block.content}
            for block in results
        ]
    return [{'role': 'user', 'content': _text_of(message)}]


def _to_wire_messages(system: str, messages) -> list[dict]:
    wire: list[dict] = []
    if system:
        wire.append({'role': 'system', 'content': system})
    for message in messages:
        if message.role == 'assistant':
            wire.append(_assistant_to_wire(message))
        elif message.role == 'user':
            wire.extend(_user_to_wire(message))
        else:
            wire.append({'role': message.role, 'content': _text_of(message)})
    return wire


class FakeLlm:
    """按脚本逐次应答：每次 stream 调用消费一个脚本步，离线演示架构。"""

    def __init__(self, script: list[dict], provider: str = 'fake', model: str = 'fake-model') -> None:
        self._script = list(script)
        self.provider = provider
        self.model = model

    async def stream(self, request: LlmRequest, signal=None) -> AsyncIterator[StreamChunk]:
        step: dict = self._script.pop(0) if self._script else {'text': '', 'finish_reason': 'stop'}
        if 'error' in step:
            error = step['error']
            raise LlmError(error['code'], error['message'])
        reasoning = step.get('reasoning', '')
        if reasoning:
            mid = len(reasoning) // 2 or 1
            yield StreamChunk(reasoning=reasoning[:mid])
            yield StreamChunk(reasoning=reasoning[mid:])
        text = step.get('text', '')
        if text:
            mid = len(text) // 2 or 1
            yield StreamChunk(text=text[:mid])
            yield StreamChunk(text=text[mid:])
        for index, call in enumerate(step.get('tool_calls', [])):
            yield StreamChunk(tool_calls=(ToolCallDelta(
                index=index,
                id=call['id'],
                name=call['name'],
                arguments=call['arguments'],
            ),))
        yield StreamChunk(finish_reason=step.get('finish_reason', 'stop'))
