# AGENTS.md

Python 复刻 deepseek-harness 架构的教学 demo（agent 框架本身，不是应用）。完整设计见 `ARCHITECTURE.md`；快速上手与不变式见 `README.md`。

## 命令

```sh
# 一切 Python 命令必须走 conda 环境 agent-demo（base 里没有 pytest/httpx）
# 质量门：ruff + mypy + pytest 三绿才可提交（pyproject.toml 已配好）
conda run -n agent-demo python -m ruff check agent_demo tests
conda run -n agent-demo python -m mypy agent_demo
conda run -n agent-demo python -m pytest        # 43 个测试

# CLI（可 pip install -e . 后直接 agent-demo；或模块方式跑）
conda run --no-capture-output -n agent-demo python -m agent_demo.cli --workspace . --fake "read README.md and summarize"

# Web UI（DeepSeek 风格，默认 http://127.0.0.1:8000；--fake 离线演示）
conda run --no-capture-output -n agent-demo python -m agent_demo.web_app --workspace . --fake
```

注意：Windows 控制台是 GBK，用 `--no-capture-output` 避免 conda run 二次打印乱码；`conda run` 的 `-c` 参数不支持多行/换行脚本，内联 Python 写到临时文件再跑。

## 架构（改动任何代码前必读）

包结构 `agent_demo/`（取代早期平铺）。依赖方向不变，仍是四层单向：
入口（`cli.py` / `web_app.py` → `factory.py` 组装）→ 框架循环（`agent.py` 被动状态机 / `loop.py` turn-step）→ 状态（`session.py`/`inbox.py`/`prompt.py`/`registry.py`）→ 能力（`llm.py`/`hooks.py`）→ 值（`values.py` + `persistence.py`）。应用内容独立成包：工具在 `agent_demo/tools/`（file_io/search/shell/todo + build_tools 组装）、渲染在 `ui.py`、路径边界在 `sandbox.py`、常量在 `constants.py`。上层依赖下层，下层不感知上层。

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

- `agent_demo/cli.py`：CLI 入口；`--fake` 用脚本化假模型离线跑通全流程（不需要 API key）；`--resume` 演示日志重放恢复
- `agent_demo/web_app.py`：Web UI（FastAPI + SSE，会话管理/标题/approval）；`factory.py` 的 `build_agent`/`load_env` 被 CLI 与 Web 共用
- `show_memory.py`：教学脚本，重放日志展示"记忆 = 日志投影"
- 工具在 `agent_demo/tools/`：`build_tools(workspace)` 组装（read_file 行号分页 / list_files / grep / glob / edit / write_file / bash / todo_write），工具类型（`ToolSpec`：schema + executor + 模式 + 超时 + requires_approval）在 `registry.py`；`--workspace` 必填（路径边界，`sandbox.py` 实现）；bash/write_file/edit 执行前需人工确认；阶段一实施进度见 `NEXT_STEPS.md`
- `.env` 存 `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`；`.sessions/`、`.codegraph/`、`.env` 均不入库
