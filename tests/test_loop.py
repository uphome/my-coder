"""框架循环：turn/step 粒度、三个钩子、取消、工具并发调度与 steer 插队。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**断言与用例体一字未改**；
唯一差异是 26 处函数内冗余 import 被 ruff 的 F401/F811 删掉——那是拆分暴露出来的旧问题。）
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from conftest import make_agent

from agent_demo.capability.hooks import Hooks, PreStepContext, RequestErrorContext
from agent_demo.capability.llm import FakeLlm, StreamChunk
from agent_demo.runtime import loop as loop_module
from agent_demo.runtime.agent import Agent
from agent_demo.state.prompt import PromptRegistry
from agent_demo.state.registry import ToolRegistry, ToolSpec
from agent_demo.state.runtime_status import RuntimeStatusRegistry
from agent_demo.state.session import Session
from agent_demo.values.messages import (
    TextBlock,
    ToolCallBlock,
    ToolOutcome,
    ToolResultBlock,
    create_user_message,
)


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


# ---------------------------------------------------------------------------
# 工具并发调度：fail-closed 默认 / 连续段屏障 / 有序提交 / 池上限 / 卸载 / 取消补记账
# 对齐 harness core/agent-loop/src/tool-calls.ts（普通说明见 loop._run_group 的注释）
# ---------------------------------------------------------------------------


def _spec(name, execute, *, execution_mode='sequential', offload=False,
          requires_approval=False, timeout_s=60.0) -> ToolSpec:
    """测试用最小注册：properties 留空 → 参数校验只兜底 required（这里没有）。"""
    return ToolSpec(
        name=name, description=f'{name} tool.', parameters={'type': 'object'},
        execute=execute, execution_mode=execution_mode, offload=offload,
        requires_approval=requires_approval, timeout_s=timeout_s,
    )


def _tool_agent(script, tools, session=None, hooks=None):
    """带自定义工具表的 agent（make_agent 固定用空 ToolRegistry，这里要注册工具）。"""
    session = session if session is not None else Session(id='s')
    return Agent(
        session=session, llm=FakeLlm(script=script), prompt=PromptRegistry(), tools=tools,
        options={'provider': 'fake', 'model': 'fake-model'}, hooks=hooks,
    ), session


def _result_call_ids(session) -> list[str]:
    """tool/result 事件的落盘顺序（按 call_id）。"""
    return [event.data.source.call_id for event in session.events if event.type == 'tool/result']


def test_execution_mode_is_fail_closed_by_default():
    """没声明执行模式 = 不许并发；拼错的模式值当场炸（宁炸勿静默）。"""
    async def noop(args, agent, signal):
        return ToolOutcome(content='ok')

    registry = ToolRegistry()
    registry.register(_spec('undeclared', noop))
    assert registry.mode('undeclared') == 'sequential'

    registry.register(_spec('declared', noop, execution_mode='parallel'))
    assert registry.mode('declared') == 'parallel'

    # 未注册的工具名也按 fail-closed 当独占：分组阶段不许抛错，
    # 否则它会在执行之前炸掉整个回合（失败必须留给执行阶段降级成结果）
    assert registry.mode('never-registered') == 'sequential'

    with pytest.raises(ValueError):
        registry.register(_spec('typo', noop, execution_mode='paralell'))


async def test_unknown_tool_name_degrades_to_a_result():
    """模型幻觉出不存在的工具：整回合照常收尾，模型拿到一条 is_error 结果。

    旧实现在分组阶段就 `mode()` → KeyError 炸出 run_turn：日志里连 turn/end 都没有，
    只留下"请求了工具却没有结果"的 assistant 消息（wire 非法），模型什么都看不到。
    """
    tools = ToolRegistry()          # 空注册表：模型点的名字必然不存在
    agent, session = _tool_agent([
        {'tool_calls': [{'id': 'c1', 'name': 'no_such_tool', 'arguments': '{}'}],
         'finish_reason': 'tool_calls'},
        {'text': 'adapted', 'finish_reason': 'stop'},
    ], tools)
    agent.followup('call a tool that does not exist')
    await agent.when_idle()

    assert session.events[-1].type == 'turn/end'
    assert session.events[-1].data['reason'] == 'completed'
    results = [block for message in session.derive_messages() for block in message.content
               if isinstance(block, ToolResultBlock)]
    assert len(results) == 1
    assert results[0].tool_call_id == 'c1'
    assert results[0].is_error and 'not registered' in results[0].content


async def test_parallel_group_stops_at_a_sequential_barrier():
    """[parallel, sequential] 不共池：独占工具必须等并发段排空，且仍在同一步里跑完。"""
    timeline = []

    async def slow(args, agent, signal):
        timeline.append('slow:start')
        await asyncio.sleep(0.05)
        timeline.append('slow:end')
        return ToolOutcome(content='slow')

    async def exclusive(args, agent, signal):
        timeline.append('exclusive:start')
        await asyncio.sleep(0.01)
        timeline.append('exclusive:end')
        return ToolOutcome(content='exclusive')

    tools = ToolRegistry()
    tools.register(_spec('slow', slow, execution_mode='parallel'))
    tools.register(_spec('exclusive', exclusive))
    agent, session = _tool_agent([
        {'tool_calls': [
            {'id': 'c1', 'name': 'slow', 'arguments': '{}'},
            {'id': 'c2', 'name': 'exclusive', 'arguments': '{}'},
        ], 'finish_reason': 'tool_calls'},
        {'text': 'done', 'finish_reason': 'stop'},
    ], tools)
    agent.followup('go')
    await agent.when_idle()

    assert timeline == ['slow:start', 'slow:end', 'exclusive:start', 'exclusive:end']
    assert _result_call_ids(session) == ['c1', 'c2']
    assert [m.role for m in session.derive_messages()] == ['user', 'assistant', 'user', 'user', 'assistant']


async def test_parallel_pool_overlaps_and_commits_in_model_order():
    """两个并发工具真的重叠（总耗时 ≈ max 而不是 sum），但结果按模型顺序落盘。"""
    timeline = []

    async def first(args, agent, signal):
        timeline.append('first:start')
        await asyncio.sleep(0.08)
        timeline.append('first:end')
        return ToolOutcome(content='first')

    async def second(args, agent, signal):
        timeline.append('second:start')
        await asyncio.sleep(0.01)
        timeline.append('second:end')
        return ToolOutcome(content='second')

    tools = ToolRegistry()
    tools.register(_spec('first', first, execution_mode='parallel'))
    tools.register(_spec('second', second, execution_mode='parallel'))
    agent, session = _tool_agent([
        {'tool_calls': [
            {'id': 'c1', 'name': 'first', 'arguments': '{}'},
            {'id': 'c2', 'name': 'second', 'arguments': '{}'},
        ], 'finish_reason': 'tool_calls'},
        {'text': 'done', 'finish_reason': 'stop'},
    ], tools)

    started = time.perf_counter()
    agent.followup('go')
    await agent.when_idle()
    elapsed = time.perf_counter() - started

    assert timeline == ['first:start', 'second:start', 'second:end', 'first:end']  # 真并发
    assert elapsed < 0.15                                                        # 不是 0.08+0.01 串行
    assert _result_call_ids(session) == ['c1', 'c2']                             # 后完成的先落盘？不——按调用顺序


async def test_parallel_pool_is_bounded(monkeypatch):
    """池上限：模型一次吐 4 个并发调用，实际同时在跑的不超过 MAX_PARALLEL_TOOL_CALLS。"""
    monkeypatch.setattr(loop_module, 'MAX_PARALLEL_TOOL_CALLS', 2)
    active = 0
    peak = 0

    async def slow(args, agent, signal):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return ToolOutcome(content='ok')

    tools = ToolRegistry()
    tools.register(_spec('slow', slow, execution_mode='parallel'))
    agent, session = _tool_agent([
        {'tool_calls': [
            {'id': f'c{index}', 'name': 'slow', 'arguments': '{}'} for index in range(4)
        ], 'finish_reason': 'tool_calls'},
        {'text': 'done', 'finish_reason': 'stop'},
    ], tools)
    agent.followup('go')
    await agent.when_idle()

    assert peak == 2
    assert _result_call_ids(session) == ['c0', 'c1', 'c2', 'c3']


async def test_offload_keeps_the_loop_responsive_and_times_out():
    """offload 的两重收益：同步阻塞不再卡住事件循环；wait_for 的超时真的能到点。"""
    ticks = []

    async def blocking(args, agent, signal):
        time.sleep(0.3)          # 同步阻塞：不卸载的话整个事件循环停在这里
        return ToolOutcome(content='too late')

    registry = ToolRegistry()
    registry.register(_spec('blocking', blocking, execution_mode='parallel',
                            offload=True, timeout_s=0.05))

    async def heartbeat():
        while True:
            ticks.append(1)
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    outcome = await registry.execute('blocking', {}, None)
    beat.cancel()

    assert outcome.is_error and 'timed out' in outcome.content
    assert len(ticks) >= 3       # 循环没被卡住（未卸载时这里会是 0 次心跳）


async def test_approval_tools_never_run_concurrently():
    """sequential + requires_approval 严格逐个：审批提示不会并发弹出。"""
    active = 0
    peak = 0
    prompts = []

    async def guarded(args, agent, signal):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return ToolOutcome(content='done')

    async def approve(name, arguments):
        prompts.append(name)
        return True

    hooks = Hooks()
    hooks.approval = approve
    tools = ToolRegistry()
    tools.register(_spec('guarded', guarded, requires_approval=True))
    agent, session = _tool_agent([
        {'tool_calls': [
            {'id': 'c1', 'name': 'guarded', 'arguments': '{}'},
            {'id': 'c2', 'name': 'guarded', 'arguments': '{}'},
        ], 'finish_reason': 'tool_calls'},
        {'text': 'done', 'finish_reason': 'stop'},
    ], tools, hooks=hooks)
    agent.followup('go')
    await agent.when_idle()

    assert peak == 1
    assert prompts == ['guarded', 'guarded']
    assert _result_call_ids(session) == ['c1', 'c2']


async def test_cancel_mid_group_leaves_no_dangling_tool_call():
    """取消时已起跑的调用要补合成结果：模型记忆里不能留下没有结果的 tool_call。"""
    async def slow(args, agent, signal):
        await asyncio.sleep(5)
        return ToolOutcome(content='never')

    tools = ToolRegistry()
    tools.register(_spec('slow', slow, execution_mode='parallel'))
    agent, session = _tool_agent([
        {'tool_calls': [
            {'id': 'c1', 'name': 'slow', 'arguments': '{}'},
            {'id': 'c2', 'name': 'slow', 'arguments': '{}'},
        ], 'finish_reason': 'tool_calls'},
    ], tools)
    agent.followup('go')
    await asyncio.sleep(0.05)
    agent.cancel()
    await agent.when_idle()

    assert session.events[-1].data['reason'] == 'aborted'
    calls, results = set(), set()
    for message in session.derive_messages():
        for block in message.content:
            if isinstance(block, ToolCallBlock):
                calls.add(block.id)
            elif isinstance(block, ToolResultBlock):
                results.add(block.tool_call_id)
                assert block.is_error
    assert calls == {'c1', 'c2'}
    assert results == calls                                   # 不留悬空
    assert [e.type for e in session.events].count('tool/result') == 2   # 也没有重复补记


@pytest.mark.asyncio
async def test_steer_inserts_and_runs_as_next_turn():
    """steer 插队：消息进 next-step，回合结束后立即作为下一轮首条被消费。

    不苛求抓到 running 窗口（fake llm 太快）；重点是 next-step 里的消息
    绝不会丢——when_idle 收敛后 inbox.has_pending 为假、两条用户消息都
    进了模型记忆。
    """
    from agent_demo.runtime.agent import Agent
    from agent_demo.state.prompt import PromptRegistry
    from agent_demo.state.registry import ToolRegistry
    from agent_demo.state.session import Session

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
    from agent_demo.capability.llm import StreamChunk
    from agent_demo.runtime.agent import Agent
    from agent_demo.state.prompt import PromptRegistry
    from agent_demo.state.registry import ToolRegistry
    from agent_demo.state.session import Session

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
    from agent_demo.capability.llm import ToolCallDelta

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


# ---------------------------------------------------------------------------
# 运行时状态贡献者（issue #19）：注册制、按注册顺序贴尾、审计字段是映射
# ---------------------------------------------------------------------------

class _RecordingLlm:
    """记下每次请求收到的 user 文本，然后一句收尾（一次请求就结束回合）。"""

    def __init__(self):
        self.seen: list[list[str]] = []

    async def stream(self, request, signal=None):  # noqa: ARG002
        self.seen.append([m.content[0].text for m in request.messages
                          if m.role == 'user' and m.content
                          and getattr(m.content[0], 'type', '') == 'text'])
        yield StreamChunk(text='done', finish_reason='stop')


def _record_agent(session_id: str) -> tuple[Agent, _RecordingLlm]:
    llm = _RecordingLlm()
    agent = Agent(
        session=Session(id=session_id), llm=llm, prompt=PromptRegistry(),
        tools=ToolRegistry(), options={'provider': 'fake', 'model': 'fake-model'},
    )
    return agent, llm


@pytest.mark.asyncio
async def test_runtime_status_contributors_are_appended_and_audited():
    """非空贡献者各贴一条合成 user 消息（按注册顺序），审计字段是 `{名字: 原文}` 映射。

    这就是 issue #19 要的形状：循环只认"贡献者"这个概念，不认识 todo——
    所以加一个状态源不需要改 `loop.py`（由 `app/factory.py` 注册）。
    """
    agent, llm = _record_agent('rs')
    agent.runtime_status.register('alpha', lambda session: 'ALPHA 状态')
    agent.runtime_status.register('silent', lambda session: None)   # 无内容 → 不叠、不入审计
    agent.runtime_status.register('beta', lambda session: 'BETA 状态')

    agent.followup('做点事')
    await agent.when_idle()

    header = next(e.data for e in agent.session.events if e.type == 'request/header')
    assert header['runtime_status'] == {'alpha': 'ALPHA 状态', 'beta': 'BETA 状态'}
    # 贴在 derive_messages 之后（纯追加，历史不动），顺序 = 注册顺序
    assert llm.seen[0] == ['做点事', 'ALPHA 状态', 'BETA 状态'], llm.seen[0]
    # 状态不进日志、不进模型记忆（derive_messages 里没有它们）
    derived = [block.text for m in agent.session.derive_messages() for block in m.content
               if getattr(block, 'type', '') == 'text']
    assert not any('状态' in text for text in derived), derived


@pytest.mark.asyncio
async def test_runtime_status_is_collected_per_request():
    """每次请求现算：贡献者读到的是**当下**的会话投影（状态是"此刻的事实"，不是快照）。"""
    agent, _ = _record_agent('rs-per-request')
    agent.runtime_status.register('count', lambda session: f'事件数 {len(session.events)}')

    agent.followup('一')
    await agent.when_idle()
    agent.followup('二')
    await agent.when_idle()

    counts = [e.data['runtime_status']['count'] for e in agent.session.events
              if e.type == 'request/header']
    assert len(counts) == 2 and counts[0] != counts[1], counts


@pytest.mark.asyncio
async def test_runtime_status_contributor_failure_does_not_kill_the_turn(caplog):
    """贡献者抛异常：记 ERROR 日志并跳过它，回合照常跑完，审计只列真被告知的项。

    判据（见 `state/runtime_status.py` 的 `collect` docstring）：这条通道是**可选的状态
    展示**，坏了不该让整个回合作废——但也不能静默，日志里必须有名字。
    """
    import logging

    from agent_demo.state.runtime_status import RuntimeStatusRegistry as _Registry

    agent, llm = _record_agent('rs-broken')

    def boom(session):
        raise RuntimeError('贡献者自己坏了')

    agent.runtime_status = _Registry()
    agent.runtime_status.register('broken', boom)
    agent.runtime_status.register('good', lambda session: 'GOOD 状态')

    with caplog.at_level(logging.ERROR, logger='runtime_status'):
        agent.followup('还能干活吗')
        await agent.when_idle()

    header = next(e.data for e in agent.session.events if e.type == 'request/header')
    assert header['runtime_status'] == {'good': 'GOOD 状态'}      # 坏的那个不进审计
    assert llm.seen[0] == ['还能干活吗', 'GOOD 状态']              # 回合照常、好状态照叠
    assert any('broken' in record.getMessage() or 'broken' in str(record.args)
               for record in caplog.records), caplog.records


def test_runtime_status_registry_is_strict_and_unregisterable():
    """注册时刻严格校验（空名/重名当场抛错），`register` 返回注销函数。"""
    registry = RuntimeStatusRegistry()
    with pytest.raises(ValueError, match='must not be empty'):
        registry.register('', lambda session: 'x')
    registry.register('todo', lambda session: 'x')
    with pytest.raises(ValueError, match='already registered'):
        registry.register('todo', lambda session: 'y')
    assert registry.names == ('todo',)

    unregister = registry.register('other', lambda session: 'y')
    assert registry.names == ('todo', 'other')
    unregister()
    assert registry.names == ('todo',)


@pytest.mark.asyncio
async def test_agent_without_contributors_changes_nothing():
    """默认注册表为空：不叠消息、不带 `runtime_status` 字段（与加这条通道之前一致）。"""
    agent, llm = _record_agent('rs-empty')
    agent.followup('只有我一条')
    await agent.when_idle()

    header = next(e.data for e in agent.session.events if e.type == 'request/header')
    assert 'runtime_status' not in header
    assert llm.seen[0] == ['只有我一条']
