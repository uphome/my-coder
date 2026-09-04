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
