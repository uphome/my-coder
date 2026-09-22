# AGENTS.md

Python 复刻 deepseek-harness 架构的教学 demo（agent 框架本身，不是应用）。完整设计见 `ARCHITECTURE.md`；快速上手与不变式见 `README.md`。

## 命令

```sh
# 一切 Python 命令必须走 conda 环境 agent-demo（base 里没有 pytest/httpx）
# 质量门：ruff + mypy + pytest 三绿才可提交（pyproject.toml 已配好）
conda run -n agent-demo python -m ruff check agent_demo tests
conda run -n agent-demo python -m mypy agent_demo
conda run -n agent-demo python -m pytest        # 126 个测试

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
入口（`cli.py` / `web_app.py` → `factory.py` 组装）→ 框架循环（`agent.py` 被动状态机 / `loop.py` turn-step）→ 状态（`session.py`/`inbox.py`/`prompt.py`/`registry.py`）→ 能力（`llm.py`/`hooks.py`）→ 值（`values.py` + `persistence.py`）。应用内容独立成包：工具在 `agent_demo/tools/`（file_io/search/shell/todo/web_search/skill + build_tools 组装）、渲染在 `ui.py`、路径边界在 `sandbox.py`、常量在 `constants.py`；技能正文在 `agent_demo/bundled_skills/`（随包发布，`pyproject` 的 package-data）与 `<workspace>/skills/`（项目自带），两边由 `skills.py` 按名字合并、`skill` 工具按名字取；工作区指令文件的发现与注入在 `instructions.py`（正文直接进 system，见约定）。上层依赖下层，下层不感知上层。

**日志（`.sessions/<id>.jsonl`）是唯一事实源**：模型记忆（`derive_messages`）、inbox 队列、回合号、模型路由全部是日志的重放投影。恢复 = 重放（`adopt`）+ **自愈**（补上崩溃留下的悬空工具调用，见 `recovery.py`），没有独立的对话状态。本仓库已建 CodeGraph 索引（`.codegraph/`），理解/定位代码先 `codegraph_explore`。

## 编辑时不可破坏的五个不变式

1. **没有状态不进日志**：新状态（工具结果、配置变更、注入）必须经 `session.append` 落事件
2. **模型可见 ⟺ 可重建**：只有 `user/message`、`assistant/message`、`tool/result` 三类 surface 事件能进模型消息，且 append 时必须带 `surface_op='append'`（`Session.append` 会校验）；`derive_messages` 必须是纯函数
3. **入队即记账**：inbox 改动先写 `agent/inbox/spliced` 事件再改内存（`_splice`），磁盘日志永远 ≥ 内存状态
4. **决策走钩子/注册声明**：`pre_step`/`request`/`request_error` 三钩子 + `execution_mode` 等注册声明，循环里不写业务 if
5. **失败降级为结果**：工具失败（坏 JSON、参数错、异常、超时）一律变 `is_error` 结果，不炸循环；`CancelledError` 沿 await 链单向传播，每层记账后放行

## 约定

- **提示词纪律的归属**：`system` 是唯一"每轮都生效"的通道；文档（含本文件）只有愿意读
  的 agent 才看得到。所以**反复被踩的坑要提成 system 里的通用规则**（`factory.py` 的
  `discipline` 段，order 10），文档只留事故、证据与理由。分层：通用规则放
  `identity`/`persona`/`discipline`，工具专属规则放各自的 `tool:*` 段——两者不互串
  （通用段塞工具细节 = 每轮都付的噪声；工具坑写进通用段 = 换个工具就失效）。
  当前四条通用纪律：**Scope**（"看看 / 评估 / 解释"= 只读调查，不为好奇制造真实副作用）、
  **Economy**（先查工作区、不重复调用、外部或昂贵操作先自问是否必要）、
  **Evidence**（命令必须自证输出——静默成功既不能证明成功也不能证明失败；多行脚本写
  临时文件再跑，内联多行的引号/换行跨 shell 会被吃掉）、
  **Instructions**（工作区里的 AGENTS.md / CLAUDE.md 是长期约定：存在就先读并遵循；
  出现稳定可复用的项目知识时提议写进去，而不是留在这次对话里；不得静默改它，
  也不得写密钥、临时状态、未验证的猜测）
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
- **工作区项目指令文件的发现与维护**（落地时遵循；DSH 对照与实测见 `agent.md` §9）：
  - **候选与范围**：工作区根的 `AGENTS.md` / `CLAUDE.md`（对齐 DSH
    `DEFAULT_INSTRUCTION_FILE_CANDIDATES`）；子目录里的同名文件**只列路径**
    （正文由模型按需 read_file，和技能同一条路），隐藏目录不扫；清单**每回合重扫
    一次**（本回合新建的子目录约定下一回合可见）。**不向上发现**——我们的工具被
    沙箱限制在 workspace 内，注入一份读不到的约定只会制造幻觉
  - **注入通道 = system 的 live 段**（`instructions.py`，order 20：紧跟通用纪律、
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
    说"没有项目指令文件"、才允许建议创建）/ **读不到**（权限拒绝、IO 错误、同名目录
    ——必须说"内容未知"：既不许当成"没有约定"，也不许提议创建，因为可能覆盖一份已
    存在只是读不到的文件）。**不要把"不知道"降级成"没有"**，这是不变式 5 与
    "宁炸勿静默"在探测上的对应物
  - **符号链接不越界**：候选文件由宿主直接读（不走工具沙箱），但做**同样的**越界
    检查——`AGENTS.md` 指向工作区外时**不注入**，并作为"读不到"报出来。否则一个
    `AGENTS.md -> ~/.ssh/id_rsa` 就能把工作区外的文件塞进 system prompt 发给模型，
    与 persona 的"工作区外不可读"直接矛盾。（与 `sandbox.py` 同级：hardlink /
    TOCTOU 不设防，"防误用保险"不是 OS 级沙箱）
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
    **bundled** 随 agent 发布，放包内 `agent_demo/bundled_skills/*.md`——agent 自带的
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
- 工具在 `agent_demo/tools/`：`build_tools(workspace, skills=…)` 组装（read_file 行号分页 / list_files / grep / glob / edit / write_file / bash / todo_write / web_search / **skill**（按名字取技能正文）），工具类型（`ToolSpec`：schema + executor + 并发模式 + 卸载声明 + 超时 + requires_approval）在 `registry.py`；`--workspace` 必填（路径边界，`sandbox.py` 实现）；bash/write_file/edit 执行前需人工确认；阶段一实施进度见 `NEXT_STEPS.md`
- `web_search` 与 `skill` 是两个"读工作区之外"的工具：前者的**搜索能力由 DeepSeek 官方在服务端提供**（Anthropic 兼容 `.../anthropic/v1/messages` + 原生服务端工具 `web_search_20250305`），我们只做"发请求 + 解析结构化块"——绝不自己抓网页、绝不从模型正文里抠 URL；没有结果块要**响亮报错**而不是退化成"没找到"；后者按**名字**（不是路径）取包内/bundled 技能正文，模型没有机会拼出任意路径。两个都不读工作区文件、无副作用，所以**不走 workspace 沙箱、也不需要 approval**（web_search 与 DSH 一致，见 `agent.md` §6）
- `.env` 存 `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`；`.sessions/`、`.codegraph/`、`.env` 均不入库
