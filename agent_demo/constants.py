"""应用层常量：搜索预算 / bash 输出上限 / fake 演示脚本。

与 ui.py（渲染）分开：budget 是工具的硬预算（进不进模型 schema、
截断上限），demo 脚本是 fake 模式的应答剧本——都是"值"不是行为。
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
# 一次搜索 = 一个完整模型轮次（服务端工具要真去搜、还要生成答复），
# 别按普通 HTTP 给 10s——60s 量级才够（DSH 也把预算交给调用方 timeoutMs）。
WEB_SEARCH_TIMEOUT_S = 60.0

# 上下文压缩默认阈值：deepseek-v4 窗口 1M token，过半（0.5M）就自动压
# 旧回合，给后续回合留足空间（摘要请求本身也吃窗口）。显式 0 可关闭。
DEFAULT_COMPACT_TOKENS = 524288  # 1M 窗口的一半

# 模型上下文窗口（deepseek-v4 系列）：前端圆环的分母与压缩阈值都基于它
MODEL_CONTEXT_WINDOW = 1_000_000

DEMO_SCRIPT: list[dict] = [
    {
        'reasoning': '用户让我总结 README，先读取文件内容再回答。',
        'tool_calls': [{'id': 'call-1', 'name': 'read_file', 'arguments': '{"file_path": "README.md"}'}],
        'finish_reason': 'tool_calls',
    },
    {
        'reasoning': 'README 已经读完，核心是四层架构，现在整理成简短总结。',
        'text': 'README 讲的是这个 demo 的四层架构。任务完成。',
        'finish_reason': 'stop',
    },
]
