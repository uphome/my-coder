# AGENTS.md

Python 复刻 deepseek-harness 架构的教学 demo（agent 框架本身，不是应用）。

- **完整架构**：[`docs/architecture.md`](docs/architecture.md)
- **设计记录（为什么这么定、放弃了什么）**：[`docs/notes/`](docs/notes/README.md)、文档分层规则 [`docs/AGENTS.md`](docs/AGENTS.md)
- **三家开源项目对照（DSH / PI / opencode）**：[`docs/prior-art.md`](docs/prior-art.md)
- **进度与下一步**：[`NEXT_STEPS.md`](NEXT_STEPS.md)

> **本文件有硬预算：≤ 8000 字符**（它被逐字注入 system，超了模型看不到后半部分，
> 实测代价见[注入预算那篇](docs/notes/implemented/process/2026-09-29-agents-md-injection-budget.md)，
> `tests/test_docs.py` 钉住）。所以这里只放**判据**；动机、实测、事故、被否决的方案都在 `docs/notes/`。

## 命令

```sh
# 一切 Python 命令必须走 conda 环境 agent-demo（base 里没有 pytest/httpx）
# 质量门：ruff + mypy + pytest 三绿才可提交
conda run -n agent-demo python -m ruff check my_coder tests eval
conda run -n agent-demo python -m mypy my_coder
conda run -n agent-demo python -m pytest

conda run --no-capture-output -n agent-demo python -m my_coder.cli --workspace . --fake "read README.md and summarize"
conda run --no-capture-output -n agent-demo python -m my_coder.web --workspace . --fake   # http://127.0.0.1:8000
```

Windows 控制台是 GBK：用 `--no-capture-output`；`conda run -c` 不支持多行脚本，
内联 Python 写到临时文件再跑。

## 架构（改任何代码前必读）

**目录 = 分层**，依赖方向由包结构 + `tests/test_architecture.py` 单向锁死（下层 import 上层当场红）：

```
0 values/       值：messages.py（消息/事件词汇表）、persistence.py（JSONL）、limits.py（跨层常量）
1 capability/   能力：llm.py、hooks.py（三个决策钩子的类型）
2 state/        状态：session.py（日志）、inbox.py、prompt.py、registry.py、recovery.py、runtime_status.py
3 runtime/      框架：agent.py（被动状态机）、loop.py（turn/step 两级循环）
4 app/          应用：factory.py + constants/sandbox/instructions/skills/compaction/ui/workspace/recall；tools/
5 web/          入口：Web 宿主；cli.py
```

- **常量下沉**：只在应用/工具层用的放 `app/constants.py`，**有更低层要用就下沉 `values/limits.py`**。
- **依赖白名单现在是空的**（两条历史例外都已修好即删）。将来再加必须带 issue 号，测试盯着不许长僵尸。
- **日志（`.sessions/<id>.jsonl`）是唯一事实源**：模型记忆（`derive_messages`）、inbox、回合号、
  模型路由全是日志的重放投影；恢复 = 重放（`adopt`）+ 自愈（`state/recovery.py`）。
- 技能在 `bundled_skills/`（随包）与 `<workspace>/skills/`；工作区指令文件的发现/注入在 `app/instructions.py`。

## 编辑时不可破坏的五个不变式

1. **没有状态不进日志**：新状态必须经 `session.append` 落事件
2. **模型可见 ⟺ 可重建**：只有 `user/message`、`assistant/message`、`tool/result` 能进模型消息，
   且 append 必须带 `surface_op='append'`；`derive_messages` 必须是纯函数
3. **入队即记账**：inbox 先写 `agent/inbox/spliced` 再改内存（`_splice`），磁盘日志永远 ≥ 内存状态
4. **决策走钩子/注册声明**：三个钩子 + `execution_mode` 等声明，循环里不写业务 if
5. **失败降级为结果**：工具失败一律变 `is_error`，不炸循环；`CancelledError` 单向传播，每层记账后放行

## 约定（每条 = 判据 + 去哪看）

- **提示词纪律的归属**：`system` 是唯一"每轮都生效"的通道，**文档挡不住**，
  所以反复被踩的坑要提成 system 里的通用规则。分层：通用规则进 `identity`/`persona`/`discipline`，
  工具专属规则进各自的 `tool:*` 段，两者不互串。当前六条纪律：**Scope**（"看看/评估/解释"=只读调查）、
  **Economy**（先查工作区、不重复调用、昂贵操作先自问必要）、**Evidence**（命令必须自证输出；
  多行脚本写临时文件——内联多行跨 shell 会被吃掉）、**Cleanup**（scratch 写系统 temp；
  必须落工作区就放一个自明目录、整目录删；**绝不删**不是自己造的 / git 跟踪的 / 会话日志；
  收尾用 `git status` 自证）、**Instructions**（`AGENTS.md`/`CLAUDE.md` 是长期约定：先读并遵循、
  稳定知识提议写进去、不静默改、不写密钥与临时状态）、**Continuity**（任务依赖你**看不到**的
  既有决定/约束时**先回查会话历史**；**绝不许**用看起来合理的具体值补空缺）。
  **新工具落地 = 实现 + 供给面**：什么时候用（通用规则）· 怎么用（`tool:*` 段）·
  在需求发生处提醒（运行时状态贡献者）。原文见 `app/factory.py`，理由见
  [召回那篇](docs/notes/implemented/feature/2026-09-19-context-recall.md)。
- **循环的 step 粒度 = 一次模型请求**（对齐 harness）：工具循环由 `run_turn` 外层驱动，
  **每轮开头都 claim inbox**；别合并回 `_run_step` 的内层 while（插队消息的落地时机、
  停止及时性、每请求卡点全依赖它）。见 `docs/architecture.md` §3.6。
- **工具并发调度**：并发许可是**声明**出来的，`ToolSpec.execution_mode` 默认 `'sequential'`，
  只有不写共享状态才声明 `'parallel'`（判据是副作用不是快慢）；非法值注册时抛错；
  **未注册的工具名按 fail-closed 当独占**。"能不能并发"与"阻不阻塞事件循环"是两根正交的轴
  （后者用 `offload=True`，只给纯 I/O）；分组按**连续段**；结果按模型顺序落盘；
  取消要补 is_error 记账。见 `docs/architecture.md` 与 `docs/prior-art.md`。
- **`bash` 工具必须真的跑 bash**：Windows 上显式挑后端（真 bash → 回退 `cmd.exe`），
  回退时**工具描述里必须明说是 cmd**（`dir`/`type`/`findstr`）；不用 PowerShell 当默认
  （5.1 没有 `&&`）；描述与后端由同一个 `pick_shell()` 产出，不许漂移。见
  [那篇 bug-fix](docs/notes/implemented/bug-fix/2026-09-29-windows-shell-backend.md)。
- **每轮叠给模型的运行时状态走注册制贡献者**（`state/runtime_status.py`）：一个源 = 一个名字 +
  `build(session) -> str | None`（**无内容返回 None**）；`register` 对空名/重名/不可调用**当场抛错**；
  非空者各贴一条合成 user 消息在末尾（不进日志、不进 `derive_messages`）；**加一个源 =
  `app/factory.py` 的 `_runtime_status()` 里一行**，循环一行都不用改；`build` 必须是日志投影的
  纯函数且便宜（每请求求值）；坏一个不炸对话（记 ERROR 跳过，`except Exception` 不捕 `BaseException`）。
- **上下文召回**：压缩只改变"看得见什么"，不改变"存在什么"——三层补差：**L0 会话目录**
  （每请求的状态贡献者）→ **L1 用户话清单**（`session_manifest`）→ **L2 回合明细**（`read_turn`）。
  判据：检索面 = 曾经进过模型上下文的事件**全集**（含被遮蔽的），痕迹事件不进；
  **不做相关性排序**，靠顺序 + 回合号 + 足迹导航；坐标是回合号（可加 `step`），seq 只在内部；
  **L1 截断必须明说并给 `read_turn` 坐标**（重复用户话不删行只标注；无文本结论显式说明）；
  **L2 按行分页**（`offset` 1 起，12,000 字符一页，在行边界停，明说总量与继续的坐标），
  **块级不许静默截断**；跨工作区 fail-closed 且不泄漏对方目录；两个工具 `parallel` + `offload=True`；
  入参自己较真类型（坏值降级 `is_error`）。见
  [机制](docs/notes/implemented/feature/2026-09-19-context-recall.md)、
  [渲染修复](docs/notes/implemented/bug-fix/2026-09-29-silent-render-truncation.md)、
  [怎么量](docs/notes/implemented/testing/2026-09-19-context-recall-evaluation.md)。
- **恢复要自愈"悬空工具调用"**：每个恢复入口（重放后、`bind_store` 后）都调
  `recovery.repair_dangling_tool_calls`；修复必须**写进日志**（`session/repaired` + is_error 合成结果），
  **禁止在请求构造时静默补占位消息**。见 `docs/architecture.md` §3.16。
- **每对话一个工作区**：创建时定下、之后不可变（再给 workspace → 400）；写进日志
  （`session/workspace` 痕迹事件，`Session.workspace()` 倒读）；按 seat 隔离（`Seat.args` 只换 workspace）；
  旧会话跟随宿主默认且不改写；记录的工作区不存在了 → **409 + 明确原因，绝不静默回退**；
  判据落在"去掉空白后"的值上（空白 = 没记录过）；选择策略只校验"存在 + 是目录"。
- **工作区指令文件的发现与维护**：候选是根的 `AGENTS.md`/`CLAUDE.md`（正文注入 system 的 live 段），
  子目录同名文件只列路径；**不向上发现**；**探测是三态**（确认存在 / 确认不存在 / **读不到**）——
  **不要把"不知道"降级成"没有"**；`lstat` 再 `stat`（断链 ≠ 不存在）；读完再取一次缓存键；
  符号链接越界不注入（与技能共用 `sandbox.workspace_escape_reason`）；输出必须可复现
  （`dirnames.sort()`）；内容按请求新鲜、清单按回合新鲜。见 `docs/prior-art.md` §9。
- **按需技能（skill）**：`*.md` + frontmatter；bundled（随包）与 workspace 两个来源按名字合并，
  **workspace 覆盖 bundled**，合并后按名字排序；目录只注入 name + description（**不列路径**——
  列了会诱导 `read_file`，而 bundled 在包外会被沙箱拒）；目录与 `skill` 工具**共用同一个
  `SkillTable`** 且工具**执行时**取表；刷新要加锁 + 双检（两个线程都会碰）；工作区来源做越界检查；
  正文按**名字**取、作为 tool/result 注入。见 `docs/prior-art.md` §3。
- **待处理消息（inbox）**：按 placement 分区渲染、恒定贴尾（`queued` → 输入框上方；
  `steering` → 消息流尾部）；队列是状态层投影（`Inbox.queued_items()`，折叠只有一份）；
  提交身份 `rpc_id`（落在 `UserSource` 与队列项，用于原子交接本地回显）；动作
  `edit`/`remove`/`steer` 与 DSH 同名错误码；SSE `queue_update`、`/steer` 响应、
  `/history` 与 `sessions/*/switch|new` 三条通道幂等。见 `docs/prior-art.md` §5。
- **发现缺口时怎么办**：① 本 PR 引入的 → **本 PR 修掉**；② 几十行且不改语义 → **顺手修**，
  PR 正文点明；③ 只有"真的大"才开 issue，且写清**什么时候修、卡在什么前提**。
  取舍与候选方案进 `NEXT_STEPS.md`，不进 issue 列表。
- **新机制/新功能先看 `docs/prior-art.md` 做调研**（DSH / PI / opencode 的对照与候选方案）。
  它是参考手册不是规范：落地规则以本文件 + `docs/architecture.md` 为准，方案定稿后把规则
  提炼进本文件或 `docs/notes/`，prior-art 只留背景。

## 其它硬规矩

- 注释/文档全部中文，教学式讲解动机；每个文件顶部 `from __future__ import annotations`
- 值对象必须 frozen dataclass + tuple，禁止把可变容器放进消息/事件
- **严格校验哲学**：未注册 prompt 变量、重复工具名、非法执行模式、缺 `surface_op` 都在写入时刻抛错，宁炸勿静默
- 提交前三绿；`docs/` 的格式与 `AGENTS.md` 的预算由 `tests/test_docs.py` 守着

## 入口与工具

- `my_coder/cli.py`（`--fake` 离线跑通、`--resume` 演示重放）；`my_coder/web/`（FastAPI + SSE，
  拆成 `app`/`state`/`sessions`/`titles`/`payload`）；`show_memory.py`（记忆 = 日志投影）
- 工具在 `my_coder/tools/`：`read_file` / `list_files` / `grep` / `glob` / `edit` / `write_file` /
  `bash` / `todo_write` / `web_search` / `skill` / `session_manifest` / `read_turn`；
  类型（schema + executor + 并发模式 + 卸载 + 超时 + approval）在 `state/registry.py`；
  bash/write_file/edit 需人工确认
- 三个"读工作区外"的东西（`web_search` / `skill` / 召回工具）都不写工作区、无副作用、不走沙箱、不需要 approval；
  web_search 的搜索由服务端提供（我们只发请求 + 解析结构化块，绝不自己抓网页）；
  skill 按**名字**取；召回工具只能给会话 id（授权判据 = 同一工作区）
- `.env` 存 `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`；`.sessions/`、`.codegraph/`、`.eval/`、`.env` 均不入库
