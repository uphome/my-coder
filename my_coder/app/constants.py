"""应用层常量：搜索预算 / bash 输出上限 / 渲染颜色 / fake 演示脚本。

与 `ui.py`（渲染）分开：budget 是工具的硬预算（进不进模型 schema、
截断上限），demo 脚本是 fake 模式的应答剧本——都是"值"不是行为。

**放这里的判据**：只有应用层/工具层用到的常量。一旦有更低的层（状态/框架循环）也要用，
就下沉到 `values/limits.py`（那条规矩写在那里）——`TOOL_RESULT_MAX_CHARS` 就是这么搬走的。
"""
from __future__ import annotations

# 终端颜色码（ui.py 用）——集中定义便于换主题；ANSI 只在 tty 下启用
REASONING_COLOR = '\033[36m'   # cyan —— 思维链
REASONING_DIM = '\033[2m'      # dim
RESET = '\033[0m'
TOOL_COLOR = '\033[33m'        # yellow —— 工具调用
RESULT_COLOR = '\033[90m'      # bright black —— 工具结果（灰，弱化刷屏）
DIM = '\033[2m'                # dim —— 分隔线 / 请求计数

# 搜索预算（对齐 harness 的 tool-fs-search，也是 Claude Code 的默认值）：
# 常规上限不进模型 schema，模型只看到"前 N 条 + 截断提示"。
GREP_MAX_MATCHES = 250   # grep 内联保留的最大匹配数
GLOB_MAX_RESULTS = 100   # glob 内联保留的最大路径数

# bash 输出截断上限：防大输出（cat 大文件、编译日志）一次撑爆上下文；
# 截断提示教模型把大输出重定向到文件，再用 read_file 分页读。
BASH_MAX_OUTPUT_CHARS = 8000

# read_file 预算：默认 200 行，单次最多 2000 行 / 16000 字符。
# 字符预算故意小于 registry 的 TOOL_RESULT_MAX_CHARS（那个在 values/limits.py），
# 给截断/导航提示留余量，避免 read_file 的提示又被 registry 通用截断盖掉。
READ_FILE_DEFAULT_LIMIT = 200
READ_FILE_MAX_LIMIT = 2000
READ_FILE_MAX_CHARS = 16000

# web_search 预算与端点（对齐 DSH packages/web/web-search-deepseek）：
# 搜索能力由 DeepSeek 官方在服务端提供（原生 web_search_20250305 服务端工具），
# 我们只做"发请求 + 解析结构化结果"——绝不自己抓网页、绝不从正文抠 URL。
#
# 注意端点：这是 DeepSeek 的 **Anthropic 兼容** Messages 端点（不是
# chat-completions 的 DEEPSEEK_BASE_URL），只共享 DEEPSEEK_API_KEY，
# 不复用那个环境变量当 base（DSH provider.ts 原话如此）。
WEB_SEARCH_BASE_URL = 'https://api.deepseek.com/anthropic/v1'
WEB_SEARCH_MODEL = 'deepseek-v4-flash'
WEB_SEARCH_MAX_USES = 5       # 服务端工具每次请求最多搜索几次（进请求体，不是 schema）
WEB_SEARCH_MAX_RESULTS = 5    # 合并去重后返回给模型的结果条数上限
WEB_SEARCH_MAX_QUERIES = 4    # 一次工具调用允许的 query 条数上限（DSH WEB_SEARCH_MAX_QUERIES）
# 摘要上限：那段摘要是搜索请求里的**辅助模型**写的转述（实测一条 ≈2.4k 字符），
# queries 拉满 4 条最坏 ~10k 字符；截断保上下文（超出会附一句截断标记）。
WEB_SEARCH_SUMMARY_MAX_CHARS = 3000
# 一次搜索 = 一个完整模型轮次（服务端工具要真去搜、还要生成答复），
# 别按普通 HTTP 给 10s——60s 量级才够（DSH 也把预算交给调用方 timeoutMs）。
WEB_SEARCH_TIMEOUT_S = 60.0

# 上下文压缩默认阈值：deepseek-v4 窗口 1M token，过半（0.5M）就自动压
# 旧回合，给后续回合留足空间（摘要请求本身也吃窗口）。显式 0 可关闭。
DEFAULT_COMPACT_TOKENS = 524288  # 1M 窗口的一半

# 模型上下文窗口（deepseek-v4 系列）：前端圆环的分母与压缩阈值都基于它
MODEL_CONTEXT_WINDOW = 1_000_000

# ---------- 工具收敛（issue #2：开放任务回合不收敛）----------
#
# 判据是"**自上次变更以来连续做了多少次只读调用**"，不是"回合走了多少步"——真实语料实测
# （4 会话 / 57 回合）：最长只读段 p50=1 / p90=8 / max=39，而一个 61 步的**实现型**回合
# 最长只读段只有 4。按步数设上限会砍掉后者（opencode 就是按步数，我们没跟）。
# 设计与证据：docs/notes/proposed/feature/2026-09-30-tool-convergence.md
READONLY_NUDGE_AT = 8     # 软：连续只读到这个数，把提示写进那次工具结果的正文
READONLY_CLOSE_AT = 16    # 硬：到这个数，请求一次**不带工具面**的收尾步（只出文字）

# 软提示的文案。要具体、可执行，并给出"继续"的合法路径——只说"该收敛了"会被当成噪声。
CONVERGENCE_NUDGE = (
    '⚠️ 工具收敛提示：本回合已连续 {run} 次只读调用（读取/搜索），期间没有任何改动或产出。'
    '如果关键事实已经验证，请先给结论；如果还要继续查，请在回复里说明还缺什么、为什么必须再查。'
)

# 收尾步的指令（与 opencode 的 MAX_STEPS_PROMPT 同构：说清做了什么/还剩什么）。
# 它只在收尾那一次请求里作为合成 user 消息叠上，不进日志、可审计。
CONVERGENCE_CLOSING = (
    '本回合的只读调查已达上限，这一步不再提供工具。请不要尝试调用工具，直接给结论：'
    '① 已经确认的事实（附证据位置）；② 尚未查清的部分，以及你打算怎么查；'
    '③ 建议的下一步（需要我确认什么）。'
)

DEMO_SCRIPT: list[dict] = [
    {
        'reasoning': '用户让我总结 README，先读取文件内容再回答。',
        'tool_calls': [{'id': 'call-1', 'name': 'read_file', 'arguments': '{"file_path": "README.md"}'}],
        'finish_reason': 'tool_calls',
    },
    {
        'reasoning': 'README 已经读完，核心是四层架构，现在整理成简短总结。',
        'text': 'README 讲的是这个项目的四层架构。任务完成。',
        'finish_reason': 'stop',
    },
]
