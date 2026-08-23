# 阶段一实施进度与后续工作计划

> 记录 `ARCHITECTURE.md` 进化路线图"阶段一：真实工具集"的实施进度、
> 已定设计决策与后续工作清单。配合 ARCHITECTURE.md 第 6 节阅读。

## 当前进度

| 任务 | 状态 |
|---|---|
| read_file 升级（行号 / offset / limit / line_numbers 开关） | ✅ 已完成（含测试） |
| grep / glob 工具 | ✅ 已完成（标准库实现，含测试） |
| 轻量路径沙箱（`--workspace` 边界） | ✅ 已完成（含测试） |
| edit 工具 | ⬜ 待做 |
| bash 工具 | ⬜ 待做 |
| approval 确认门 | ⬜ 待做 |
| 阶段一收尾（更新 README / ARCHITECTURE 定稿） | ⬜ 阶段一完成时做 |

> 当前全量测试：25 passed（AGENTS.md 里的数字保持同步）。

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
- **覆盖**：read_file / list_files / write_file / grep / glob 全部走统一入口；
  将来的 edit / bash 的路径参数自动继承
- **对齐 harness**：`canonicalPath()`（realpath 归一化）+ `writableRoots()` 
  （allow-list 单一事实源）+ `dsh-fs-sandbox`（进程内 fence）——同构思路；
  harness 在其上还有 OS 级强制（landlock / bwrap / restricted token / seatbelt）
- **局限**（README 安全警告如实声明）：工作区内全裸、TOCTOU 竞态、非 OS 级
- **演进预留**：OS 级沙箱留给 bash 落地之后——届时 bash 的执行做成可替换后端，
  沙箱 runner 是第三个后端（Windows: CreateRestrictedToken + CreateProcessAsUserW，
  Linux: bwrap / Landlock，macOS: seatbelt），继承 harness 的 fail-closed 哲学

## 后续任务（阶段一剩余）

### edit
- 精确字符串替换式编辑：`edit(file_path, old_string, new_string)`
- 替换 `write_file` 全量覆盖（省 token）；依赖 read_file 的行号定位
- "old_string 未找到"是高频失败 → 明确 `is_error` 报错（宁炸勿静默）

### bash
- `subprocess` 执行 + 超时 + 输出截断 + 工作目录；退出码非 0 → `is_error` 结果（不是炸循环）
- 与 approval 确认门绑定实现（危险操作先确认）

### approval 确认门
- 危险工具（bash / write_file）执行前确认；`tool/skipped` 事件 + `_execute_tool_calls`
  的 aborted 路径是现成接入点
- `ToolSpec` 加 `requires_approval` 注册声明，循环不写业务 if（决策走注册声明）
- CLI 交互式确认 → 工具声明 `execution_mode='sequential'`（交互式工具逐个执行）

## 实施约定（延续项目哲学）

1. 新增状态一律落日志——"没有状态不进日志"不变式不能破
2. 决策走钩子 / 走注册声明，不写死进循环
3. 失败降级为结果，不炸循环
4. 每步跑测试（全量 pytest 必须全绿）；阶段一收尾时更新 README 与 ARCHITECTURE 定稿
