# AGENTS.md

Python 复刻 deepseek-harness 架构的教学 demo（agent 框架本身，不是应用）。完整设计见 `ARCHITECTURE.md`；快速上手与不变式见 `README.md`。

## 命令

```sh
# 一切 Python 命令必须走 conda 环境 agent-demo（base 里没有 pytest/httpx）
# 质量门：ruff + mypy + pytest 三绿才可提交（pyproject.toml 已配好）
conda run -n agent-demo python -m ruff check my_coder tests
conda run -n agent-demo python -m mypy my_coder
conda run -n agent-demo python -m pytest        # 154 个测试（3 条平台相关：Windows 建不了符号链接时 skip）

# CLI（可 pip install -e . 后直接 my-coder；或模块方式跑）
conda run --no-capture-output -n agent-demo python -m my_coder.cli --workspace . --fake "read README.md and summarize"

# Web UI（DeepSeek 风格，默认 http://127.0.0.1:8000；--fake 离线演示）
conda run --no-capture-output -n agent-demo python -m my_coder.web --workspace . --fake
```

注意：Windows 控制台是 GBK，用 `--no-capture-output` 避免 conda run 二次打印乱码；`conda run` 的 `-c` 参数不支持多行/换行脚本，内联 Python 写到临时文件再跑。

## 架构（改动任何代码前必读）

> **实现新的机制/新功能前，先看仓库根的 `agent.md` 做调研参考**：它记录了
> DSH（deepseek-harness）、PI（pi-mono）、opencode 三家开源项目对同类机制
> （skill 按需加载、issue/PR 工作流、agent 角色、工具组织等）的现成实现与
> 对照，以及本仓库历次讨论的候选方案与动机。**注意**：agent.md 是参考手册
> 不是规范——落地规则以本文件（AGENTS.md）与架构文档为准；方案定稿后把
> 规则提炼进本文件，agent.md 只留背景。

**目录 = 分层**（2026-09 分层重构）：依赖方向不再只是本文件的约定，而是包结构 + 一条
`tests/test_architecture.py` 的断言（下层 import 上层当场红）。四层单向，数字越小越底层：

```
0 values/       值：values/messages.py（消息/事件/工具返回值的词汇表）+ values/persistence.py（JSONL 读写）
                + values/limits.py（**跨层共享的常量**：有更低层要用的数字就下沉到这里）
1 capability/   能力：capability/llm.py（LLM 客户端）、capability/hooks.py（三个决策钩子的类型）
2 state/        状态：state/session.py（日志，唯一事实源）/ state/inbox.py / state/prompt.py / state/registry.py
                / state/recovery.py / state/runtime_status.py（每轮叠给模型的运行时状态贡献者）
3 runtime/      框架循环：runtime/agent.py（被动状态机）、runtime/loop.py（turn/step 两级循环）
4 app/          应用内容：app/factory.py（组装）+ app/constants.py / app/sandbox.py / app/instructions.py /
                app/skills.py / app/compaction.py / app/ui.py / app/workspace.py（工作区选择策略）；tools/（工具实现）
5 web/          入口：Web 宿主（app / state / sessions / titles / payload）；cli.py
```

规则：**一个模块只能 import 同层或更低层**。常量同理：只在应用/工具层用的放 `app/constants.py`，**一旦有更低层要用就下沉到 `values/limits.py`**（`TOOL_RESULT_MAX_CHARS` 就是这么搬的）。**依赖白名单现在是空的**：两条历史例外都已按"修好即删"清掉（`registry → app.constants` 靠常量下沉、`runtime.loop → tools.todo` 靠注册制贡献者，见 issue #19）；将来再加例外必须带 issue 号，测试盯着不许长僵尸。

技能正文在 `my_coder/bundled_skills/`（随包发布，`pyproject` 的 package-data）与 `<workspace>/skills/`（项目自带），两边由 `app/skills.py` 按名字合并、`skill` 工具按名字取；工作区指令文件的发现与注入在 `app/instructions.py`（正文直接进 system，见约定）。上层依赖下层，下层不感知上层。

**日志（`.sessions/<id>.jsonl`）是唯一事实源**：模型记忆（`derive_messages`）、inbox 队列、回合号、模型路由全部是日志的重放投影。恢复 = 重放（`adopt`）+ **自愈**（补上崩溃留下的悬空工具调用，见 `state/recovery.py`），没有独立的对话状态。本仓库已建 CodeGraph 索引（`.codegraph/`），理解/定位代码先 `codegraph_explore`。

## 编辑时不可破坏的五个不变式

1. **没有状态不进日志**：新状态（工具结果、配置变更、注入）必须经 `session.append` 落事件
2. **模型可见 ⟺ 可重建**：只有 `user/message`、`assistant/message`、`tool/result` 三类 surface 事件能进模型消息，且 append 时必须带 `surface_op='append'`（`Session.append` 会校验）；`derive_messages` 必须是纯函数
3. **入队即记账**：inbox 改动先写 `agent/inbox/spliced` 事件再改内存（`_splice`），磁盘日志永远 ≥ 内存状态
4. **决策走钩子/注册声明**：`pre_step`/`request`/`request_error` 三钩子 + `execution_mode` 等注册声明，循环里不写业务 if
5. **失败降级为结果**：工具失败（坏 JSON、参数错、异常、超时）一律变 `is_error` 结果，不炸循环；`CancelledError` 沿 await 链单向传播，每层记账后放行

## 约定

- **提示词纪律的归属**：`system` 是唯一"每轮都生效"的通道；文档（含本文件）只有愿意读
  的 agent 才看得到。所以**反复被踩的坑要提成 system 里的通用规则**（`app/factory.py` 的
  `discipline` 段，order 10），文档只留事故、证据与理由。分层：通用规则放
  `identity`/`persona`/`discipline`，工具专属规则放各自的 `tool:*` 段——两者不互串
  （通用段塞工具细节 = 每轮都付的噪声；工具坑写进通用段 = 换个工具就失效）。
  当前五条通用纪律：**Scope**（"看看 / 评估 / 解释"= 只读调查，不为好奇制造真实副作用）、
  **Economy**（先查工作区、不重复调用、外部或昂贵操作先自问是否必要）、
  **Evidence**（命令必须自证输出——静默成功既不能证明成功也不能证明失败；多行脚本写
  临时文件再跑，内联多行的引号/换行跨 shell 会被吃掉）、
  **Cleanup**（任务结束后工作区要干净：能用 bash 就把 scratch 写到系统 temp；必须落在
  工作区时统一放进**一个**自明名字的临时目录、整目录删掉；**绝不删**不是自己造的 /
  git 跟踪的 / 会话日志；收尾用 `git status` 自证——只该剩下你要交付的东西。判据是
  "属于本次交付"，不是"用户点名要的"：顺手补的测试、顺手修的小缺陷**是交付**，别当垃圾删了）、
  **Instructions**（工作区里的 AGENTS.md / CLAUDE.md 是长期约定：存在就先读并遵循；
  出现稳定可复用的项目知识时提议写进去，而不是留在这次对话里；不得静默改它，
  也不得写密钥、临时状态、未验证的猜测）
  - **事故档案（为什么要有一条 Cleanup，2026-09）**：两次实测。① 一次真模型会话在仓库根
    留下三个扫描脚本（`_tmp_demo_scan.py` / `_tmp_demo_scan.txt` / `_tmp_scan.py`），
    用户得先分辨哪些是垃圾才能看清真正的 diff；② `369aa10` 的提交信息里记着"工作区清理：
    删掉 8 天前的 web_search diff 残留 `_tmp_d.txt` 与 `eval/recall/__pycache__`"——
    说明真实残留**不止"临时脚本及其输出"**（`__pycache__` 一类的缓存/复制件也会留），
    所以规则写**类别**而不是穷举。反面证据同样在案：`85a39eb` 早就写着"临时脚本写在系统
    temp、跑完即删，不进仓库"——**写在文档里挡不住**，这才是把它提成 system 规则的理由。
  - **两条硬护栏的来历**：`--sessions` 默认 `.sessions`（相对**工作区**，见 `cli.py`），
    而 `.sessions/` 在 `.gitignore` 里——会话日志既是唯一事实源、又是"带 log 字样、躺在
    工作区根、`git status` 里还看不见"的东西。所以"删掉 logs"这种措辞在本仓库是危险的：
    Cleanup 因此显式列出**绝不删**的三类（不是自己造的 / git 跟踪的 / 会话日志），
    并把"我清干净了"这种无法自证的声明换成可核对的 `git status`。
- **循环的 step 粒度 = 一次模型请求**（对齐 harness `core/agent-loop` 的
  `step()`：发完一次请求 + 执行完这次的工具调用就返回）：工具循环由 `run_turn`
  的外层循环驱动，**每轮开头都 claim inbox**。不要把它合并回 `_run_step` 的
  内层 while——插队消息的落地时机、停止的及时性、将来 guard 的每请求卡点
  全依赖这一点（旧实现把整段工具循环当一个 step，实测插队消息在 next-step 里
  躺了 2 分 40 秒后被 cancel 清掉，模型从没见过它）。细节见
  `ARCHITECTURE.md` §3.6
- **工具并发调度的规则**（落地时遵循；DSH 源码对照与实测证据见 `agent.md`）：
  - **并发许可是声明出来的（fail-closed）**：`ToolSpec.execution_mode` 默认
    `'sequential'`，只有"不写共享状态"的工具才显式声明 `'parallel'`。判据是工具的
    **副作用**而不是它快不快：读文件/搜索并发安全；`todo_write` 改日志投影，
    `bash`/`edit`/`write_file` 要人工把关——都是独占。漏声明的代价是不确定的交错
    （并发读-改-写丢更新），多声明一次的代价只是慢一点：方向反过来代价不对称。
    非法模式值在注册时刻抛错（宁炸勿静默）；**未注册的工具名同样按 fail-closed
    当独占**——分组阶段不许抛错，否则"工具未注册"这条失败会抢在执行之前的
    `mode()` 上炸掉整个回合，`_run_one` 的降级兜底永远没机会跑（不变式 5）
  - **"能不能并发"与"阻不阻塞事件循环"是两根正交的轴**：后者用 `offload=True`
    单独表达（executor 体内全是同步 I/O、没有任何 await 才声明）。不卸载的后果是
    `asyncio.wait_for` 的定时器根本没机会触发（超时保护形同虚设）+ 同进程的
    SSE/审批被那次同步读盘卡住。**卸载只给纯 I/O**：换了线程，`session`/`agent`
    的内存状态就没人在循环线程上守了
  - **分组按"连续段"，不是"整批跟第一个"**：并行段里一旦冒出独占调用就停手，
    把它和后面的留给外层**在同一步内**重新分类（`_run_group` 回传 `consumed`，
    对齐 harness `runGroup`）。池上限 `MAX_PARALLEL_TOOL_CALLS` 防止模型一次吐
    十几个调用全放出去
  - **结果按模型顺序落盘**：能并发 ≠ 能乱序记账。谁先跑完不一定谁先 append——
    结果先进槽位，队首连续就绪才提交（harness `commitReady` 的 contiguous slots），
    日志顺序因此恒等于调用顺序：模型记忆确定、前缀缓存可复用、前端按 call_id
    配对不必处理乱序
  - **取消也要补记账**：取消时"已请求但没有结果"的调用必须补一条 is_error 合成
    结果，否则模型记忆里会留下"带了 tool_calls 却没有结果"的 assistant 消息——
    wire 格式非法，下一轮请求直接 400。`tool/skipped` 这类痕迹事件不算数：
    它进不了 `derive_messages`
- **每轮叠给模型的运行时状态走"注册制贡献者"**（落地规则，2026-09，issue #19；
  DSH 的 runtime-context contributor 与 opencode 的 SystemContext 对照见 `agent.md` §4）：
  - **形态**：`state/runtime_status.py` 的 `RuntimeStatusRegistry` —— 一个状态源 = 一个名字 +
    `build(session) -> str | None`（**无内容返回 None**，那一轮就不出现，绝不用空串占位）；
    `register` 对空名/重名/`build` 不可调用**当场抛错**（重名会悄悄只留一个、不可调用会在
    求值时才炸并被兜底吞掉，两种都该在装配时暴露），返回注销函数。
  - **通道**：`runtime/loop.py` 每请求 `collect()` 一次，**非空者各贴一条合成 user 消息**在
    messages 末尾——不进日志、不进 `derive_messages`（历史零污染），放 system 则会把缓存
    前缀每请求打碎（动态事实不该进稳定前缀）。
  - **审计**：`request/header.runtime_status = {名字: 原文}`（映射形态，痕迹数据），回答
    "这一轮模型被告知了哪些运行时状态"；多个来源不必加平铺字段。**所有贡献者都返回空时，
    这个字段整体缺席**（与改造前的 `todo_status` 行为一致，不是空映射）。
  - **加一个状态源 = `app/factory.py` 的 `_runtime_status()` 里一行注册，循环一行都不用改**
    ——这就是这条通道的意义（此前是 `runtime/loop.py` 直接 import `tools.todo` 的反向依赖，
    见 `tests/test_architecture.py` 那条已清空的白名单）。将来的 L0 会话目录 / M2 预算水位
    （issue #3）也在这里各加一行。
  - **`build` 必须是日志投影的纯函数**（同一段日志 → 同一份状态，符合"模型可见 ⟺ 可重建"），
    且要便宜：**每个模型请求都会求值一次**。
  - **坏一个不炸对话**：单个贡献者求值时抛异常、或返回非字符串（契约是 `str | None`）→
    记一条 ERROR 日志（带名字）并跳过它，这一轮就是"没有这份状态"；审计映射只列**真的被告知
    模型**的项。迭代用**快照**，所以贡献者在求值期间注册/注销自己也不会炸（**求值期间的增删
    本轮不生效，下一请求生效**）。判据：这是可选的状态展示通道（对照：工具失败要降级成
    `is_error` 结果——那是模型输入可能不合法的通道；而**注册时刻**的空名/重名/不可调用仍然
    当场抛，那是宿主写错了代码）。`except Exception` **不捕 `BaseException`**：`CancelledError`
    照常穿透，"取消单向传播"不被这条兜底破坏（有用例钉住）。
- **恢复要自愈"悬空工具调用"**（落地时遵循；实测证据与 400 原文见
  `ARCHITECTURE.md` §3.16）：取消路径能补记账，但**进程被 kill / 断电 / OOM** 时
  没有任何代码有机会跑——日志会停在 `tool/call`（痕迹已落）与 `tool/result`
  （surface 未落）之间。后果不是"少一条结果"，而是模型记忆里留下"带了 tool_calls
  却没有结果"的 assistant 消息 → wire 非法 → **之后每次发送都失败，整个会话报废**
  （用户只能新建会话或手改 JSONL；UI 上先表现为那条工具行永远转圈）。规则：
  **每个恢复入口（重放之后、`bind_store` 之后）都调
  `recovery.repair_dangling_tool_calls`**；修复必须**写进日志**（补 is_error 合成
  结果 + 先落一条 `session/repaired` 痕迹），**禁止在请求构造时静默补占位消息**——
  那会把"这里断过"从唯一事实源里抹掉。函数幂等，可无条件调用
- **每对话一个工作区**（落地规则，2026-09；DSH 对照与取舍见 `agent.md` §10）：
  - **创建时定下，之后不可变**：`POST /sessions/new` 可带 `{"workspace": "<目录>"}`；
    已有会话再给 workspace → 400 `workspace is fixed`。半个对话换沙箱根会让"前几轮读 A、
    后几轮写 B"说不清楚——**换目录 = 新建对话**（DSH 同样靠 cwd 不可变绕开了"切换后重建什么"）
  - **写进日志**：落一条 `session/workspace` **痕迹事件**（`{workspace, source}`），
    由 `Session.workspace()` 从日志倒读——配置事实也从唯一事实源读回来，与 `session/title`
    同构。**不进 `derive_messages`**（不变式 ②），但重放/换进程/隔几天再开都回到同一个目录
  - **按 seat 隔离**：`Seat.args` = 复制宿主参数、只换 `workspace`；工具沙箱、指令探测、
    技能表、`{{workspace}}` 叙事都由 `build_agent` 当场从 `args.workspace` 派生，所以
    "每会话一套"就是"每会话一份 args"，不必再给 Seat 挂派生对象
  - **旧会话与三态**：本功能之前建的会话（日志里没有这条事件）**跟随宿主默认工作区**，
    且**不回头改写它的日志**；日志里记着的工作区**不存在了** → **409 + 明确原因**，
    **绝不静默回退**（静默回退 = 工具指向另一个项目，而模型以为还在原目录）。
    **判据落在"去掉空白后"的值上**：记录是空白的（写坏/手改坏）按"没记录过"处理——
    空白不是路径，若放它进 `resolve()` 会折叠成**进程 cwd**，那就是一次静默换根。
    列表项同时给 `workspace_ok`（目录还在不在），目录已失效的会话在界面上提前标出来
    （点开才会 409 的体验太差）；**已经开着的 seat 不因为目录被删而被打断**（工具调用
    自己会报 file not found），只有**重新打开**时才校验
  - **选择策略 = 信任界面使用者**（对齐 DSH，它也没有 allowlist）：只校验"存在 + 是目录"，
    判据集中在 `app/workspace.py` 的 `resolve_workspace`（相对路径按**进程 cwd**、空 = 宿主
    默认）。想收紧就只改这一处。Web 默认只绑 `127.0.0.1`，能点界面的人本来就等于把该目录
    的读写授权给 agent——这条权衡如实写在 README 的安全警告里
- **工作区项目指令文件的发现与维护**（落地时遵循；DSH 对照与实测见 `agent.md` §9）：
  - **候选与范围**：工作区根的 `AGENTS.md` / `CLAUDE.md`（对齐 DSH
    `DEFAULT_INSTRUCTION_FILE_CANDIDATES`）；子目录里的同名文件**只列路径**
    （正文由模型按需 read_file，和技能同一条路），隐藏目录不扫；清单**每回合重扫
    一次**（本回合新建的子目录约定下一回合可见）。**不向上发现**——我们的工具被
    沙箱限制在 workspace 内，注入一份读不到的约定只会制造幻觉
  - **注入通道 = system 的 live 段**（`app/instructions.py`，order 20：紧跟通用纪律、
    先于工具段与技能目录）。正文**直接进 system**（与 DSH 一致）而不是只给路径：
    项目约定属于"每轮都该生效"的规则。system 里有两个 live 段（另一个是技能目录），
    都是"读磁盘 + stat 键控缓存"：文件没变就不重读、字节就不变，所以不打碎缓存前缀
  - **两级新鲜度（文件来源的通用规则，判据是代价而不是"越新越好"）**：**内容按请求
    新鲜**——根目录正文每请求探测一次（`stat` 是微秒级，文件真变了才读盘）；**清单类
    信息按回合新鲜**——子目录清单要一次全树遍历（本仓库实测 ~600 µs，比一次 stat 贵
    三个数量级），所以拿回合号当刷新纪元（`agent.last_turn`）：同回合内多次请求复用
    同一份、新回合开头重扫。技能目录介于两者之间——它的"清单"来自 `scandir` + 每文件
    stat（实测 ~70 µs），所以按请求新鲜（见"按需技能"约定）
  - **预算**：单文件 8k / 整段 20k 字符；超预算**截断并明说**"用 read_file 读剩下的"，
    超过 1 MiB 的文件不读进内存（只留指引）。缺文件时给"没有指令文件 + 建议创建"
    的确定性提示（issue #6 的方案 C 落地）
  - **规则与状态分离**：通用规则（存在就先读并遵循 / 稳定知识提议写进去 / 不静默改 /
    不写密钥临时状态未验证猜测）属于**每轮都生效**的通用纪律 → 进 `discipline` 段；
    live 段只承载**状态与内容**。内容载体是**随包发布的技能** `project-instructions`
    （bundled，见"按需技能"约定：任何工作区都取得到），由技能目录按需加载
  - **探测是三态，不是两态**（对齐 DSH 的 `ScopeInstructionProbe` 与 opencode 的
    `SystemContext.unavailable`，后者原话是"distinguishes confirmed absence from
    provider failure"）：**确认存在**（注入正文）/ **确认不存在**（只有这一态才允许
    说"没有项目指令文件"、才允许建议创建）/ **读不到**（权限拒绝、IO 错误、同名目录、
    **断链符号链接**、**读的时候文件在动**、**符号链接指向工作区外**——必须说"内容未知"：
    既不许当成"没有约定"，也不许提议创建，因为可能覆盖一份已存在只是读不到的文件）。
    **不要把"不知道"降级成"没有"**，这是不变式 5 与"宁炸勿静默"在探测上的对应物
  - **三态落在"读盘的顺序"上**（两个坑，复盘见 issue #17）：① **`lstat` 再 `stat`**——
    `stat()` 跟随符号链接，断链抛的 `FileNotFoundError` 与"目录里没这个文件"长得一样，
    照旧写法会把断链当成"确认不存在"、让模型去创建一份其实已经存在（只是目标没了）的约定；
    ② **读完再取一次缓存键**——键是读之前取的 `(mtime_ns, size)`，读的过程中文件在变
    （编辑器保存 / agent 写文件 / git checkout）就可能读到半截，而半截会被当成"这个键
    对应的内容"**一直服务下去**（模型看到缺条目的约定还不自知）。键不一致就不写缓存、
    按"读不到"报出来，下个请求自然重读
  - **符号链接不越界**：候选文件由宿主直接读（不走工具沙箱），但做**同样的**越界
    检查——`AGENTS.md` 指向工作区外时**不注入**，并作为"读不到"报出来。否则一个
    `AGENTS.md -> ~/.ssh/id_rsa` 就能把工作区外的文件塞进 system prompt 发给模型，
    与 persona 的"工作区外不可读"直接矛盾。（与 `app/sandbox.py` 同级：hardlink /
    TOCTOU 不设防，"防误用保险"不是 OS 级沙箱）**技能的工作区来源是同一个洞**，
    共用同一判据：`sandbox.workspace_escape_reason`（见"按需技能"约定）
  - **输出必须可复现**：子目录扫描 `dirnames.sort()` 后再走（`os.walk` 的顺序取决于
    文件系统，排序前提前 `break` 收前 N 条会让不同机器得到不同子集 → 段字节不可复现）；
    文件没变时两次渲染**字节相同**，缓存前缀才不因"每请求重新渲染"而失效
  - **写入一律走 approval**：指令文件归根到底是个文件，创建/修改走 `write_file`/`edit`
    的 approval 门 → 变更作为 `tool/call` + `tool/result` 进日志（不变式 1）。**不做**
    DSH 的 baseline/delta 版本账（它注入一次所以要记增量）——我们每请求重渲染，
    重算替代版本账
- 注释/文档全部用中文，教学式讲解设计动机——新注释保持此风格
- 值对象必须 frozen dataclass + tuple，禁止把可变容器放进消息/事件（JSON 往返依赖）
- 严格校验哲学：未注册 prompt 变量、重复工具名、非法执行模式、surface_op 缺失都在写入时刻抛错，宁炸勿静默
- 每个文件顶部有 `from __future__ import annotations`
- **按需技能（skill）机制的设计决策**（落地时遵循；三家对照与取舍背景见
  `agent.md` §3，那里只留动机不作规范）：
  - 技能 = `*.md` 文件 + YAML frontmatter（name/description），正文是操作指南
    （纯指令，不携带执行代码）
  - **两个来源，按名字合并（对齐 DSH 的 project / user / bundled 与"同名覆盖"）**：
    **bundled** 随 agent 发布，放包内 `my_coder/bundled_skills/*.md`——agent 自带的
    通用能力（如"怎么写 AGENTS.md"）；**workspace** 放 `<workspace>/skills/*.md`——
    项目自己的约定与工作流。同名时 **workspace 覆盖 bundled**；合并后**按名字排序**，
    目录字节可复现。**自带能力必须跟着 agent 走**：放在工作区里等于"换个工作区就
    消失"（issue #22 实测：用户自己的项目里目录为空、`read_file` 读包内技能被沙箱拒）
  - **目录（catalog）以 live 段注入 system**（order 95：排在工具段之前；动态状态不走
    system——todo 状态栏挂在 messages 末尾）：只放 name + description，**不列路径**
    ——列路径会诱导模型去 `read_file`，而 bundled 技能在工作区之外、读了会被拒。
    段文本由 `SkillTable.skills()` 现算：每次求值只算一遍**内容指纹**（两个技能目录的
    `*.md` 名单 + 每文件 `(mtime_ns, size)`，实测 ~70 µs），指纹变了才重扫重解析
    （~300 µs）——所以**会话中途新增/改写/删除技能，下一次模型请求就生效**，
    文件没变时目录字节不变、缓存前缀照样命中。目录与 `skill`
    工具**共用同一个 `SkillTable` 实例**（`factory` 构造一次传两处），且工具在**执行
    时**取表：两处永不漂移，也不会出现"目录念旧描述、工具给新正文"（早期目录段是
    build 时的静态字符串，这两条都做不到）。**刷新要加锁 + 双检**：这张表被两个线程
    碰（live 段在循环线程、`offload=True` 的 skill 工具在工作线程），无锁时两次刷新
    可能交错成"指纹是新的、表是旧的"——那之后每次求值都以为没变，描述就**永远**停在
    旧值；快路径（指纹没变）不碰锁，所以循环线程不会被工作线程的重扫阻塞
  - **工作区来源必须做越界检查（bundled 豁免）**：技能正文由宿主直接读、不走工具沙箱，
    所以 `skills/x.md -> 工作区外的 md` 与指令文件那条是同一个洞（issue #25）——放行的话
    一份项目里的符号链接就能把工作区外的文件读给模型。判据与措辞两处共用
    `sandbox.workspace_escape_reason`（**一条规则一处实现**）；越界的技能在**读文件之前**
    就被跳过并打 stderr 诊断（否则等于"先泄后拦"），于是目录与工具两边同时看不到它。
    `scan_skills(source=WORKSPACE)` 少给 `boundary` 直接抛错：宁炸勿静默
  - **正文 = 工具结果注入，按名字取**：模型调 `skill(name)` → host 把名字解析到文件
    → 正文作为 tool/result（source.kind='tool'）进 derive_messages，与读任何文件机制
    一致（落日志可重建、可被 compaction 折叠）。**这是对早期"不新增 skill() 加载
    工具"那条决策的收窄**：它的前提是"技能文件总能被 `read_file` 读到"，而 bundled
    技能在包内、被沙箱挡住——所以自带技能必须有一条**不经过路径**的通道；
    workspace 技能为统一取法也走同一个工具。**沙箱承诺不受影响**：工具入参只有
    名字，模型没有机会拼出路径；查不到就是一条 `is_error` 结果（失败降级为结果）
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
    做序列化（`web.payload.queue_rows(agent)` 摊平成 JSON）
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
- **发现缺口时怎么办（本仓库的工作方式，2026-09 复盘）**：实现或回顾时发现问题，
  **先判断能不能就地修，再决定要不要开 issue**——① 是本 PR 引入的 → **必须在本 PR
  修掉**，不留尾巴（哪怕窗口极小）；② 修它只要几十行、且不改变本 PR 的语义 →
  **顺手修**，在 PR 正文里点明；③ 只有"真的大 / 真该单独评估"的才开 issue，且必须
  写清**建议什么时候修、卡在什么前提**。同主题**合并成一条**（别把一个批次拆成三条）；
  **没承诺要做的取舍与候选方案进 `NEXT_STEPS.md`，不进 issue 列表**——issue 只装
  "决定要修 / 要做的事"。起因：一度"发现即记 issue"，两天开了 6 条，列表长得比关得
  快（11 开 / 5 关），其中几条是十几行就能修完的小缺陷——那不是记录，是把活推给未来

## 入口与工具

- `my_coder/cli.py`：CLI 入口；`--fake` 用脚本化假模型离线跑通全流程（不需要 API key）；`--resume` 演示日志重放恢复
- `my_coder/web/`：Web 宿主（入口层，FastAPI + SSE，会话管理/标题/approval/工作区）——拆成 `app.py`（路由+装配）/ `state.py`（Seat+宿主状态，Seat 带自己的 `args`）/ `sessions.py`（seat 生命周期 + 每会话工作区）/`titles.py`（自动标题）/ `payload.py`（纯函数投影，不依赖 FastAPI）；`python -m my_coder.web` 是它的入口，`app/factory.py` 的 `build_agent`/`load_env` 被 CLI 与 Web 共用；Web 的 `--workspace` 是**默认**工作区，每个对话可在界面上另选一个（见约定"每对话一个工作区"）
- `show_memory.py`：教学脚本，重放日志展示"记忆 = 日志投影"
- 工具在 `my_coder/tools/`：`build_tools(workspace, skills=…)` 组装（read_file 行号分页 / list_files / grep / glob / edit / write_file / bash / todo_write / web_search / **skill**（按名字取技能正文）），工具类型（`ToolSpec`：schema + executor + 并发模式 + 卸载声明 + 超时 + requires_approval）在 `state/registry.py`；`--workspace` 在 CLI 是**必填的路径边界**、在 Web 是**默认工作区**（每个对话可另选，见约定"每对话一个工作区"），边界实现同在 `app/sandbox.py`；bash/write_file/edit 执行前需人工确认；阶段一实施进度见 `NEXT_STEPS.md`
- `web_search` 与 `skill` 是两个"读工作区之外"的工具：前者的**搜索能力由 DeepSeek 官方在服务端提供**（Anthropic 兼容 `.../anthropic/v1/messages` + 原生服务端工具 `web_search_20250305`），我们只做"发请求 + 解析结构化块"——绝不自己抓网页、绝不从模型正文里抠 URL；没有结果块要**响亮报错**而不是退化成"没找到"；后者按**名字**（不是路径）取包内/bundled 技能正文，模型没有机会拼出任意路径。两个都不读工作区文件、无副作用，所以**不走 workspace 沙箱、也不需要 approval**（web_search 与 DSH 一致，见 `agent.md` §6）
- `.env` 存 `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`；`.sessions/`、`.codegraph/`、`.env` 均不入库
