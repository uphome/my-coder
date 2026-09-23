"""Web 宿主：自动会话标题（对齐 harness session-title 的三级来源）。

标题 = **日志投影**：一条 `session/title` 事件（data: `{'title', 'source'}`）就是标题，
恢复/列表都从日志读，不存在第二份状态源。来源三级：

- `'user'`：手动改名（优先级最高，覆盖自动标题）；
- `'auto'`：LLM 自动起名（首条用户消息落地即触发）；
- `'fallback'`**不落日志**：没有标题事件时，列表显示首条用户消息摘要（`sessions.py`
  的列表快扫负责）。

起名失败一律静默降级（网络/超时/空输出/逐字复读），列表继续显示 fallback——
起名绝不能打扰主流程。
"""
from __future__ import annotations

import asyncio

from ..capability.llm import LlmRequest
from ..state.session import Session
from ..values.messages import TextBlock, create_user_message
from .state import Seat, state

TITLE_MAX_CHARS = 40

# 起名 prompt 的核心约束：总结意图、禁止逐字复读原文——"你好"的标题应是
# "问候"而不是把原话再抄一遍（对齐 chat.deepseek 的概括式起名）
TITLE_SYSTEM = (
    'You are a conversation titler for a coding-agent session. Given the first '
    'user message, reply with ONLY a short title that SUMMARIZES THE USER\'S '
    'INTENT (same language as the message, under 12 words). '
    'Rules: never repeat the user message verbatim or near-verbatim; condense it '
    'into a category/action label (e.g. greeting, bug fix, code review, install '
    'deps). No quotes, no punctuation, no explanation.'
)

AUTO_TITLE_TIMEOUT_S = 25  # 起名失败静默降级（fallback 摘要兜底），别拖垮主流程


def clean_title(raw: str) -> str:
    """模型返回的标题规范化：去引号/多余空白/尾标点，限长。空则视为无标题。"""
    title = (raw or '').strip().strip('"\'“”‘’「」『』').strip()
    title = ' '.join(title.split()).strip(' .,;:。，；：！!？?、-—')
    return title[:TITLE_MAX_CHARS]


def is_verbatim_copy(title: str, first_text: str) -> bool:
    """标题是否只是逐字/近逐字复读首条消息——是则视为起名失败（退回 fallback）。

    模型对短消息（寒暄/单句）容易偷懒把原文抄回来当标题，那不是标题。
    归一化后比较：完全相等、或标题是原文的子串（方向各一）。
    """
    def norm(s: str) -> str:
        return ''.join((s or '').split()).lower()
    t, m = norm(title), norm(first_text)
    if not t or not m:
        return False
    return t == m or t in m or m in t


def should_auto_title(session: Session) -> bool:
    """首条消息后是否值得自动起名：真模型 + 还没有标题 + 尚无任何用户消息。

    用于 chat 请求开始时（user/message 尚未 append）的判断。
    """
    args = state.args
    if args is None or args.fake:
        return False
    if any(e.type == 'session/title' for e in session.events):
        return False
    if any(e.type == 'user/message' for e in session.events):
        return False
    return True


def first_user_message_just_landed(session: Session) -> bool:
    """事件已落日志后的判断：这是否恰是第一条 user/message（且无标题、真模型）。

    事件监听回调发生在 append 之后，此刻 session 里 user/message 计数已是 1——
    若还套用 should_auto_title（要求 0 条）就永远 False。所以单独判断：
    真模型 + 无 title + user/message 恰好 1 条（= 刚落地的那条是第一条）。
    """
    args = state.args
    if args is None or args.fake:
        return False
    if any(e.type == 'session/title' for e in session.events):
        return False
    user_messages = [e for e in session.events if e.type == 'user/message']
    return len(user_messages) == 1


async def auto_title(seat: Seat, first_text: str) -> None:
    """自动起名：复用该会话 agent 的 LLM 客户端发一个小请求，结果落 session/title。

    后台任务、与主对话并发互不干扰；任何失败（网络/超时/空输出）都静默
    跳过——列表继续显示 fallback 摘要，起名失败绝不打扰主流程。
    按 seat 取 agent（并发隔离：起名请求用本会话的 llm，不读全局焦点）。
    """
    agent = seat.agent
    if agent is None:
        return
    try:
        request = LlmRequest(
            system=TITLE_SYSTEM,
            model=agent.options.get('model', ''),
            messages=(create_user_message([TextBlock(text=first_text[:400])]),),
            max_tokens=30,
            thinking=False,  # 起名是短请求：关 thinking，别让 30 token 预算被思维链耗尽
        )
        parts: list[str] = []

        async def collect() -> None:
            async for chunk in agent.llm.stream(request):
                if chunk.text:
                    parts.append(chunk.text)
                if chunk.finish_reason:
                    break

        await asyncio.wait_for(collect(), timeout=AUTO_TITLE_TIMEOUT_S)
        title = clean_title(''.join(parts))
        if not title:
            return
        if is_verbatim_copy(title, first_text):
            # 逐字复读原文 = 起名失败：不落 auto 事件，列表继续显示 fallback
            print('[title] auto-title rejected: verbatim copy of user message', flush=True)
            return
        seat.session.append('session/title', {'title': title, 'source': 'auto'})
    except Exception as error:  # noqa: BLE001 - 起名失败不影响主流程
        print(f'[title] auto-title skipped: {type(error).__name__}: {error}', flush=True)
