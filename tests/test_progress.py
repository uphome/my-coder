"""工具收敛（issue #2）：声明校验、无进展计数、软提示与收尾步。

验收标准逐条对 `docs/notes/proposed/feature/2026-09-30-tool-convergence.md`：
① `cacheable` 声明校验 ② 计数与变更清零 ③ 提示不刷屏、收尾只请求一次
④ 收尾步（请求不带工具面 + 收尾指令 + `turn/end` 记 `read-budget`） ⑤ 关掉开关回到原行为。

**全部零 API**：`FakeLlm` 按脚本喂"读一次再读一次"的序列，断言看**日志**与**真实请求**。
"""
from __future__ import annotations

import pytest

from my_coder.capability.llm import FakeLlm, LlmRequest
from my_coder.runtime.agent import Agent
from my_coder.state.progress import CLOSE, NUDGE, ProgressPolicy
from my_coder.state.prompt import PromptRegistry
from my_coder.state.registry import ToolRegistry, ToolSpec
from my_coder.state.session import Session
from my_coder.values.messages import ToolOutcome

NUDGE_TEXT = '已连续 {run} 次只读调用'
CLOSING_TEXT = '只读调查已达上限'


async def _noop(args, agent, signal):
    return ToolOutcome(content='ok')


def _spec(name: str, **kwargs) -> ToolSpec:
    return ToolSpec(name=name, description=f'{name}.', parameters={'type': 'object'},
                    execute=_noop, **kwargs)


# ---------------------------------------------------------------------------
# 一、声明：`ToolSpec.cacheable`
# ---------------------------------------------------------------------------


def test_cacheable_declaration_is_checked_at_registration():
    """声明字段自己较真：`cacheable='false'` 是真值；"要审批却号称只读"自相矛盾。"""
    registry = ToolRegistry()
    registry.register(_spec('read_ok', cacheable=True))
    assert registry.get('read_ok').cacheable is True

    with pytest.raises(ValueError, match='non-boolean cacheable'):
        registry.register(_spec('string_flag', cacheable='false'))
    with pytest.raises(ValueError, match='cannot be both cacheable and requires_approval'):
        registry.register(_spec('write_and_read', cacheable=True, requires_approval=True))


def test_cacheable_defaults_to_false():
    """没声明 = 会变更（fail-closed）：声明错了最多让收敛压力来得晚，反过来会逼停长任务。"""
    registry = ToolRegistry()
    registry.register(_spec('edit_like'))
    assert registry.get('edit_like').cacheable is False


# ---------------------------------------------------------------------------
# 二、计数：连续只读 / 变更清零
# ---------------------------------------------------------------------------


def test_readonly_run_counts_and_resets_on_a_change():
    policy = ProgressPolicy(nudge_at=3, close_at=10)
    assert [policy.note(True) for _ in range(2)] == [None, None]
    assert policy.note(False) is None               # 一次变更 → 清零
    assert policy.readonly_run == 0
    assert [policy.note(True) for _ in range(3)][-1] == NUDGE   # 重新数到阈值才提示


# ---------------------------------------------------------------------------
# 三、阈值：提示不刷屏、收尾只请求一次
# ---------------------------------------------------------------------------


def test_nudge_repeats_every_four_without_spamming():
    policy = ProgressPolicy(nudge_at=3, close_at=100)
    verdicts = [policy.note(True) for _ in range(8)]
    nudged_at = [index + 1 for index, verdict in enumerate(verdicts) if verdict == NUDGE]
    assert nudged_at == [3, 7], f'第 3 次与之后每 4 次提示，实际 {nudged_at}'


def test_close_is_requested_once_and_then_stays_closed():
    policy = ProgressPolicy(nudge_at=2, close_at=4)
    verdicts = [policy.note(True) for _ in range(6)]
    assert verdicts[3] == CLOSE
    assert verdicts[4:] == [None, None], '已经在收尾了，不再重复请求'
    assert policy.closing is True


def test_policy_can_be_switched_off_or_partially_configured():
    off = ProgressPolicy(enabled=False)
    assert [off.note(True) for _ in range(50)] == [None] * 50
    assert off.closing is False

    only_soft = ProgressPolicy(nudge_at=2, close_at=None)
    verdicts = [only_soft.note(True) for _ in range(20)]
    assert CLOSE not in verdicts and NUDGE in verdicts and only_soft.closing is False

    only_hard = ProgressPolicy(nudge_at=None, close_at=2)
    verdicts = [only_hard.note(True) for _ in range(4)]
    assert NUDGE not in verdicts and verdicts[1] == CLOSE


# ---------------------------------------------------------------------------
# 四、接进循环：软提示写进结果正文；收尾步不带工具面
# ---------------------------------------------------------------------------


class RecordingLlm(FakeLlm):
    """记下每次请求（工具面 + 消息），用来断言**模型真的看到了什么**。"""

    def __init__(self, script: list[dict]) -> None:
        super().__init__(script=script)
        self.requests: list[LlmRequest] = []

    async def stream(self, request: LlmRequest, signal=None):
        self.requests.append(request)
        async for chunk in super().stream(request, signal):
            yield chunk


def _read_script(times: int, *, then: str = 'done') -> list[dict]:
    """脚本：连续 `times` 次调用只读工具，最后一段文字收尾。"""
    script = [{
        'tool_calls': [{'id': f'c{index}', 'name': 'peek', 'arguments': '{}'}],
        'finish_reason': 'tool_calls',
    } for index in range(times)]
    script.append({'text': then, 'finish_reason': 'stop'})
    return script


def _agent(policy: ProgressPolicy, script: list[dict],
           calls: list[str] | None = None) -> tuple[Agent, Session, RecordingLlm]:
    """`calls` 收集真正**执行过**的工具名——用来证明收尾步里的调用没有执行。"""
    seen = calls if calls is not None else []

    async def peek(args, agent, signal):
        seen.append('peek')
        return ToolOutcome(content='ok')

    async def touch(args, agent, signal):
        seen.append('touch')
        return ToolOutcome(content='ok')

    tools = ToolRegistry()
    tools.register(ToolSpec(name='peek', description='peek.', parameters={'type': 'object'},
                            execute=peek, execution_mode='parallel', offload=True,
                            cacheable=True))
    tools.register(ToolSpec(name='touch', description='touch.', parameters={'type': 'object'},
                            execute=touch, execution_mode='sequential'))
    session = Session(id='progress')
    llm = RecordingLlm(script)
    agent = Agent(session=session, llm=llm, prompt=PromptRegistry(), tools=tools,
                  options={'provider': 'fake', 'model': 'fake-model'}, progress=policy)
    return agent, session, llm


def _results(session: Session) -> list[tuple[str, bool]]:
    """日志里每条 `tool/result` 的（正文, 是否出错）。"""
    out: list[tuple[str, bool]] = []
    for event in session.events:
        if event.type != 'tool/result':
            continue
        for block in event.data.content:
            out.append((block.content, block.is_error))
    return out


def _reasons(session: Session) -> list[str]:
    return [event.data.get('reason') for event in session.events if event.type == 'turn/end']


@pytest.mark.asyncio
async def test_nudge_is_written_into_that_tool_result():
    """软信号落在**那次工具结果的正文**里：进日志、就在模型刚拿到的输出里。"""
    policy = ProgressPolicy(nudge_at=2, close_at=100, nudge_text=NUDGE_TEXT)
    agent, session, _ = _agent(policy, _read_script(4))
    agent.followup('看看这个')
    await agent.when_idle()

    results = _results(session)
    assert len(results) == 4
    assert NUDGE_TEXT.format(run=2) not in results[0][0]
    assert NUDGE_TEXT.format(run=2) in results[1][0], '第 2 次只读时该提示'


@pytest.mark.asyncio
async def test_a_change_resets_and_no_nudge_appears():
    """中间做了一次变更（`touch`）→ 计数清零，后续只读不继承之前的次数。"""
    policy = ProgressPolicy(nudge_at=3, close_at=100, nudge_text=NUDGE_TEXT)
    script = [
        {'tool_calls': [{'id': 'r1', 'name': 'peek', 'arguments': '{}'}],
         'finish_reason': 'tool_calls'},
        {'tool_calls': [{'id': 'w1', 'name': 'touch', 'arguments': '{}'}],
         'finish_reason': 'tool_calls'},
        {'tool_calls': [{'id': 'r2', 'name': 'peek', 'arguments': '{}'}],
         'finish_reason': 'tool_calls'},
        {'text': 'done', 'finish_reason': 'stop'},
    ]
    agent, session, _ = _agent(policy, script)
    agent.followup('先看再改再看')
    await agent.when_idle()

    assert all(NUDGE_TEXT.format(run=3) not in text for text, _ in _results(session))
    assert policy.readonly_run == 1


@pytest.mark.asyncio
async def test_closing_step_has_no_tools_and_records_read_budget():
    """收尾步：请求**不带工具面** + 叠收尾指令 + `turn/end` 记 `read-budget`。"""
    policy = ProgressPolicy(nudge_at=2, close_at=3, nudge_text=NUDGE_TEXT,
                            closing_text=CLOSING_TEXT)
    executed: list[str] = []
    agent, session, llm = _agent(policy, _read_script(5), calls=executed)
    agent.followup('看一下')
    await agent.when_idle()

    tool_faces = [len(request.tools) for request in llm.requests]
    assert tool_faces[:3] == [2, 2, 2], f'收尾前应带工具面，实际 {tool_faces}'
    assert tool_faces[3] == 0, f'收尾步必须不带工具面，实际 {tool_faces}'
    closing = [message for message in llm.requests[3].messages
               if any(CLOSING_TEXT in getattr(block, 'text', '')
                      for block in getattr(message, 'content', ()))]
    assert closing, '收尾步必须告诉模型"这一步没有工具、直接给结论"'
    assert _reasons(session) == ['read-budget'], f'实际 {_reasons(session)}'
    # 审计：收尾步的指令原文要进 request/header（它与状态栏同性质：per-request 注入、不进日志，
    # 不记就答不出"这一步为什么没有工具面"）
    headers = [event.data for event in session.events if event.type == 'request/header']
    assert headers[3]['tools'] == [], headers[3]
    assert headers[3]['convergence'] == {'closing': True, 'instruction': CLOSING_TEXT}, \
        headers[3].get('convergence')
    assert 'convergence' not in headers[0], '不是收尾步就不该有这个字段'
    # 收尾步里模型仍然发了工具调用（脚本这么写的）→ **不执行**，补一条 is_error 结果。
    # 为什么不丢掉：wire 上 tool_calls 后面必须跟结果，否则整个会话之后都发不出去。
    assert executed == ['peek'] * 3, f'收尾步的调用不该被执行，实际执行 {executed}'
    results = _results(session)
    assert len(results) == 4 and results[3][1] is True, results
    assert 'disabled on this closing step' in results[3][0]


@pytest.mark.asyncio
async def test_disabled_policy_behaves_exactly_as_before():
    """关掉开关：没有提示、没有收尾步、reason 照旧（`completed`）。"""
    agent, session, llm = _agent(ProgressPolicy(enabled=False), _read_script(5))
    agent.followup('看一下')
    await agent.when_idle()

    assert all(len(request.tools) == 2 for request in llm.requests)
    assert all(NUDGE_TEXT.format(run=2) not in text for text, _ in _results(session))
    assert _reasons(session) == ['completed']
    assert len(_results(session)) == 5
