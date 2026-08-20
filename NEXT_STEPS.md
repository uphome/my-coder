# 阶段一实施进度与后续工作计划

> 记录 `ARCHITECTURE.md` 进化路线图"阶段一：真实工具集"的实施进度、
> 已定设计决策与后续工作清单。配合 ARCHITECTURE.md 第 6 节阅读。

## 当前进度

| 任务 | 状态 |
|---|---|
| read_file 升级（行号 / offset / limit / line_numbers 开关） | ✅ 已完成（含测试，全量 17 passed） |
| grep / glob 工具 | 🔶 设计已定稿，实现路线待拍板 |
| edit 工具 | ⬜ 待做 |
| bash 工具 | ⬜ 待做 |
| approval 确认门 | ⬜ 待做 |
| 阶段一收尾（更新 README / ARCHITECTURE 定稿） | ⬜ 阶段一完成时做 |

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

## grep / glob 设计（已定稿）

对齐 deepseek-harness 的实现（`packages/fs/tool-fs-search/` + 架构笔记
`2026-07-09-bash-backed-grep-glob-discovery`）：

- **schema 极简**：`grep(pattern, path?, include?)`、`glob(pattern, path?)`；
  预算（上限、超时）不进模型 schema
- **grep**：正则匹配；默认大小写敏感（不公开参数）；输出
  `Found N of M matches` + 按文件分组 `Line 12: ...`；匹配上限 250 + 截断页脚
- **glob**：路径模式（如 `**/*.py`）；每行一个相对路径；路径上限 100 + 截断页脚
- **错误语义**：坏正则 / 路径不可访问 → `is_error`；0 命中 → 正常结果
- **跳过隐藏目录与 `__pycache__`**：`.git` / `.sessions` / `.codegraph` 不进搜索结果
- **待拍板：实现路线**：
  - A. 标准库（`pathlib` + `re`）：零依赖、Windows 开箱即用、遍历逻辑可见（教学价值）
  - B. 复用 `rg`：完全对齐 harness（argv 无 shell 层、条件式注册探测 `command -v rg`），
    但 demo 环境默认没有 rg，需用户安装
  - 倾向 A，但 schema / 输出格式 / 预算 / 错误语义完全对齐 harness——将来要升级真 rg，
    工具层形状不变，只换 executor

## 后续任务（阶段一剩余）

### grep / glob
- 实现 + 测试：命中与分组、include 过滤、坏正则、结果截断页脚、隐藏目录跳过、0 命中

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
