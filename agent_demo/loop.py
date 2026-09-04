"""循环层：turn/step 两级循环 + 工具分组执行。

step 内层 while(true)：组请求 → 流式（每 chunk 落 assistant/chunk 日志，
思维链另落 assistant/reasoning/chunk 痕迹日志）→ 组装消息（落
assistant/message）→ 有工具调用就执行（结果落 tool/result 表面日志）→
再调模型，直到纯文本。工具结果直接进日志，下一步请求的 derive_messages
自动带上它们——不需要另存一份对话状态。思维链只作为痕迹数据，不回灌。
"""
from __future__ import annotations

import asyncio
import json
import logging

from .hooks import PreStepContext, RequestContext, RequestErrorContext
from .llm import LlmError, LlmRequest, StreamChunk
from .registry import ToolOutcome
from .values import (
    Message,
    TextBlock,
    ToolCallBlock,
    create_assistant_message,
    create_tool_result_message,
)

log = logging.getLogger('loop')


class _BlockAssembler:
    """把流 chunk 折叠成最终 blocks：text 拼接 + 工具调用按 index 增量组装。

    思维链（reasoning）也在这里累积，但**不会**进入 blocks / assistant message，
    只作为痕迹数据由循环层单独落日志。

    为什么需要它：API 的流式响应把一条消息撕成几十个碎片——
    text 是逐字增量，工具调用按 index 分片（id/name 只出现一次，
    arguments 分散在多帧）。组装器负责把碎片重新折叠成
    create_assistant_message 需要的完整 blocks。
    """

    def __init__(self) -> None:
        self.text = ''
        self.reasoning = ''
        self.finish_reason: str | None = None
        self.usage: dict | None = None
        self._tool_calls: dict[int, dict] = {}  # index -> {'id','name','arguments'} 增量累积

    def push(self, chunk: StreamChunk) -> None:
        """吞一个流帧：text/reasoning 直接拼接，工具调用按 index 累积（arguments 是 += 不是 =）。"""
        self.text += chunk.text
        self.reasoning += chunk.reasoning
        if chunk.finish_reason:
            self.finish_reason = chunk.finish_reason
        if chunk.usage:
            self.usage = chunk.usage
        for delta in chunk.tool_calls:
            call = self._tool_calls.setdefault(delta.index, {'id': '', 'name': '', 'arguments': ''})
            if delta.id:
                call['id'] = delta.id
            if delta.name:
                call['name'] = delta.name
            if delta.arguments:
                call['arguments'] += delta.arguments

    def blocks(self) -> list:
        """输出最终 blocks：text（非空才有）+ 按 index 排序的工具调用。"""
        blocks: list = []
        if self.text:
            blocks.append(TextBlock(text=self.text))
        for index in sorted(self._tool_calls):
            call = self._tool_calls[index]
            blocks.append(ToolCallBlock(id=call['id'], name=call['name'], arguments=call['arguments']))
        return blocks


async def run_turn(agent) -> bool:
    """一个 turn：turn/start → [pre_step + step] 循环 → turn/end。返回是否还有下一回合。

    外层循环的语义：
    - 第 1 步认领 next-turn（一条普通输入），后续步认领 next-step（插队消息）
    - claim 为空：首步直接完成（没活干），后续步直接收尾
    - pre_step 返回 None：本回合 blocked（钩子拒绝）
    - next-step 空了就收尾，否则继续下一步

    五种结束原因（reason）：completed / blocked / aborted / error / max-tokens。
    aborted 和 error 记完账后必须重新抛——不能吞掉取消和架构性失败。
    返回 agent.inbox.has_pending：还有排队输入就再来一个 turn。
    """
    session = agent.session
    turn = agent._last_turn + 1
    session.append('turn/start', {'turn': turn})
    agent._last_turn = turn
    assembly = agent.prompt.assemble({'agent': agent})  # 每回合求值一次提示词快照
    end_reason = 'completed'
    try:
        step = 0
        while True:
            step += 1
            target = 'next-turn' if step == 1 else 'next-step'
            claimed = agent.inbox.claim(target, turn)
            messages = await _resolve_pre_step(agent, turn, step, claimed)
            if messages is None:
                end_reason = 'blocked'
                break
            if not messages:
                # 认领为空：首步不花模型调用直接完成，后续步直接收尾
                end_reason = 'completed'
                break
            session.append('step/start', {'turn': turn, 'step': step})
            for message in messages:
                # 认领到的消息在此刻浮上水面：从队列载荷变成模型记忆
                session.append('user/message', message, surface_op='append')
            end_reason = await _run_step(agent, turn, step, assembly)
            session.append('step/end', {'turn': turn, 'step': step})
            if not agent.inbox.next_step:
                break
    except asyncio.CancelledError:
        session.append('turn/end', {'turn': turn, 'reason': 'aborted'})
        raise
    except LlmError as failure:
        session.append('turn/end', {
            'turn': turn, 'reason': 'error',
            'code': failure.code, 'message': failure.message,
        })
        raise
    session.append('turn/end', {'turn': turn, 'reason': end_reason})
    return agent.inbox.has_pending


async def _resolve_pre_step(agent, turn: int, step: int, claimed: list[Message]):
    """pre_step 钩子：默认直接放行认领到的消息；钩子可改写或返回 None 拒绝。"""
    ctx = PreStepContext(turn=turn, step=step, messages=tuple(claimed))

    async def default():
        return list(claimed)

    if agent.hooks.pre_step is None:
        return await default()
    return await agent.hooks.pre_step(ctx, default)


async def _run_step(agent, turn: int, step: int, assembly: dict) -> str:
    """一个 step：内层 while，直到模型给出纯文本。返回结束原因。

    循环体内的关键机制：
    - 组请求：system 用本回合的提示词快照，messages 是此刻日志折叠出的记忆，
      tools 是全部工具 schema——三者都从投影来，不存第二份状态
    - request/header 落日志：含 system 全文和工具名，resume 恢复路由靠它
    - 流式：每个 chunk 都落 assistant/chunk 痕迹日志，再喂给组装器
    - request_error 钩子：返回 'retry' 就 continue 重新组请求
    - 工具结果只落日志；回到 while 顶部，derive_messages 自动带上结果——
      循环不需要"把结果发给模型"的代码
    """
    session = agent.session
    while True:
        config = await _resolve_request(agent, turn, step)
        header = session.request_header()
        # 三级 fallback：request 钩子 > agent.options > 上次 request/header（resume 恢复）
        provider = config.get('provider') or agent.options.get('provider') or (header or {}).get('provider', '')
        model = config.get('model') or agent.options.get('model') or (header or {}).get('model', '')
        if not provider or not model:
            raise RuntimeError('agent has no provider/model: set options or supply both via the request hook')
        request = LlmRequest(
            provider=provider,
            model=model,
            system=agent.prompt.render(assembly),
            messages=tuple(session.derive_messages()),
            tools=tuple(agent.tools.schemas()),
            max_tokens=config.get('max_tokens') or agent.options.get('max_tokens'),
        )
        session.append('request/header', {
            'provider': request.provider,
            'model': request.model,
            'system': request.system,
            'tools': [tool['name'] for tool in request.tools],
        })
        assembler = _BlockAssembler()
        try:
            async for chunk in agent.llm.stream(request):
                # 纯思维链帧只落 reasoning 痕迹，不产生空的 assistant/chunk。
                if chunk.text or chunk.tool_calls or chunk.finish_reason or chunk.usage:
                    session.append('assistant/chunk', {
                        'turn': turn, 'step': step, 'chunk': _chunk_to_data(chunk),
                    })
                if chunk.reasoning:
                    session.append('assistant/reasoning/chunk', {
                        'turn': turn, 'step': step, 'reasoning': chunk.reasoning,
                    })
                assembler.push(chunk)
        except LlmError as failure:
            action = 'throw'
            if agent.hooks.request_error is not None:
                action = await agent.hooks.request_error(RequestErrorContext(
                    turn=turn, step=step, code=failure.code, message=failure.message,
                ))
            if action == 'retry':
                continue
            raise

        if assembler.reasoning:
            session.append('assistant/reasoning', {
                'turn': turn,
                'step': step,
                'reasoning': assembler.reasoning,
            })

        message = create_assistant_message(
            assembler.blocks(), provider=request.provider, model=request.model,
        )
        session.append('assistant/message', {
            'turn': turn,
            'step': step,
            'message': message,
            **({'usage': assembler.usage} if assembler.usage else {}),
        }, surface_op='append')

        if assembler.finish_reason == 'length':
            # 输出被 max_tokens 截断。demo 到此收尾（记 max-tokens）；
            # 续写粘性（自动继续）是进化阶段的课题。
            return 'max-tokens'
        tool_calls = [block for block in message.content if isinstance(block, ToolCallBlock)]
        if not tool_calls:
            return 'completed'
        await _execute_tool_calls(agent, turn, step, tool_calls)
        # 结果已落 tool/result 日志；回到 while 顶部，derive_messages 自动带上


async def _resolve_request(agent, turn: int, step: int) -> dict:
    """request 钩子：默认返回 options 快照；钩子可改写 provider/model/max_tokens。"""
    default = {
        'provider': agent.options.get('provider', ''),
        'model': agent.options.get('model', ''),
        'max_tokens': agent.options.get('max_tokens'),
    }
    if agent.hooks.request is None:
        return default
    return await agent.hooks.request(RequestContext(turn=turn, step=step), default)


async def _confirm_approval(agent, name: str, arguments: dict) -> bool:
    """approval 确认：默认 CLI 交互（stdin），hooks.approval 可注入替换。

    fail-safe：EOF（stdin 关闭）/非交互输入都视为拒绝——只有用户明确输入
    y/yes 才放行。取消（CancelledError）不在这里吞，沿 await 链传播。
    """
    if agent.hooks.approval is not None:
        return await agent.hooks.approval(name, arguments)
    print(f'[approval] {name}({json.dumps(arguments, ensure_ascii=False)})? [y/N] ', end='', flush=True)
    try:
        answer = await asyncio.to_thread(input)
    except EOFError:
        return False
    return answer.strip().lower() in ('y', 'yes')


async def _execute_tool_calls(agent, turn: int, step: int, tool_calls: list[ToolCallBlock]) -> bool:
    """按工具声明的模式分组执行：parallel 一次全发，sequential 逐个。

    分组策略：看批次第一个工具的模式——parallel 就把整批一起跑
    （asyncio.gather 并发），sequential 就只跑第一个，剩下的等下一轮
    模型决定。这样循环层不硬编码"全部并行"，模式是注册时的声明。
    """
    session = agent.session
    pending = list(tool_calls)
    while pending:
        first = pending[0]
        if agent.tools.mode(first.name) == 'parallel':
            group, pending = pending, []
        else:
            group, pending = pending[:1], pending[1:]
        aborted = await _run_group(agent, turn, step, group)
        if aborted:
            for call in pending:
                session.append('tool/skipped', {'call_id': call.id, 'name': call.name})
            break
    return False


async def _run_group(agent, turn: int, step: int, group: list[ToolCallBlock]) -> bool:
    """执行一组工具调用，四层兜底全部在这里闭环。

    先给组里每个调用落 tool/call 痕迹日志，然后并发（组内）执行。
    run_one 的四层兜底：
    1. arguments 坏 JSON → is_error 结果（连 execute 都不进）
    2. arguments 不是对象 → is_error 结果
    3. 工具执行抛异常 → 捕获降级成 is_error 结果
    4. 超时（tools.execute 内部 wait_for 兜底）→ is_error 结果

    唯一能传过这里的异常是 CancelledError（用户取消），
    它沿 asyncio.gather 的 await 链往上走，由 run_turn 记 aborted。
    """
    session = agent.session
    for call in group:
        session.append('tool/call', {'call_id': call.id, 'name': call.name, 'arguments': call.arguments})

    async def run_one(call: ToolCallBlock) -> None:
        try:
            arguments = json.loads(call.arguments) if call.arguments.strip() else {}
        except json.JSONDecodeError as error:
            message = create_tool_result_message(call.id, f'invalid JSON arguments: {error}', True)
            session.append('tool/result', message, surface_op='append')
            return
        if not isinstance(arguments, dict):
            message = create_tool_result_message(
                call.id, f'arguments must be a JSON object, got {type(arguments).__name__}', True,
            )
            session.append('tool/result', message, surface_op='append')
            return
        try:
            spec = agent.tools.get(call.name)
            if spec.requires_approval and not await _confirm_approval(agent, call.name, arguments):
                # 拒绝不是失败：落 tool/skipped 痕迹（审计） + tool/result surface
                # 事件（模型必须看到"没执行"，否则以为工具跑过了——模型可见 ⟺ 可重建）
                session.append('tool/skipped', {
                    'call_id': call.id, 'name': call.name, 'reason': 'not-approved',
                })
                message = create_tool_result_message(call.id, 'skipped: user did not approve', True)
                session.append('tool/result', message, surface_op='append')
                return
            outcome = await agent.tools.execute(call.name, arguments, agent)
        except Exception as error:  # noqa: BLE001 - 工具失败必须变成 is_error 结果，不能炸掉循环
            outcome = ToolOutcome(content=f'{type(error).__name__}: {error}', is_error=True)
        message = create_tool_result_message(call.id, outcome.content, outcome.is_error)
        session.append('tool/result', message, surface_op='append')

    if len(group) == 1:
        await run_one(group[0])
    else:
        await asyncio.gather(*(run_one(call) for call in group))
    return False


def _chunk_to_data(chunk: StreamChunk) -> dict:
    """流帧 → 日志用的纯数据形态（assistant/chunk 事件的 data）。"""
    return {
        'text': chunk.text,
        'tool_calls': [
            {'index': delta.index, 'id': delta.id, 'name': delta.name, 'arguments': delta.arguments}
            for delta in chunk.tool_calls
        ],
        'finish_reason': chunk.finish_reason,
        'usage': chunk.usage,
    }
