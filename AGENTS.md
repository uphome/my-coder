# AGENTS.md

Python 复刻 deepseek-harness 架构的教学 demo（agent 框架本身，不是应用）。完整设计见 `ARCHITECTURE.md`；快速上手与不变式见 `README.md`。

## 命令

```sh
# 一切 Python 命令必须走 conda 环境 agent-demo（base 里没有 pytest/httpx）
conda run -n agent-demo python -m pytest -q        # 30 个测试，唯一验证手段（无 lint/typecheck 配置）

# 跑 CLI 演示（Windows 控制台是 GBK，中文输出需 UTF-8，否则乱码）
# --workspace 必填：工具只能读写该目录（纯用户态路径边界，非 OS 沙箱）
PYTHONIOENCODING=utf-8 conda run -n agent-demo python main.py --fake --workspace . "read README.md and summarize"
# 真实模型需要 .env 里的 DEEPSEEK_API_KEY（不入库）
```

注意：`conda run` 的 `-c` 参数不支持多行/换行脚本；如需内联 Python，写到临时文件再跑。

## 架构（改动任何代码前必读）

四层单向依赖：入口 `agent.py`（被动状态机）→ 循环 `loop.py`（turn/step 两级）→ 状态 `session.py`/`inbox.py`/`prompt.py`/`tools.py` → 能力 `llm.py`/`hooks.py` → 值 `values.py` + `persistence.py`。上层依赖下层，下层不感知上层。

**日志（`.sessions/<id>.jsonl`）是唯一事实源**：模型记忆（`derive_messages`）、inbox 队列、回合号、模型路由全部是日志的重放投影。恢复 = 重放（`adopt`），没有独立的对话状态。本仓库已建 CodeGraph 索引（`.codegraph/`），理解/定位代码先 `codegraph_explore`。

## 编辑时不可破坏的五个不变式

1. **没有状态不进日志**：新状态（工具结果、配置变更、注入）必须经 `session.append` 落事件
2. **模型可见 ⟺ 可重建**：只有 `user/message`、`assistant/message`、`tool/result` 三类 surface 事件能进模型消息，且 append 时必须带 `surface_op='append'`（`Session.append` 会校验）；`derive_messages` 必须是纯函数
3. **入队即记账**：inbox 改动先写 `agent/inbox/spliced` 事件再改内存（`_splice`），磁盘日志永远 ≥ 内存状态
4. **决策走钩子/注册声明**：`pre_step`/`request`/`request_error` 三钩子 + `execution_mode` 等注册声明，循环里不写业务 if
5. **失败降级为结果**：工具失败（坏 JSON、参数错、异常、超时）一律变 `is_error` 结果，不炸循环；`CancelledError` 沿 await 链单向传播，每层记账后放行

## 约定

- 注释/文档全部用中文，教学式讲解设计动机——新注释保持此风格
- 值对象必须 frozen dataclass + tuple，禁止把可变容器放进消息/事件（JSON 往返依赖）
- 严格校验哲学：未注册 prompt 变量、重复工具名、surface_op 缺失都在写入时刻抛错，宁炸勿静默
- 每个文件顶部有 `from __future__ import annotations`

## 入口与工具

- `main.py`：CLI 入口；`--fake` 用脚本化假模型离线跑通全流程（不需要 API key）；`--resume` 演示日志重放恢复
- `show_memory.py`：教学脚本，重放日志展示"记忆 = 日志投影"
- 工具注册在 `main.py:build_tools(workspace)`（read_file 行号分页 / list_files / write_file / todo_write / grep / glob），通过 `tools.py` 的 `ToolSpec`（schema + executor + 模式 + 超时绑定注册）；`--workspace` 必填（路径边界）；阶段一实施进度见 `NEXT_STEPS.md`
- `.env` 存 `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`；`.sessions/`、`.codegraph/`、`.env` 均不入库
