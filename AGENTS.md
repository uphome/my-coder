# AGENTS.md

Python 复刻 deepseek-harness 架构的教学 demo（agent 框架本身，不是应用）。完整设计见 `ARCHITECTURE.md`；快速上手与不变式见 `README.md`。

## 命令

```sh
# 一切 Python 命令必须走 conda 环境 agent-demo（base 里没有 pytest/httpx）
# 质量门：ruff + mypy + pytest 三绿才可提交（pyproject.toml 已配好）
conda run -n agent-demo python -m ruff check agent_demo tests
conda run -n agent-demo python -m mypy agent_demo
conda run -n agent-demo python -m pytest        # 85 个测试

# CLI（可 pip install -e . 后直接 agent-demo；或模块方式跑）
conda run --no-capture-output -n agent-demo python -m agent_demo.cli --workspace . --fake "read README.md and summarize"

# Web UI（DeepSeek 风格，默认 http://127.0.0.1:8000；--fake 离线演示）
conda run --no-capture-output -n agent-demo python -m agent_demo.web_app --workspace . --fake
```

注意：Windows 控制台是 GBK，用 `--no-capture-output` 避免 conda run 二次打印乱码；`conda run` 的 `-c` 参数不支持多行/换行脚本，内联 Python 写到临时文件再跑。

## 架构（改动任何代码前必读）

> **实现新的机制/新功能前，先看仓库根的 `agent.md` 做调研参考**：它记录了
> DSH（deepseek-harness）、PI（pi-mono）、opencode 三家开源项目对同类机制
> （skill 按需加载、issue/PR 工作流、agent 角色、工具组织等）的现成实现与
> 对照，以及本仓库历次讨论的候选方案与动机。**注意**：agent.md 是参考手册
> 不是规范——落地规则以本文件（AGENTS.md）与架构文档为准；方案定稿后把
> 规则提炼进本文件，agent.md 只留背景。

包结构 `agent_demo/`（取代早期平铺）。依赖方向不变，仍是四层单向：
入口（`cli.py` / `web_app.py` → `factory.py` 组装）→ 框架循环（`agent.py` 被动状态机 / `loop.py` turn-step）→ 状态（`session.py`/`inbox.py`/`prompt.py`/`registry.py`）→ 能力（`llm.py`/`hooks.py`）→ 值（`values.py` + `persistence.py`）。应用内容独立成包：工具在 `agent_demo/tools/`（file_io/search/shell/todo/web_search + build_tools 组装）、渲染在 `ui.py`、路径边界在 `sandbox.py`、常量在 `constants.py`。上层依赖下层，下层不感知上层。

**日志（`.sessions/<id>.jsonl`）是唯一事实源**：模型记忆（`derive_messages`）、inbox 队列、回合号、模型路由全部是日志的重放投影。恢复 = 重放（`adopt`），没有独立的对话状态。本仓库已建 CodeGraph 索引（`.codegraph/`），理解/定位代码先 `codegraph_explore`。

## 编辑时不可破坏的五个不变式

1. **没有状态不进日志**：新状态（工具结果、配置变更、注入）必须经 `session.append` 落事件
2. **模型可见 ⟺ 可重建**：只有 `user/message`、`assistant/message`、`tool/result` 三类 surface 事件能进模型消息，且 append 时必须带 `surface_op='append'`（`Session.append` 会校验）；`derive_messages` 必须是纯函数
3. **入队即记账**：inbox 改动先写 `agent/inbox/spliced` 事件再改内存（`_splice`），磁盘日志永远 ≥ 内存状态
4. **决策走钩子/注册声明**：`pre_step`/`request`/`request_error` 三钩子 + `execution_mode` 等注册声明，循环里不写业务 if
5. **失败降级为结果**：工具失败（坏 JSON、参数错、异常、超时）一律变 `is_error` 结果，不炸循环；`CancelledError` 沿 await 链单向传播，每层记账后放行

## 约定

- **循环的 step 粒度 = 一次模型请求**（对齐 harness `core/agent-loop` 的
  `step()`：发完一次请求 + 执行完这次的工具调用就返回）：工具循环由 `run_turn`
  的外层循环驱动，**每轮开头都 claim inbox**。不要把它合并回 `_run_step` 的
  内层 while——插队消息的落地时机、停止的及时性、将来 guard 的每请求卡点
  全依赖这一点（旧实现把整段工具循环当一个 step，实测插队消息在 next-step 里
  躺了 2 分 40 秒后被 cancel 清掉，模型从没见过它）。细节见
  `ARCHITECTURE.md` §3.6
- 注释/文档全部用中文，教学式讲解设计动机——新注释保持此风格
- 值对象必须 frozen dataclass + tuple，禁止把可变容器放进消息/事件（JSON 往返依赖）
- 严格校验哲学：未注册 prompt 变量、重复工具名、surface_op 缺失都在写入时刻抛错，宁炸勿静默
- 每个文件顶部有 `from __future__ import annotations`
- **按需技能（skill）机制的设计决策**（落地时遵循；三家对照与取舍背景见
  `agent.md` §3，那里只留动机不作规范）：
  - 技能 = `skills/<name>.md` 文件 + YAML frontmatter（name/description）
  - **目录（catalog）静态注入 system**：只放 name + description + 相对
    workspace 路径（正文绝不进 system）；作为静态 section 注册，order 必须
    **小于 todo:state 等动态 live 段**——目录处于缓存稳定前缀，不被动态段
    拖累（todo:state 现状 order=100，故目录 order 取 <100）
  - **正文 = 工具结果注入**：模型用现有 read_file 按目录路径读技能文件 →
    正文作为 tool/result（source.kind='tool'）进 derive_messages，与读任何
    文件机制一致（落日志可重建、可被 compaction 折叠）；**不新增 skill() 专用
    加载工具**，不搞 DSH 式注入 user 快照
- **待处理消息（inbox 队列）的显示与操作规则**（落地时遵循；DSH 源码对照、
  三个配套机制与两次位置 bug 的复盘见 `agent.md` §5）：
  - **按 placement 分区渲染，恒定贴尾**——未 claim 的消息在日志里没有 seq
    位置，往流中间插只能靠 `(turn, step)` 锚点猜顺序（上一版就是这么错位的）：
    - `queued`（next-turn）→ 输入框上方 `#queue-dock`
    - `steering`（next-step）→ **消息流尾部** `#messages > .flow-tail` 的
      pending 气泡（`.pending-steering` + 待处理标记）
    - claim 落 `user/message` 后 durable 节点落到真实 seq 位置，尾部那条消失。
      **注意**：DSH 里 steering 也是画在消息流尾部的（不是"不画进流"），
      别再把这条写成"待处理消息一律不进消息流"
  - **队列是状态层投影**：`Inbox.queued_items()` 折重放结果产出
    `QueuedItem(placement, message)`，和 `Session.derive_messages()` 并列；
    **折叠只有一份**（`_apply`/`_splice` 共用 splice 语义），web/cli 等宿主只
    做序列化（`web_app._queue_rows(agent)` 摊平成 JSON）
  - **提交身份 `rpc_id`**（对齐 DSH 的 `beginSubmission`/`rpcId`）：前端提交时
    铸 uuid 随请求上来，落到 `UserSource.rpc_id`（durable）与 `QueuedItem.rpc_id`
    （队列项）；前端据此在同一次渲染里把本地回显换成真身（原子交接——不重复、
    不留空档），`observed` 延后一帧真删、`failed` 立即删。回显只活在客户端内存
  - **队列动作**：`POST /queue/update {item_id, action}`，action =
    `edit`（同 id 原地换文案，一次原子 splice）/ `remove`（`outcome='canceled'`）
    / `steer`（next-turn→next-step 搬家，两步 splice、摘除那步 `discard=False`）。
    状态码与 DSH 错误码同名：`ok` / `queue-item-not-found`（并发收敛，HTTP 200）/
    `steer-unavailable`（agent 空闲时没有"下一步"）/ `unknown-action`
  - 三条推送通道幂等：SSE `queue_update` 帧、`POST /steer` 响应体、
    `/history` 与 `/sessions/*/switch|new` 响应体

## 入口与工具

- `agent_demo/cli.py`：CLI 入口；`--fake` 用脚本化假模型离线跑通全流程（不需要 API key）；`--resume` 演示日志重放恢复
- `agent_demo/web_app.py`：Web UI（FastAPI + SSE，会话管理/标题/approval）；`factory.py` 的 `build_agent`/`load_env` 被 CLI 与 Web 共用
- `show_memory.py`：教学脚本，重放日志展示"记忆 = 日志投影"
- 工具在 `agent_demo/tools/`：`build_tools(workspace)` 组装（read_file 行号分页 / list_files / grep / glob / edit / write_file / bash / todo_write / web_search），工具类型（`ToolSpec`：schema + executor + 模式 + 超时 + requires_approval）在 `registry.py`；`--workspace` 必填（路径边界，`sandbox.py` 实现）；bash/write_file/edit 执行前需人工确认；阶段一实施进度见 `NEXT_STEPS.md`
- `web_search` 是唯一"读工作区之外"的工具：**搜索能力由 DeepSeek 官方在服务端提供**（Anthropic 兼容 `.../anthropic/v1/messages` + 原生服务端工具 `web_search_20250305`），我们只做"发请求 + 解析结构化块"——绝不自己抓网页、绝不从模型正文里抠 URL；没有结果块要**响亮报错**而不是退化成"没找到"。它不读文件、无副作用，所以**不走 workspace 沙箱、也不需要 approval**（与 DSH 一致，见 `agent.md` §6）
- `.env` 存 `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`；`.sessions/`、`.codegraph/`、`.env` 均不入库
