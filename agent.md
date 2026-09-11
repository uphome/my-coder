# Agent 机制实现参考（DSH / PI / opencode + codegraph）

> 用途：**讨论 agent-demo（本仓库）某个机制怎么做之前，先翻本文件看三家现成项目
> （deepseek-harness / pi / opencode）各自怎么实现**，再决定教学复刻的取舍。
> 本文件是跨会话的参考手册，不是本仓库的实现约定——实现约定见 AGENTS.md。
>
> 三个母本都在本地，且都建了 codegraph 索引，随时可以深入查源码：
>
> | 项目 | 本地路径 | 一句话定位 |
> |---|---|---|
> | DSH（deepseek-harness） | `E:\codeall\deepseek-harness` | TS 企业级编码 agent 框架（本 demo 的母本） |
> | PI（pi-mono） | `E:\codeall\pi` | `@earendil-works/pi`，TS 编码 agent（agent/tui/ai 分包） |
> | opencode | `E:\codeall\opencode` | `@opencode-ai`，开源编码 agent（内置大量 issue/PR 自动化） |

---

## 0. codegraph：怎么查这三家

理解/抄机制的第一动作是 codegraph（**不是 grep/Read 循环**）。每个项目根有
`.codegraph/` 索引；跨项目查询要显式传 `projectPath`（默认只查当前工作区
agent-demo，而 agent-demo 的索引不含那三家）。

```text
mcp__codegraph__codegraph_explore(
  projectPath: "E:\codeall\deepseek-harness" | "E:\codeall\pi" | "E:\codeall\opencode",
  query: "要查的符号 / 文件 / 一句话问题"
)
```

- 返回：命中符号所在文件的**逐行源码**（= 已 Read，别再重复打开）+ 调用路径 +
  blast radius（依赖方，改前必看）
- 语义：自然语言问题或符号袋都能查；想查的符号越具体越省
- 局限：CSS/HTML/纯数据文件不在 Python/TS 索引里（前端布局问题 codegraph
  帮不上，直接 read 文件）；索引落后于磁盘时结果会标注 stale
- 三个仓库里搜机制的关键入口：
  - DSH skill：`packages/skill/{skill,skill-filesystem,tool-skill}`
  - PI skill：`packages/coding-agent/src/core/skills.ts` + `packages/agent/src/harness/skills.ts`
  - opencode skill：`packages/opencode/src/skill/index.ts` + `packages/opencode/src/tool/skill.ts`
  - 各家“issue/PR 工作流”都在 `.pi/prompts/*.md`、`.opencode/{agent,command,tool}/*`、
    DSH 的 `.agents/skills/*`（文件形态，直接读 md 比 codegraph 快）

---

## 1. 三家的 SKILL 机制（逐家）

三家都遵循同一个**开放约定**（Agent Skills / agentskills.io）：
**技能 = 一个 Markdown 文件（YAML frontmatter 声明 name/description，正文是操作
指南）**；运行时只把“技能目录”（name+一句话描述）低成本放进系统提示；
**正文按需加载**——区别就在“怎么发现、怎么注入、模型怎么取正文、谁能触发”。

### 1.1 DSH（deepseek-harness）——企业级注册表

入口（都可用 codegraph 查）：
- `packages/skill/skill/src/index.ts`：`SkillRegistry`（Service）、`SkillProvider` 抽象、
  `SkillDefinition`（summary + content）、invocation policy
- `packages/skill/skill-filesystem/src/index.ts`：`FileSystemSkillProvider`——把本地
  技能根目录映射成候选（扫描 `**/SKILL.md`，尊重 ignore 文件）
- `packages/skill/tool-skill/src/index.ts`：模型侧 `skill` 工具 + 目录渲染 +
  `/skill-name` 手势识别（`SKILL_GESTURE`）
- `packages/api/session-controller/src/skill-catalog.ts`：会话可见技能列表 Remote

机制要点：
- **模型侧只有一个常驻 `skill` 加载工具**（不是每技能一工具）：参数是技能名，
  返回该技能完整正文。技能是**指令包**，不携带执行代码
- 目录注入：系统提示里给“可用技能”几行摘要，模型判断任务匹配 → 调 `skill`
  工具拿正文 → 按正文干活；正文不进常驻 prompt
- 调用策略：`SkillInvocationPolicy { modelInvocable, userInvocable }` 区分模型自取
  与用户显式触发（`disable-model-invocation` 可禁模型侧）
- frontmatter 常见字段：`name` / `description` / `whenToUse`（路由引导）
- 技能来源分多根：project / user / bundled，按作用域（scope）分层、同名覆盖
- 用户侧手势：消息里的 `/skill-name`（`SKILL_GESTURE` 正则）

### 1.2 PI（pi-mono）——文件即技能，无加载工具

入口：
- `packages/coding-agent/src/core/skills.ts`：扫描/校验/渲染（模型侧技能解析）
- `packages/agent/src/harness/skills.ts`：harness 侧 `loadSkills`（经 ExecutionEnv
  抽象读文件系统，忽略文件规则、递归找 `SKILL.md` 或根目录平铺 `.md`）
- `.pi/skills/*.md`（如 `add-llm-provider.md`）——技能样本
- `.pi/prompts/*.md`（`is.md`/`pr.md`/`wr.md`/`sa.md`/`cl.md`/`deslop.md`）——
  用户斜杠命令模板（不是技能，是“命令”）

机制要点（**与 DSH 最大的不同**）：
- **模型取正文不用专用工具**：技能目录以 XML 编译进系统提示
  （`<available_skills>` 内 `<skill><name>…</name><description>…</description>
  <location>/abs/…md</location></skill>`），并明示“Use the read tool to load a
  skill's file when the task matches its description”——**模型用现有 read 工具
  按 location 读文件**，零新增执行工具
- 校验极严：技能名必须小写 `a-z0-9-`、description 必填（越界/缺失报诊断）
- 用户侧是另一套文件：`.pi/prompts/*.md` 斜杠命令（`/is`、`/pr`、`/wr`），
  frontmatter `description` + `argument-hint`，正文可含 `$ARGUMENTS` 占位符与
  bash 示例——**issue/PR 工作流全在这里，不落任何专用工具**
- AGENTS.md 常驻纪律兜底（`gh … --body-file`、只 stage 自己的文件、不 force
  push 等）——技能只讲流程，安全规矩常驻

### 1.3 opencode——混合：skill 工具 + 多来源发现 + 权限门

入口：
- `packages/opencode/src/skill/index.ts`：发现逻辑（模式扫描 + 缓存 + Info schema）
- `packages/core/src/skill.ts`：`available()` 按权限过滤 + V2 Service（source →
  load → list 缓存）
- `packages/opencode/src/tool/skill.ts`：模型侧 `skill` 工具本体
- `packages/opencode/src/session/prompt.ts`：`sys.skills(agent)` 把目录拼进系统提示
- `.opencode/skills/<name>/SKILL.md`（技能样本）、`.opencode/tool/*.ts`
  （自定义工具）、`.opencode/agent/*.md`（agent 角色）、`.opencode/command/*.md`
  （用户命令，可 `subtask:true` / 指定模型）

机制要点：
- **发现来源最多**：内置技能（`customize-opencode`）+ 项目 `{skill,skills}/**/SKILL.md`
  + 外部兼容目录（`.claude/`、`.agents/` 的 `skills/**/SKILL.md`）+ 配置
  `skills.paths` + 远程 `skills.urls`（git 拉取）
- **模型侧 `skill` 工具**（同 DSH 思路）执行时：
  1. 按名取技能（未命中报错）；
  2. **先走权限门**（`ctx.ask({ permission: "skill", patterns: [name] })`——
     加载技能本身也要用户批准）；
  3. 返回 `<skill_content>` 包裹的正文 + **skill 目录文件采样列表**
     （ripgrep 列同目录非 SKILL.md 文件，教模型相对路径基准）
- frontmatter 可带 `slash: true`（注册为用户斜杠命令）；文件名也能当技能名
- 同一份“技能目录”既喂模型（`sys.skills`）也喂用户（TUI 里 `/skill` 选择器）

### 1.4 对照表

| 维度 | DSH | PI | opencode |
|---|---|---|---|
| 技能载体 | `SKILL.md` + frontmatter | 任意 `.md` / `SKILL.md` + frontmatter | `SKILL.md` + frontmatter |
| 技能目录进提示 | 几行摘要（tool-skill 渲染） | XML `<available_skills>`（name/description/location） | XML `<available_skills>`（sys.skills） |
| 模型怎么取正文 | **专用 `skill(name)` 工具** | **现有 read 工具读 `<location>`** | **专用 `skill` 工具**（+权限批准） |
| 技能可否带执行 | 否（纯指令） | 否（纯指令） | 否（但附带文件清单/相对路径基准） |
| 加载是否要批准 | 否（模型自取） | 否（read 即已授权） | **要**（permission 门） |
| 用户侧触发 | `/skill-name` 手势 | prompts 斜杠命令 | command/*.md + slash:true |
| 来源分层 | project/user/bundled + scope | 项目 + 用户目录 | 内置/项目/外部(.claude,.agents)/配置路径/远程 git |
| issue 工作流落点 | 无内置（靠工具扩展） | prompts 模板 + AGENTS 纪律 | agent 角色（白名单）+ 窄专用工具 + command |

**三家的共同结论**（agent-demo 照抄时优先对齐这些）：
1. 常驻的只是“技能目录”，正文永远按需加载 → 加再多的技能也不费常驻 token
2. 技能 = 指令，不造新执行能力；执行走通用工具（read/bash）
3. 是否给模型加“skill 加载工具”是可选项：PI 证明 read 就够了（省一个工具），
   DSH/opencode 用专用工具换结构化返回（含技能目录文件清单）
4. 权限/批准是独立维度：opencode 甚至对“加载技能”单独要批准

---

## 2. 三家怎么处理 GitHub issue/PR（本项目当前主线的对照）

| | DSH | PI | opencode |
|---|---|---|---|
| 工具形态 | 无 gh 专用工具（靠扩展生态） | **零专用工具** | **极窄专用工具**（`github-triage.ts` 只做“指派 assignee”一件事） |
| 读 issue/PR | — | prompt 教模型 `gh issue view --json …` | command 教模型 gh cli 搜索；agent 角色声明权限 |
| 工作流载体 | — | `.pi/prompts/is.md`（只分析不实现）、`pr.md`（审 PR 不切分支）、`wr.md`（收尾：changelog→评论→commit closes→push→close issue） | `.opencode/agent/triage.md`：专职 agent，`tools: {"*": false, "github-triage": true}` 白名单；`.opencode/command/issues.md` 按 gh 搜 issue |
| 模型路由 | — | command 可 `subtask:true`、指定专用模型 | command frontmatter 指定 model（如 claude-haiku）；agent frontmatter 指定 model |
| 防呆纪律 | — | AGENTS.md：`gh comment --body-file`、只 stage 自己的文件 | AGENTS.md 同款 + tool 用 `GITHUB_TOKEN` 直连 API |

要点：
- PI 与 opencode 都**不把“读/搜 issue”做成专用工具**——模型用通用 bash + gh cli
  （或 command 指引）完成；专用工具只留给“有副作用、需要权限/随机选择”的一步
  （opencode 的指派工具）
- issue 分析的正确姿势两家一致：**不信任 issue 里写的根因**，独立读代码验证
- 收尾路径（PI `/wr`）是完整闭环样板：最终评论必须经 `--body-file`、结尾带
  AI 声明行、commit message 带 `closes #N`、push 后 `gh issue close --reason completed`

---

## 3. 对 agent-demo 的落点候选（讨论档案——落地规则以 AGENTS.md 为准）

> 本节是 2026-09 讨论后的**候选方案记录**（为什么这么选、有哪些取舍），
> **不是本仓库的实现约定**。若方案定稿落地，把该怎么做提炼进 AGENTS.md
> （约定节），本节保留为背景与动机。

agent-demo 现状：Python 四层单向架构、日志唯一事实源、已有 read/bash/edit 等
通用工具 + approval 门、gh CLI 已装已登录（uphome）、无 skill 基础设施。

### 3.1 候选方案（2026-09 讨论倾向：PI 式 + 工具结果注入）

复刻“按需能力”的最小教学版，参照 **PI 式 + 工具结果注入**：

1. **技能存储**：仓库内建 `skills/` 目录，技能 = 中文 markdown + frontmatter
   （name/description），第一个技能写 `gh-issue`（照 `.pi/prompts/is.md` 缩水：
   只分析、不实现、不信任 issue 内根因）
2. **目录（catalog）常驻 system**：技能目录是**静态**的（技能文件不变 → 目录
   字节不变），作为普通静态 section 注入 system（纯文本行，附相对 workspace
   的 location）。落点规则：**固定 order、放在 todo 等动态 live 段之前**——
   保证目录处于缓存稳定前缀，不被动态段拖累（todo:state 现状 order=100）
3. **正文 = 工具结果注入（讨论中用户已拍板此方向）**：模型用现有 **read_file**
   读技能文件 → 正文作为 **tool/result**（source.kind='tool'）进 derive_messages
   → 模型下一步请求读到。与读任何文件机制一致：落日志可重建、可被 compaction
   折叠、前端画成"读取技能"工具卡片——不是用户气泡、不是 DSH 注入式 user
   快照（对比：DSH/opencode 用专用 skill() 工具 + 注入式；PI 用 read +
   location，本仓库倾向 PI，理由见第 1 节）
4. **不新增加载工具**：倾向不建 skill() 工具
5. 执行走现有 bash + gh（bash 已有 approval 门；如需 gh 只读免批，再讨论
   白名单——那是 bash 层改动，与 skill 机制正交）
6. 若未来要“专职 triage agent/子任务/模型路由”，再参考 opencode 的
   agent + command frontmatter——那是更大的角色系统，非本期

### 3.2 候选方案的缓存考量（讨论记录，非约定）

- 技能目录静态 → system 前缀稳定 → 缓存无损；**技能文件改动只损失当轮**
- 目录 section 的 order 应**小于 todo:state 等动态 live 段**——目录保持在
  每次命中的缓存前缀内（规范以 AGENTS.md 约定节为准）
- 目录保持短（description 截断，参考 DSH `catalogDescriptionMaxLength` 思路）

### 3.3 候选实现面（落地时照此评估，具体以 AGENTS.md/实现为准）

- `agent_demo/skills.py`：`Skill` 值对象（frozen dataclass：name/description/
  path/model_invocable）+ `scan_skills(dir)`（dir 参数化，为多 agent 留缝）+
  `format_catalog()`（纯文本目录行）
- `factory.py build_agent`：注册 `prompt.section('skill:catalog', order<100,
  …)`——目录随 system 进 request/header 落日志，可重建
- `skills/gh-issue.md`：第一个技能（frontmatter + 中文工作流正文）
- 测试：scan 解析 / 目录注入进 system / 正文不进常驻 system（只 read 时进来）
- 质量门三绿后提交

### 3.4 候选方案的语义要点（为什么倾向它）

- “模型看过技能正文” = 一次 read_file 的 tool/call + tool/result 事件 → 完整
  可重建、可审计（它读了哪个技能、什么时刻、按什么干的活）
- 正文用后能被 compaction 折叠，需要时再 read——不占常驻 token
- 技能正文在日志里与普通文件读取**无机制差异**——这正是“技能 = 指令文件”的
  教学点：不需要特权通道

实施顺序（待落地时走）：先在 NEXT_STEPS.md 记设计 → 落地 skills 扫描 + 注入 +
gh-issue 技能 → Web/CLI 实测“处理 issue #N” → 质量门三绿提交。

## 4. 待讨论：todo 与"运行时状态栏"（2026-09 记录，未决）

> 背景：讨论「模型每步是否知道自己在做什么、进行到哪一步」时，演化成
> 「todo 是否该成为通用 agent 状态栏的一个贡献者」。本节记录问题与对照。

### 4.0 关键调研：DSH / opencode 的"运行时状态注入"机制（2026-09 实测源码）

讨论中发现的直接参照——两家都有**把动态状态注入模型**的成熟机制，且形态
惊人地相似（user 消息快照 + 变化才更新 + 注册贡献者）：

**DSH：runtime context（`packages/core/agent-loop/src/runtime-context.ts`）**
- 位置：**不在 system**，作为 **user 角色消息** append 进 messages 尾部
  （`preStep`：`messages: [...claimed, context]`）
- 形态：`Current runtime context. This snapshot supersedes earlier…` + 各贡献
  者内容（policy / todo / time-context / approval 状态等——全是注册制贡献者）
- 核心：`RuntimeContextProjection.project(current)`——**内容变化才 append 新
  快照**（`retained.text === snapshot` 则跳过）；compaction 遮蔽旧快照时置
  retained=null，下轮重新投影
- 贡献者注册：system prompt 的 contexts 桶（`systemPrompt.context(...)`），
  persona 可 `includeRuntimeContext: false` 整体关掉

**opencode：SystemContext（`packages/core/src/system-context/`）**
- 同思路但更"事件化"：每个 context 有 `baseline`（首见）+ `update`（变化时），
  SystemContextRegistry 注册；builtins 就有 **environment + date**（即那五种
  里的"系统状态"与"时间戳"的工程版）
- 注意：**todo 不走 SystemContext**——todowrite 的 `toModelOutput` 直接返回
  完整清单 JSON 作为 tool/result（模型从历史读最新），这是另一条路

**共同结论**（对 agent-demo 的启示）：
1. "运行时状态" = user 消息快照放 messages 尾部（不是 system）→ 前缀缓存稳定
2. **变化才更新**（去重）而非每轮合成——避免"状态没变也重复附加"
3. 注册制贡献者（每个插件/模块注册自己那块状态）——与 agent-demo 的
   `prompt.section`/`ToolRegistry` 同哲学
4. 快照作为 user/plugin 消息**落日志** → 完全可重建（agent-demo 若做需扩展
   source 类型 + compaction 联动，中型改动）

### 问题 1：todo_write 的完整结果到底该放哪？（已收敛到"状态栏"方向）

现状（agent-demo）：`todo_write` 执行时把完整清单写进 `todo/write` **痕迹事件**
（不进 derive_messages），返回给模型的 tool/result 只有**计数摘要**
（"Updated todo list: 3 pending, 1 in progress…"）。完整清单靠
`fold_todos()` 折叠 + system 里 `todo:state` live section 注入模型。

对照三家（详见第 2 节与 4.0）：
- opencode：todowrite 的 `toModelOutput` 返回**完整清单 JSON** → 作为 tool/result
  进消息历史，模型从历史读最新清单；无 system 注入、无独立 section
- PI：无 todo 机制
- DSH：todo 作为 **runtime-context 的贡献者**——动态上下文渲染成 user 角色
  快照消息进历史（durable snapshot + 变化才更新 + compaction 联动）

**讨论方向（2026-09 已多次往返）**：用户提出"todo 状态栏以 XML 框住、放每轮
消息末尾（每轮替换、不破坏前缀缓存）"——这与 DSH runtime-context / opencode
SystemContext 的形态一致，todo 只是通用状态栏的第一个贡献者。DSH 的
"变化才 append + 落日志 + 注册贡献者"是完整工程参考；简化版可不落日志、
每轮 fold 现算合成。

### 问题 1 结论：方案 A（2026-09 已定稿，待实现）

**关键讨论澄清**：状态栏**不进日志**——每轮模型请求的 messages 从日志
`derive_messages()` 重建，瞬态合成消息不在日志里 → 下一轮重建后不存在。
因此"模型看之前的"不成立（对比 DSH：快照是日志消息，模型能从历史 derive
读到，去重才安全）。**结论：不物化路线必须每轮都叠**（状态栏是 todo 唯一
可见通道），不能做"不变就不加"的跨轮去重。

**方案 A 规格**：
- 状态栏只存在「模型调用过 todo_write 且清单未全部 completed」时的每轮
  组请求中；普通对话（无 todo）、清单全 completed（任务收尾）→ 不叠
- 形态：role=user 合成消息，XML 包裹，叠在 messages 末尾
  ```xml
  <todo_status>
  1. [completed] 加载技能
  2. [in_progress] 验证根因
  </todo_status>
  ```
- 改动清单：
  - `agent_demo/tools/todo.py`：新增 `build_todo_status(session)`——fold 出
    清单 → XML `<todo_status>` 块；无清单或 `all_completed` 返回 None
  - `agent_demo/loop.py _run_step`：组 messages 时若 `build_todo_status` 非
    None 则 append 一条 `create_user_message([TextBlock(text=status)])`
  - `agent_demo/factory.py`：删 `todo:state` live section + `_todo_context`
    （todo 离开 system；注释同步）
  - `web/index.html`：不改——前端 dock 由 todo_update 帧驱动，状态栏只影响
    模型上下文
  - 测试：system 含 todo 的断言改 messages 含状态栏；补 XML 格式/全
    completed 不叠/无 todo 不叠测试
- 不变式对照：状态栏 = fold_todos(日志) 现算 → 同一日志同一状态栏 →
  resume 可重建 ✓；完整清单已由 todo/write 痕迹记录（审计可重建）✓；
  不进 derive_messages → 历史零污染 ✓
- 缓存：状态栏在 messages 尾部 → 前缀（system+历史）稳定命中，只有尾部
  新内容 ✓（对比 system 动态段一变断全前缀）
- **未来扩展**（已共识方向、非本期）：状态栏容器可含多块
  （`<agent_status>` 内 `<todo_status>` + `<goal>` + 未来贡献者）；
  goal 语义与机制待单独讨论（DSH 参照：`<goal_round>` XML + objective/phase
  生命周期 + 独立 driver，与 runtime-context 是两套机制）

### 问题 2：长任务中 LLM 是否知道自己进行到 todo 的哪一步？

现状：模型每步请求只能看到「system 注入的 todo 清单 + 历史 surface 消息」；
turn/step 序号、step/start、todo 痕迹都是**模型不可见的痕迹事件**。模型既不被
告知「当前第几步/共几步」，也看不到自己上一步的思维链（reasoning 不回灌，
只走 assistant/reasoning 痕迹）——只能从「上一步 assistant/message 文本 +
工具结果 + 清单里哪项标了 in_progress」自行推断进度。

对照三家：opencode / PI / DSH **都没有**把「你正处在第 N 步」显式注入模型
（三家同样靠模型从历史 + todo 状态自推断）。

**未决点**：是否做「进度指针注入」——把 system 里的 todo 文本从纯清单升级为
带指针的进度报告（已完成 N/M、当前在做 X、下一步做 Y），让模型每步显式看到
自己的位置；以及是否要求模型把「这一步怎么做」写进可见文本而非思维链
（思维链不回灌是刻意设计，回灌深度是独立话题）。

### 问题 3：开放任务回合不收敛——agent 深调查无"够了先汇报"的收敛压力

现象（2026-09 实测实录）：处理 GitHub issue（CSS 布局问题）的单个回合内，
模型连续 15+ 次模型请求、24 次工具调用才收敛——每轮都"再查一下"（读不同
文件区段/不同 grep 模式），信息在增长但迟迟不给结论。分析不是循环（每轮
读不同内容、无重复调用），但暴露**回合缺少收敛压力**：

- 无请求次数/工具调用上限：模型可以无限"深入调查"下去
- 提示词未引导"信息足够就先汇报结论，不足再查"（与 gh-issue 技能要求的
  "独立验证"叠加，容易无限扩大调查面）
- 后果：长回合期间用户无法及时得到中间结论；配合当时的 SSE 断开 bug（已修，
  见 web_app sse_stream finally agent.cancel）还会放大"停不下来"的观感

**候选方向**（未决）：回合内请求次数上限（如单 step 最多 N 轮工具循环后强制
要求给结论）；或提示词/技能正文加收敛纪律（"验证核心事实后即汇报，把后续
验证留给用户决定"）。DSH/opencode/PI 是否有对应的收敛机制待查（可作下一轮
讨论的 codegraph 调研目标）。

## 5. 待处理消息怎么显示：分区渲染 + 提交回显——2026-09 已定稿并落地

### 5.1 问题：插队消息没有"位置"

`steer` 消息进 `next-step` 队列后，要等当前 step 结束、下一次 `claim()` 才落
`user/message`（surface）。这段"半开窗口"里它在日志里**没有 seq 位置**（实测
延迟 890~1698 个事件）。上一版前端为了"用户必须立刻看到发出去了"，把它当
乐观气泡插进消息流，靠 `(turn, step)` 锚点猜顺序——结果位置反复出错
（气泡被后续输出挤到中间/下方），见 `mountPendingUser`/`pendingAnchor` 两次
修 bug 的记录（提交 `8b17c24`、`00533de`）。

### 5.2 DSH 的做法（2026-09 读源码实测）

**⚠️ 先纠正一个流传过的错误结论**："DSH 从不把待处理消息画进消息流"——**只对
`queued` 成立**。真实的三分法（`placement` 决定渲染面）：

| placement | 渲染在哪 | 证据 |
|---|---|---|
| `transcript`（idle 发送的回显） | 消息流尾部（普通气泡） | `ChatView.tsx` 的 `visibleSubmissions` |
| `steering`（next-step 插队） | **消息流尾部** + `data-pending-steering` 标记 | `ChatView.tsx:284,802`（`pendingSteering = inbox.filter(placement==='steering')` → `PendingSteeringBubble`）、`MessageItem.tsx:174,200` |
| `queued`（next-turn 排队） | QueueDock（composer 上方 `conversation.input.dock` slot，`order: 20`） | `QueueDock.tsx:66,70`（只取 `placement==='queued'`） |

所以"位置 bug"的正解不是"不画"，而是**恒定贴尾**：插队消息马上要进对话，
它就该是一条贴尾的 pending 气泡；claim 之后 durable 节点落到自己真正的 seq
位置，交接时旧的那条消失。

### 5.3 三个配套机制（DSH 源码对照）

1. **`SessionQueuedItem{id, placement, rpcId?, message}`**——队列项是会话层
   快照（`SessionSnapshot['queue']`）的一部分，不是渲染层算出来的。
2. **`PendingSubmission`（本地提交回显）**——`session.beginSubmission({mode,
   text, images})` 在序列化/发请求**之前**同步登记：铸 `requestId =
   randomUUID()`，placement 当场定（`running` 为假 → `transcript`；为真且
   `mode==='steer'` → `steering`；否则 `queued`）。`prompt(content, mode,
   signal, requestId)` 把身份带上；Host 回显进 durable `user source.rpcId`，
   队列 occurrence 也投影成 `SessionQueuedItem.rpcId`。
   **退休**走单一出口 `finishSubmission`：`observed`（看到 durable 事件/队列项）
   → **延后一个动画帧**退休（保证替代内容就绪前回显仍可渲染）；`failed`
   （被拒/放弃/销毁）→ 立即退休；`onRetire` 恰好触发一次。
   `observedRpcIds()`（durable 节点 source.rpcId + 队列项 rpcId）让回显在
   **同一次渲染**里消失——交接原子，不重复也不留空档。回显只活在客户端内存，
   刷新/重连只从 durable 事件重建。
3. **`QueueAction`**（`packages/api/session-controller/src/types.ts:148`）：
   `{kind:'edit', content} | {kind:'remove'} | {kind:'steer'}`，入口
   `session.updateQueue(itemId, action)`。UI 三件：行内编辑、删除、
   "提升为插队"。另有 `steerQueue()`（`input/hub.ts:198`，绑 Cmd/Ctrl+Enter
   "插话发送全部排队消息"）把整队 `queued` 逐条 `{kind:'steer'}`；
   `session/queue-item-not-found` 静默收敛（行可能已被 host 处理），
   `session/steer-unavailable` 直接返回。

### 5.4 本仓库落地方案（已实现）

- **分区渲染**：`placement='queued'` → 输入框上方 `#queue-dock`；
  `placement='steering'` → 消息流尾部 `#messages > .flow-tail` 的 pending
  气泡（`.msg.user.pending-steering` + "插队 · 待处理"标记）。尾部容器在
  每次挂载节点后 `renderFlowTail()` 重新 append，永远保持最后一个子节点。
- **队列是状态层投影**：`Inbox.queued_items()` 折重放结果产出
  `QueuedItem(placement, message)`（next-turn→`queued`、next-step→`steering`），
  和 `Session.derive_messages()` 并列——都是"日志 → 不可变投影"。和 todo 一样
  "不物化"：日志里没有独立队列状态，投影是纯函数。
  - **分层教训**：投影一度写在 `web_app._queue_rows()` 里自己重放
    `agent/inbox/spliced`——后果有两个：① 同一事件类型出现**两份折叠实现**
    （`Inbox._apply` 一份、web 一份），语义一变就分叉；② 投影绑死在 Web
    宿主上（CLI、测试都拿不到，测试要绕过 web 模块才测得到）。现在折叠只有
    一份，web 层只把值对象摊平成 JSON。
  - `QueuedItem.rpc_id` 取自 `message.source`（不另存副本）：同一条消息在
    "队列项"与"durable 消息"两个形态下带的是**同一个提交身份**。
- **提交身份 `rpc_id`**：前端 `beginSubmission()` 铸 uuid（`crypto.randomUUID`
  缺失时退化计数器）→ `/chat`、`/steer` 带 `request_id` → `UserSource.rpc_id`
  落进 durable 消息（JSONL 里 `{'$user': '<rpc_id>'}`，旧格式 `true` 兼容读）
  → 队列项与 `user_message` 帧都带出来。前端 `visibleSubmissions()` 用
  `observedRpcIds()` 过滤，`finishSubmission(id, 'observed')` 延后一帧真删，
  `'failed'` 立即删。
- **三条推送通道**（幂等，互为兜底）：
  1. SSE `queue_update` 帧（`agent/inbox/spliced` → 全量快照）
  2. `POST /steer` 响应带 `queue`
  3. `/history` 与 `/sessions/*/switch`、`/sessions/new` 带 `queue`
- **队列动作**：`POST /queue/update {item_id, action}` → `Agent.update_queue`，
  返回状态码与 DSH 的错误码同名：`ok` / `queue-item-not-found`（并发收敛，
  HTTP 仍 200）/ `steer-unavailable`（空闲时不能提升，没有"下一步"）/
  `unknown-action`。状态层原语是 `Inbox.edit`（同 id 原地换文案，一次原子
  splice）/ `Inbox.promote`（two-splice 搬家，摘除那步 `discard=False`——
  搬家不是丢弃）/ `Inbox.remove`（`outcome='canceled'`）。
  前端行内：✎ 编辑（就地输入框，Enter 保存 / Esc 取消 / 失焦保存）、
  × 撤回、↥ 提升（仅运行中）；多条时头部有"全部插队"按钮，草稿为空 +
  运行中 + 有排队项时 `Ctrl/Cmd+Enter` 也能整队插队。
- **两处有意的差异**（本仓库多给的，记在这里免得当成漏做）：
  1. DSH 的 `edit` 带 `content: ContentBlock[]`，我们只有文本框，收 `text`；
  2. DSH 的 pending-steering 气泡只有复制类图标动作，我们额外给了 × 撤回。
- **兜底对账**：回合收尾（正常结束/停止/断开）后 `refreshQueue()` 拉一次
  `/history` 快照——取消时服务端清空 inbox 的 spliced 事件推给了已关闭的流，
  没人读。
- **渲染合并**：同一事件批次里可能连推多条 `queue_update`（普通发送是
  「入队 queued → 第一步立刻 claim 空」），合并到微任务末尾只画最终态，
  所以"发出即被认领"的消息不会闪一下。

## 6. web_search：联网搜索走官方原生能力（2026-09 已定稿并落地）

### 6.1 问题：第一版自己抓网页

工具集里 `web_search` 要"读工作区之外"，第一版实现抓 DuckDuckGo 的 HTML
页面、正则抠结果、还原 `uddg=` 跳转参数。问题不在于能不能跑，而在于**收集
能力建立在猜页面结构上**：对方改一次模板就全废，而且只能拿到标题+链接。

### 6.2 DSH 的做法（读源码实测）

DSH 把"搜索"这一能力**完全交给提供方**，自己只做协议与解析：

- `packages/web/web-search-deepseek/src/provider.ts`：搜索 = 向 DeepSeek 的
  **Anthropic 兼容**端点 `https://api.deepseek.com/anthropic/v1` + `/messages`
  发一次 Messages 请求，body 里挂**服务端工具**
  `{type:'web_search_20250305', name:'web_search', max_uses:N}`。注释写明
  **这不是 chat-completions 的 base**（`https://api.deepseek.com`），
  **只共享 API key**。DeepSeek 没有专用搜索端点，所以一次搜索 = 一个完整
  模型轮次（延迟 + token 都按模型算）。
- 结果只从**结构化块**取：`content[]` 里的 `web_search_tool_result` →
  `web_search_result{url,title,page_age}`；snippet 来自 text 块的
  `citations[].cited_text`（按 url 拼）。**绝不从模型正文里抓 URL**。
  没有结果块 = `WEB_PROVIDER_ERROR` **响亮失败**，不降级。
- `packages/web/tool-web/src/search.ts`：模型可见的工具形状——
  `queries: string[]`（`WEB_SEARCH_MAX_QUERIES = 4`）、结果格式
  `formatSearchOutput`（外部内容提示 → `Sources:` 列表 → 截断提示 →
  引用纪律）；`trust.ts` 的 `EXTERNAL_WEB_CONTENT_NOTICE` 提醒模型
  搜索结果**是不可信数据**。
- 另有 `web-search-exa` / `web-search-perplexity` 两个 provider 与 `web_fetch`：
  provider 是可替换的接缝（我们这个 demo 不需要，一个够）。

### 6.3 本仓库落地（与 DSH 的对应与差异）

| DSH | 我们 |
|---|---|
| `web-search-deepseek` provider | `agent_demo/tools/web_search.py` 的 `deepseek_search_backend`（默认后端，可注入） |
| `tool-web` 的 `web_search` 工具 | 同名工具，schema/输出格式照抄 |
| `web/deepseek-search-llm-request` 痕迹 | **`web/search`** 痕迹事件（query/endpoint/model/max_uses，无 key） |
| `WebError(code)` 抛给调用方 | `WebSearchError(code)` → 工具层降级为 `is_error` 结果（不变式⑤） |
| provider 按次投影 Settings（凭证/端点/上限） | `SearchConfig` 值对象：工具层解析一次，**trace 与真实请求共用** |
| 多 provider + `web_fetch` | 未做（单 provider；`web_fetch` 待定） |

**实测形状差异**（对着真响应验的，别照抄 DSH 注释里的字段假设）：
`page_age` 实测多为 `null`；`text` 块实测**没有** `citations`；`web_search_result`
还带一个 `encrypted_content`（我们不用）；`usage.server_tool_use.web_search_requests`
是本次服务端搜索次数（可留作审计，未用）。

**不做 approval 门**：判据是"不可逆/会执行/会改磁盘"，它不沾；而且搜索请求打的是
**同一家厂商**的另一个端点——会话里读过的文件本来就随每次聊天请求发给它了，
增量外泄面只是"这句 query 会到搜索索引/第三方去"。DSH 也不给 web 工具审批门。

**真实成本**（实测一次）：`input_tokens ≈ 11.3k / output_tokens ≈ 1.2k`——
所以超时给 60s（工具 65s），`max_uses`/`max_results`/`max_queries` 都压在 4~5。

