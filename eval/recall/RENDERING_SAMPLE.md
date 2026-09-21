# 三层投影渲染样本（真实日志，未加工）

> 生成方式：`conda run --no-capture-output -n agent-demo python <临时脚本>`，
> 调用 `eval/recall/manifest.py` 的 `render_session_list` / `render_manifest` / `render_turn`。
> 数据源：仓库 `.sessions/` 里 4 个真实会话（其中 `web-1788604766` 37.8 MB、44 回合，被压缩过 4 次）。

---

## L0 会话目录（产品里放状态栏；每会话一行）

```
<context_sessions>
[web-1789573652] 2 条用户话 · 「你好」 · 2 回合  ← 当前
[web-1788604766] 54 条用户话 · 「你先探索这个项目。 后续你需要为这个agent添加一个工具。代码风格按照原来的进」 · 44 回合 · 已压缩×4
[web] 13 条用户话 · 「你好」 · 8 回合
…（另有 1 个更早会话，用 session_manifest 查看）
</context_sessions>
```

## L1 用户话清单 —— `web.jsonl`（8 回合，全文）

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
      用户：请逐个读这五个文件的开头各 50 行并分别汇报要点：…（与上一条完全相同）
      用户：(插队) 插队：先别继续了，用一句话总结你现在的发现。
[  8] 一句话总结：`web_search` 工具已在本分支实现并接线完毕（`tools/web_search.py` + 常量… · 2 step · read_file · README.md · completed
      用户：请逐个读这五个文件的开头各 50 行并分别汇报要点：…（与上一条完全相同）
      用户：(插队) 插队：先别继续了，用一句话总结你现在的发现。
```

**该清单 1,712 字符（≈856 token）。**

## L1 用户话清单 —— `web-1788604766.jsonl`（44 回合，共 98 行，显示前 22 行）

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
…（其余 76 行省略）
```

**该清单 6,782 字符（≈3,391 token）。**

## L2 回合明细 —— `web-1788604766` · turn 32

（足迹：2 step · 工具 `read_file,grep` · 文件 `agent_demo/skills.py`、`factory.py`、`tools/todo.py`、`tools/__init__.py`）

```
[turn 32 · step 1 · user] 继续读。
[turn 32 · step 1 · assistant] tool_call read_file({"file_path": "agent_demo/skills.py"})
[turn 32 · step 1 · assistant] tool_call read_file({"file_path": "agent_demo/factory.py"})
[turn 32 · step 1 · assistant] tool_call read_file({"file_path": "agent_demo/tools/todo.py"})
[turn 32 · step 1 · assistant] tool_call read_file({"file_path": "agent_demo/tools/__init__.py"})
[turn 32 · step 1 · tool_result] tool_result 1: """应用层：按需技能（skill）——技能 = 文件，目录静态注入 system，正文按需 read。
2: 3: 对齐三家（详见 agent.md §1/§3，落地规则以 AGENTS.md 约定节为准）： 4: 5: - 技能 = `skills/<name>.md` 文件 + YAML frontmatter…
…（此处为真实文件内容，样本里省略）
…（本回合内容超过上限被截断；用 step 参数精读某一步）
```

- **本回合完整渲染 12,785 字符**；截断由 `max_events` / `max_chars` 双上限控制。
- 截断示例（`max_events=4 / max_chars=400`）：保留 4 行后追加
  `…（本回合内容超过上限被截断；用 step 参数精读某一步）`。

## 规模统计

| 会话 | 回合数（有用户话） | L1 清单 | L2 中位 | L2 最大 |
|---|---|---|---|---|
| `web-1788604766.jsonl` | 44（44） | 6,782 字符（≈3.4k token） | 1,072 字符 | **62,577 字符** |
| `web.jsonl` | 8（8） | 1,712 字符（≈0.9k token） | 2,678 字符 | 44,839 字符 |
| `web-1789573652.jsonl` | 2（2） | 222 字符 | 525 字符 | 525 字符 |

---

## 评审时看到的五个问题（待定）

1. **L1 行序**：现在是「结论摘录 + 足迹」在前、用户原话在后（缩进）。要不要反过来
   （用户话在前、足迹在后）？前者利于"我找的是当时干了什么"，后者利于"我找的是我说过什么"。
2. **结论摘录长度**：现在 60 字符，44 回合合计 6.8k 字符。缩到 40 字符能省约 1k 字符/会话。
3. **L2 里的工具结果**：现在是**原样回灌**（上面那次 `read_file` 就是一整份文件，单回合 12.8k 字符、
   全局最大 62.6k）。文件类结果其实**可以只给指针**（"读了 `agent_demo/skills.py`，内容见工作区"），
   因为文件现在还在工作区、而且是**当前版本**；但 `bash` 输出/一次性测量值**不可重得**，必须回灌。
   → 建议：按工具类型分级（`read_file`/`list_files`/`glob` → 只给指针 + 摘要；`bash`/`grep` 结果 → 回灌）。
4. **L1 的重复用户话**：`web.jsonl` 的回合 6/7/8 用户话**完全相同**（同一请求被反复发），
   清单里出现三行几乎一样 → 对导航是歧义（答案不唯一）。要不要折叠成一行 + `×3`？
5. **`N step` 在旧日志里不准**：这几个会话是"step = 整段工具循环"的旧实现写的，
   所以回合 1 明明跑了几十个工具调用却显示 `1 step`。新会话（step = 一次模型请求）才准。
   → 要么只对新日志显示 step 数，要么改用"工具调用次数"当足迹（更稳）。
