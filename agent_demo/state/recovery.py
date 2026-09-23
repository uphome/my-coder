"""状态层：会话自愈——把"没有结果的工具调用"补成一条明确的失败结果。

**为什么需要它**：工具执行期间会写两类事件——`tool/call`（痕迹，起跑前落）和
`tool/result`（surface，跑完后落）。取消路径两者都补记账（见 `loop._run_group`），
但**进程被 kill / 断电 / OOM / 宿主机崩**时没有任何 Python 代码有机会运行：
日志就停在这两个时刻之间。

后果不是"少一条结果"这么轻。`derive_messages` 会把那条"请求了工具"的 assistant
消息折进模型记忆，而 `_to_wire_messages` 是纯翻译、不做修复——wire 上就成了
"assistant 带 `tool_calls`、却没有对应的 tool 消息"。实测 DeepSeek 的答复是：

    HTTP 400 An assistant message with 'tool_calls' must be followed by tool
    messages responding to each 'tool_call_id'.

而且**不会自愈**：那条 assistant 消息永久留在日志里，之后每一次发送都是同一个
400，整个会话报废（实测连发两次同样失败；用户只能新建会话或手改 JSONL）。

**修法**：恢复（重放）之后扫一遍投影，给缺结果的调用补一条 `is_error` 合成结果，
并**先落一条 `session/repaired` 痕迹**说明这次修复。两个刻意的选择：

- 修复走**日志追加**（不是内存里的临时补丁）：修完即持久，重放依然一致；
- **不做"请求构造时补占位消息"**（那能让 400 消失，但会把"这里断过"从日志里
  抹掉，而日志是唯一事实源）。合成结果的文案本身就说清它是怎么来的。

反向的"孤儿结果"（有结果、没有对应调用）当前没有任何路径能造出来——结果总是
跟在调用之后落盘，compaction 也按整段遮蔽——所以不在这里修。
"""
from __future__ import annotations

from ..values.messages import ToolCallBlock, ToolResultBlock, create_tool_result_message
from .session import Session

# 修复痕迹事件的 reason（审计用：一眼看出这段记忆里有几条是人造的）
REPAIR_REASON = 'dangling-tool-call'

# 合成结果的文案：模型要据此知道"这次调用没有结果、可以重发"，
# 而不是以为自己读过文件（失败必须显式化，才不会有基于幻觉的后续推理）
REPAIR_TEXT = (
    'Error: no result was recorded — the session was interrupted (crash or kill) '
    'after this call was requested. Treat it as failed and re-issue it if you still need it.'
)


def dangling_tool_calls(session: Session) -> list[ToolCallBlock]:
    """模型可见记忆里"请求了却没有结果"的工具调用（按出现顺序）。

    判据只看**投影**（`derive_messages`），不看痕迹事件：只有进得了模型记忆的
    调用才会让 wire 非法，被 compaction 遮蔽掉的老调用不该被算进来。
    用 dict 累积是为了保序（Python 3.7+ 的插入序）——补记的顺序要和模型请求的
    顺序一致，否则日志里会出现"结果早于调用"的畸形序列。
    """
    pending: dict[str, ToolCallBlock] = {}
    for message in session.derive_messages():
        for block in message.content:
            if isinstance(block, ToolCallBlock):
                pending[block.id] = block
            elif isinstance(block, ToolResultBlock):
                pending.pop(block.tool_call_id, None)
    return list(pending.values())


def repair_dangling_tool_calls(session: Session) -> tuple[str, ...]:
    """补齐悬空的工具调用，返回被修好的 `call_id`（没有要修的返回空元组）。

    幂等：补完之后再扫就什么都不缺，第二次调用什么都不写。所以可以在每个恢复
    入口无条件调一次，不必先判断"这个会话是不是坏的"。
    """
    calls = dangling_tool_calls(session)
    if not calls:
        return ()
    # 先落修复痕迹，再补结果：日志读起来是"这里发生过一次自愈，下面这些结果是
    # 合成的"。顺序反过来的话，中途再崩一次就只剩一堆看起来很正常的结果
    session.append('session/repaired', {
        'reason': REPAIR_REASON,
        'call_ids': [call.id for call in calls],
    })
    for call in calls:
        session.append(
            'tool/result',
            create_tool_result_message(call.id, REPAIR_TEXT, True),
            surface_op='append',
        )
    return tuple(call.id for call in calls)
