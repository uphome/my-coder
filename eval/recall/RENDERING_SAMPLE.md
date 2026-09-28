# 三层投影渲染样本（真实日志，产品实现输出）

> 生成：`conda run --no-capture-output -n agent-demo python eval/recall/render_sample.py`
> —— 用 `my_coder/app/recall.py`（产品实现）渲染，样本 = 模型真正会看到的东西。
> 数据源：仓库 `.sessions/`（当前最大会话 `web-1788604766.jsonl`，54.2 MB）。

## L0 会话目录（产品里放状态栏；每会话一行，只列同一工作区）

```
<context_sessions>
[web-1788604766] 73 条用户话 · 「项目探索与工具添加规划」 · 59 回合 · 已压缩×4  ← 当前
[web-1789573652] 5 条用户话 · 「问候」 · 5 回合
[main] 3 条用户话 · 「read README.md and summarize」 · 3 回合
[web] 14 条用户话 · 「Greeting」 · 9 回合
</context_sessions>
```

## L1 用户话清单 —— `web-1788604766.jsonl`（59 回合，共 132 行，前 20 行）

```
[  1] 探索完成。以下是这个项目的全景总结。 ## 项目定位 deepseek-harness 核心架构的 Python 复刻（… · 1 step · list_files,glob,read_file,grep,bash · README.md,ARCHITECTURE.md,AGENTS.md(+16) · completed [已压缩]
      用户：你先探索这个项目。 后续你需要为这个agent添加一个工具。代码风格按照原来的进行。
[  2] 命令需要你批准才能执行（沙箱规则要求）。我计划执行的操作如下，请批准： 1. `git stash push` —— 把… · 1 step · bash · — · completed [已压缩]
      用户：先建一个分支。在新分支上进行工作
[  3] 明白 —— 你的意思是新工具需要**手动批准**（`requires_approval=True`，执行前走 appro… · 2 step · bash · — · completed [已压缩]
      用户：现在建一个分支。
      用户：(插队) 手动
[  4] 好，等你。需要我做什么时随时说。 （提醒一下当前状态：在干净的 `feat/add-tool` 分支上，工作区无改动，随… · 2 step · — · — · completed [已压缩]
      用户：先总结这个项目
      用户：(插队) 等等我
[  5] 当前分支是 **`feat/web-session-isolation`**（工作区干净，无改动）。 分支列表： ```… · 1 step · bash · — · completed [已压缩]
      用户：当前是在什么分支下？
[  6] 我能直接调用的工具如下（沙箱只能访问工作区 `E:\codeall\agent-demo`，外部路径不可读/写）： ##… · 1 step · — · — · completed
      用户：你能执行什么工具？
[  7] 我按 `gh-issue` 工作流处理了这个 issue：读了全文，然后用当前工作区（正好在本 issue 所述分支 `… · 1 step · read_file,bash,glob,grep,list_files · skills/gh-issue.md,web/index.html,web/PROJECTION_DESIGN.md(+1) · completed
      用户：处理 https://github.com/uphome/my-coder/issues/1
[  8] 是的。收到你要我处理 GitHub issue（`uphome/my-coder#1`）后，我按需加载了 **`gh-i… · 1 step · — · — · completed
      用户：ok。停止工作，刚才你是不是加载了一个skill？
[  9] 是。为了处理 `uphome/my-coder#1` 这个 GitHub issue，我加载（`read_file` 读… · 1 step · — · — · completed
      用户：你是不是加载了一个skill？
…（其余 112 行省略）
```

**该清单 9,386 字符（≈4,693 token）。**

## L1 用户话清单 —— `web.jsonl`（9 回合，全文）

```
[  1] 你好！我是 deepseek-v4-flash，可以帮你处理编程任务。请问有什么我可以帮你的吗？ · 1 step · — · — · completed
      用户：你好
[  2] 已切到 AGENTS.md（共 87 行，一次读完）。它是**给 AI 协作者的仓库规约文档**，下面是逐段解释。 ##… · 2 step · read_file · README.md,AGENTS.md · completed
      用户：读 README.md 的前 40 行，然后逐段解释它们在讲什么。
      用户：(插队) 插队：改成只读 AGENTS.md
[  3] `NEXT_STEPS.md` 里的「目录结构」出现在 **第 362 行的 `## 架构重构` 小节**（该文档没有独… · 2 step · read_file,grep · README.md,AGENTS.md,NEXT_STEPS.md · completed
      用户：先读 README.md 的前 60 行并逐段解释，再读 AGENTS.md 的开头。
      用户：(插队) 再读一下 NEXT_STEPS.md 的目录结构。
[  4] 侦察完成。现在实现。先写工具模块： · 1 step · bash,list_files,grep,read_file,todo_write,write_file,edit · .,agent.md,agent_demo/tools/__init__.py(+18) · aborted
      用户：你现在创建一个新分支。 然后添加一个新的工具，web_search。
[  5] **① README.md（前 40 行）要点** - **定位**：`agent-demo` 是 deepseek-h… · 2 step · read_file · README.md · completed
      用户：请依次读 README.md、AGENTS.md、ARCHITECTURE.md 的开头各 40 行，每读一个文件就汇报一次要点，然后再读 NEXT_STEPS.md。
[  6] 一句话总结：`web_search` 工具已在 `feat/web-search-tool` 分支落地（含常量、`bui… · 2 step · read_file · README.md · completed
      用户：请逐个读这五个文件的开头各 50 行并分别汇报要点：README.md、AGENTS.md、ARCHITECTURE.md、NEXT_STEPS.md、agent.md。每读完一个立刻汇报，再读下一个。
      用户：(插队) 插队：先别继续了，用一句话总结你现在的发现。
[  7] 一句话总结：`web_search` 工具代码已写到 `feat/web-search-tool` 分支上（含常量、组装… · 2 step · read_file · AGENTS.md · completed
      用户：请逐个读这五个文件的开头各 50 行并分别汇报要点：README.md、AGENTS.md、ARCHITECTURE.md、NEXT_STEPS.md、agent.md。每读完一个立刻汇报，再读下一个。
      用户：(插队) 插队：先别继续了，用一句话总结你现在的发现。
[  8] 一句话总结：`web_search` 工具已在本分支实现并接线完毕（`tools/web_search.py` + 常量… · 2 step · read_file · README.md · completed
      用户：请逐个读这五个文件的开头各 50 行并分别汇报要点：README.md、AGENTS.md、ARCHITECTURE.md、NEXT_STEPS.md、agent.md。每读完一个立刻汇报，再读下一个。
      用户：(插队) 插队：先别继续了，用一句话总结你现在的发现。
[  9] README 讲的是这个 demo 的四层架构。任务完成。 · 2 step · read_file · README.md · completed
      用户：hello
```

**该清单 1,808 字符（≈904 token）。**

## L2 回合明细 —— `web-1788604766.jsonl` · turn 32

（足迹：2 step · read_file,grep · agent_demo/skills.py,agent_demo/factory.py,agent_demo/tools/todo.py(+24) · completed）

```
[turn 32 · 2 步 · 62 条事件]
[turn 32 · step 1 · user] 继续读。
[turn 32 · step 1 · assistant] tool_call read_file({"file_path": "agent_demo/skills.py"})
[turn 32 · step 1 · assistant] tool_call read_file({"file_path": "agent_demo/factory.py"})
[turn 32 · step 1 · assistant] tool_call read_file({"file_path": "agent_demo/tools/todo.py"})
[turn 32 · step 1 · assistant] tool_call read_file({"file_path": "agent_demo/tools/__init__.py"})
[turn 32 · step 1 · tool_result] tool_result 1: """应用层：按需技能（skill）——技能 = 文件，目录静态注入 system，正文按需 read。 2: 3: 对齐三家（详见 agent.md §1/§3，落地规则以 AGENTS.md 约定节为准）： 4: 5: - 技能 = `skills/<name>.md` 文件 + YAML frontmatter（name/description）， 6: 正文是操作指南（纯指令，不携带执行代码） 7: - **目录（catalog）静态注入 system**：只放 name + description + 相对 8: workspace 的路径，正文绝不进 system——目录字节稳定，处于缓存稳定前缀 9: - **正文 = 工具结果注入**：模型判断任务匹配某技能后，用现有 read_file 10: 按目录里的路径读文件 → 正文作为 tool/result 进 derive_messages，与读 11: 任何文件机制一致（落日志可重建、可被 compaction 折叠） 12: - 不新增 skill() 专用加载工具（对比 DSH/opencode 的取舍背景见 agent.md） 13: 14: 本模块三个职责： 15: 1. Skill 值对象：frontmatter 解析结果（frozen，供扫描与目录格式化共用） 16: 2. scan_skills(dir)：扫目录解析技能（dir 参数化——为将来多 agent 各自 17: 传技能根目录留缝，不用重构） 18: 3. format_catalog(skills)：目录 → system 注入文本（纯文本行 + read 指引） 19: """ 20: from __future__ import annotations 21: 22: import re 23: import sys 24: from dataclasses import dataclass 25: from pathlib import Path 26: 27: # 技能名只允许小写字母/数字/连字符（对齐 Agent Skills 约定的名字规则）。 28: _NAME = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$') 29: 30: 31: @dataclass(frozen=True) 32: class Skill: 33: """一条技能：目录里的一行（name/description）+ 取正文的路径。 34: 35: path 是文件绝对路径；相对 workspace 的路径由调用方（目录格式化）生成， 36: 因为只有它知道 workspace 根。 37: """ 38: name: str 39: description: str 40: path: Path 41: 42: 43: def scan_skills(root: Path) -> list[Skill]: 44: """扫描技能目录：每个 .md 文件即一个技能（解析 frontmatter 取 name/description）。 45: 46: 只扫顶层 *.md（扁平形态，教学够用）；SKILL.md 目录形态等有需要再加。 47: 解析容错：坏技能（不可读/无 frontmatter/缺 name 或 description/名字非法） 48: **跳过并打诊断**（对齐项目"宁炸勿静默"——技能作者要能发现自己写坏了； 49: 但技能坏了不炸 system，只…
[turn 32 · step 1 · tool_result] tool_result 1: """应用层：agent 工厂——build_agent() 组装一次完整 agent（CLI / Web 共用）。 2: 3: system prompt 由 PromptRegistry 的 section 拼装（identity/persona/工具提示， 4: 按 order 排序）；{{model}}/{{workspace}} 是严格插值变量，组装时求值。 5: 真实模型从环境变量取 key；--fake 注入脚本化假模型（离线演示，不需 key）。 6: 渲染订阅（session.on_event → render_event）也在这里挂——UI 是日志投影。 7: """ 8: from __future__ import annotations 9: 10: import os 11: from pathlib import Path 12: 13: from .agent import Agent 14: from .constants import DEFAULT_COMPACT_TOKENS, DEMO_SCRIPT 15: from .llm import FakeLlm, OpenAiCompatibleLlm 16: from .prompt import PromptRegistry 17: from .session import Session 18: from .skills import format_catalog, scan_skills 19: from .tools import build_tools 20: from .ui import render_event 21: 22: 23: def load_env(path: Path) -> None: 24: """把 .env 里的 KEY=VALUE 注入进程环境；已存在的环境变量优先（不覆盖）。""" 25: if not path.exists(): 26: return 27: for line in path.read_text(encoding='utf-8').splitlines(): 28: line = line.strip() 29: if not line or line.startswith('#') or '=' not in line: 30: continue 31: key, _, value = line.partition('=') 32: key = key.strip() 33: if key and key not in os.environ: 34: os.environ[key] = value.strip() 35: 36: 37: def build_agent(session: Session, args, ui_state: dict, hooks=None) -> Agent: 38: prompt = PromptRegistry() 39: prompt.section('identity', -100, 'You are {{model}}, a coding agent that helps with programming tasks. Read, search, edit, and run commands in the workspace to help the user — verify your work instead of guessing. Never …
…（截断展示；本回合完整渲染 12,827 字符）
```

截断规则：`max_events=80` / `max_chars=12000` 双上限（**痕迹行同样计入**），超限追加一句"…（本回合内容超过上限被截断；本回合的 step：…）"——截断提示必须给出可用的 step，否则"用 step 精读"是空头支票。

第一行是**回合头** `[turn N · K 步 · M 条事件]`：先告诉模型这个回合有多大（`K` = 出现过的 step 数），被截断时再给出可选的 step 列表。

## 规模统计

| 会话 | 回合（有用户话） | L1 清单 | L2 中位 | L2 最大 |
|---|---|---|---|---|
| `web-1788604766.jsonl` | 59（59） | 9,386 字符 | 1,433 | 62,602 |
| `web.jsonl` | 9（9） | 1,808 字符 | 2,535 | 44,863 |
| `web-1789573652.jsonl` | 5（5） | 747 字符 | 557 | 9,020 |
| `main.jsonl` | 3（3） | 355 字符 | 435 | 1,760 |

## 评审时看到的五个问题（待定）

1. **L1 行序**：现在「结论摘录 + 足迹」在前、用户原话在后（缩进）。要不要反过来？
2. **结论摘录长度**：现在 60 字符；缩到 40 能省约 1k 字符/会话。
3. **L2 里的工具结果**：现在原样回灌（一次 `read_file` 可能就是一整份文件，单回合最大 6 万字符）。建议按工具类型分级：`read_file`/`list_files`/`glob` 只给指针（文件还在工作区、且是**当前版本**），`bash`/`grep` 这类不可重得的才回灌。
4. **重复用户话**：同一请求被反复发时清单里会出现几行近似（导航歧义）——要不要折叠成一行 + `×N`？
5. **`N step` 在旧日志里不准**（旧实现 step = 整段工具循环，`step/start` 可能整段缺失 → 全部事件算在 step 0，回合头会写成"1 步"）。已做的缓解：截断时**列出**实际存在的 step（模型不必猜）；未决：要不要改用"工具调用次数"当足迹（更稳、与 step 语义无关），以及"1 步"这种可疑行要不要显式标"step 不可用"。
