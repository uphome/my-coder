"""agent_demo：Python 复刻 deepseek-harness 架构的 agent 框架（含应用）。

包结构与依赖方向：
    cli / web_app（入口）→ factory（组装）→ agent / loop（框架循环层）
    → session / inbox / prompt / registry（状态层）→ hooks / llm / values
应用工具在 tools/，渲染在 ui/，路径边界在 sandbox/——入口与工具是
"应用内容"，框架四层与不变式见 README / ARCHITECTURE。
"""
