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
