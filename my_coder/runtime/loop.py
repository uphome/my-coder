"""循环层：turn/step 两级循环 + 工具分组执行。

step 内层 while(true)：组请求 → 流式（**流帧不落日志**：走 `session.emit_stream` 喂实时
订阅者，每步只在末尾落一条 `assistant/stream` 汇总）→ 组装消息（落
assistant/message，正文与工具调用参数全文都在这里）→ 有工具调用就执行（结果落 tool/result
表面日志）→ 再调模型，直到纯文本。工具结果直接进日志，下一步请求的 derive_messages
自动带上它们——不需要另存一份对话状态。思维链全文落 `assistant/reasoning`，只作为痕迹
数据，不回灌。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from ..capability.hooks import PreStepContext, RequestContext, RequestErrorContext
from ..capability.llm import LlmError, LlmRequest, StreamChunk
from ..state.progress import NUDGE
from ..values.limits import ATTEMPT_PARTIAL_MAX_CHARS
from ..values.messages import (
    Message,
    TextBlock,
    ToolCallBlock,
    ToolOutcome,
    ToolResultBlock,
    create_assistant_message,
    create_tool_result_message,
    create_user_message,
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

    def partial_text(self, limit: int = ATTEMPT_PARTIAL_MAX_CHARS) -> str:
        """已经流出来的半截文本（**给失败/取消的尝试留档用**）。

        为什么留它：请求失败或被杀时，日志原先只剩半截 stream，事后答不出
        "它到哪一步才挂的"。截到 `limit` 就够判断了，记全文只会把日志撑大。
        """
        if len(self.text) <= limit:
            return self.text
        return self.text[:limit] + f'…(+{len(self.text) - limit} chars)'


async def run_turn(agent) -> bool:
    """一个 turn：turn/start → [pre_step + step] 循环 → turn/end。返回是否还有下一回合。

    **step 的粒度 = 一次模型请求**（对齐 harness：`core/agent-loop/src/agent.ts`
    的 `step()` 发完一次请求、执行完这次发起的工具调用就返回；工具循环由本函数
    的外层循环驱动，每轮回到顶部重新 claim inbox）。

    这个粒度是"插队能生效"的前提：steer 消息进 next-step 后，要等下一次 claim
    才浮上水面，而 claim 就发生在下面每次循环的开头。旧实现把**整段工具循环**
    算作一个 step（`_run_step` 内部 `while True` 反复模型↔工具），于是插队消息
    得等整段自主运行结束——长任务里是几分钟，甚至永不。实测：一条插队消息在
    next-step 里躺了 2 分 40 秒，最后被 cancel 清掉，模型从没见过它。

    外层循环的语义：
    - 第 1 步认领 next-turn（一条普通输入），后续步认领 next-step（插队消息）
    - `end_reason is None` = 上一步发起了工具调用，回合还没到收尾：即使这一步
      claim 为空也要继续发请求，把工具结果送回模型
    - 已收尾且没有新插队 → 结束；首步就 claim 为空 → 不花模型调用直接完成
    - pre_step 返回 None：本回合 blocked（钩子拒绝）
    - max-tokens 有粘性：某一步被截断后，后续步正常完成也不降级

    五种结束原因（reason）：completed / blocked / aborted / error / max-tokens。
    aborted 和 error 记完账后必须重新抛——不能吞掉取消和架构性失败。
    返回 agent.inbox.has_pending：还有排队输入就再来一个 turn。
    """
    session = agent.session
    turn = agent._last_turn + 1
    session.append('turn/start', {'turn': turn})
    agent._last_turn = turn
    assembly = agent.prompt.assemble({'agent': agent})  # 每回合求值一次提示词快照
    end_reason: str | None = None   # None = 还在工具循环里，回合尚未收尾
    try:
        step = 0
        target = 'next-turn'
        agent.progress.reset()      # 回合是收敛的自然边界：计数与收尾标志都从头来
        closing_step = False
        while True:
            step += 1
            # **取消点全部提到动队列之前**：pre_step 钩子与 request 钩子都可能 await，
            # 而"认领"会把消息从队列摘掉。旧顺序（claim → await pre_step）留下一个
            # 致命窗口：取消打在那儿，消息三处都不在（队列没了、surface 没有、日志也
            # 没有 canceled 标记）——用户原话静默消失。现在两个 await 都在 claim 前，
            # 取消时队列**一个字没动**，clear() 能正常把它标成 canceled。
            pending = agent.inbox.peek(target)
            messages = await _resolve_pre_step(agent, turn, step, pending)
            if messages is None:
                # 钩子拒绝：**也要把批次消费掉**。不消费的话消息还躺在队列里，
                # run_turn 返回 has_pending=True → 被动状态机立刻拿同一条消息再开
                # 一轮 → 无限空转（实测：测试 40s 不返回）。拒绝的语义是"这条不发给
                # 模型"，不是"留着下次再问一次"；消费动作无 await，仍然原子。
                agent.inbox.claim(target, turn)
                end_reason = 'blocked'
                break
            if end_reason is not None and not messages:
                break                      # 已收尾，且没有新插队 → 回合结束
            if step == 1 and not messages:
                end_reason = 'completed'   # 首步没货：不花模型调用
                break
            config = await _resolve_request(agent, turn, step)   # 取消点（仍在 claim 前）
            # 上一步的只读调查触到上限 → 这一步是**收尾步**：不带工具面 + 一条收尾指令。
            # 它仍是一次正常的模型请求（模型有完整的文字表达能力），所以不会产生
            # "带了 tool_calls 却没有结果"的非法 wire。
            closing_step = agent.progress.closing
            # ---- 下面到 user/message 落盘之间**没有 await**：认领与落盘一次性提交 ----
            # 注意 pre_step 允许"改写"：它可能返回一条全新 id 的消息（原来那条
            # 随之被 claim 摘掉）。这是钩子契约的一部分，不是异常——所以这里只做
            # 提交，不校验"返回的是不是刚认领的那几条"。
            agent.inbox.claim(target, turn)
            session.append('step/start', {'turn': turn, 'step': step})
            for message in messages:
                # 认领到的消息在此刻浮上水面：从队列载荷变成模型记忆
                session.append('user/message', message, surface_op='append')
            outcome = await _run_step(agent, turn, step, assembly, config=config,
                                      closing=closing_step)
            session.append('step/end', {'turn': turn, 'step': step})
            if end_reason != 'max-tokens':     # max-tokens 粘性：不降级
                end_reason = outcome
            if closing_step and end_reason != 'max-tokens':
                end_reason = 'read-budget'     # 收尾步走完 → 记下"因只读预算而收尾"
            if end_reason is not None and not agent.inbox.next_step:
                break                      # 收尾了、也没有插队 → 不再空转一步
            target = 'next-step'
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


async def _run_step(agent, turn: int, step: int, assembly: dict,
                    config: dict | None = None, closing: bool = False) -> str | None:
    """一个 step：**只发一次**模型请求（+ 它发起的工具调用）。返回结束原因。

    返回 None = 本步发起了工具调用、回合还没收尾：工具结果已落日志，
    下一次请求由 run_turn 的下一个 step 发出（那时 derive_messages 自动带上
    结果）。返回字符串 = 本步给这次回合定了性（completed / max-tokens）。

    为什么不在本函数里接着循环（旧实现的做法）：那样"一个 step"就变成整段
    工具循环，插队消息要等整段跑完才可能被 claim——实测能等 2 分 40 秒然后
    被 cancel 掉。一步 = 一次请求，claim 才有机会在每次请求前发生。

    内层 while 只服务一件事：request_error 钩子返回 'retry' 时重发本次请求。

    `config` 由 `run_turn` 在**认领之前**解析好传进来（这样才能把 request 与
    pre_step 两个 await 都挪到动队列之前，见 run_turn 的注释）；`None`（或重试）
    时本函数自己解析——重试要按当时的钩子重新取路由，不能用第一次的快照。

    循环体内的关键机制：
    - 组请求：system 用本回合的提示词快照，messages 是此刻日志折叠出的记忆，
      tools 是全部工具 schema——三者都从投影来，不存第二份状态
    - request/header 落日志：含 system 全文和工具名，resume 恢复路由靠它
    - 流式：帧**不落日志**（`emit_stream` 只喂实时订阅者），末尾落一条 `assistant/stream`
      汇总；内容完整性由 `assistant/message` + `assistant/reasoning` 保证（issue #42）
    - request_error 钩子：返回 'retry' 就 continue 重新组请求
    - 工具结果只落日志；下一个 step 组请求时 derive_messages 自动带上
    """
    session = agent.session
    # 第一次用调用方（run_turn）解析好的 config；之后（重试）自己重新解析——
    # 重试要按当时的钩子重新取路由，不能用失败前那份快照。
    attempt = config if config is not None else await _resolve_request(agent, turn, step)
    while True:
        config = attempt
        header = session.request_header()
        # 三级 fallback：request 钩子 > agent.options > 上次 request/header（resume 恢复）
        provider = config.get('provider') or agent.options.get('provider') or (header or {}).get('provider', '')
        model = config.get('model') or agent.options.get('model') or (header or {}).get('model', '')
        if not provider or not model:
            raise RuntimeError('agent has no provider/model: set options or supply both via the request hook')
        # 运行时状态（issue #19）：注册制贡献者——循环只知道"遍历 + 贴尾 + 记审计"，
        # 不认识任何具体状态源。每个非空状态各作为一条合成 user 消息叠在 messages 末尾：
        # 不进日志、不进 derive_messages（历史零污染），但可重建（贡献者都是日志投影的
        # 纯函数）；无内容时贡献者返回 None，循环一行都不多叠。
        # 它不是事件，但 request/header.runtime_status 会把 {名字: 原文} 记下来供审计。
        statuses = agent.runtime_status.collect(session)
        messages = list(session.derive_messages())
        for _, text in statuses:
            messages.append(create_user_message([TextBlock(text=text)]))
        if closing and agent.progress.closing_text:
            # 收尾指令与运行时状态同一通道（合成 user 消息、不进日志、可审计）：
            # 它是"这一步为什么没有工具面"的原因说明——原因必须和事实一起给模型
            messages.append(create_user_message([TextBlock(text=agent.progress.closing_text)]))
        request = LlmRequest(
            provider=provider,
            model=model,
            # system 每次请求渲染一次：live 段（instructions）在这里重新求值。
            # 它只在工作区指令文件真的变了的时候才变字节，所以前缀缓存照样命中；
            # 运行时状态在 messages 末尾（每步都变的东西不该进稳定前缀）。
            system=agent.prompt.render(assembly, ctx={'agent': agent}),
            messages=tuple(messages),
            # 收尾步（只读预算用尽）：这一次请求**不带工具面**，模型只能出文字。
            tools=() if closing else tuple(agent.tools.schemas()),
            max_tokens=config.get('max_tokens') or agent.options.get('max_tokens'),
        )
        session.append('request/header', {
            'turn': turn, 'step': step,   # 请求边界帧需要（前端按请求分块）
            'provider': request.provider,
            'model': request.model,
            'system': request.system,
            'tools': [tool['name'] for tool in request.tools],
            # 审计字段：本轮叠给模型的运行时状态原文（痕迹数据，不进模型上下文）。
            # 状态栏本身不落事件，靠这里回答"这轮模型被告知了哪些运行时状态"；
            # 多个来源时是一个映射 {贡献者名: 原文}，加来源不必加平铺字段。
            **({'runtime_status': dict(statuses)} if statuses else {}),
            # 审计字段：收尾步（只读预算用尽）的指令原文。它与状态栏同一性质——
            # 合成的 per-request 注入、不进日志，所以必须在这里留痕，否则
            # "这一步为什么没有工具面"在唯一事实源里答不出来。
            # （软提示不用记：它写在 tool/result 正文里，本身就在日志里。）
            **({'convergence': {'closing': True, 'instruction': agent.progress.closing_text}}
               if closing and agent.progress.closing_text else {}),
        })
        assembler = _BlockAssembler()
        # 流式帧的统计（issue #42）：帧本身**不落日志**，只发瞬时通道；
        # 这一步结束后落一条 `assistant/stream` 汇总。为什么改：实测 423,617 行日志里
        # 98.9% 是帧，而其中 98–99% 的字节是**逐行 JSON 固定开销**（一个 step 2,793 行 = 736 KB，
        # 内容只有 8.9 KB）；内容并没有丢——`_BlockAssembler` 就是用同一批 chunk 拼出
        # `assistant/message`（正文 + 工具调用参数）与 `assistant/reasoning`（思维链全文）的。
        stream_started = time.monotonic()
        frames = reasoning_frames = 0
        text_chars = reasoning_chars = 0
        try:
            async for chunk in agent.llm.stream(request):
                # 纯思维链帧只发 reasoning 帧，不产生空的 assistant/chunk。
                if chunk.text or chunk.tool_calls or chunk.finish_reason or chunk.usage:
                    session.emit_stream('assistant/chunk', {
                        'turn': turn, 'step': step, 'chunk': _chunk_to_data(chunk),
                    })
                    frames += 1
                    text_chars += len(chunk.text or '')
                if chunk.reasoning:
                    session.emit_stream('assistant/reasoning/chunk', {
                        'turn': turn, 'step': step, 'reasoning': chunk.reasoning,
                    })
                    reasoning_frames += 1
                    reasoning_chars += len(chunk.reasoning)
                assembler.push(chunk)
        except asyncio.CancelledError:
            # 取消是一类**尝试的结局**（不是成功）：留档后原样放行（不变式⑤）。
            # 不记的话日志里只剩半截 stream，事后答不出"这次请求怎么了"。
            _record_attempt(session, turn, step, request, 'cancelled',
                            partial=assembler.partial_text())
            raise
        except LlmError as failure:
            action = 'throw'
            if agent.hooks.request_error is not None:
                action = await agent.hooks.request_error(RequestErrorContext(
                    turn=turn, step=step, code=failure.code, message=failure.message,
                ))
            # 失败/重试都留档：`assistant/message` 只记成功的那次，失败的那次
            # 此前在日志里**什么都不留**——长任务事后答不出"它试了几次、为什么失败"。
            _record_attempt(session, turn, step, request, action, error=failure,
                            partial=assembler.partial_text())
            if action == 'retry':
                # 重发要重新解析路由（钩子可能已经改了 model/max_tokens），
                # 不能拿失败前那份快照再发一次
                attempt = await _resolve_request(agent, turn, step)
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

        # 流式汇总（issue #42）：每 step **一条**，替代原来每个流帧一条事件。
        # 落 `ignorable=True`：这是**词汇增长**（新类型），旧读取者不认识它可以跳过，
        # 所以按仓库规则**不 bump 格式版本**（结构性变化才 bump）。
        if frames or reasoning_frames:
            session.append('assistant/stream', {
                'turn': turn,
                'step': step,
                'frames': frames,
                'reasoning_frames': reasoning_frames,
                'text_chars': text_chars,
                'reasoning_chars': reasoning_chars,
                'ms': round((time.monotonic() - stream_started) * 1000),
            }, ignorable=True)

        if assembler.finish_reason == 'length':
            # 输出被 max_tokens 截断。demo 到此收尾（记 max-tokens）；
            # 续写粘性（自动继续）是进化阶段的课题。
            return 'max-tokens'
        tool_calls = [block for block in message.content if isinstance(block, ToolCallBlock)]
        if not tool_calls:
            return 'completed'
        if closing:
            # 收尾步里模型仍然调工具：不执行，补 is_error 结果（wire 必须成对）
            _reject_calls_on_closing_step(agent, turn, step, tool_calls)
        else:
            await _execute_tool_calls(agent, turn, step, tool_calls)
        # 工具结果已落 tool/result 日志。本 step 到此为止：回到 run_turn 的外层
        # 循环 → 下一个 step 的 claim 有机会吸收插队消息 → 再发下一次请求
        # （那时 derive_messages 自动带上工具结果，不需要"把结果发给模型"的代码）。
        return None


def _record_attempt(session, turn: int, step: int, request, outcome: str,
                    error: LlmError | None = None, partial: str = '') -> None:
    """给一次**没能成功结算**的模型请求留档（`assistant/attempt`）。

    为什么需要：`assistant/message` 只记"成功的那次"。失败、重试、取消此前在
    日志里**什么都不留**——长任务事后答不出"它试了几次、为什么失败"。
    对齐 DSH 的 `assistant/attempt`：每次尝试要么结算为 `assistant/message`
    （成功），要么结算为 `assistant/attempt`（失败/重试/取消/流错误）。

    **它不进模型历史**（不是 surface 事件，`derive_messages` 只认三类 surface）：
    留档是给人看的可观测性，不是给模型的上下文——失败的半截输出回灌给模型
    反而会污染它。

    已知代价：硬进程丢失（kill/断电）发生在结算之前时，**没有尝试可恢复**
    （DSH 同样如此）——那种情况下日志停在半截 stream 上。
    """
    payload = {
        'turn': turn,
        'step': step,
        'outcome': outcome,          # cancelled / retry / throw
        'provider': getattr(request, 'provider', ''),
        'model': getattr(request, 'model', ''),
    }
    if error is not None:
        payload['code'] = error.code
        payload['message'] = error.message
    if partial:
        payload['partial'] = partial
    session.append('assistant/attempt', payload)


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


# 取消时补的合成结果文案（对齐 harness appendSkippedToolCall 的措辞）。
# 两种情况分开写：还没起跑的调用"什么都没发生"，起跑后被取消的调用"可能已经
# 产生了副作用"——模型据此判断要不要重试，合并成一句话会丢掉这个区别。
ABORTED_BEFORE_DISPATCH = 'Error: tool call aborted before dispatch'
# 收尾步（只读预算用尽）里模型仍发工具调用时给它的合成结果：不执行，只说明为什么
CLOSED_STEP_TOOL_REFUSAL = ('Error: tool calls are disabled on this closing step. Answer in text: '
                          'what you confirmed, what is still open, and the next step you suggest.')
ABORTED_WHILE_RUNNING = 'Error: tool call aborted while running'

# 并发池上限（对齐 harness agentLoop.config 的 maxParallelToolCalls）：
# 模型一次吐十几个调用时不该把十几个同时打开——尤其 web_search 这种
# 一次调用就等于"一个完整模型轮次"的工具。池子腾出空位就补下一个。
MAX_PARALLEL_TOOL_CALLS = 4


async def _execute_tool_calls(agent, turn: int, step: int, tool_calls: list[ToolCallBlock]) -> None:
    """按工具声明的模式分组执行：并行段一次起跑，独占工具逐个跑。

    分组对齐 harness `core/agent-loop/src/tool-calls.ts` 的 `executeToolCalls`：
    - **每次都用当前队首重新分类**，不预先切分整批（注册表可能在执行期间变化）
    - 一组实际跑了几个由 `_run_group` 回传（harness 的 `consumed`）：并行段
      撞上非并发安全的调用会停在它前面，外层从那里**在同一步内**重新开始
    - 取消时把"已请求但没结果"的调用补齐（见 `_record_aborted_calls`），
      记账完成后原样放行 CancelledError（不变式 5）
    """
    session = agent.session
    pending = list(tool_calls)
    while pending:
        mode = agent.tools.mode(pending[0].name)
        group = pending if mode == 'parallel' else pending[:1]
        try:
            consumed = await _run_group(agent, turn, step, group, mode)
        except asyncio.CancelledError:
            _record_aborted_calls(session, turn, step, pending)
            raise
        pending = pending[consumed:]


async def _run_group(agent, turn: int, step: int, group: list[ToolCallBlock], mode: str) -> int:
    """跑一组工具调用：**并发起跑、按模型顺序落盘、取消不留悬空**。

    返回实际消费了几个调用（harness `runGroup` 的 `consumed`）。

    三个机制缺一不可：
    1. **自限池**（`fill`）：并行段里一旦冒出非并发安全的调用就停手，把它和
       后面的留给外层重新分类——所以 `[read_file, bash]` 不会被塞进同一个并发池
       （真实会话实测：69 个多调用批次里有 7 个是这种形状，旧实现全都会被并发
       调度）。池上限 `MAX_PARALLEL_TOOL_CALLS` 防止一次吐十几个调用全放出去。
    2. **按模型顺序提交**（`commit_ready`）：谁先跑完不等于谁先落盘——结果先进
       槽位，等队首连续就绪才 append（harness `commitReady` 的 contiguous slots）。
       日志顺序因此恒等于调用顺序：模型记忆确定、前缀缓存可复用、前端按 call_id
       配对也不用处理乱序。
    3. **取消补记账**：CancelledError 不是"整组作废"——已起跑却拿不到结果的调用
       要补一条 is_error 结果，否则日志里会留下"请求了工具却没有结果"的
       assistant 消息（wire 格式非法，恢复后下一轮请求直接 400）。

    单条调用的四层兜底（坏 JSON / 参数不是对象 / 抛异常 / 超时）全在 `_run_one`
    里降级成结果——它只**返回**结果、不落盘，落盘统一由这里按顺序做。这条分工
    还顺手解决了取消时"孤儿任务写不回日志"的问题：结果没经手就没人能乱写。
    """
    session = agent.session
    slots: list[Message | None] = [None] * len(group)
    running: dict[asyncio.Task[Message], int] = {}
    started = 0
    committed = 0

    def fill() -> None:
        """起跑尽量多的调用（受池上限约束）；并行段撞上独占调用就停手。"""
        nonlocal started
        while started < len(group) and len(running) < MAX_PARALLEL_TOOL_CALLS:
            call = group[started]
            # 屏障：组内第一条总是跑（它就是本组的模式），之后一旦出现非并发
            # 安全的调用就交给外层重新分类——"连续段"就是这么做出来的
            if started > 0 and mode == 'parallel' and agent.tools.mode(call.name) != 'parallel':
                break
            # tool/call 痕迹事件：turn/step 一并落日志，前端 SSE 靠 (turn,step)
            # 把工具调用挂到对应 assistant 节点（统一投影按节点定位，不猜"当前块"）
            session.append('tool/call', {
                'turn': turn, 'step': step,
                'call_id': call.id, 'name': call.name, 'arguments': call.arguments,
            })
            running[asyncio.create_task(_run_one(agent, call))] = started
            started += 1

    def commit_ready() -> None:
        """按模型顺序落盘：只有队首连续就绪的槽位能提交。"""
        nonlocal committed
        while committed < started:
            message = slots[committed]
            if message is None:
                break
            # 工具收敛（state/progress.py）：按**声明**统计"无进展只读"（`ToolSpec.cacheable`），
            # 到阈值就把提示追加到**这条结果的正文**——它进日志（可重建、可审计），
            # 而且就在模型刚拿到的那份输出里，比另起一条状态栏更贴近决策点。
            message = _apply_progress_nudge(agent, group[committed], message)
            session.append('tool/result', message, surface_op='append')
            committed += 1

    fill()
    try:
        while running:
            done, _ = await asyncio.wait(set(running), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                index = running.pop(task)
                slots[index] = (
                    _aborted_message(group[index], ABORTED_WHILE_RUNNING)
                    if task.cancelled() else task.result()
                )
            commit_ready()
            fill()
    except asyncio.CancelledError:
        # 取消：先 drain 已起跑的调用，再给拿不到结果的补合成结果
        for task in running:
            task.cancel()
        if running:
            # drain（harness 语义）：只等它们停下。挡住二次取消（用户连点两次
            # 停止 / 停止后又关会话）——它不能让下面的补记账被跳过
            try:
                await asyncio.gather(*running, return_exceptions=True)
            except asyncio.CancelledError:
                pass
            # 取消前就跑完的调用，认它的真结果——drain 的意义就在这里
            for task, index in running.items():
                if not task.cancelled() and task.exception() is None:
                    slots[index] = task.result()
        for index in range(started):
            if slots[index] is None:
                slots[index] = _aborted_message(group[index], ABORTED_WHILE_RUNNING)
        commit_ready()
        raise
    return started


async def _run_one(agent, call: ToolCallBlock) -> Message:
    """跑一个工具调用，**只返回结果消息**（落盘由 `_run_group` 按模型顺序统一做）。

    四层兜底都在这里闭环：
    1. arguments 坏 JSON → is_error 结果（连 execute 都不进）
    2. arguments 不是对象 → is_error 结果
    3. 工具执行抛异常 → 捕获降级成 is_error 结果
    4. 超时（tools.execute 内部 wait_for 兜底）→ is_error 结果

    唯一穿得过去的是 CancelledError（用户取消）：它沿 await 链向上走，
    由 `_run_group` 补记账、`run_turn` 记 aborted。
    """
    try:
        arguments = json.loads(call.arguments) if call.arguments.strip() else {}
    except json.JSONDecodeError as error:
        return create_tool_result_message(call.id, f'invalid JSON arguments: {error}', True)
    if not isinstance(arguments, dict):
        return create_tool_result_message(
            call.id, f'arguments must be a JSON object, got {type(arguments).__name__}', True,
        )
    try:
        spec = agent.tools.get(call.name)
        if spec.requires_approval and not await _confirm_approval(agent, call.name, arguments):
            # 拒绝不是失败：落 tool/skipped 痕迹（审计）+ 一条 is_error 结果
            # （模型必须看到"没执行"，否则以为工具跑过了——模型可见 ⟺ 可重建）
            agent.session.append('tool/skipped', {
                'call_id': call.id, 'name': call.name, 'reason': 'not-approved',
            })
            return create_tool_result_message(call.id, 'skipped: user did not approve', True)
        outcome = await agent.tools.execute(call.name, arguments, agent)
    except Exception as error:  # noqa: BLE001 - 工具失败必须变成 is_error 结果，不能炸掉循环
        outcome = ToolOutcome(content=f'{type(error).__name__}: {error}', is_error=True)
    return create_tool_result_message(call.id, outcome.content, outcome.is_error)


def _is_cacheable(agent, name: str) -> bool:
    """这条工具算不算"只读、无产出"——按声明取，取不到就当**会变更**（fail-closed）。"""
    try:
        return agent.tools.get(name).cacheable
    except KeyError:
        return False


def _apply_progress_nudge(agent, call: ToolCallBlock, message: Message) -> Message:
    """记一次调用并（必要时）把收敛提示追加到这条结果正文的末尾。

    追加而不是另起消息：`tool_result` 块的 `content` 就是模型读到的工具输出，
    往里加一行既保持 wire 合法（仍是一个 tool_result），又让它随结果一起进日志。
    """
    verdict = agent.progress.note(_is_cacheable(agent, call.name))
    if verdict != NUDGE or not agent.progress.nudge_text:
        return message
    block = message.content[0]
    if not isinstance(block, ToolResultBlock):
        return message   # 只对工具结果加成（其它形状不该出现在这里，但不炸）
    return create_tool_result_message(
        call.id, f'{block.content}\n\n{agent.progress.nudge_text.format(run=agent.progress.readonly_run)}',
        block.is_error)


def _aborted_message(call: ToolCallBlock, text: str) -> Message:
    """取消时给没有结果的调用补的合成结果（对齐 harness appendSkippedToolCall）。"""
    return create_tool_result_message(call.id, text, True)


def _record_aborted_calls(session, turn: int, step: int, calls: list[ToolCallBlock]) -> None:
    """取消时补齐"已请求但没有结果"的调用：先补 tool/call 痕迹，再补 is_error 结果。

    求差集直接读日志（日志是唯一事实源），所以取消发生在组的哪一步都不会重复
    补记——`_run_group` 已经落过结果的调用在这里自然被跳过。
    没有这一步，日志里会留下"assistant 请求了 N 个工具、只有 M 条结果"的回合，
    derive_messages 出来的记忆在 wire 上非法（下一轮请求 400）。取消是用户随时
    可做的操作，不能靠"取消一般发生在流式阶段、工具早就跑完了"这种概率兜底。
    """
    called: set[str] = set()
    answered: set[str] = set()
    for event in session.events:
        if event.type == 'tool/call':
            called.add((event.data or {}).get('call_id', ''))
        elif event.type == 'tool/result':
            source = getattr(event.data, 'source', None)
            answered.add(getattr(source, 'call_id', ''))
    for call in calls:
        if call.id in answered:
            continue
        if call.id not in called:
            session.append('tool/call', {
                'turn': turn, 'step': step,
                'call_id': call.id, 'name': call.name, 'arguments': call.arguments,
            })
        session.append(
            'tool/result', _aborted_message(call, ABORTED_BEFORE_DISPATCH), surface_op='append')


def _reject_calls_on_closing_step(agent, turn: int, step: int, calls: list[ToolCallBlock]) -> None:
    """收尾步里模型仍然发工具调用 → **不执行**，补 is_error 结果。

    为什么是"补结果"而不是"丢掉"：这次请求我们已经声明"不带工具面"，模型还是调了；
    但 wire 上"带 tool_calls 的 assistant 消息"后面**必须**跟结果（不变式 5 的邻居），
    否则整个会话之后都发不出去。所以按既有规则降级成结果：告诉它"这一步工具被禁用"，
    结果本身进日志，下一回合模型能看到。
    """
    for call in calls:
        agent.session.append('tool/call', {
            'turn': turn, 'step': step,
            'call_id': call.id, 'name': call.name, 'arguments': call.arguments,
        })
        agent.session.append('tool/result', create_tool_result_message(
            call.id, CLOSED_STEP_TOOL_REFUSAL, True), surface_op='append')


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
