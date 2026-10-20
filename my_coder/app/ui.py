"""应用层：CLI 渲染——UI 是日志的投影（终端版）。

on_event 只负责"怎么显示"：把一条事件画到终端。渲染状态（request_no /
tool_no / 思维链开关 / 去重标记）是可变的 UI 状态，resume 重放历史时从 0 递增，
新回合接续——序号天然连贯。颜色只在 tty 下启用（_USE_COLOR），
管道/重定向输出不被 ANSI 污染。

**同一函数服务实时与重放**（issue #51 之后尤其重要）：实时路径的助手正文
由 `assistant/chunk` 流帧逐字打印，而新日志不再写 chunk 帧，重放时只剩
`assistant/message`——同一条正文在两个路径上各出现一次，去重靠 state 里的
`streamed` 标记（见 `render_event`）。
"""
from __future__ import annotations

import sys

from ..values.messages import TextBlock
from .constants import DIM, REASONING_COLOR, REASONING_DIM, RESET, RESULT_COLOR, TOOL_COLOR

# 管道/重定向（非 tty）时禁用颜色：UI 是日志的投影，颜色只是装饰，
# 不能让 ANSI 码污染被重定向的输出。
_USE_COLOR = sys.stdout.isatty()


def paint(code: str, text: str) -> str:
    """按是否 tty 决定要不要包 ANSI 颜色码。"""
    return f'{code}{text}{RESET}' if _USE_COLOR else text


def _close_reasoning(state: dict) -> None:
    """关闭未闭合的思维链显示（换行 + 重置颜色）。"""
    if state['reasoning_started']:
        print('\n' if not _USE_COLOR else f'{RESET}\n', end='', flush=True)
        state['reasoning_started'] = False


def render_event(event, hide_reasoning: bool, state: dict) -> None:
    """把一条事件（或流帧）渲染到终端。

    state：可变渲染状态 {'reasoning_started', 'request_no', 'tool_no', 'streamed'}——
    resume 重放历史时从 0 递增，新回合接续，序号天然连贯。

    `streamed` 记"本 (turn, step) 的助手正文是否已由流式帧打印过"（issue #51
    之后新日志没有 chunk 帧，所以重放时正文改由 assistant/message 补打）。
    键是按需补上的：旧调用点只初始化另外三个键也能正常渲染。
    """
    if event.type == 'turn/start':
        _close_reasoning(state)
        state['streamed'] = None       # 新回合：上一回合的"已流式打印"标记作废
        print(paint(DIM, f'\n════ turn {event.data["turn"]} ════'), flush=True)
    elif event.type == 'step/start':
        # 边界分隔线：长任务有进度感（回合.步骤 两级）
        state['streamed'] = None       # 新 step 同理：标记只对一个 (turn, step) 有效
        print(paint(DIM, f'── step {event.data["turn"]}.{event.data["step"]} ──'), flush=True)
    elif event.type == 'request/header':
        # 每次新请求前关闭可能未闭合的思维链（例如失败重试场景）。
        _close_reasoning(state)
        state['request_no'] += 1
        print(paint(DIM, f'[req {state["request_no"]} {event.data["model"]}]'), flush=True)
    elif event.type == 'assistant/chunk':
        text = event.data['chunk']['text']
        if text:
            _close_reasoning(state)
            print(text, end='', flush=True)
            # 记下"这一步的正文已经流式打印过"，assistant/message 到达时据此跳过。
            state['streamed'] = (event.data.get('turn'), event.data.get('step'))
    elif event.type == 'assistant/message':
        # 重放补正文：issue #51 之后新日志不再写 assistant/chunk 帧，历史助手
        # 正文只存在于 assistant/message 里；不补的话 --resume 看不到正文。
        # 实时路径上同一段正文已由上面的 chunk 分支逐字打印，必须跳过——
        # 判据是本 (turn, step) 的 streamed 标记（step/start、turn/start 处复位）。
        data = event.data
        if state.get('streamed') == (data.get('turn'), data.get('step')):
            return
        # 消息 content 是 block tuple：只取文本块（工具调用块不是给人看的正文，
        # 它随后会由 tool/call 事件渲染），多块按顺序、空文本块不打印。
        texts = [block.text for block in data['message'].content
                 if isinstance(block, TextBlock) and block.text]
        if texts:
            _close_reasoning(state)
            # 重放是"一次性写完历史"，逐条换行比实时打字机的 end='' 更好读；
            # 实时路径不会走到这里（上面的去重已 return）。
            print('\n'.join(texts), flush=True)
    elif event.type == 'assistant/reasoning/chunk':
        if not hide_reasoning and event.data['reasoning']:
            if not state['reasoning_started']:
                print(paint(f'{REASONING_COLOR}{REASONING_DIM}', '[思考] '),
                      end='', flush=True)
                state['reasoning_started'] = True
            print(event.data['reasoning'], end='', flush=True)
    elif event.type == 'assistant/reasoning':
        if not hide_reasoning and event.data['reasoning']:
            # 思维链流式片段已经实时打印，这里补一个换行，避免和正式回答粘在一起。
            _close_reasoning(state)
    elif event.type in ('step/end', 'turn/end'):
        _close_reasoning(state)
    elif event.type == 'tool/call':
        _close_reasoning(state)
        state['tool_no'] += 1
        print(paint(
            TOOL_COLOR,
            f'\n[tool {state["tool_no"]}] {event.data["name"]}({event.data["arguments"]})',
        ), flush=True)
    elif event.type == 'tool/result':
        # 结果摘要：前 3 行 + 统计（灰色缩进）——想看全貌去日志，UI 不刷屏
        text = event.data.content[0].content
        lines = text.splitlines()
        body = '\n'.join(f'  {line}' for line in lines[:3])
        summary = f'  (…共 {len(text)} 字符 / {len(lines)} 行)'
        print(paint(RESULT_COLOR, f'[result] {body}\n{summary}'), flush=True)
