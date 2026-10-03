"""issue #42：流式帧**不落日志**——每 step 一条汇总，内容一条不少。

逐条对应 issue 的验收清单：

① 日志里 0 条 `assistant/chunk` / `assistant/reasoning/chunk`，每 step 恰好 **1 条** `assistant/stream`
② **内容等价可机械证明**：瞬时通道收到的帧拼接 == 落盘的正文 / 工具参数 / 思维链
③ 实时通道仍**逐帧**送达（打字机不丢）
④ v1（带帧）与 v2（不带帧）日志**都能读、都能渲染历史**（#4 的思维链显示不能因此回归）
⑤ 新类型标了 `ignorable` ⇒ **旧读取者跳过它而不是拒绝重建**（词汇增长不 bump 版本）
⑥ **行数与帧数解耦**：帧数翻 10 倍，日志行数不变
"""
from __future__ import annotations

import json

import pytest

from my_coder.capability.llm import StreamChunk, ToolCallDelta
from my_coder.runtime.agent import Agent
from my_coder.state.prompt import PromptRegistry
from my_coder.state.registry import ToolRegistry, ToolSpec
from my_coder.state.session import Session
from my_coder.values.messages import StreamFrame, ToolOutcome
from my_coder.values.persistence import load_events
from my_coder.web.payload import event_to_payload, history_payloads


class ManyFrameLlm:
    """发很多小帧的假模型：用来证明"行数与帧数解耦"。"""

    def __init__(self, *, text='答', reasoning='想', frames=4) -> None:
        self.provider = 'fake'
        self.model = 'fake-model'
        self._text, self._reasoning, self._frames = text, reasoning, frames

    async def stream(self, request, signal=None):
        for _ in range(self._frames):
            yield StreamChunk(reasoning=self._reasoning)
        for _ in range(self._frames):
            yield StreamChunk(text=self._text)
        yield StreamChunk(finish_reason='stop', usage={'prompt_tokens': 1, 'completion_tokens': 1})


def _agent(llm, tools=None) -> tuple[Agent, Session]:
    session = Session(id='stream')
    agent = Agent(session=session, llm=llm, prompt=PromptRegistry(),
                  tools=tools or ToolRegistry(),
                  options={'provider': 'fake', 'model': 'fake-model'})
    return agent, session


async def _run(agent: Agent, prompt: str = '说点什么') -> None:
    agent.followup(prompt)
    await agent.when_idle()


def _types(session: Session) -> list[str]:
    return [event.type for event in session.events]


def _stream_events(session: Session) -> list:
    return [event for event in session.events if event.type == 'assistant/stream']


# ---------------------------------------------------------------------------
# ① 帧不落日志、每 step 一条汇总
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frames_are_not_persisted_but_summarized():
    agent, session = _agent(ManyFrameLlm(frames=6))
    await _run(agent)

    types = _types(session)
    assert 'assistant/chunk' not in types, '流式帧不该再逐条落盘'
    assert 'assistant/reasoning/chunk' not in types
    streams = _stream_events(session)
    assert len(streams) == 1, f'每个 step 恰好一条汇总，实际 {len(streams)}'
    data = streams[0].data
    assert data['frames'] == 7      # 6 个正文帧 + 1 个带 finish_reason 的收尾帧
    assert data['reasoning_frames'] == 6
    assert data['text_chars'] == 6 and data['reasoning_chars'] == 6
    assert isinstance(data['ms'], int) and data['ms'] >= 0


@pytest.mark.asyncio
async def test_log_rows_do_not_grow_with_frame_count():
    """⑥ 帧数翻 10 倍，日志行数不变——这是 issue 的核心回归测试。"""
    few, few_session = _agent(ManyFrameLlm(frames=4))
    many, many_session = _agent(ManyFrameLlm(frames=40))
    await _run(few)
    await _run(many)

    assert len(many_session.events) == len(few_session.events), (
        f'帧数不该影响行数：4 帧 {len(few_session.events)} 行 vs 40 帧 {len(many_session.events)} 行')
    assert _stream_events(many_session)[0].data['frames'] > _stream_events(few_session)[0].data['frames']


# ---------------------------------------------------------------------------
# ② 内容等价（机械证明）+ ③ 实时逐帧
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frame_content_equals_persisted_content():
    """帧的内容必须**一条不少**地存在于落盘事件里（正文 / 工具参数 / 思维链）。"""
    executed: list[str] = []

    async def echo(args, agent, signal):
        executed.append(json.dumps(args, ensure_ascii=False))
        return ToolOutcome(content='ok')

    tools = ToolRegistry()
    tools.register(ToolSpec(name='peek', description='peek.', parameters={'type': 'object'},
                            execute=echo, execution_mode='parallel', offload=True,
                            cacheable=True))
    llm = ManyFrameLlm(text='答', reasoning='想', frames=3)
    agent, session = _agent(llm, tools)

    frames: list = []
    session.on_stream(frames.append)
    await _run(agent)

    text_deltas = ''.join(
        frame.data['chunk']['text'] for frame in frames if frame.type == 'assistant/chunk')
    reasoning_deltas = ''.join(
        frame.data['reasoning'] for frame in frames if frame.type == 'assistant/reasoning/chunk')

    messages = [event.data['message'] for event in session.events
                if event.type == 'assistant/message']
    persisted_text = ''.join(block.text for message in messages for block in message.content
                             if block.type == 'text')
    persisted_reasoning = ''.join(event.data['reasoning'] for event in session.events
                                  if event.type == 'assistant/reasoning')

    assert frames, '实时通道必须仍然逐帧送达（打字机不丢）'
    assert text_deltas == persisted_text, f'正文不等价：{text_deltas!r} vs {persisted_text!r}'
    assert reasoning_deltas == persisted_reasoning, '思维链不等价'


@pytest.mark.asyncio
async def test_tool_call_argument_frames_equal_persisted_arguments():
    """工具参数也是流式增量进来的——它的内容必须落在 `assistant/message` 的 tool_call 里。"""
    tools = ToolRegistry()
    tools.register(ToolSpec(name='peek', description='peek.', parameters={'type': 'object'},
                            execute=lambda args, agent, signal: _ok(), execution_mode='parallel',
                            offload=True, cacheable=True))
    script = [
        {'tool_calls': [{'id': 'c1', 'name': 'peek', 'arguments': '{"a": 1}'}],
         'finish_reason': 'tool_calls'},
        {'text': 'done', 'finish_reason': 'stop'},
    ]
    agent, session = _agent(ManyFrameLlm(), tools)
    agent.llm = _FakeLike(script)

    frames: list = []
    session.on_stream(frames.append)
    await _run(agent)

    arg_deltas = ''.join(
        delta['arguments']
        for frame in frames if frame.type == 'assistant/chunk'
        for delta in frame.data['chunk']['tool_calls'])
    persisted_args = ''.join(
        block.arguments
        for event in session.events if event.type == 'assistant/message'
        for block in event.data['message'].content if block.type == 'tool-call')
    assert arg_deltas == '{"a": 1}', f'帧里的参数增量：{arg_deltas!r}'
    assert persisted_args == arg_deltas, '工具参数不等价（丢参数就是丢事实）'


async def _ok():
    return ToolOutcome(content='ok')


class _FakeLike:
    """把一个脚本喂给 ManyFrameLlm 的形状（复用 FakeLlm 的脚本语义）。"""

    def __init__(self, script: list[dict]) -> None:
        self.provider = 'fake'
        self.model = 'fake-model'
        self._script = list(script)

    async def stream(self, request, signal=None):
        step = self._script.pop(0) if self._script else {'text': '', 'finish_reason': 'stop'}
        for call in step.get('tool_calls', []):
            yield StreamChunk(tool_calls=(ToolCallDelta(
                index=0, id=call['id'], name=call['name'], arguments=call['arguments']),))
        if step.get('text'):
            yield StreamChunk(text=step['text'])
        yield StreamChunk(finish_reason=step.get('finish_reason', 'stop'))


# ---------------------------------------------------------------------------
# ⑤ ignorable：旧读取者跳过新类型，而不是拒绝重建
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_event_is_ignorable_for_old_readers(tmp_path):
    agent, session = _agent(ManyFrameLlm(frames=2))
    path = tmp_path / 'stream.jsonl'
    session.bind_store(path)
    await _run(agent)

    names = [json.loads(line).get('type') for line in path.read_text(encoding='utf-8').splitlines()]
    assert 'assistant/stream' in names
    raw = next(json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
               if json.loads(line).get('type') == 'assistant/stream')
    assert raw.get('ignorable') is True, '词汇增长必须标 ignorable，否则旧读取者会拒绝重建'

    # 老读取者（词表里没有 assistant/stream）→ 跳过它，仍然重建成功
    old_types = frozenset(t for t in _known_types() if t != 'assistant/stream')
    events = load_events(path, known_types=old_types)
    assert events and all(event.type != 'assistant/stream' for event in events)


def _known_types() -> frozenset[str]:
    from my_coder.values.messages import KNOWN_SESSION_EVENT_TYPES
    return KNOWN_SESSION_EVENT_TYPES


# ---------------------------------------------------------------------------
# ④ v1（带帧）与 v2（不带帧）都能读、都能渲染历史
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_renders_both_v1_frames_and_v2_summary(tmp_path):
    """v2：没有帧，历史仍要有正文与思维链（**#4 的思维链显示不能回归**）。"""
    agent, session = _agent(ManyFrameLlm(text='答', reasoning='想', frames=3))
    await _run(agent)

    # 历史是按角色的行（不是按事件类型），思维链由 `assistant/reasoning` 折进 assistant 行
    payloads = history_payloads(session)
    assert [payload['role'] for payload in payloads] == ['user', 'assistant'], payloads
    assert payloads[0]['text'] == '说点什么'
    assert payloads[1]['text'] == '答' * 3      # 每帧一个 '答'，拼起来才是正文
    assert payloads[1]['reasoning'] == '想' * 3, '历史里的思维链不能因为帧不落盘而消失（#4）'

    # v1 旧日志的帧走的是另一条通道（`event_to_payload`，实时/回放）——形状不变
    legacy = StreamFrame(type='assistant/chunk',
                         data={'turn': 1, 'step': 1, 'chunk': {'text': '旧帧', 'tool_calls': []}})
    assert event_to_payload(legacy, session, None) == {
        'type': 'chunk', 'turn': 1, 'step': 1, 'text': '旧帧'}
