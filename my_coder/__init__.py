"""my_coder：Python 复刻 deepseek-harness 架构的 agent 框架（含应用）。

**目录 = 分层**（2026-09 分层重构后）：依赖方向不再只是文档里的约定，而是包结构 + 一条
`tests/test_architecture.py` 的断言（下层 import 上层当场红）：

```
my_coder/
├── values/        层 1 值：消息/事件词汇表（values/messages.py）+ JSONL 持久化
├── capability/    层 2 能力：LLM 客户端、决策钩子（只依赖 values）
├── state/         层 3 状态：日志与它的投影（session / inbox / prompt / registry / recovery）
├── runtime/       层 4 框架循环：被动状态机（agent）+ turn/step 循环（loop）
├── app/           应用内容：组装（factory）+ 指令文件 / 技能 / 压缩 / 渲染 / 沙箱 / 常量
├── tools/         应用工具（file_io / search / shell / todo / web_search / skill）
├── web/           入口：Web 宿主（FastAPI + SSE，拆成 app / state / sessions / titles / payload）
├── cli.py         入口：CLI
└── bundled_skills/ 随包发布的技能正文
```

规则：一个模块只能 import **同层或更低层**。两条已知例外（都带 issue 号）写在
`tests/test_architecture.py` 的 `KNOWN_VIOLATIONS` 里，修好即删。四层与五条不变式的完整
理由见 `README.md` / `ARCHITECTURE.md`。
"""
