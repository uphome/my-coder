"""Web 宿主：**纯函数投影**——日志/事件/消息 → 前端载荷。

这个模块的硬规矩（拆分时立的）：**只 import 标准库 + `..compaction` / `..constants` /
`..session` / `..values`，不 import `.state` / `.sessions` / `.app`**。理由有两个：

- **不依赖 FastAPI**：投影是纯函数，可以脱离 Web 宿主做纯函数测试（issue #21 的验收）；
- **不成环**：`.sessions` 要调这里的投影来组装响应，`.app` 要调这里的投影来推 SSE 帧；
  反过来（这里去读宿主状态）就会形成循环导入，而且会把"投影"重新绑死在 Web 宿主上。

投影的第一原则仍然是"**日志是唯一事实源**"：这里只做"把已有投影摊平成 JSON"，
绝不自己重放日志（那会造成同一事件类型两份折叠，必然分叉）。
"""
from __future__ import annotations

from typing import cast

from ..compaction import cache_hit_rate, estimate_context_tokens, session_token_totals
from ..constants import MODEL_CONTEXT_WINDOW
from ..session import Session

# 工具结果预览上限：展开卡片想看到全文（对齐 CLI 的 BASH_MAX_OUTPUT_CHARS 量级），
# 只在真正超长时截断；chars/lines/truncated 让前端能显示统计与截断徽标。
RESULT_MAX_CHARS = 20000


def first_text_of(message) -> str:
    """从一条 Message（或 user/message 事件载荷）提取文本（TextBlock 拼接）。"""
    texts = [b.text for b in getattr(message, 'content', ()) if getattr(b, 'type', '') == 'text']
    return ''.join(texts)


def result_stats(content: str) -> tuple[int, int]:
    """工具结果统计：(字符数, 行数)——前端摘要行显示用。"""
    return len(content), content.count('\n') + 1 if content else 0


def result_payload(block) -> dict:
    """ToolResultBlock → 前端载荷：预览内容 + 全文统计（截断标记交给前端徽标）。"""
    content = getattr(block, 'content', '') or ''
    chars, lines = result_stats(content)
    return {
        'call_id': getattr(block, 'tool_call_id', ''),
        'content': content[:RESULT_MAX_CHARS],
        'chars': chars,
        'lines': lines,
        'truncated': chars > RESULT_MAX_CHARS,
        'is_error': bool(getattr(block, 'is_error', False)),
    }


def surface_with_seq(session) -> list[tuple[int, object]]:
    """按 surface 序的 (seq, Message)——镜像 derive_messages 但保留 seq。

    历史渲染需要知道每条 user 消息属于第几个回合（turn/start 到 turn/end
    之间）——同回合后续的 user 消息 = steer 插队，前端不该画新回合分隔线。
    derive_messages 只返回 Message（无 seq），这里补上 seq 供回合映射用。
    保持与 derive_messages 相同的折叠规则（assistant 空 content 跳过）。
    """
    out: list[tuple[int, object]] = []
    events = session.events
    for seq in session.surface:
        event = events[seq]
        if event.type == 'user/message':
            out.append((seq, event.data))
        elif event.type == 'assistant/message':
            message = cast(dict, event.data)['message']
            if message.content:
                out.append((seq, message))
        elif event.type == 'tool/result':
            out.append((seq, event.data))
    return out


def user_message_turns(session) -> dict[int, int]:
    """user/message surface 事件的 seq → 回合号（扫描 turn/start 划界）。

    只有真人发言（surface append 的 user/message）需要回合归属；checkpoint
    （replace 顶替）单独由 role 识别，不进此表。
    """
    mapping: dict[int, int] = {}
    turn = 0
    for event in session.events:
        if event.type == 'turn/start':
            turn = int(event.data['turn'])
        elif event.type == 'user/message' and event.surface_op == 'append':
            mapping[event.seq] = turn
    return mapping


def reasoning_by_assistant_seq(session) -> dict[int, str]:
    """assistant/message 事件的 seq → 该次模型请求的完整思维链（痕迹投影）。

    UI 是日志的投影——思维链虽是痕迹（不回灌模型），但**要给人看**：
    历史/刷新后深度思考块必须能重建（issue #4）。

    配对规则（依赖日志顺序）：每次请求的 `assistant/reasoning`（完整思维链，
    痕迹）紧跟在它的 `assistant/message`（surface）之前落盘 → 按 seq 顺序用
    buffer 配对即可。同一 (turn, step) 内工具循环的多次请求也天然一次一份
    ——这正好回答了"同一步多次请求的思维链合并"问题：历史里按请求分开。
    """
    out: dict[int, str] = {}
    buffer: str | None = None
    for event in session.events:
        if event.type == 'assistant/reasoning':
            data = event.data if isinstance(event.data, dict) else {}
            text = data.get('reasoning')
            buffer = text if isinstance(text, str) and text else None
        elif event.type == 'assistant/message':
            if buffer:
                out[event.seq] = buffer
            buffer = None
    return out


def history_payloads(session) -> list[dict]:
    """会话历史消息载荷（页面加载/刷新用），user 消息附带其回合归属。

    message_to_payload 是纯消息 → dict，不知道回合；这里在构造处补上
    'turn' 字段，前端据此区分"新回合首条"与"同回合插队（steer）"。
    user 消息再补 'rpc_id'（提交身份）：前端全量重建投影后，仍能认出
    "这条 durable 消息就是我刚才那条回显的落地"。
    assistant 消息补 'reasoning'（该次请求的思维链，痕迹投影给人看）。
    """
    turns = user_message_turns(session)
    reasoning = reasoning_by_assistant_seq(session)
    payloads = []
    for seq, message in surface_with_seq(session):
        payload = message_to_payload(message)
        if payload['role'] == 'user':
            if seq in turns:
                payload['turn'] = turns[seq]
            source = getattr(message, 'source', None)
            payload['rpc_id'] = getattr(source, 'rpc_id', '')
        elif payload['role'] == 'assistant' and seq in reasoning:
            payload['reasoning'] = reasoning[seq]
        payloads.append(payload)
    return payloads


def queue_rows(agent) -> list[dict]:
    """待处理队列 → 前端队列区的行（web 层只做序列化）。

    投影本身在状态层：`Inbox.queued_items()` 折 agent/inbox/spliced 得到
    `QueuedItem(placement, message)`——和 `Session.derive_messages()` 一样是
    "日志 → 不可变投影"。为什么不在这一层自己重放日志：web 层重放会造成
    **同一事件类型两份折叠**（Inbox._apply 一份、这里一份）必然分叉，而且
    投影被绑死在 Web 宿主上（CLI / 测试都拿不到）。所以这里只把值对象摊平成
    JSON：id 给前端去重与操作、text 给显示、placement 给分区渲染、
    rpc_id 给本地回显做原子交接。

    分区渲染（对齐 DSH）：placement='queued' 进输入框上方的队列区；
    placement='steering' 画在**消息流尾部**（pending 气泡 + 待处理标记）——
    未 claim 的消息都没有 seq 位置，所以既不能插进流中间，也不能靠锚点猜。
    """
    return [
        {'id': item.id, 'text': first_text_of(item.message),
         'placement': item.placement, 'rpc_id': item.rpc_id}
        for item in agent.inbox.queued_items()
    ]


def context_payload(session: Session | None) -> dict | None:
    """当前上下文占用（前端圆环）+ 会话级累计账（消耗 token / 缓存命中率）。

    used/percent/window 是"当前上下文快照"（最后一条真实 usage 或估算）；
    cache_hit_pct / total_tokens 是"全会话累计"（对齐 dsh StatsLine 的
    totals 投影语义：Σ 求和、token 加权，不是逐请求平均，也不是单次快照）。

    `session` **必给**：拆分前这个参数缺省时会回退去读全局焦点会话——那是
    "同一份状态两个来源"，正是并发隔离要消灭的东西（两个会话并行时，A 的
    圆环会显示 B 的占用）。调用方本来就都传了。
    """
    if session is None:
        return None
    used = estimate_context_tokens(session)
    percent = min(100, round(used * 100 / MODEL_CONTEXT_WINDOW))
    payload: dict = {'used': used, 'window': MODEL_CONTEXT_WINDOW, 'percent': percent}
    totals = session_token_totals(session)
    if totals is None:
        return payload  # 无真实 usage（如 fake）：只有占用快照，没有累计账
    rate = cache_hit_rate(session)
    payload['session'] = {
        'total_tokens': totals['input_tokens'] + totals['output_tokens'],
        'input_tokens': totals['input_tokens'],
        'output_tokens': totals['output_tokens'],
    }
    if rate is not None:  # 有请求报了缓存拆分才带命中率（fake/全缺拆分不显示）
        payload['session']['cache_hit_pct'] = round(rate * 100)
    return payload


def event_to_payload(event, session: Session | None = None, agent=None) -> dict | None:
    """会话事件 → 前端最小协议（只挑前端关心的；其余事件前端不渲染）。

    session / agent 参数都是"这个事件属于哪个会话"的定位：turn/end 附带的
    context 读 session，agent/inbox/spliced 的队列快照读 agent.inbox——并发下
    多个会话各自跑，绝不能读全局焦点会话的状态。SSE 循环按 seat 传入。

    统一投影模型的协议：chunk/reasoning/tool_call 都带 turn/step，前端
    据此把事件挂到对应 assistant 节点（不再靠"当前块"猜）。turn/start
    与真人 user 消息各发一帧，前端不再自数回合。
    """
    if event.type == 'turn/start':
        return {'type': 'turn_start', 'turn': int(event.data['turn'])}
    if event.type == 'request/header':
        # 请求边界帧（issue #4 ③）：同一步内工具循环会有多次模型请求，前端
        # 据此在节点内开新的"请求块"（关闭上一个 assistant 节点再建新的），
        # 让思维链/文本/工具按"每次请求"分开——与历史路径（每条
        # assistant/message = 一次请求）对齐。
        data = event.data if isinstance(event.data, dict) else {}
        return {'type': 'request_start',
                'turn': int(data.get('turn', 0)),
                'step': int(data.get('step', 0))}
    if event.type == 'user/message' and event.surface_op == 'append':
        # 真人发言帧（surface replace 的 checkpoint 除外——它独立成卡）。
        # 文本取第一条 text block；纯工具结果的 user 消息没有 text，不发帧
        # （工具结果显示由 tool/result 事件驱动）。turn 由前端用最近一次
        # turn_start 推导（user/message 事件本身不带 turn）。
        # 带 message_id + rpc_id：前端据此把这个提交从"待处理"换成真身
        # （steering 气泡在流尾就地转正、本地回显在同一次渲染里消失）。
        text = first_text_of(event.data)
        if text:
            source = getattr(event.data, 'source', None)
            return {'type': 'user_message', 'text': text,
                    'message_id': event.data.id,
                    'rpc_id': getattr(source, 'rpc_id', '')}
        return None
    if event.type == 'assistant/chunk':
        text = event.data['chunk']['text']
        return ({'type': 'chunk', 'turn': event.data['turn'], 'step': event.data['step'],
                 'text': text} if text else None)
    if event.type == 'assistant/reasoning/chunk':
        reasoning = event.data['reasoning']
        return ({'type': 'reasoning', 'turn': event.data['turn'], 'step': event.data['step'],
                 'text': reasoning} if reasoning else None)
    if event.type == 'tool/call':
        return {
            'type': 'tool_call',
            'turn': event.data.get('turn', 0),
            'step': event.data.get('step', 0),
            'call_id': event.data['call_id'],
            'name': event.data['name'],
            'arguments': event.data['arguments'],  # 原始 JSON 字符串
        }
    if event.type == 'tool/result':
        block = event.data.content[0]
        return {'type': 'tool_result', **result_payload(block)}
    if event.type == 'todo/write':
        # 常驻 dock 的实时更新：模型每次重写清单，前端面板跟着变
        todos = event.data.get('todos') if isinstance(event.data, dict) else None
        return {'type': 'todo_update', 'todos': todos or []}
    if event.type == 'agent/inbox/spliced' and agent is not None:
        # 队列区实时更新（对齐 DSH QueueDock）：入队/认领/撤回都落 spliced，
        # 前端据此显示"待处理消息"（未 claim 不进消息流）。
        return {'type': 'queue_update', 'queue': queue_rows(agent)}
    if event.type == 'turn/end':
        # 回合结束附带上下文占用（圆环数据）：真实 usage 估算 / 1M 窗口。
        # 用事件所属会话的占用（并发隔离：不要读全局焦点会话的）
        return {
            'type': 'turn_end',
            'reason': event.data['reason'],
            'context': context_payload(session),
        }
    return None


def message_to_payload(message) -> dict:
    """折叠出的模型消息 → 历史渲染用（页面加载时一次性展示）。

    tool/result 在内部是 user 角色消息（README：wire 层才变 role: tool），
    但历史渲染里它们应并入工具活动块而不是当用户消息——这里显式标记
    role='tool_result'，避免前端把"空文本的 user 角色消息"渲染成空白气泡。
    """
    texts = [b.text for b in message.content if getattr(b, 'type', '') == 'text']
    tool_calls = [
        {'call_id': b.id, 'name': b.name, 'arguments': b.arguments}
        for b in message.content if getattr(b, 'type', '') == 'tool-call'
    ]
    tool_results = [
        result_payload(b)
        for b in message.content if getattr(b, 'type', '') == 'tool-result'
    ]
    role = message.role
    text = '\n'.join(texts)
    if role == 'user' and not texts and tool_results:
        role = 'tool_result'  # 纯工具结果消息：并入当前工具活动块
    elif role == 'user' and '<compacted-summary>' in text:
        # 压缩 checkpoint（replace 顶替旧回合的 user/message）：不是真人发言，
        # 前端渲染成可折叠的"上下文已压缩"卡片而非用户气泡
        role = 'checkpoint'
    return {
        'role': role,
        'text': text,
        'tool_calls': tool_calls,
        'tool_results': tool_results,
    }
