# 阶段一实施进度与后续工作计划

> 记录 `ARCHITECTURE.md` 进化路线图"阶段一：真实工具集"的实施进度、
> 已定设计决策与后续工作清单。配合 ARCHITECTURE.md 第 6 节阅读。

## 当前进度

| 任务 | 状态 |
|---|---|
| read_file 升级（行号 / offset / limit / line_numbers 开关） | ✅ 已完成（含测试） |
| grep / glob 工具 | ✅ 已完成（标准库实现，含测试） |
| 轻量路径沙箱（`--workspace` 边界） | ✅ 已完成（含测试） |
| edit 工具 | ✅ 已完成（含测试） |
| bash 工具 | ✅ 已完成（含测试） |
| approval 确认门 | ✅ 已完成（含测试） |
| CLI 显示优化（A+B 档） | ✅ 已完成（颜色分层 + 结果摘要 + 进度指示） |
| resume 历史重放 | ✅ 已完成（`--resume` 打开会话 = 看到完整历史对话） |
| Web UI（最小方案） | ✅ 已完成（FastAPI + SSE + 深色单页：流式/思考折叠/工具卡片） |
| Web UI 增强 | ✅ 已完成（会话新建/切换/删除/改名、approval 批准按钮、停止按钮、常驻 todo dock） |
| 上下文压缩（compaction 全套） | ✅ 已完成（surface replace 位置语义 → 四步事务 → checkpoint → 自动阈值 → 溢出恢复 → Web 手动压缩按钮） |
| Web ContextMeter（占用圆环 + 会话账） | ✅ 已完成（占用快照圆环 + 点击面板：会话累计消耗 / 全会话缓存命中率） |
| Web 会话并发隔离（seat 化） | ✅ 已完成（两会话并行互不干扰，见下节） |
| steer 插队 + CLI REPL | ✅ 已完成（双队列第二队实战入口；CLI 无任务参数进 REPL） |
| **Web 前端统一消息投影** | ✅ 已完成（前端 nodes 投影；实时 SSE 与 /history 收敛同一渲染，见下节） |
| **web_search（联网搜索）** | ✅ 已完成（DeepSeek 官方原生搜索，见下节） |
| 阶段一收尾（更新 README / ARCHITECTURE 定稿） | ✅ 已完成（含 2026-09 架构重构与本文档同步） |

> 当前全量测试：85 passed（AGENTS.md 里的数字保持同步）。

## read_file 升级（已完成）

- 参数：`file_path`（必填）/ `offset`（1-based 起始行，默认 1）/ `limit`（最大行数，默认 200）/
  `line_numbers`（默认 true，LLM 自己决定要不要行号）
- 输出：固定宽度右对齐行号 `   1: content`；窗口截断时末尾追加导航提示
  `(file has N lines; showing lines S-E; increase offset to continue)`
- 错误一律降级为 `is_error` 结果（不炸循环）：非整数参数、offset < 1、limit < 1、
  offset 越界（附文件总行数）、line_numbers 坏值
- 空文件返回 `(empty file)`（信息，不是错误）
- 设计要点：行号 = 坐标系（模型可引用"第 N 行"，edit 工具的前置）；窗口 = token 预算可控
  （成本 O(窗口) 而非 O(文件)）；截断提示 = 方向感（模型必须知道窗外还有内容）

## 思维链可视化（已完成）

- 能力层统一解析 `reasoning_content` / `reasoning` / `thinking`，映射为 `StreamChunk.reasoning`
- 循环层落非 surface 痕迹事件：`assistant/reasoning/chunk`（流式）+ `assistant/reasoning`（完整）
- 思维链**不回灌模型**，不进入 `derive_messages()`
- CLI 默认彩色实时显示，`--hide-reasoning` 可折叠隐藏
- FakeLlm 支持 `reasoning` 字段，离线 demo / 测试可覆盖

## grep / glob（已实现：路线 A 标准库）

对齐 deepseek-harness 的实现（`packages/fs/tool-fs-search/` + 架构笔记
`2026-07-09-bash-backed-grep-glob-discovery`），用标准库（`pathlib` + `re` +
`fnmatch`）实现：零依赖、Windows 开箱即用；schema / 输出格式 / 预算对齐 harness。

- **schema 极简**：`grep(pattern, path?, include?)`、`glob(pattern, path?)`；
  预算（上限、超时）不进模型 schema
- **grep**：正则匹配；默认大小写敏感（不公开参数）；输出
  `Found N of M matches` + 按文件分组 `Line N: ...`；匹配上限 250（`GREP_MAX_MATCHES`）；
  `path` 可以是单个文件或目录（对齐 harness）
- **glob**：路径模式（如 `**/*.py`）；每行一个相对路径；路径上限 100（`GLOB_MAX_RESULTS`）
- **错误语义**：坏正则 → `is_error`；路径不存在 / 不是目录 → `is_error`；0 命中 → 正常结果
- **跳过隐藏条目与 `__pycache__`**：`.git` / `.sessions` / `.codegraph` / `.env` 不进搜索结果
  （隐藏条目是噪音甚至敏感数据——`.env` 里有 API key）
- **include 校验**：单个正向 glob，拒绝 `!` 否定与逗号列表（对齐 harness）；
  include 预编译成正则（更快，坏模式在编译时刻报错）
- **与 harness 的已知差异**：Python 3.13 的 pathlib 对畸形 glob 模式宽容
  （按字面量处理，不抛错），不像 rg 会报 invalid-regex——工具如实返回空结果，
  模型自查修正；executor 保留 `re.error` / `ValueError` 兜底防御其他平台/版本
- 将来要升级真 rg（路线 B）：工具层形状不变，只换 executor

## 轻量路径沙箱（`--workspace` 边界，已完成）

**动机**：模型路径拼接是高频翻车点（幻觉路径、`..`、跨项目污染）；工作区边界
把"写错地方"的破坏限制在用户声明的目录内。这是**防误用保险**，不是防恶意、
也不是防"工作区内乱改"（后者是 approval 门的职责）。

- **边界**：`--workspace` 必填（安全边界必须由调用者显式声明）；`build_tools(workspace)` 
  注入——测试传 tmp_path，生产传用户选定的目录（"边界是注册时的声明"）
- **原理**：`_resolve_in_workspace()` = 相对路径锚定到 workspace → `resolve()` 
  归一化（折叠 `..`、解符号链接）→ `relative_to()` 逐段前缀匹配 → 越界返回
  `is_error`（模型看到原因自己改正）
- **覆盖**：read_file / list_files / write_file / grep / glob / edit / bash 全部走
  统一入口（edit / bash 的路径参数自动继承边界）
- **对齐 harness**：`canonicalPath()`（realpath 归一化）+ `writableRoots()` 
  （allow-list 单一事实源）+ `dsh-fs-sandbox`（进程内 fence）——同构思路；
  harness 在其上还有 OS 级强制（landlock / bwrap / restricted token / seatbelt）
- **局限**（README 安全警告如实声明）：工作区内全裸、TOCTOU 竞态、非 OS 级
- **演进预留**：OS 级沙箱留给 bash 落地之后——届时 bash 的执行做成可替换后端，
  沙箱 runner 是第三个后端（Windows: CreateRestrictedToken + CreateProcessAsUserW，
  Linux: bwrap / Landlock，macOS: seatbelt），继承 harness 的 fail-closed 哲学

## edit（已完成）

精确字符串替换式编辑：`edit(file_path, old_string, new_string)`，替换
`write_file` 全量覆盖（省 token）。对齐 harness `str_replace_editor` 的
`str_replace` 语义：

- **字面量精确匹配**（非正则），缩进/空白敏感——模型先 read_file 看准确内容再改
- **零匹配 → is_error**：提示检查空白/缩进，或先 read_file
- **多匹配 → is_error**：报所有出现行号，要求扩大上下文使其唯一——刻意没有
  `replace_all`（改错位置比拒绝更危险，宁炸勿静默）
- **new_string 可选**，缺省空 = 删除片段
- 成功返回 `edited <path> (replaced at line N)`——模型用 read_file 验证闭环
- 路径走 workspace 沙箱（自动继承）；文件不存在 → is_error
- 与 write_file / bash 一起声明 `requires_approval`（写操作统一确认，approval 门已实现）

## bash（已完成）

执行能力：跑测试、git、构建——编码闭环"读→改→验证"的最后一步。
- **参数**：`bash(command, cwd?)`；`cwd` 限制在 workspace 内（沙箱继承）
- **执行后端接缝**：`_run_command()` 收执行细节（shell 语义、合并输出、
  kill-on-cancel）——将来接 OS 级沙箱 runner 只换这一个函数
- **超时 kill-on-cancel**：超时/取消时杀掉子进程再传播（不泄漏卡死进程）；
  `build_tools(bash_timeout_s=60.0)` 注入短值供测试验证
- **退出码**：非 0 → `is_error` + `[exit code: N]` 前缀（README 对照表
  harness 的"跨调用准则"在此补上）；0 且无输出 → `(no output)`
- **输出截断**：`BASH_MAX_OUTPUT_CHARS = 8000` + 重定向导航提示
  （教模型：大输出写文件再 read_file 分页读）
- **命令级刹车是 approval 门**（已绑定）：命令本身不过路径沙箱
  （无法校验命令里的路径），受限的是 cwd；执行前需人工确认
- prompt 已加 `tool:bash` 提示（测试用 `conda run -n agent-demo python -m pytest -q`）

## approval 确认门（已完成）

敏感工具（bash / write_file / edit）执行前的人工确认——阶段一最后一块
"刹车"，对齐 harness 的 approval/权限桥。

- **注册声明**：`ToolSpec.requires_approval: bool = False`——"要不要确认"是
  工具的性质（写/执行要，只读不要），循环不写业务 if（不变式④）
- **确认钩子**：`Hooks.approval: (name, arguments) -> bool`——"怎么确认"是
  策略：默认实现 CLI stdin 交互（`[approval] bash(...)? [y/N]`，fail-safe
  默认拒绝），测试注入假确认
- **拒绝路径**：落 `tool/skipped`（痕迹，`reason: 'not-approved'`，审计）+
  `tool/result`（surface，`is_error`，`skipped: user did not approve`）——
  模型**必须**看到"没执行"，否则以为工具跑过了（模型可见 ⟺ 可重建）
- **拒绝是结果不是失败**：模型拿到 skipped 会自己调整方案，循环正常继续
- 敏感工具声明 `requires_approval=True`，并且**独占执行**（2026-09 起
  `execution_mode` 默认就是 `'sequential'`，fail-closed：并发许可要自己声明）
- harness 对照：harness 用 `fs/write-intent` / `fs/edit-intent` 事件瀑布
  （waterfall）实现审批桥；demo 用"声明 + 钩子"简化版，功能等价

## CLI 显示优化（A+B 档，已完成）

UI 是日志的投影：on_event 只负责"怎么显示"，状态全在事件里。

- **A 档 颜色分层**：`[tool N]` 黄色、`[result]` 灰色缩进、分隔线/`[req N]` dim——
  一眼分清"模型说的 vs 系统做的"
- **A 档 结果摘要**：工具结果只显示前 3 行 + `(…共 N 字符 / M 行)` 统计，
  看全貌去日志（之前硬切 200 字符断在行中间）
- **B 档 进度指示**：`════ turn N ════` / `── step N.M ──` 边界分隔线 +
  `[req N model]` 请求计数 + `[tool N]` 工具序号
- **tty 感知**：`_USE_COLOR = sys.stdout.isatty()`——管道/重定向时禁用 ANSI，
  颜色只是装饰，不污染被重定向的输出（`_paint()` 统一入口）
- 真实终端里工具调用是黄色、结果灰色；管道里纯文本

## resume 历史重放（已完成）

用户痛点："`--resume` 跑完后看不到之前的对话"——resume 只恢复记忆不重放显示。
修复：渲染逻辑抽成模块级 `_render_event(event, hide_reasoning, state)`（UI 是
日志的投影，同一份渲染同时服务实时事件与历史重放），`run()` 在 adopt 每一条
历史事件时也走一遍渲染——**打开会话 = 看到完整历史对话**，`[req N]`/`[tool N]`
序号从历史连续到新回合。

## 阶段一验收（✅ 已完成）

> "找 bug 并修复、跑测试"端到端跑通。2026-08 用真实模型（deepseek-chat +
> .env 的 DEEPSEEK_API_KEY）验收实录：

- 模型自主组合工具：grep 定位 → read_file(offset) 分页 → bash 跑 pytest
- approval 门实时生效：`[approval] bash(...)? [y/N]` 人工批准后才执行
- 真实 pytest 结果回传：`31 passed in 1.12s`，模型正确报告
- resume 重放：676 事件恢复后继续新任务（记忆 = 日志投影的实战验证）
- 运行提示：`conda run --no-capture-output` 可避免 Windows 控制台中文乱码
  （仅 `PYTHONIOENCODING` 不够，conda run 捕获 stdout 后按 GBK 再打印）

## 阶段一收尾清单

- [x] read_file 升级 / grep / glob / 路径沙箱 / edit / bash / approval（各带测试）
- [x] 用真实模型端到端验收（读→搜→分页→执行→批准→验证→报告 全链路）
- [x] 更新 ARCHITECTURE.md 阶段一表格（全部 ✅）与 README 模块清单/安全警告

## Web UI（最小方案 → 增强，已完成）

浏览器里的 DeepSeek 风格对话——**UI 是日志的投影的第二个渲染器**：
同一份事件流，CLI 渲染成终端、Web 渲染成 DOM，框架代码一行不改。

- **技术栈**：FastAPI + SSE（`web_app.py`）+ 原生 JS 单页（`web/index.html`，
  深色仿 chat.deepseek.com，零构建）
- **桥接**：`session.on_event` → `asyncio.Queue` → SSE 帧；客户端断开即
  `task.cancel()`（取消单向传播）
- **功能**：流式打字、可折叠"已深度思考"块、工具调用卡片（黄/灰）、
  历史加载（`GET /history` 返回 `derive_messages()` 投影——Web 视角的记忆）
- **会话管理**：多会话列表（`GET /sessions` 磁盘行内快扫）/ 新建 / 切换 /
  删除 / 双击改名；首条消息后模型自动概括起名（逐字复读会被拒绝退回
  fallback）——标题是日志投影，没有第二份状态
- **approval**：Web 批准/拒绝按钮（`POST /approval/respond`，钩子经 SSE
  队列推送请求；超时 fail-safe 拒绝）
- **常驻 todo dock**：todo/write 事件实时更新面板（有清单才显示、可折叠）；
  清单跨回合持续（模型可继续更新），**实时**收到全勾清单时播 2 秒"收尾告别"
  再隐藏——恢复路径（切会话/刷新/压缩）遇到的旧全勾清单直接隐藏、不播动画
  （曾经的 bug：动画挂在状态入口上，切窗口时会把别人的完成动画重放一遍）
- **安全**：默认只监听 127.0.0.1；`--workspace` 必填（路径边界不丢）

## 上下文压缩 compaction 全套（已完成）

harness "上下文太大就折叠旧对话"的教学复刻，三个递进的子能力
（对应五次提交）：

- **surface replace 改位置语义**：遮蔽区间按 surface 的【位置】切，不是
  seq 数值——checkpoint 的 seq 大于被它顶替的旧 seq（追加式），replace 后
  surface 不再 seq 单调；位置语义让嵌套/连续多次压缩不乱（曾有 bug 被
  测试钉住）
- **压缩引擎四步事务**：`compaction/start`（加锁）→ `compaction/summary`
  （审计：摘要全文 + 遮蔽范围 + 文件操作）→ `user/message` surface replace
  （checkpoint 原位顶替旧回合）→ `compaction/end`（成功/失败都落）——对齐
  dsh 的 start→summary→replace→end
- **checkpoint 结构化**：摘要提示词融合 dsh 8 段骨架（Primary Request /
  Key Technical Concepts / Files and Code / Errors and Fixes / Pending Jobs /
  Current Work / Next Step / Critical Context）+ PI 三态（Done/In Progress/
  Blocked）与有序 Next Steps；文件操作清单由代码拼入（路径不交给模型猜）；
  preamble + `<compacted-summary>` 标签让后续模型当既定背景、迭代合并
- **自动触发两机制**：
  1. 溢出恢复：模型报上下文过长（HTTP 400 特征串）→ `request_error` 钩子
     压缩后重试（恒开，兜底）
  2. 阈值自动压缩：`turn/end` 后量上下文超阈值（默认 0.5M = 1M 窗口一半，
     `--compact-at` 可调/关闭）→ 后台压缩
- **手动压缩**：`POST /compact`（复用四步事务）——fake 模式拒绝（脚本模型
  不能真摘要）、agent 运行中拒绝（动 surface 必须串行）、无可压段给提示

## Web ContextMeter 占用圆环（已完成）

发送按钮旁的 45px 触发钮（内含 20px+ SVG 环），对齐 dsh ContextMeter：
**常态不占输入行布局**——百分比/明细全部收进点击展开的悬浮面板（absolute
定位，不参与流布局），面板内各行独立成段留距。

- 圆环数据：占用快照（`used/window/percent`，来自最后一条真实 usage 或
  字符估算兜底）；过半（≥50%）黄、临限（≥90%）红——与压缩线呼应
- **会话累计账**（对齐 dsh token-meter 的 totals 投影语义）：
  - 全会话累计消耗 `Σ(input+output)` token
  - 全会话缓存命中率 `Σ hit / Σ prompt`（token 加权"总账"，不是逐请求
    平均也不是单次快照）；某请求没报缓存拆分就不计入命中统计（宁缺毋滥）
- 来源：`session_token_totals()` 扫全部带 usage 的 assistant/message 累加；
  fake 模式无真实 usage → 面板不显示累计账
- 手动压缩按钮在面板底部（见上节）

## Web 会话并发隔离（已完成）

Web 端原先是**全局单例**：`_session/_agent/_current_sid` 指向"当前会话"、
`_active_queue` 是唯一活跃 SSE 队列——两个标签页各跑一个会话时会互相覆盖
（A 的审批推到 B 的流、事件只进焦点会话）。seat 化改造：

- **Seat 注册表** `_seats[sid]`：每个会话一个 `{session, agent, queue, approvals}`，
  get-or-create（切回会话复用运行中的 agent，不重建销毁）；`init_web` 清空
  （测试隔离目录不串 state）
- **approval 钩子 per-seat 闭包**：审批请求推本 seat 的队列、等待表存
  seat.approvals（旧实现读全局 `_active_queue`）——各会话审批天然隔离
- `/chat`、`/history?sid=`、`/compact` 按 sid 路由（缺省焦点会话，兼容旧前端）
- `turn/end` 附带的 context 用事件所属会话而非全局焦点
- 测试：两会话并行各跑一轮 chat——消息只进各自日志、agent 实例独立、
  seat 复用、焦点别名正确

## 提示词纪律：三条通用规则进 system（2026-09）

**问题（按通用形态记，不针对某次事故）**：agent 在被要求"看看代码"时会执行真实外部
调用、为观察某个行为而重复调用、并跑出静默无输出的命令——每次还占掉一次人工确认。
根因不在某个工具，而在 system 里**只写了"要验证"，没写"验证的边界与成本"**：
`identity`/`persona`/`tool:bash` 三处都把"验证"等同于"跑命令"，而"什么时候读就够了"
"这次调用值不值"从未被写下来；同时 `AGENTS.md` 里记着的坑（如内联多行脚本在
Windows 下会被吃掉引号/换行）**没有进 system**，agent 每轮都要重新踩。

**改动（通用规则，不指名任何工具）**
- `factory.py` 新增静态段 `discipline`（order 10，通用规则区）：**Scope**（"看看 /
  评估 / 解释"= 只读调查，不为好奇制造真实副作用）、**Economy**（先花工作区里已有的
  答案、不重复调用、外部或昂贵操作先自问是否必要）、**Evidence**（命令必须自证输出；
  静默成功不算证据；多行脚本写临时文件再跑）
- 收敛"验证 = 执行"的措辞：`persona` 改成"验证**改动**才跑；**读代码不必执行**"，
  `tool:bash` 从"verify work"改成"run things（验证改动 / 观察运行态）"
- `tool:web_search` 段补它的成本性质（真实联网 + 一个完整模型轮次），首次出现即提示
- 归属规则写进 `AGENTS.md` 约定与 `ARCHITECTURE.md` §3.13：**反复被踩的坑提成 system
  的通用规则，文档只留事故与理由**；通用段与工具段不互串

**为什么这么写（普适性）**：三条规则描述的是**任何工具、任何任务**下的行为边界，
而不是"别重复调用某个工具"这类一次性补丁；将来加工具、换宿主、换模型，它们依然成立。
具体事故（log 里那几次多余的联网调用与静默命令）只作为"为什么要有这条"的证据留在
本节，不进 system。

**测试**：`test_system_prompt_carries_general_discipline` 守住"三条纪律确实渲染进
system，且排在工具段之前"——否则规则会退化成"文档里的建议"。

## steer 插队（双队列第二队实战，已完成）

`run_turn` 每步 claim 先排空 next-step、再取 next-turn——next-step 队列
从建起就有完整实现与 claim 测试，但从未有入口真的用过。这次接通：

- **`POST /steer`**：把消息塞进当前回合 next-step（`agent.steer`），同回合
  下一步即时生效，事件沿已打开的 SSE 流继续推；无活跃流 / agent 已 idle
  的竞态窗口一律 409（避免入队后事件没人读）
- **Web 前端**：agent 运行中输入框不再禁用——回车 busy 时走 `/steer`
  （插队，不画回合分隔线）、idle 时走 `/chat`（新回合）
- **CLI 侧刻意不做运行中打断**：CLI 下回合运行中读 stdin 会与 approval 的
  stdin 交互竞争输入（y/n 可能被当消息吃掉）；双队列的实战入口在 Web

**测试覆盖（四层）**：
1. agent 语义：进行中插队 = 同回合下一步（事件日志断言：单 turn/start +
   多 step/start + 两条 user 消息顺序）
2. agent 语义：idle 后插队 = 下轮消费，消息不丢
3. Web 边界：无活跃流 → 409
4. **Web 端到端**：`/chat` 的 SSE 流开着时 `POST /steer` → 插队回答沿原流
   推回。同步 TestClient 测不了（post 阻塞到回合结束），用
   `httpx.AsyncClient + ASGITransport` 并发两请求：可控挂起 LLM
   （`asyncio.Event` 卡住回合进行中窗口）→ `/steer` 入队 → 放行 → 断言
   同流出现两条回答、全程一个 turn/start

**真实模型验收实录（2026-09，deepseek-v4-flash）**：
任务"逐步完成三件事（读 README 总结 / 读 AGENTS.md 找质量门 / 给计划）"，
回合进行中连续插队两条——`插队1：跳过第③件事`、`插队2：②只总结质量门
命令`。模型行为：先完成正在进行的①②，随后在同一回合内重写收敛答复
（`③ 跳过`、`② 收敛`），全程无第二个回合，`reason=completed`——插队是
"下一步批量吸收并重新规划"，不是生硬打断。

## web_search：联网搜索（DeepSeek 官方原生搜索，已完成）

工具集里唯一"读工作区之外"的能力。第一版自己抓 DuckDuckGo HTML 页面（正则 +
还原 `uddg=` 跳转），**已废弃**——收集能力不该靠猜页面结构。改为对齐 DSH
`packages/web/web-search-deepseek`：搜索由 DeepSeek 在服务端执行，我们只
"发请求 + 解析结构化结果"。

**协议（实测 HTTP 200 可用，用的是同一个 `DEEPSEEK_API_KEY`）**
- `POST https://api.deepseek.com/anthropic/v1/messages`
  （**Anthropic 兼容**端点；`DEEPSEEK_BASE_URL` 是 chat-completions 的 base，
  **不复用**，只共享 API key——DSH provider.ts:30-35 的原话）
- headers：`x-api-key` + `anthropic-version: 2023-06-01`
- body：`{model, max_tokens, messages:[{role:user, content:[{type:text,
  text:'Perform a web search for the query: <q>'}]}],
  tools:[{type:'web_search_20250305', name:'web_search', max_uses:N}]}`
- 响应 `content[]`：`thinking` → `server_tool_use` → **`web_search_tool_result`**
  （`content[]` 里是 `web_search_result{title,url,page_age,encrypted_content}`）
  → `thinking` → `text`

**落地要点**
- **只认结构化块**：`web_search_tool_result` → `web_search_result`；**绝不从
  text 正文里抠 URL**。snippet 走 text 块的 `citations[].cited_text`（按 url 键），
  `page_age` 与 citations **实测都常缺** → 一律可选，缺失留空不报错
- **没触发原生搜索 = 响亮失败**：响应里没有结果块时抛 `WEB_PROVIDER_ERROR`
  （DSH 同款），**不**退化成"没找到"——否则模型会把"搜索没发生"当成"世上没有"
- **失败降级为结果**（不变式⑤）：缺 key / HTTP 非 200 / 超时 / 响应不可解析 /
  后端任意异常 → `is_error` 的 ToolOutcome，不炸循环
- **出站请求也记账**（不变式①）：派发前落 `web/search` 痕迹（query / endpoint /
  model / max_uses，**绝不含 key**），对齐 DSH 的
  `web/deepseek-search-llm-request`；它**不是 surface**，不进模型记忆
- **配置只有一个来源**：`SearchConfig(endpoint, model, max_uses, include_summary)`
  值对象，由工具层解析一次、**trace 与真实请求共用**——修掉了"日志记一套、
  请求发另一套"以及 `register(endpoint=...)` 静默失效的问题（有专门的回归测试
  逐字段对账）
- 模型可见形状对齐 DSH `tool-web/src/search.ts`：外部内容提示（逐字取自
  `trust.ts`）→ **摘要** → `Sources:` markdown 列表（title 空则退化 hostname）
  → 截断提示 → 引用纪律；入参 `queries: string[]`（1–4 条，执行期校验 + 精确
  重复折叠），多 query 逐条搜索后按 url 去重合并再截断
- **返回那段摘要（与 DSH 的 deepseek provider 相反，是本仓库的有意选择）**：
  原因是我们**没有 `web_fetch`**，摘要是不读原文时唯一的内容线索。三条约束：
  ① 摘要前必须贴 `SUMMARY_NOTICE`——它是**那个辅助模型的转述、且没有结构化
  引用**，标注成"线索、以 Sources 为准"才不会被当事实复述；② 每条摘要按
  `WEB_SEARCH_SUMMARY_MAX_CHARS = 3000` 截断（实测一条 ≈2.4k 字符，4 条 query
  最坏 ~10k）；③ `SearchConfig.include_summary=False` 可一键退回 DSH 的
  deepseek 策略——将来补上抓取、模型能读原文时就该关掉

**关键实测结论：DeepSeek 不返回结构化 `citations`**（探针两次确认，连 system 里
明确要求"每句都标来源 URL"也没有）。所以：snippet 永远是空；摘要句句无据；摘要
正文里那些 markdown 链接是**那个模型自己写的**，可能漏、可能挂错。这也是 DSH 的
`citationSnippets()` 在我们这儿恒为空映射的原因。

**成本**：一次搜索 = 一个完整模型轮次。实测 `input_tokens` **10.6k~22.4k**（服务端
把搜索结果原文喂回那个模型，让它写摘要），`output_tokens` 0.5k~1.2k；摘要本身再以
~2.4k 字符进会话上下文。所以 `WEB_SEARCH_TIMEOUT_S = 60`（工具超时 65s），
`max_uses = 5`、`max_results = 5`、`max_queries = 4`。

**不做 approval**：它不读文件、无副作用（判据是"不可逆/会执行/会改磁盘"）；
且搜索请求打的是**同一家厂商**的另一个端点——会话里读过的文件本来就随每次
聊天请求发给它了，增量外泄面只是"这句 query 会到搜索索引去"。DSH 也不给
web 工具审批门（它用 trust notice 管入站内容、用 sandbox 管执行）。

**未做**：DSH 的 `web_fetch`（抓取单个 URL 转正文）、多 provider 接缝
（Exa / Perplexity）、`usage.server_tool_use.web_search_requests` 审计字段。

### step 粒度改成"一次模型请求"（2026-09，修"插入信息不起作用"）

现象（用户实测 + 日志定位）：agent 跑长任务时插队，消息进了队列但**模型从没
见到它**。`.sessions/web.jsonl` 铁证：

```
seq=13494  23:29:56  agent/inbox/spliced  target=next-step  inserted=['这个 web_search …']
seq=23233  23:32:36  agent/inbox/spliced  target=next-step  removed=1  outcome=canceled
seq=23234  23:32:36  turn/end reason=aborted
```

没有对应的 `user/message`——它在 next-step 里躺了 2 分 40 秒，最后被 cancel
当"未处理输入"清掉。期间 37 次 `request/header` **step 恒为 1**。

根因：我们的 step 语义是"整段工具循环"（`_run_step` 内部反复模型↔工具），
只有模型最终给出纯文本才回到 `run_turn` 外层 claim；而 harness 的
`core/agent-loop/src/agent.ts` 里 `step()` **发完一次请求、执行完这次的工具
调用就 return**（唯一 `continue` 是 `request_error` 重试），工具循环由 turn
循环驱动，**每轮回到顶部重新 claim**。于是 harness 的插队在一个模型往返内被
吸收，我们的要等整段自主运行结束。

修法（`loop.py`，语义变化大、代码很小）：
- `_run_step` 执行完工具调用后 `return None`（"本步到此为止，工具结果留给下
  一个 step 回送"），内层 while 只保留 retry 路径
- `run_turn`：`end_reason` 可空（None = 工具循环还在继续），每轮都 claim；
  已收尾且无新插队 → 结束；工具循环未完时即使 claim 为空也继续发下一次请求
- max-tokens 粘性保持；blocked/aborted/error 语义不变

顺带收益：插队延迟从"整段工具循环"降到"一次模型往返"；停止更及时；将来
guard（重复工具提醒 / 单次调用超时）有了天然的每请求卡点。
代价：日志事件更细（`step/start` 与 `request/header` 现在 1:1）。

测试：新增 `test_steer_absorbed_at_next_request_inside_tool_loop`——模型连续
三轮调用工具，中途插队；断言**第 2 次请求的 messages 里就有插队文本**
（旧实现要等到第 5 次，即工具循环跑完）。已验证该测试在旧代码上失败。

未做（harness 有、我们没有）：`agent/turn-stopping` 钩子、工具结果带
`concludesTurn` 提前收尾。

### 待处理消息：分区渲染 + 提交回显 + 队列动作（对齐 DSH，已完成）

插队消息要等 step 边界 claim 才落 `user/message`（实测延迟 890~1698 个
事件），这段半开窗口里把它当乐观气泡往流中间插，靠 `(turn,step)` 锚点猜顺序，
位置反复出错（两次修 bug：`8b17c24`/`00533de`）。读 DSH 源码后定案并落地：

- **勘误**：DSH 并非"待处理消息一律不进消息流"——只有 `placement='queued'`
  （next-turn）进 QueueDock；`placement='steering'`（next-step）**画在消息流
  尾部**（`ChatView.tsx` 的 `pendingSteering` → `PendingSteeringBubble` →
  `data-pending-steering`）。所以正解是**恒定贴尾**，不是"不画"
- 后端：`Inbox.queued_items()` 在状态层投影 `QueuedItem(placement, message)`；
  提交身份 `rpc_id` 落进 `UserSource`（`/chat`、`/steer` 收 `request_id`）；
  SSE `queue_update` 帧 + `user_message` 帧带 `rpc_id`；`/history`、
  `/sessions/*/switch|new` 带 `queue`；新增 `POST /queue/update`
  （`edit` / `remove` / `steer`，状态码与 DSH 错误码同名）
- 前端：`queued` → `#queue-dock`（计数头 / 折叠 / ✎ 编辑 / × 撤回 / ↥ 提升 /
  "全部插队" + Ctrl+Enter）；`steering` + 本地回显 → `#messages > .flow-tail`
  pending 气泡；`pendingSubmissions` 生命周期（登记 → 渲染 → 按 rpc_id 原子
  交接 → observed 延后一帧退休 / failed 立即退休）
- 测试：pytest 76（新增队列三动作与重放、`/queue/update` 语义、rpc_id 全链路、
  `queue_update` 帧序列）+ jsdom 无头 47 项 DOM 断言（两区域分区、折叠、编辑、
  提升、撤回、整队插队快捷键、回显交接、失败回填、无脚本错误）

## Web 前端统一消息投影（已完成）

> 目标：前端只保留**一份从日志重建的投影状态**，实时 SSE 与 /history 全量
> 加载收敛到同一投影 → 同一渲染入口；**DOM 不再当状态**。设计注记与 schema
> 见 `web/PROJECTION_DESIGN.md`。

**为什么做**：此前前端把 DOM 当状态——实时路径用 `cur` 指针（当前助手
DOM + 挂在其上的 `_raw/_toolById/_thinking`）逐块生长，刷新/切会话则走
另一套"历史渲染"直接铺 DOM。两套路径产出不一致（曾出现重开会话时插队
消息被渲染成独立回合、助手消息渲染不出来等 bug），根源都是渲染逻辑散在
两处、没有统一的中间表示。

**改造（分两步提交）**：

1. **SSE 帧协议增强（输入契约）**：`chunk/reasoning/tool_call` 帧补带
   `turn`/`step`（loop.py 落事件时带、web_app 的 `event_to_payload` 透传）；
   新增 `turn_start` / `user_message`（带 turn）帧——真人发言不再由前端
   本地渲染猜测回合号，改由后端帧驱动。历史载荷补 `user.turn`。
2. **前端 nodes 投影（核心）**：`nodes` 有序数组 = 唯一事实源；节点是不可
   变快照（`user`/`assistant`/`checkpoint`，assistant 带 `tools` 子项）。
   - `renderProjection()`：清空后从 nodes 全量重建（刷新/切会话/压缩后）；
     空会话走 emptyHint——**renderHistory 开头先清空 messagesEl**，两条分支
     都从干净画布出发（曾修：新建/切空会话残留上个会话 DOM，只因清空藏在
     renderProjection 首行、空分支进不去）
   - `applyFrame()`：SSE 增量驱动同一投影——按 (turn,step) 找/建 assistant
     节点（`liveAssistantFor`），chunk 追加 text；与历史构建**同构**
   - **partialRender 降级为渲染优化**：`.pseg` 段落固化只服务"最后一个
     assistant 文本块平滑更新"，`_raw/_seg` 只活在 DOM 上；任何时刻全量
     重画一致
   - 回合分隔线从 user.turn 推导（同 turn 后续 user = steer 插队不画线）

**踩过的渲染 bug（各有修复提交）**：chunk 只拼 node.text 但 flush 读
`textEl._raw` → 镜像 `_raw`；turn_end 与末尾 chunk 同批次时 rAF flush 排在
finalize（清 `_raw`）之后 → closeAssistantNode 先同步 flush 再 finalize；
closeAssistantNode 清零 `_errCount` 把错误工具红标抹掉 → 删除清零。

**验证**：jsdom 无头驱动真实页面脚本断言 DOM（12 项：历史双回合线、同回合
插队不画第三条、checkpoint 卡、流式段落固化等）+ 后端 pytest 补 SSE 帧
断言；后端 65 测试三绿（ruff/mypy/pytest）。

## CLI REPL（已完成）

`prompt` 变可选：带任务 = 单次跑完退出（原行为）；不带 = 进入 REPL：

- `agent> ` 提示符循环：每轮输入开新回合、`when_idle` 收敛后回提示，
  `/exit` 退出、`/compact` 手动压缩——替代"每次跑命令 + --resume"
- 会话装载抽成 `_prepare()`（单次与 REPL 共用）；测试用 subprocess 喂多行
  stdin 验证两回合两条 user 消息落日志

## 架构重构（求职作品级，✅ 已完成 2026-09）

> **这是第一次重构的记录：把根目录的 `main.py` 拆成 `agent_demo/` 包**（当时模块仍平铺
> 在一层）。下面那棵树描述的是**当时**的形状，2026-09 的第二次重构（分层目录化，见下一节）
> 已经取代它——要查当前结构请看 `AGENTS.md` 的包结构段。

**定位转变**：项目已从"教学 demo"成长为**个人工具 / 求职作品**。

**保留的卖点（重构没有丢）**：日志唯一事实源 / 被动状态机 / 钩子 / approval /
路径沙箱——框架四层（agent→loop→session→values）原封不动，只动了原 main.py
里的"应用内容"（工具/沙箱/UI/执行后端/入口）。

**执行结果**：

```text
agent_demo/               包结构（取代平铺）
├── cli.py                入口：argparse + run（原 main.py 瘦身 ~80 行）
├── web_app.py            Web UI（从根迁入，import 改包内）
├── factory.py            build_agent / load_env（CLI 与 Web 共用）
├── ui.py                 渲染（render_event / paint）
├── sandbox.py            路径边界（resolve_in_workspace / iter_files）
├── constants.py          预算 / 颜色 / demo 脚本
├── 框架四层不动           agent / loop / session / inbox / prompt /
│                          values / persistence / hooks / llm
├── registry.py           原 tools.py 改名（ToolSpec/ToolRegistry/ToolOutcome）
├── compaction.py         上下文压缩引擎（四步事务 + checkpoint + 会话 token 账）
└── tools/                应用工具包
    ├── __init__.py       build_tools() 注册表组装
    ├── file_io.py        read_file / list_files / write_file / edit
    ├── search.py         grep / glob
    ├── shell.py          bash + _run_command 执行后端
    └── todo.py           todo_write
pyproject.toml            打包 + ruff / mypy / pytest 配置 + console scripts
（CI 不信 GitHub Actions：曾在 `447564c` 接过、又在 `eca8d98`（2026-09-05）移除——
理由是 Python 3.12 的 pathlib `**` 语义差异让 glob 测试云端红，而单人本地开发收益低。
要恢复先把跨版本 glob 语义问题修掉）
```

- 步骤 1 模块化 ✅（38 测试全绿，git 全程识别 rename 保留历史）
- 步骤 2 打包 ✅（`pip install -e .`，`agent-demo` / `agent-demo-web` 命令）
- 步骤 3 工程化 ✅（ruff 清零 / mypy 清零；质量门三绿才提交。**CI 已按上面的理由移除**）
- 步骤 4 功能：上下文压缩全套 + Web ContextMeter + Web 会话并发隔离 +
  steer 插队 + CLI REPL（已完成，见下节）；阶段四工程化打磨（配置/日志
  查看器）仍未开始

**测试拆分**（蓝图里的 tests/ 按主题拆分）✅ **已完成（2026-09）**：单文件
`tests/test_demo.py`（4293 行 / 132 用例）按关注点拆成 14 个测试文件 + `conftest.py`
（值/日志、inbox、prompt、llm、loop、tools、todo、recovery、compaction、instructions、skills、
web_search、web、cli），**只搬家不改断言**，用例总数不变。起因：单文件已经 4293 行，
定位慢、review diff 噪声大；拆分同时让 26 处函数内冗余 import 暴露出来（ruff F401/F811）。

## 分层重构（目录 = 层，2026-09 已完成）

**动机**：功能攒到 21 个模块平铺在一层之后，"谁是层、谁是应用内容、谁只能 import 谁"
只能靠文件名猜；依赖方向只写在 `AGENTS.md` 里靠纪律维持，实测真的长出了两处反向依赖
（`loop → tools.todo`、`registry → constants`）；`web_app.py` 993 行、测试文件 4300 行。

**做法**：按四层目录化，并把"依赖方向"从文档约定变成可执行断言。

```text
agent_demo/
├── values/        层1 值：messages.py（消息/事件词汇表）+ persistence.py
├── capability/    层2 能力：llm.py / hooks.py
├── state/         层3 状态：session / inbox / prompt / registry / recovery
├── runtime/       层4 框架循环：agent（状态机）/ loop（turn-step）
├── app/           应用内容：factory / constants / sandbox / instructions / skills /
│                  compaction / ui
├── web/           入口：Web 宿主（app / state / sessions / titles / payload）
├── cli.py         入口：CLI
├── tools/         应用工具
└── bundled_skills/ 随包技能正文（与代码包同级，`pyproject` package-data 按此声明）
```

配套：`tests/test_architecture.py`（116 行：核心断言约 20 行 `ast` 解析，其余是分层规则与
白名单的说明）机械检查"只能 import 同层或更低层"，白名单只收带 issue 号的例外并断言
它们仍然真实（不许长僵尸）。

**落地时的三个坑（都已修，留作记录）**：

1. **相对 import 的点数要按"文件的新位置"算**：源文件自己搬进子包时，即使目标模块没搬，
   `from .tools.todo` 也必须变成 `from ..tools.todo`（第 3 步第一版漏了这条，测试直接
   报 `No module named 'agent_demo.runtime.tools'`）；
2. **函数体内的局部 import 要保留缩进**：替换文本顶到行首会把 `if` 块掏空（语法错误）；
3. **`Path(__file__).parent` 类路径会随层数变化**：`app/skills.py` 找包内 `bundled_skills/`
   要用 `parents[1]`——写错时表现是"技能表静默变空"（7 条测试红，不是崩溃）。

**第 4 步（同一条线的收尾，已完成）**：把"值对象住错层"和"常量住错层"两件事一起修掉——
`ToolOutcome` 从 `state/registry.py` 搬进 `values/messages.py`（它和 `ToolResultBlock` 是
同一件事的两个阶段：执行返回值 → 补 call_id 落日志的 wire 形态；`ToolSpec` 带 executor，
是行为不是值，留在 registry）；`TOOL_RESULT_MAX_CHARS` 从 `app/constants.py` 下沉到
`values/limits.py`（判据：**有更低层要用就下沉**——状态层要用应用层的常量，方向本来就反了）。
两条都修完后，`KNOWN_VIOLATIONS` 只剩 issue #19 那一条（白名单僵尸检查会盯着删干净）。
落地过程同样留记录：搬完后 `test_known_violations_are_still_real` **主动报红**"这条例外已经
不再越界了，请从白名单删掉"——机制按设计工作。

## 每对话一个工作区（2026-09 已落地，issue #32）

**动机**：`--workspace` 曾经是进程级唯一边界，一个 Web 宿主只能服务一个项目；想同时开两个项目的
对话就没辙。DSH 的做法是**工作区属于会话**（`SessionHeader.cwd`），我们照这个形状做——
细节对照见 `agent.md` §10。

**做法**（三处，都不改框架层）：

1. `app/workspace.py`（新）：`resolve_workspace(raw, default=…)` —— 空 = 宿主默认、
   相对路径按**进程 cwd**、`~` 展开、显式路径必须存在且是目录（否则 `ValueError` → 400）。
   **选择策略 = 信任界面使用者**（对齐 DSH：它也没有 allowlist），想收紧只改这一处。
2. `Seat.args`：每会话一份装配参数，**只有 `workspace` 不同**——工具沙箱、指令探测、
   技能表、`{{workspace}}` 叙事本来就是 `build_agent` 当场从 `args.workspace` 派生的。
3. `session/workspace` 痕迹事件 + `Session.workspace()`：工作区**写进日志**（与
   `session/title` 同构），重放/换进程都回到同一个目录；旧会话没有这条事件 → 跟随宿主默认
   且**不改写它的日志**；日志里的工作区不存在了 → **409 + 明确原因，绝不静默回退**
   （记录是空白时按"没记录过"处理——空白进 `resolve()` 会折叠成进程 cwd，那也是一次静默换根）；
   列表项带 `workspace_ok`，目录已失效的会话提前在界面上标出来。
4. **新会话 id 原子占坑**（`open_session_seat` 里 `touch(exist_ok=False)`）：`web-<秒级时间戳>`
   撞车顺延 `-2`/`-3`…，判据同时看磁盘与内存 seat——多 worker 下两个进程都可能判定"这个 id
   还没人用"，只有原子占坑能保证只建一次（另一个退回"已有会话"语义，不覆盖别人的工作区）。

**顺手修掉的旧 bug**：`POST /sessions/new` 的 id 是 `web-<秒级时间戳>`，同一秒连开两个对话
会**静默复用**同一个 sid（工作区还会被"已固定"拒掉）——改成撞车顺延 `-2`/`-3`。

**明确不做（连同理由，将来要动先看这里）**：

| 没做 | 理由 / 前提 |
|---|---|
| 工作区**热切换**（把已有对话迁到别的目录） | DSH 靠 cwd 不可变绕开了"切换后重建什么"；半个对话跨两个根，语义说不清。要做得先回答"旧目录读到的内容算不算这个对话的记忆" |
| 会话日志**按工作区分目录**（DSH 是 `<--cwd-->/<id>/`） | 我们的列表是"平铺 `.sessions/` + 每项一列工作区"，够用且改面小；真要做等价于一次存储迁移（扫描/删除/切换/测试全要动） |
| 目录**浏览/新建目录**接口（DSH 的 browse 后端：`list`/`createDirectory`） | 现在是"可编辑路径输入框 + 最近使用"。要加就是 `GET /fs/list?path=` + 前端 Miller 分栏，**纯增量**、不动现有语义 |
| 工作区**白名单 / 授权根** | 见上：DSH 没有，我们按"信任界面使用者"落地。判据已收在一处，加它不影响调用方 |
| 允许"路径不存在就 mkdir -p"（DSH 的裸 cwd 路径会建目录） | 我们只有一条路径（显式选择）→ 选"严格"：打错字当场 400，而不是在别处造一个空目录 |
| CLI 的每会话工作区 | CLI 一次一个会话，`--workspace` 就是它的边界；`--resume` 别人的日志时按 CLI 参数走（文档已说明） |

## 运行时状态：注册制贡献者（2026-09 已落地，issue #19）

**动机**：`runtime/loop.py` 原先直接 `from ..tools.todo import build_todo_status` —— 框架层
反向依赖应用层工具（依赖方向评审里那条唯一的白名单例外）。加一个状态源（L0 会话目录、
M2 预算水位）就得改循环，`request/header` 的审计字段也只能一个来源一个平铺字段。

**做法**（三处，循环不认识任何具体来源）：

1. `state/runtime_status.py`（新）：`RuntimeStatusRegistry` —— 一个状态源 = 名字 +
   `build(session) -> str | None`（**无内容返回 None**）；`register` 对空名/重名当场抛错，
   返回注销函数；`collect(session)` 按注册顺序求值，只收非空。
2. `runtime/loop.py`：每请求 `collect()` → 非空者**各贴一条合成 user 消息**在 messages 末尾
   （不进日志、不进 `derive_messages`）→ 审计字段变成**映射** `request/header.runtime_status
   = {名字: 原文}`（原来是平铺的 `todo_status`）。
3. `app/factory.py`：`_runtime_status()` 里一行 `register('todo', build_todo_status)`。
   **加一个状态源 = 这里一行，循环一行都不用改**（这就是这条改造的意义）。

**落地时的证据**：改造完成的同一刻，`tests/test_architecture.py` 的
`test_known_violations_are_still_real` **主动报红**（"这些例外已经不再越界了，请删掉"）
——按"白名单不许长僵尸"的规矩删掉最后一条，白名单现在是**空的**。

**明确不做**：DSH 的"内容变化才 append 新快照"（它靠 `retained.text === snapshot` 去重，
因为快照会**留在历史里**）；我们每请求现算、不进历史，所以不需要去重账——同一个效果
（前缀稳定）由"贴尾 + 不落事件"达成。opencode 的 baseline/update 双形态同理不做。
真要做"状态进入历史"（例如 compaction 后要把状态栏一起折叠），那是另一条改造，前提是先
回答"状态栏算不算对话内容"。

## 实施约定（延续项目哲学）

1. 新增状态一律落日志——"没有状态不进日志"不变式不能破
2. 决策走钩子 / 走注册声明，不写死进循环
3. 失败降级为结果，不炸循环
4. 每步跑测试（全量 pytest 必须全绿）；阶段一收尾时更新 README 与 ARCHITECTURE 定稿
