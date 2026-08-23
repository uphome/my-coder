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
| 阶段一收尾（更新 README / ARCHITECTURE 定稿） | 🔶 进行中 |

> 当前全量测试：31 passed（AGENTS.md 里的数字保持同步）。

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
- 三个敏感工具同时声明 `execution_mode='sequential'`（确认是交互，逐个来）
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

## 实施约定（延续项目哲学）

1. 新增状态一律落日志——"没有状态不进日志"不变式不能破
2. 决策走钩子 / 走注册声明，不写死进循环
3. 失败降级为结果，不炸循环
4. 每步跑测试（全量 pytest 必须全绿）；阶段一收尾时更新 README 与 ARCHITECTURE 定稿
