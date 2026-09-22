# 文件结构重构执行文档：目录即分层

> **状态**：计划已定，逐步执行（每步一个 PR，各自三绿、可单独回退）。
> **追踪**：issue #21（工程卫生批次——四层依赖自动约束 + 拆 `web_app.py` + constants/测试拆分）。
> **本文是什么**：执行文档。每一步的**改动面、验收标准、风险与回退**都写在这里；落地规则以
> `AGENTS.md` 为准，架构理由以 `ARCHITECTURE.md` 为准，本文只管"怎么搬、按什么顺序搬"。

## 0. 一句话

把 `agent_demo/` 从 **21 个模块平铺**改成"**目录 = 层**"的包结构：依赖方向从
"写在文档里、靠纪律维持"变成"看一眼目录就知道、有测试拦着"。

## 1. 为什么做（判据）

### 症状

1. **四层只写在文档里**。`AGENTS.md` 写着"入口 → 框架循环 → 状态 → 能力 → 值"，但代码里
   21 个模块全平铺在同一层，谁是层、谁是应用内容、谁只能 import 谁，**只能靠文件名猜**。
2. **越界没人拦**。实测已有两处反向依赖（见 §2.3）；`web_app.py` 甚至 import 了
   `tools.todo`。分层是这套架构唯一的"防腐机制"，一旦破防，`derive_messages` 纯函数、
   surface 校验这些不变式也就失去了结构性依托。
3. **两个文件已经太胖**：`web_app.py` 993 行 / 13 路由 / 41 个顶层定义；`tests/test_demo.py`
   3512 行 / 132 个用例。历史上 bug 密度最高的就是 Web 那一层（SSE 断连、steer 气泡位置、
   todo dock 闪动、checkpoint 卡片），改一处要在千行文件里找上下文。

### 做完怎么算成（目标判据）

| 判据 | 现状 | 目标 |
|---|---|---|
| 依赖方向可执行 | 只有文档 | `tests/test_architecture.py`：越界白名单为空（或有 issue 号的例外） |
| 层可见 | 21 个模块平铺 | `values/ capability/ state/ runtime/ app/ web/ tools/` |
| 最大文件 | `web_app.py` 993 行 | 每个文件 < 400 行 |
| 测试可定位 | 单文件 3512 行 | 按关注点拆成 7~8 个文件，**用例总数不变** |
| 行为 | — | 零行为变化：前端协议、日志形状、事件类型全部不动 |

## 2. 现状事实（重构前测绘）

### 2.1 模块清单（`agent_demo/`，21 个平铺模块）

```
993  web_app.py        446  loop.py          415  compaction.py    308  instructions.py
266  values.py         261  skills.py        201  inbox.py         201  llm.py
192  agent.py          159  registry.py      126  session.py       126  factory.py
107  cli.py             97  prompt.py         70  sandbox.py        67  recovery.py
 63  constants.py       59  ui.py             33  hooks.py          21  persistence.py
  3  __init__.py
子包：tools/（6 模块 1545 行）、bundled_skills/（1 文件）
测试：tests/test_demo.py 3512 行 / 132 用例（单文件、无 conftest）
```

### 2.2 导入图（实测，`ast` 解析）

```
values        → （无）
persistence   → values
hooks         → values
llm           → （无）
prompt        → （无）
session       → persistence, values
inbox         → session, values
recovery      → session, values
registry      → constants                     ← 越界（见 2.3）
sandbox       → registry                      ← 怪边（见 2.3）
agent         → hooks, inbox, loop, prompt, registry, session, values
loop          → hooks, llm, registry, values  + tools.todo   ← 越界
compaction    → llm, session, values
instructions  → sandbox
skills        → sandbox
ui            → constants
factory       → agent, compaction, constants, instructions, llm, prompt, session, skills, ui + tools
cli           → compaction, factory, persistence, recovery, session, ui
web_app       → compaction, constants, factory, hooks, llm, persistence, recovery, session, values + tools.todo
```

### 2.3 两处真越界 + 一处怪边

| 位置 | 导入 | 性质 | 处置 |
|---|---|---|---|
| `registry.py`（状态） | `from .constants import TOOL_RESULT_MAX_CHARS` | 状态 → 应用内容常量 | 拆 `constants.py`（PR 4） |
| `loop.py`（框架） | `from .tools.todo import build_todo_status` | 框架 → 应用层工具 | 并入 issue #19（注册制状态贡献者）；PR 3 先记进白名单 |
| `sandbox.py` | `from .registry import ToolOutcome` | `ToolOutcome` 是 **frozen 值对象**却住在状态层 | 搬进 `values`（PR 4），sandbox 变成叶子 |

### 2.4 死代码（实测：全仓库无引用）

| 位置 | 情况 | 处置 |
|---|---|---|
| `web_app._focus_seat()` | 注释说"与历史测试兼容"，但没有任何调用点 | 删 |
| `Seat.context` | 只赋值 `None`，从不读也不写 | 删 |
| 仓库根 `conftest.py` | **0 行空文件** | 删（PR 2 建 `tests/conftest.py` 时一并处理） |

### 2.5 兼容面（搬东西会碰到谁）

| 面 | 现状 | 处置 |
|---|---|---|
| 测试戳的私有全局 | `web_app._session/_agent/_seats/_current_sid/_args` + `_should_auto_title/_is_verbatim_copy/_queue_rows` | PR 1 改成 `web.state.session` / `web.titles.*` / `web.payload.queue_rows`（映射表见 §5.2） |
| 命令行 | `python -m agent_demo.web_app`（AGENTS.md / README / USAGE_zh 共 4 处文档） | PR 1 起改为 `python -m agent_demo.web`（`web/__main__.py`），文档同步 |
| console script | `pyproject.toml`: `agent-demo-web = "agent_demo.web_app:main"` | PR 1 改为 `agent_demo.web.app:main` |
| `_ROOT` 计算 | `web_app.py` 在包内一层：`Path(__file__).parent.parent` | PR 1 起 `web/app.py` 深一层 → `parents[2]`（**易错点**，见 §6） |
| 打包发现 | `[tool.setuptools.packages.find] include = ["agent_demo*"]` | 无需改（子包自动发现）；`package-data` 的 `bundled_skills` 路径也不变 |
| `show_memory.py`（根脚本） | import `agent_demo.persistence` / `agent_demo.session` | PR 3 跟着改路径 |

## 3. 目标形状

```
agent_demo/
├── __init__.py          包 docstring：依赖方向（本文件要同步改）
├── values.py            层 1 值：不可变 Message/SessionEvent/ToolOutcome + tagged 编解码
├── persistence.py       层 1 值：JSONL 追加写 + 重放读
├── capability/          层 2 能力（只依赖 values）
│   ├── __init__.py
│   ├── llm.py           SSE 流式客户端 + FakeLlm + wire 双向翻译
│   └── hooks.py         三个决策钩子的类型
├── state/               层 3 状态（只依赖 values / capability）
│   ├── __init__.py
│   ├── session.py       日志 + surface 折叠投影
│   ├── inbox.py         双队列 + claim + 持久化重放 + queued_items 投影
│   ├── prompt.py        sections + 严格插值
│   ├── registry.py      工具类型（ToolSpec）+ 注册表
│   └── recovery.py      悬空工具调用自愈
├── runtime/             层 4 框架循环（只依赖 state / capability / values）
│   ├── __init__.py
│   ├── agent.py         被动状态机
│   └── loop.py          turn/step 两级循环 + 工具分组执行
├── app/                 应用内容（依赖上面各层）
│   ├── __init__.py
│   ├── constants.py     框架级常量（应用内容常量在 PR 4 拆出去）
│   ├── sandbox.py       路径边界（越界判据 + 工具入参沙箱）
│   ├── instructions.py  工作区指令文件发现与注入
│   ├── skills.py        两来源技能表
│   ├── compaction.py    上下文压缩
│   ├── ui.py            终端渲染
│   └── factory.py       build_agent / load_env（组装）
├── web/                 入口：Web 宿主（PR 1 建）
│   ├── __init__.py      公共面重导出（唯一允许 re-export 的包）
│   ├── __main__.py      python -m agent_demo.web
│   ├── state.py         Seat + WebState + state 单例（唯一的可变状态容器）
│   ├── sessions.py      seat 生命周期 / 会话列表 / 标题落盘 / 审批钩子 / 后台任务
│   ├── titles.py        自动会话标题（三级来源 + 逐字复读判定）
│   ├── payload.py       纯函数投影：事件/消息 → 前端载荷
│   └── app.py           FastAPI 路由 + init_web + main
├── cli.py               入口：CLI
├── tools/               应用工具（内部结构不动，只搬家后的 import 路径）
└── bundled_skills/      随包发布的技能正文
```

### 3.1 依赖规则（PR 3 用 ast 测试钉住）

| 包 | 允许 import |
|---|---|
| `values`（+ `persistence`） | 只允许包外标准库 |
| `capability` | `values` |
| `state` | `values`, `capability` |
| `runtime` | `values`, `capability`, `state` |
| `app` | 以上全部 + `tools` + `app` 内部 |
| `web` / `cli`（入口） | 不限（但不得被任何下层 import） |
| `tools` | `state.registry`, `app.sandbox`, `app.constants`, `app.skills` |

> 注：`runtime → tools.todo` 这条（issue #19）在 PR 3 落地时进**带 issue 号的白名单**；
> `state.registry → app.constants` 由 PR 4 拆常量后从白名单删掉。白名单为空是 PR 4 的验收。

### 3.2 两条编码规矩（避免"包化"变成新的混乱）

1. **`__init__.py` 不 re-export**（`web/` 是唯一例外：它要暴露 `app`/`init_web` 给 uvicorn
   与测试）。理由：re-export 会让依赖图在 ast 上看不出来（`from ..state import Session`
   到底是 state 的哪个模块？），而"依赖方向可被机械检查"正是这次重构的目的。
2. **显式模块路径**：`from ..state.session import Session`，不写 `from ..state import Session`。
   好处：grep 得到、ast 测试读得到、搬哪个模块都只改一处。

## 4. 分步执行计划（每步一个 PR）

| # | 分支 | 范围 | 关掉 #21 的哪一条 |
|---|---|---|---|
| 0 | `docs/refactor-plan` | 本文档 | — |
| 1 | `refactor/web-package` | `web_app.py` → `web/` 包（7 个文件，最大 < 400 行） | ② 拆 web_app |
| 2 | `refactor/split-tests` | `tests/test_demo.py` → 按关注点拆 7~8 个文件 + `tests/conftest.py` | ④ 测试拆分 |
| 3 | `refactor/layered-packages` | 四层目录化 + `tests/test_architecture.py` + 清 `runtime → tools` | ① 依赖自动约束 |
| 4 | `refactor/values-constants` | `ToolOutcome` 搬进 `values`；`constants.py` 拆框架级/应用级；compaction 选段单位 | ③⑤ + #21 ① 的白名单清空 |

每一步的硬要求（所有 PR 一致）：

- **纯搬运与真改动分开**：能"只移动 + 改 import"的，单独一个 commit（review 时看
  `git diff --stat` 与 `-M` 改名检测）；真改行为/接口的另起 commit 并在 PR 正文点明。
- **三绿**：`ruff` / `mypy` / `pytest`。
- **用例总数只增不减**（拆分 PR 必须"总数不变"）。
- **文档同步**：搬了哪个模块，就把点到它的文档一次改干净（清单见 §5.3）。
- **不做行为改动**：前端协议、日志事件类型、system 段形状一个字不动。

### PR 1（第一步，详案）

**范围**：`web_app.py` 993 行 → `agent_demo/web/` 包；去掉两处死代码；命令行改
`python -m agent_demo.web`。

**新文件与来源**：

| 新文件 | 内容 | 源区间（`web_app.py`） | 预估行数 |
|---|---|---|---|
| `web/state.py` | `Seat`（删 `context`）、`WebState`（args/sessions_dir/seats/session/agent/current_sid/background_tasks）、`state` 单例 | 42–87、243–253 | ~90 |
| `web/sessions.py` | seat get-or-create（`open_session_seat`）、焦点切换（`open_session`）、`validate_sid`、`scan_sessions`、`append_title`、`approval_for`、`spawn`、`check_init` | 243–326、439–505 | ~200 |
| `web/titles.py` | `TITLE_*` 常量、`clean_title`、`is_verbatim_copy`、`should_auto_title`、`first_user_message_just_landed`、`auto_title` | 89–221 | ~130 |
| `web/payload.py` | `RESULT_MAX_CHARS`、`first_text_of`、`result_stats`、`result_payload`、`surface_with_seq`、`user_message_turns`、`reasoning_by_assistant_seq`、`history_payloads`、`queue_rows`、`context_payload`、`event_to_payload`、`message_to_payload` | 224–241、328–436、531–662 | ~260 |
| `web/app.py` | `_ROOT`、`app`、13 个路由、`init_web`、`main`、`APPROVAL_TIMEOUT_S`、`DONE_MARKER` | 42–50、508–528、665–993 | ~360 |
| `web/__init__.py` | 公共面：`app`, `init_web`, `state`, `event_to_payload`, `message_to_payload`, `main` | — | ~20 |
| `web/__main__.py` | `python -m agent_demo.web` → `main()` | 992–993 | ~5 |

**接口调整（唯一的真改动，除此之外零行为变化）**：

- 5 个模块私有全局（`_session/_agent/_seats/_current_sid/_args`）→ `state` 对象的字段
  （`state.session` / `state.agent` / `state.seats` / `state.current_sid` / `state.args`）。
  测试随之改（§5.2 映射表）。
- `context_payload(session)` 的 `session` 变成**必给**（原来缺省回退读全局焦点——那是
  "同一份状态两个来源"，正好是并发隔离要消灭的东西）。所有调用点本来就已经传了。
- 私有函数去掉下划线前缀（`clean_title` / `is_verbatim_copy` / `should_auto_title` /
  `queue_rows` / `history_payloads` …）：它们跨模块被用了，就不再是"私有"。
- 删死代码：`_focus_seat()`、`Seat.context`。

**验收**（PR 1 专属）：

- [ ] `web/` 下每个文件 < 400 行；`python -m agent_demo.web --workspace . --fake` 起得来，
      浏览器手测清单通过（发消息 / 流式 / 工具卡 / 审批 / steer / 队列编辑 / 切会话 / 改名 /
      删除 / 手动压缩 / 刷新恢复历史 / 停止）
- [ ] `from agent_demo.web import app, init_web` 可用；`agent-demo-web` 入口可执行
- [ ] 三绿；用例总数不变（132）；`web_app.py` 已删

### PR 2（测试拆分）

按关注点拆 `tests/test_demo.py`（3512 行 / 132 用例）→
`test_values_persistence.py` / `test_session_inbox.py` / `test_prompt_registry.py` /
`test_loop_tools.py` / `test_compaction.py` / `test_instructions_skills.py` /
`test_web.py` / `test_recovery.py`；公共 fixture 提到 `tests/conftest.py`（顺带删掉仓库根
那个 0 行的 `conftest.py`）。

- 判据：**用例总数不变、只搬家不改断言**；`pytest -q` 的输出按文件分组可读。
- 风险：`tests/test_demo.py` 里有跨用例共用的模块级 helper（如 `_fake_llm` 之类）→ 先 grep
  出所有模块级 `def`/`class`，把它们放进 `conftest.py` 或各自文件，避免拆完互相 import。

### PR 3（分层目录化）

`git mv` 21 个模块进 §3 的包 + 每个文件的相对 import 加一层（`from .values` →
`from ..values`）+ 新增 `tests/test_architecture.py`（约 20 行 ast 测试）+ 清
`runtime → tools.todo`（并 issue #19）+ 同步文档与 `show_memory.py`。

- 判据：越界白名单只剩 `state.registry → app.constants`（PR 4 清）；故意往 `session.py` 加
  `from ..web import app` → 测试红。
- 风险：**这是最大的一次机械 diff**（153 处 import 引用）。对策：脚本化替换 + 逐文件
  review + 三绿；不夹带任何行为改动。

### PR 4（值层收编 + 常量分家）

`ToolOutcome` 从 `state/registry.py` 搬进 `values.py`（`state.registry` re-export 一个
迁移期别名？——**不**：直接改全部调用点，`values.py` 是层 1，`registry` 从层 3 import 层 1
是合法方向）；`constants.py` 拆"框架级（`TOOL_RESULT_MAX_CHARS`/并发池上限…）"与
"应用与演示（`DEMO_SCRIPT`/搜索预算/`MODEL_CONTEXT_WINDOW`）"；`compaction.py` 选段单位
与压力单位统一为 token（与 issue #3 重叠，可拆出去单独做）。

- 判据：白名单为空；`registry.py` 不再 import 应用内容模块。

## 5. 机械步骤清单

### 5.1 通用流程（每个搬运动作）

1. `git mv`（或新建 + 删旧）——**保持改名检测**，让 diff 显示为 rename 而不是 delete+add。
2. 改 import：先全仓库 grep 旧路径（`Select-String`），改完再 grep 一次确认零残留
   （包括 docstring / 注释里的路径引用）。
3. 跑三绿 + 目标命令手测。
4. 更新 §5.3 的文档清单。
5. 提交信息里写清：搬了什么、**为什么**、验收证据（命令与输出摘要）。

### 5.2 PR 1 测试替换映射表

| 旧 | 新 |
|---|---|
| `from agent_demo import web_app` | `from agent_demo import web` |
| `web_app.init_web(...)` | `web.init_web(...)` |
| `web_app.app` | `web.app` |
| `web_app._session` | `web.state.session` |
| `web_app._agent` | `web.state.agent` |
| `web_app._seats` | `web.state.seats` |
| `web_app._current_sid` | `web.state.current_sid` |
| `web_app._args` | `web.state.args` |
| `web_app._should_auto_title` | `web.titles.should_auto_title` |
| `web_app._is_verbatim_copy` | `web.titles.is_verbatim_copy` |
| `web_app._queue_rows` | `web.payload.queue_rows` |
| `web_app.MODEL_CONTEXT_WINDOW` | `web.app.MODEL_CONTEXT_WINDOW` |

### 5.3 文档同步清单（每次搬模块都要过一遍）

| 文件 | 要改的地方 |
|---|---|
| `AGENTS.md` | 命令块（`-m agent_demo.web_app`）、§架构的包结构段、§入口与工具、队列那节的 `web_app._queue_rows` |
| `README.md` | 命令块、四层依赖图、文件职责表 |
| `ARCHITECTURE.md` | §2 分层图、§4 生命周期里提到的模块、§5 文件职责清单 |
| `USAGE_zh.md` | Web 启动命令（2 处） |
| `agent.md` | 提到 `web_app` 的复盘段落（历史叙述可保留，但"当前路径"要改） |
| `web/PROJECTION_DESIGN.md` | 引用 `web_app.event_to_payload` 的地方 |
| `agent_demo/__init__.py` | 包 docstring 里的依赖方向 |
| `web/index.html` | 注释里提到的后端函数名 |

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| **`_ROOT` 层级算错**（包深一层） | `web/app.py` 用 `parents[2]`；PR 1 手测清单第一条就是"起得来且静态资源 200" |
| **循环导入**：`payload.py` 若 import `state/sessions` 就成环 | 硬规矩：`payload.py` 只 import 标准库 + `..compaction` / `..constants` / `..session` / `..values`；手测清单与 ast 测试都会盯 |
| **命令行变更打断你的本地习惯** | PR 1 在正文显著提示；`agent-demo-web` console script 保持可用；4 处文档一次改净 |
| **与并行会话 / 相邻 PR 的冲突** | 每步一个小 PR、尽快合。PR 1 与 PR 2 都要碰 `tests/test_demo.py`，**两者不要同时开着**（谁后做谁 rebase）。顺序选"先拆 web、后拆测试"的理由：PR 1 只改测试里 Web 那一段（约 40 行），PR 2 再把这批已改好的用例整块搬进 `tests/test_web.py`——反过来（先拆测试）也成立，但那样 PR 1 的收益（最大文件从 993 行降下来）要等第二步才拿到 |
| **大 diff 掩盖真改动** | 纯搬运单独 commit + PR 正文列出"唯一的真改动"（PR 1 只有 §4 里那四条） |
| **搬到一半发现形状不对** | 本文档 §3 是承诺形状；若某步发现要改形状，**先改本文档再动代码**，别让文档与代码分叉 |

## 7. 明确不做（边界）

- **不改行为**：前端协议（`nodes` 投影）、日志事件类型、system 段形状、prompt 文本，一个字不动。
- **不动前端**：`web/index.html` 只改注释里的后端函数名，不重构前端。
- **不引入 `src/` 布局**：对 demo 没有收益（`pip install -e .` 已经能跑），徒增路径噪音。
- **不引入 import-linter / 分层插件**：15 行 `ast` 测试够用，且**零新依赖**（教学项目要能一眼看懂）。
- **不改 `tools/` 内部结构**：这一步只搬家，不重划工具边界。
- **不做 compaction 选段单位**（PR 4 的最后一条）：它与 issue #3 重叠，等那条线的设计定了再动，
  本重构只保证"搬完还能跑"。

## 8. 总验收

- `python -m agent_demo.web --workspace . --fake` 与 `python -m agent_demo.cli --workspace . --fake "..."`
  都能跑；`agent-demo` / `agent-demo-web` 两个 console script 都能执行；
- `tests/test_architecture.py` 的越界白名单为空（或有带 issue 号的例外）；
- 三绿；用例总数 ≥ 132；每个 `.py` 文件 < 400 行（`tests/` 单文件同理）；
- 前端手测清单全过（§4 PR 1 验收）；
- `AGENTS.md` / `README.md` / `ARCHITECTURE.md` / `USAGE_zh.md` 里的模块路径与命令全部为**当前**
  事实（零残留 `web_app`），`agent.md` / `NEXT_STEPS.md` 里的历史叙述保留原样并注明是历史。
