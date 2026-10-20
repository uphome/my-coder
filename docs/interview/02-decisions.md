# 决策卡：二十一个"为什么不是另一种"

> 每张卡回答四件事：**当时的替代方案是什么、为什么否决、选中它的代价、哪里能证明**。
> 这是面试里信息密度最高的部分——能说出"否决了什么"的人，才是做过决定的人。
> 详细理由在 [`../notes/`](../notes/)，这里只压成一页。

## A. 事件与记忆

### 1. 日志是唯一事实源

- **问题**：会话状态住哪？
- **否决**：内存里维护对话对象 + 定期/退出时快照持久化。
- **理由**：快照与实时状态必然漂移；崩溃丢的是"上一次快照之后"的窗口，而这个窗口的
  大小不可控。事件日志是只追加的，崩溃最多丢最后一条。（事后重构的叙事：没有设计记录
  保存当时对被否决方案的评估。）
- **代价**：日志会很大（长会话 MB 级）；读取要重放，必须做增量与跳过优化
  （`Session.from_path(skip_types=…)`、`events_since`）。
- **证据**：`my_coder/state/session.py`、`docs/notes/implemented/simplification/2026-10-03-events-view-cache.md`。

### 2. surface：只有三类事件能进模型上下文

- **问题**：日志全量都该给模型看吗？
- **否决**：所有事件都进上下文（"日志即提示词"）。
- **理由**：痕迹事件（流式帧、`request/header` 等）占日志 99.6% 的行数，
  它们对模型没有信息量，却会吃掉整个窗口。（否决理由只有 `architecture.md` §3.2
  的一句"为什么不让所有事件浮上来"，没有独立设计记录——这条是事后重构的叙事。）
- **代价**：要维护一层"投影"（`_surface` 索引），并且任何新事件都必须先回答
  "它是 surface 还是痕迹"——写作时校验（`surface_op`），写错当场炸。
- **证据**：`my_coder/state/session.py:SURFACE_EVENT_TYPES`。

### 3. `replace` 用位置语义，不用 seq 区间

- **问题**：压缩时 checkpoint 顶替旧对话，遮蔽范围怎么标？
- **否决**：`start_seq <= seq <= end_seq` 的数值过滤。
- **理由**：checkpoint 的 seq 必然**大于**被它顶替的旧 seq（追加写），替换后 `_surface`
  不再单调。第二次压缩时，数值区间会把范围内的无关节点误吞——这是真实 bug。
- **代价**：`_apply_surface` 要做 `index()` 查找（O(n)，但只在压缩时发生）。
- **证据**：`my_coder/state/session.py:_apply_surface`、
  `docs/notes/implemented/bug-fix/2026-10-15-position-vs-seq.md`。

### 4. 帧不落盘

- **问题**：流式打字机的每一帧要不要进日志？
- **否决**：每帧一条事件（早期实现）。
- **理由**：实测同一份日志：116.3 MB / 425,854 行 → 约 12.5 MB / 4,530 行；
  帧占 89.3% 字节、98.9% 行。而内容并没有丢——`assistant/message` 有正文与工具参数全文，
  `assistant/reasoning` 有思维链全文。
- **代价**：历史刷新后看到的是"结论"，不是"当时一帧帧打出来的样子"；需要一个
  `emit_stream` 瞬时通道与"每步一条汇总"。
- **证据**：`docs/notes/implemented/feature/2026-10-03-stream-log-granularity.md`、
  `state/session.py:emit_stream`。

## B. 循环与调度

### 5. step 的粒度 = 一次模型请求

- **问题**：一个"步"是整段工具循环，还是一次模型往返？
- **否决**：整段工具循环（`_run_step` 内部反复模型 ↔ 工具）。
- **理由**：插队消息要等下一次 `claim` 才浮上水面，而 claim 在 `run_turn` 每轮开头。
  旧实现下它要等整段自主运行结束——实测一条插队消息在队列里躺了 2 分 40 秒，
  期间 50+ 次工具调用、`step` 恒为 1，最后被当未处理输入清掉。
- **代价**：日志事件更细（`step/start` 与 `request/header` 1:1）；每步都要重新组请求。
- **证据**：`my_coder/runtime/loop.py:run_turn`、`docs/architecture.md` §3.6。

### 6. `peek` 与 `claim` 分离

- **问题**：认领与落盘之间能不能有 `await`？
- **否决**：先 `claim` 摘掉消息，再跑 `pre_step`（钩子要 await）。
- **理由**：取消点落在中间时，消息**三处都不在**——队列没了、surface 没有、
  日志也没有 `canceled` 标记。用户原话静默消失。现在两个 await 都在 claim 之前，
  取消时队列一个字没动。
- **代价**：`peek` 的批次规则必须与 `claim` 逐字一致，否则"钩子看到的"与"真正认领的"会漂。
- **证据**：`my_coder/state/inbox.py:peek`、
  `docs/notes/implemented/architecture/2026-10-01-cancel-atomicity.md`。

### 7. 工具并发许可是"声明"，且 fail-closed

- **问题**：多个工具调用能不能并发？
- **否决**：默认并发，或由循环按工具名判断。
- **理由**：漏声明的代价是**不确定的交错**（写共享状态的工具并发读-改-写会丢更新）；
  多声明一次的代价只是慢一点——两边不对称，所以默认独占。`execution_mode` 非法值注册时抛错。
- **代价**：多写一个字段；真实会话里 `[parallel…, sequential…]` 形状的批次会被切成两段串行。
- **证据**：`my_coder/state/registry.py:ToolSpec`、`docs/architecture.md` §3.15。

### 8. `execution_mode` 与 `offload` 是两根正交的轴

- **问题**：并发 与 阻塞事件循环 是同一件事吗？
- **否决**：用一个"安全/不安全"标志同时表达。
- **理由**：`todo_write` 不并发但也不阻塞（无 I/O）；`read_file` 既并发安全又**阻塞**
  （同步盘 I/O）。若不给阻塞 executor 卸载，协程体没有挂起点，
  `asyncio.wait_for` 的超时**永远不触发**——超时保护形同虚设，SSE 与审批也被卡住。
- **代价**：两条声明都要手工维护对；难度在于"卸载只给纯 I/O"这条硬约束（换线程后
  内存状态没人守）。
- **证据**：`my_coder/state/registry.py:ToolRegistry.execute`、`tools/__init__.py:build_tools` 的注册声明。

### 9. 结果按模型顺序落盘

- **问题**：并发跑完的先后，要不要反映到日志顺序？
- **否决**：完成顺序落盘。
- **理由**：并发只该改变"谁先跑完"，不该改变日志顺序。顺序一乱，同一段对话每次重放的
  前缀就不同——前缀缓存命中率下降、测试不稳定、前端除了按 call_id 配对还得处理
  "结果早于调用"的畸形序列。
- **代价**：需要槽位 + `commit_ready` 的连续推进逻辑。
- **证据**：`my_coder/runtime/loop.py:_run_group`。

### 10. 未注册的工具名按 fail-closed 当独占

- **问题**：模型幻觉出一个不存在的工具，分组阶段怎么办？
- **否决**：`mode()` 抛 `KeyError`。
- **理由**：分组发生在执行**之前**。在这一步抛错，`_run_one` 里那段"把工具未注册降级成
  is_error 结果"的兜底永远没机会执行——整回合没有 `turn/end`，日志里留下
  "请求了工具却没有结果"的 assistant 消息，wire 非法，模型什么都看不到。
- **代价**：类型检查上"未注册"被当成合法输入，错误推迟到执行阶段才变成结果。
- **证据**：`my_coder/state/registry.py:ToolRegistry.mode`。

### 11. 取消要补记账，崩溃要在恢复时自愈

- **问题**：工具调用落了两条事件之间的进程被 kill，怎么办？
- **否决**：在请求构造时补占位消息（能让 400 消失）。
- **理由**：那会把"这里断过"从日志里抹掉，而日志是唯一事实源。正确做法是**恢复时修**，
  写一条 `session/repaired` 痕迹 + is_error 合成结果，文案本身说明来历。
- **代价**：两个恢复入口都必须显式调一次；修复是持久化的（写日志），不能在内存里"顺手补"。
- **证据**：`my_coder/state/recovery.py`、`docs/architecture.md` §3.16。

### 12. 收敛判据 = "连续只读调用次数"，不是回合步数

- **问题**：怎么发现模型在原地打转？
- **否决**：按总步数设上限（opencode 的做法）。
- **理由**：实测一个 61 步的**实现型**长回合，最长连续只读段只有 4。按总步数砍会砍掉
  合法的大任务；而"连续读了多少次、中间没有任何变更"才真正区分"打转"与"边改边验"。
- **代价**：每个纯读工具必须显式声明 `cacheable=True`（默认 `False` = 会变更），
  并且 `cacheable` 与 `requires_approval` 互斥（注册时校验）。
- **证据**：`my_coder/state/progress.py`、
  `docs/notes/implemented/feature/2026-09-30-tool-convergence.md`。

## C. 上下文与提示词

### 13. 压缩只改"看得见什么"，不改"存在什么"；召回分三层

- **问题**：压缩之后细节丢了怎么办？
- **否决**：把被压缩的原文删掉，或做相关性排序后塞回上下文。
- **理由**：`replace` 只动 surface 索引，日志一个字不改——所以原文一直可读。
  语料是**自述的时间线**而不是文档堆，模型看清单自己挑回合比任何打分都准。
- **代价**：要维护三层投影（L0 目录每请求求值、L1 清单、L2 回合明细），
  且每请求路径不许碰"整表"（踩过四层 O(n)：278 ms → 0.1–0.2 ms）。
- **证据**：`my_coder/app/recall.py`、`tools/recall.py`、`docs/architecture.md` §3.22。

### 14. 指令文件注入 system，运行时状态贴 messages 末尾

- **问题**：每轮都要生效的内容放哪？
- **否决**：全部塞进 system；或全部作为消息追加。
- **理由**：判据是**变化频率**。`AGENTS.md` 正文会话内几乎不变 → 进 system 的 live 段，
  字节稳定所以不打断前缀缓存；todo 状态每步都可能变 → 作为合成 user 消息贴 messages 末尾，
  放 system 会每请求打碎一次缓存前缀。
- **代价**：两条通道两套新鲜度（正文每请求按 `(mtime_ns,size)` 缓存；子目录清单每回合重扫）。
- **证据**：`my_coder/app/instructions.py`、`docs/architecture.md` §3.17。

### 15. 探测是三态："读不到"不等于"没有"

- **问题**：`AGENTS.md` 不存在、和存在但读不到，要渲染成一样吗？
- **否决**：两态（读到正文 / 当成没有）。
- **理由**：把"不知道"降级成"没有"，模型会提议创建一份其实**已经存在**的约定；
  也可能覆盖它。所以有第三态 `files="unreadable"` + "内容未知、不要当成没有约定"。
- **代价**：要 `lstat` 再 `stat`（断链符号链接 ≠ 不存在），并且**读完再取一次缓存键**
  （读到半截会被缓存住并持续服务）。
- **证据**：`my_coder/app/instructions.py`、`docs/architecture.md` §3.17。

### 16. 技能按名字取，不按路径取

- **问题**：模型怎么读技能正文？
- **否决**：目录里列路径，让模型 `read_file`（PI 式）。
- **理由**：bundled 技能在**包内**、工作区外，沙箱会拒；要么给沙箱开例外，要么改取法。
  按名字取只需要一个 `skill(name)` 工具，沙箱承诺一个字不用改，正文仍作为 `tool/result`
  进日志（可重建、可折叠、能画成工具卡）。
- **代价**：目录段不能列路径（列了会诱导模型去 `read_file`）；需要 `SkillTable` 保证
  目录描述与工具正文永不漂移（加锁 + 双检 + 内容指纹）。
- **证据**：`my_coder/app/skills.py`、`tools/skill.py`、`docs/architecture.md` §3.18。

### 17. 运行时状态走注册制贡献者

- **问题**：循环怎么把"当下才算得出来的状态"（todo 等）告诉模型？
- **否决**：循环里直接 import 具体来源（旧实现）。
- **理由**：那是框架层反向依赖应用层；加一个状态源就要改循环，审计字段也只能平铺。
  现在循环只认"名字 + `build(session) -> str | None`"，注册点在 `app/factory.py` 一行。
- **代价**：贡献者必须是日志投影的**纯函数**且便宜（每请求求值）；坏一个记 ERROR 跳过。
- **证据**：`my_coder/state/runtime_status.py`、`docs/architecture.md` §3.21。

## D. 宿主与工程

### 18. 会话工作区不可变、进日志

- **问题**：允许一个对话中途换目录吗？
- **否决**：支持热切换（把已有对话迁到别的目录）。
- **理由**：半个对话跨两个根，语义说不清——旧目录读到的内容算不算这个对话的记忆？
  DSH 靠 cwd 不可变绕开了这个问题。换目录 = 新建对话。
- **代价**：日志里要有 `session/workspace` 痕迹；工作区不存在时只能 **409 + 明确原因**，
  绝不静默回退（静默回退会让模型以为自己还在原目录）。
- **证据**：`my_coder/app/workspace.py`、`docs/architecture.md` §3.20。

### 19. Web 端每个会话一个 seat（不是全局单例）

- **问题**：两个标签页各跑一个会话，怎么不互相干扰？
- **否决**：全局 `_session/_agent/_active_queue`（旧实现）。
- **理由**：A 的审批会推到 B 的流，事件只进焦点会话。seat 化后每个会话一份
  `{session, agent, queue, approvals}`，approval 钩子是 per-seat 闭包。
- **代价**：每个 seat 一份 `args`（只有 workspace 不同）；要处理打开竞态（按 sid 分锁 + 双检）
  与常驻上限（LRU 只释放空闲会话）。
- **证据**：`my_coder/web/state.py`、`web/sessions.py`、`docs/notes/implemented/feature/2026-10-03-seat-eviction.md`。

### 20. 前端 nodes 投影：DOM 不是状态

- **问题**：实时 SSE 与历史加载要不要共用一套渲染逻辑？
- **否决**：实时路径直接长 DOM、历史路径另铺一套（旧实现）。
- **理由**：两套路径产出必然不一致（重开会话时插队消息渲染成独立回合、助手消息渲染不出来）。
  现在 `nodes` 数组是唯一事实源，实时帧与 `/history` 都喂进同一套构建逻辑。
- **代价**：需要 `(turn, step)` 结构标记、`rpc_id` 原子交接、`partialRender` 降级为纯渲染优化。
- **证据**：`web/index.html`、`docs/subsystems/web-projection.md`。

### 21. 门禁写成测试，不写成文档

- **问题**：分层、文档预算、文档数字这些规则怎么不腐化？
- **否决**：写在 `AGENTS.md` 里靠自觉（实测无效——文档里的规则挡不住漂移）。
- **理由**：`AGENTS.md` 长到 22,553 字符时只有前 8,000 进 system，13 个规则块里 9 个
  模型从没见过。同理，依赖方向写在文档里时实测长出了两处反向依赖。
- **代价**：规则改动要同时改测试；白名单必须"修好即删"（`test_known_violations_are_still_real`
  会主动报红）。
- **证据**：`tests/test_architecture.py`、`tests/test_docs.py`、`tests/test_doc_numbers.py`。

## 明确不做（连同前提）

| 没做 | 前提条件 |
|---|---|
| OS 级沙箱 | 需要 bash 执行后端可替换 + 平台特定 API（Windows token / Linux bwrap / macOS seatbelt） |
| 工作区热切换 | 先回答"旧目录读到的内容算不算这个对话的记忆" |
| 会话日志按工作区分目录 | 等价于一次存储迁移（扫描/删除/切换/测试全要动） |
| 持久化后端抽象 | 现在只有 JSONL 一种；抽象要有第二个实现才不是空壳 |
| 按参数的并发谓词 | harness 的 `isConcurrencySafe(args)` 是动态判定，我们只有静态声明 |
| max-tokens 粘性续写 | 需要"截断后自动继续"的回合语义，与"用户取消"要分开 |
| 多 provider / web_fetch | 搜索只接 DeepSeek 官方原生搜索；抓正文是另一个能力 |
| RATE_LIMIT 退避 | `request_error` 钩子已被"溢出恢复"占用，两者要共存得先定优先级 |
