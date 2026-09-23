# agent-demo 架构与核心机制说明

> 本文档整合代码带读过程中厘清的全部概念：架构分层、核心机制、
> 关键设计决策，以及后续进化路线图。配合 `README.md`（快速上手）
> 阅读，本文是"为什么这样设计"的完整回答。

---

## 1. 项目定位

deepseek-harness 核心架构的 Python 复刻（学习用），约 1000 行，忠实实现
harness 的四个核心设计：

1. **日志是唯一事实源**
2. **模型可见 ⟺ 可重建**
3. **被动状态机 + Inbox**
4. **决策走钩子**

运行产物：`.sessions/<id>.jsonl`，每行一条事件。日志本身就是调试器。

---

## 2. 架构：四层单向依赖

```
入口层  cli.py / web/         CLI 与 Web 两个入口（经 factory.build_agent 组装）
        │
循环层  runtime/agent.py      被动状态机：send → inbox → wake → driver → idle
        runtime/loop.py       turn/step 两级循环 + 三个钩子
        │
状态层  state/session.py    追加式事件日志（唯一事实源）+ derive_messages 投影
        state/inbox.py      双队列 pending 消息（spliced 事件的持久化投影 + queued_items 队列投影）
        state/prompt.py     sections 按 order 拼接 + {{变量}} 严格插值
        state/registry.py   工具类型：ToolSpec（schema + executor + 模式）
        │
能力层  capability/llm.py        OpenAI 兼容 SSE 流式客户端 + 可脚本化 FakeLlm
        capability/hooks.py      三个决策钩子的类型（属循环层接口）
        │
值 层  values/messages.py      不可变 Message/SessionEvent + JSONL 编解码
        values/persistence.py JSONL 追加写 + 重放读（横切状态层的 I/O 通道）
```

依赖方向只有一条：上层依赖下层，下层不感知上层。工具在 `tools/` 包
（应用内容，依赖状态层与 registry）、渲染在 `app/ui.py`、路径边界在
`app/sandbox.py`、上下文压缩在 `app/compaction.py`——都是"应用内容"，框架
四层不感知它们。

---

## 3. 核心机制

### 3.1 日志是唯一事实源，记忆 = 日志的投影

整个架构最核心的一句话：

> **模型的记忆 = 日志的投影 = derive_messages() 的输出 = 下次 request 的 messages**

"记忆"不是一份独立存储的对话状态——它根本没被存下来。每次发起模型
请求前，循环实时地从日志折叠出来（`runtime/loop.py` 调 `session.derive_messages()`）：

- 删掉日志 → 记忆消失
- 重放日志 → 记忆完整还原
- 同一段日志 → 永远折叠出同一份记忆（纯函数）

所以 `--resume` 不需要"恢复记忆"这个动作，只有"重放日志"：
`load_events` 读回事件 → 逐条 `session.adopt` 重建投影 → 记忆自然还原。
这就是"恢复 = 重放，零额外代码"。

### 3.2 surface：浮上水面的才是记忆

把日志想象成一片海：**所有**事件都沉入海底（含痕迹数据——流式 chunk、
turn/step 边界、todo 更新、spliced 队列账目），但模型只能看见**海面**。

只有三类事件能浮上水面（`SURFACE_EVENT_TYPES`）：

| 事件类型 | 折叠成什么 |
|---|---|
| `user/message` | 一条用户消息 |
| `assistant/message` | 一条模型消息 |
| `tool/result` | 一条工具结果消息 |

浮标是 `surface_op='append'`：写日志时显式声明（`append` 校验：这三类
必须带、其他类带了报错），Session 把该事件的 seq 记进 `_surface` 投影。
`derive_messages` 按 surface 顺序折叠出消息序列。

**为什么不让所有事件浮上来**：日志负责"完整"（调试、重放），海面负责
"精选"（模型上下文）。两者分离，各司其职。

### 3.3 append / adopt 的不对称：resume 的全部秘密

```python
append：新事件 → 校验 + 落日志 + 更新投影 + 通知 listener
adopt ：旧事件重放 → 只重建日志与投影，不触发监听、不重跑逻辑
```

进程重启恢复时，磁盘上每行事件只经 `adopt` 走一遍——不是"重新计算"，
是"重新投影"。listener（持久化/UI）只在 append 后触发，保证订阅者看到
的状态和日志一致。

### 3.4 入队即记账：Inbox 的先记账后投影

双队列：`next-turn`（普通输入，开新回合）/ `next-step`（插队，当前回合
内即时生效）。路由不靠判断消息内容，**由调用方意图决定**——`followup()`
进 next-turn，`steer()` 进 next-step，输入发生的时机天然携带意图。

队列改动的唯一入口是 `_splice`（append/prepend/clear/claim 都归结于此），
顺序严格：

```
先：session.append('agent/inbox/spliced', ...)   # 记账
后：改内存队列                                   # 投影
```

如果先改内存再落日志，进程在两步之间崩溃，日志就丢了这次改动。先记账
的顺序保证**磁盘日志永远 ≥ 内存状态**。重启后 Inbox 构造时重放 spliced
事件（`_apply`，_splice 的"只改内存"版本），队列原地复活。

claim 的批次语义：先取空整个 next-step，再从 next-turn 取一条；
`discard=False` 表示认领不是丢弃，不触发 discarded 通知。

**队列投影和记忆投影并列，都住状态层**：`Inbox.queued_items()` 折 `_state`
（重放 spliced 的结果）产出 `QueuedItem(placement, message)`——
`placement='queued'`（next-turn）/ `'steering'`（next-step）。这和
`Session.derive_messages()` 是同一种东西：**日志 → 不可变投影的纯函数**，
只是对象一个是"还没浮上水面的待处理输入"，一个是"模型可见的记忆"。

分层要求：**折叠实现只有一份**（`_apply`/`_splice` 共用同一套 splice 语义），
上层宿主（web/cli）只做序列化。投影一度被写在 Web 宿主（当时是单文件 `web_app.py`）里自己重放
spliced——同一事件类型两份折叠必然分叉，而且投影绑死在 Web 宿主上
（CLI/测试拿不到）。

队列项的三个动作也归状态层（`Inbox.edit` / `promote` / `remove`，都走
`_splice` 先记账后改内存）：`edit` 原地换文案但**保住消息 id**（队列项身份
不变），`promote` 是 next-turn→next-step 的两步搬家（摘除那步 `discard=False`
——搬家不是丢弃），`remove` 带 `outcome='canceled'`。`Agent.update_queue`
把它们包成 DSH 同名的状态码（`ok` / `queue-item-not-found` /
`steer-unavailable` / `unknown-action`），web 层只做 HTTP 映射。

**提交身份（`rpc_id`）**：前端提交时铸 uuid 随请求上来，落进
`UserSource.rpc_id`（durable 消息的 source）——于是同一条消息在"队列项"和
"durable 消息"两个形态下带的是同一个身份，前端靠它把本地回显原子地换成
真身。它跟着消息走，不需要额外的旁路状态（`QueuedItem.rpc_id` 也只是从
`message.source` 读出来）。

### 3.5 被动状态机：wake / 补拉 / when_idle

agent 从不主动干活：谁要跟它说话谁就拍它一下（`_wake`）。

```
idle --wake(拍一下)--> running --跑空 inbox--> idle
```

- 忙时拍不醒：置 `_wake_requested` 标记，本轮回 idle 的瞬间补拉
  （`_kick` 的 finally 里检查标记 + has_pending，自己再拉起自己）
- `_kick`：`while await run_turn(self): pass`——run_turn 返回"队列还有
  没有货"，入口层和循环层靠这个布尔值接力
- `when_idle`：do/while 语义——等完一个 driver 若期间起了新活动继续等，
  保证"空闲"是稳定的收敛状态

### 3.6 turn/step 两级循环

两层循环不是随便嵌套，来自交互语义的分解：

```
turn 循环（外层，run_turn）     完成条件：inbox 没货 / blocked / 取消 / 出错
   每循环一次：claim 一批消息 → 开一个 step → 干完，再转一圈
step（_run_step）               **只发一次模型请求**（+ 它发起的工具调用）
                                返回 None = 有工具调用、回合还没收尾
```

> 外层循环回答"还有没有任务"，step 回答"这一次请求得到什么"。
> **工具循环本身由外层循环驱动**——这是 harness 的粒度
> （`core/agent-loop/src/agent.ts` 的 `step()` 发完一次请求就 `return`）。

**为什么 step 必须是一次请求**（2026-09 实测教训）：插队消息进 next-step 后
要等下一次 `claim` 才浮上水面，而 claim 就在外层循环每次循环的开头。若把
**整段工具循环**算作一个 step（旧实现：`_run_step` 内部反复模型↔工具），插队
就得等整段自主运行结束——长任务里是几分钟甚至永不。真实日志：一条插队消息在
next-step 里躺了 2 分 40 秒（期间 50+ 次工具调用、`step` 恒为 1），最后被
`cancel` 清掉（`outcome='canceled'`），模型从头到尾没见过它——用户看到的现象
就是"插入信息不起作用"。

关键点：
- 第 1 步认领 next-turn，后续步认领 next-step
- 认领到的消息在循环里落成 `user/message` surface 事件——从"水下"队列
  载荷变成模型记忆
- `end_reason is None` = 上一步发起了工具调用、回合还没收尾：即使这一步
  claim 为空也要继续发请求，把工具结果送回模型
- 工具结果**只落日志**，没有"把结果发给模型"的代码——下一个 step 组请求时
  下一次 `derive_messages()` 自动带上。循环不保存对话状态，只写日志，
  记忆自己浮现
- 日志顺序就是因果顺序：step/start → user/message → request/header →
  chunks → assistant/message → tool/call → tool/result → step/end
  （`step/start` 与 `request/header` 现在 1:1，除非 `request_error` 钩子重试）
- 日志里的 `tool/call` / `tool/result` 顺序**恒等于模型请求的顺序**，即使同一步
  里多个工具并行跑完的先后不同（按模型顺序提交，见 §3.15）
- max-tokens 有粘性：某步被截断后，后续步正常完成也不降级
- turn/end 五种结局：completed / blocked / aborted / error / max-tokens；
  aborted 和 error 记完账后必须重新抛
- config 三级 fallback：request 钩子 > agent.options > 上次 request/header
  （resume 恢复模型路由）

> 代价与收益：step 变细后日志事件更多（一次请求一组 step/start…step/end），
> 换来的是**插队延迟从一个工具循环降到一次模型往返**、停止更及时，以及将来
> guard（重复工具提醒 / 单次调用超时）有了天然的"每请求卡点"。

### 3.7 决策走钩子：循环是骨架，钩子是关节

三个决策点预留在循环里，策略由外部注入：

| 钩子 | 问题 | 典型决策 |
|---|---|---|
| `pre_step` | 这条消息该不该进模型？ | 安全审查拒绝、消息改写 |
| `request` | 这次请求怎么配置？ | 模型路由、动态 max_tokens |
| `request_error` | 模型报错怎么办？ | RATE_LIMIT 重试、401 放弃 |

钩子签名携带**默认实现**：钩子想放行就调 `default()`，想改写就自己返回，
想拒绝就返回 None。未挂载时循环走默认路径——**架构完整、插座已装**。
demo 运行时挂的第一个钩子是 `request_error`（上下文压缩的溢出恢复：
模型报"上下文过长"→ 压缩后重试）；`request` / `pre_step` 仍未挂业务实现
（只有测试验证语义）——插座已装，随功能演进插上。

钩子的改写也都落日志（pre_step 改过的消息落 user/message，request 结果
进 request/header）——不变式②"模型可见 ⟺ 可重建"没有破功。

### 3.8 失败降级为结果：工具失败是对话的一部分

"工具失败 → is_error 结果"是循环层和工具层共同维护的契约，四层兜底：

| 失败发生在哪 | 谁在兜底 |
|---|---|
| 坏 JSON（模型给了坏参数） | runtime/loop.py——连 execute 都不进 |
| 参数校验失败（缺 required / 多参数） | state/registry.py 抛 ValueError → runtime/loop.py 捕获降级 |
| 工具执行抛异常 | runtime/loop.py 捕获降级 |
| 工具卡死超时 | tools/ 包 wait_for 兜底（**同步阻塞的 executor 要先声明 `offload`，否则 wait_for 的定时器永远不会触发**，见 §3.15） |
| 用户取消（组执行中途） | runtime/loop.py 给已请求但没结果的调用补一条 is_error 合成结果——失败要降级成结果，取消也不例外（见 §3.15） |
| 进程被 kill / 断电（没有任何代码有机会跑） | 恢复时自愈：`state/recovery.py` 补 is_error 合成结果 + `session/repaired` 痕迹（见 §3.16） |

模型看到 `ValueError: missing required argument` 这类结果，自己知道怎么
改。**任何异常都不能越过 `_run_one` 炸掉循环**，唯一能打断的只有用户
的取消。原则：失败被局部化、显式化，变成模型可以理解和修复的输入。

### 3.9 取消单向传播：CancelledError 即 AbortSignal

Python 的 `asyncio.CancelledError` 扮演 harness AbortSignal：

```
cancel() → driver.cancel() → 异常沿 await 链穿过
_run_one → _run_group（补记账）→ _execute_tool_calls（补记账）
→ _run_step → run_turn（记 turn/end aborted 后 re-raise）→ _kick（吞掉，回 idle）
```

**每一层只记账，不拦截**——记账是义务，拦截是背叛。取消只往一个方向
传，没人半路吞掉。"记账"在工具层有具体含义：取消时不能留下**没有结果的
工具调用**（那会让后续请求的 wire 格式非法），见 §3.15。

### 3.10 能力层：双向翻译 + StreamChunk 契约

能力层（capability/llm.py）是内部词汇表和外部协议之间的**双向翻译器**：

```
出站: 内部 Message/Block ──build_payload/_to_wire_messages──▶ wire JSON
入站: SSE 原始帧 ──解析 + 封装──▶ StreamChunk 值对象
```

- 真模型和 FakeLlm **没有共同基类**，契约是鸭子类型：
  `stream(request) -> AsyncIterator[StreamChunk]`。循环层只认 StreamChunk，
  不知道 httpx/SSE 的存在——换模型实现不动循环层一行
- 流式输出的三层分工：能力层**产生**流（SSE 帧 → StreamChunk），循环层
  **消费**流（落日志 + 喂组装器），UI **展示**流（订阅 assistant/chunk
  事件渲染）。UI 显示的字不是从模型回调来的，是从日志来的——UI 是日志
  的投影
- 思维链（`reasoning_content` / `reasoning` / `thinking`）在能力层统一映射为
  `StreamChunk.reasoning`；循环层把它作为**非 surface 痕迹**落
  `assistant/reasoning/chunk` 和 `assistant/reasoning`，只用于展示与调试，
  **不会**进入 `assistant/message` / `derive_messages()`，因此不会回灌给模型
- wire 翻译的讲究：工具结果在内部是 user 角色消息，wire 层变成
  `role: 'tool'`；带工具调用的助手消息 content 必须是 null；tools 要
  function 包装格式
- `_tool_call_deltas` 处理工具调用的碎片重组：id 只出现在第一帧、
  arguments 分散在多帧，靠 `index_by_id` 字典补全

### 3.11 JSONL 编解码：tagged dict 方案

`values/messages.py` 负责"单个事件 ↔ JSON"的纯转换（**没有文件 I/O**），
`values/persistence.py` 才是碰文件的通道。分工：Session 管状态、persistence
管 I/O、values 管类型转换。三者分开的理由：纯函数 vs 副作用分离、
存储后端可替换（harness 里是抽象）、单向依赖。

JSON 没有类型信息，用 `$xxx` 前缀 key 做类型标记：`$text`/`$tool-call`/
`$tool-result`/`$message`/`$dict`/`$list`，`data_to_json` 递归遍历任意
嵌套，to/from 严格对称——任何值经过 JSON 往返必能还原（测试断言）。

### 3.12 严格校验哲学：错误在最早的时刻显式暴露

全项目贯穿同一条原则，四例：

1. `surface_op` 在 append 写入时校验，而不是 derive 时猜
2. prompt 变量未注册/无值在组装时抛错，而不是把 `{{modl}}` 字面量
   静默发给模型（提示词 bug 昂贵且难查）
3. 工具注册重复名字抛错，注册即返回注销函数（对称操作）
4. Inbox 重复消息 id 拒绝入队（跨队列防错）

> 宁炸勿静默：错误在发生现场立刻爆炸，好过在远处变成难查的偏差。

### 3.13 其他值得记住的细节

- **工具注册的两份用途**：ToolSpec 把"给模型看的 schema"和"给自己跑的
  executor"绑在一个对象里，漂移在结构上不可能——模型看到的和系统执行
  的是同一个东西的两面
- **出站辅助请求也要记账**：`web_search` 会向 DeepSeek 的搜索端点发一条
  **独立**的模型请求（不属于会话上下文）。派发前先落一条痕迹事件
  `web/search`（query / endpoint / model / max_uses），**绝不含 key**——
  对齐 DSH 的 `web/deepseek-search-llm-request`（"模型可见的辅助输入不能
  逃出日志"）。配置是一个 `SearchConfig` 值对象、由工具层解析一次后
  **trace 与真实请求共用**：否则日志可能记一个端点、请求打到另一个
  （而且 `register(endpoint=...)` 这类覆盖会静默失效）
- **每次请求都带全部工具 schema**：模型在请求间无状态，工具描述是每轮
  的固定 token 成本——进化时加工具要算 token 账
- **提示词每回合快照一次**：`assemble` 在 turn 开头求值，整个 turn 内
  system 恒定，可预期、可调试
- **提示词分三层，别互串**：通用规则（`identity`/`persona`/`discipline`）、
  工具专属规则（`tool:*`）、技能目录（`skill:*`）。通用规则的作用域是"任何工具、
  任何任务"——换工具仍然成立；工具规则换个工具就该失效。混起来两种坏结果：通用段
  塞工具细节 = 每轮都付的噪声；工具坑写进通用段 = 失去作用域、换个工具还挂着。
  维护动作是单向的：**反复被踩的坑从文档上移到通用段**（文档只有愿意读的 agent
  才看得到，system 才是每轮都生效的通道）
- **prompt sections 用 order 数值排序**：主序按 order 升序、平局按名字；
  app/factory.py 用 -100/0/110 间隔留插队空间。排序本身是架构性的（插件插队），
  长提示词下首因/近因效应才变成真实的调优手段
- **usage 进日志 → 成本是日志的投影**：`assistant/message` 落 usage 后，
  会话累计消耗 token / 缓存命中率只需扫日志求和（`app/compaction.py` 的
  `session_token_totals`），运行时不需要第二份记账状态——"记忆机制的直接受益"
- **模型是否每次看到全部工具**：是。每次请求全量携带 tools 清单

### 3.14 Web 前端也是投影：nodes 数组是浏览器里的"唯一事实源"

后端"日志是唯一事实源、UI 是投影"的哲学一直延伸到浏览器 DOM。Web 前端
**不再把 DOM 当状态**，只维护一份从日志重建的投影状态：

- **投影** = `nodes` 有序数组（`web/index.html`），每类节点是不可变快照
  数据：`user`（真人发言）/ `assistant`（一个应答 + 工具活动）/ `checkpoint`
  （压缩摘要卡）。渲染器只读它画 DOM。
- **两条路径同构**：实时 SSE 帧（chunk/reasoning/tool_call 带 `turn`/`step`
  结构标记，`tool_result` 按 call_id 配对）与 `/history` 全量载荷喂进同一
  构建逻辑，产出同一形状的节点——刷新/切会话 = 从 /history 重建投影，
  实时流 = 同一投影尾部增量生长，**不存在两条独立的渲染路径**。
- **回合分隔线从数据推导**：user 节点带 `turn`，出现新 turn 才画线；同
  turn 的后续 user = steer 插队，不画新线（前端不自数 turnNo）。
- **partialRender 降级为渲染优化**：段落固化（`.pseg`）只服务"最后一个
  assistant 文本块如何平滑更新"，`_raw/_seg` 状态只活在 DOM 上，不再是
  会话事实的一部分——任何时刻全量重画都一致。
- 后端配合：`event_to_payload` 透传 turn/step、SSE 补 `turn_start` /
  `user_message`（带 turn + message_id + 提交身份 rpc_id）/ `queue_update` 帧；
  设计注记见 `web/PROJECTION_DESIGN.md`。
- **待处理消息按 placement 分区，恒定贴尾**（都不进 `nodes` 投影）：
  `queued`（next-turn）画在输入框上方的 `#queue-dock`；`steering`（next-step）
  画在消息流尾部 `#messages > .flow-tail` 的 pending 气泡。未 claim 的消息没有
  seq 位置，**往流中间插只能靠 `(turn,step)` 锚点猜顺序（上一版的位置 bug 就
  是这么来的）**；贴尾 + claim 后 durable 节点落到真实 seq 位置，位置语义天然
  正确。对齐 DSH（`agent.md` §5 有源码对照与勘误：DSH 的 steering **也是**
  画在流尾，只有 queued 进 QueueDock）。
- **本地提交回显**（`pendingSubmissions`，对齐 DSH `PendingSubmission`）：提交
  当帧就在尾部画出来，durable 内容出现（带同一个提交身份 `rpc_id`）时在同一次
  渲染里被隐藏、随后退休——所以"发出去没反应"和"重复画两条"都不会发生。
  回显只活在客户端内存：刷新/重连只从 durable 事件重建。
- todo dock / context 面板 / 队列区 / **尾部待处理气泡** / approval 卡片
  **不进**本投影（独立订阅、即时 UI）。

### 3.15 工具并发调度：声明、连续段、有序提交、取消补记账

执行策略全部来自注册声明（不变式 4），循环层只读不猜。五条规则与 harness
`core/agent-loop/src/tool-calls.ts` 一一对应：

| 规则 | 我们 | harness |
|---|---|---|
| **并发许可 fail-closed** | `ToolSpec.execution_mode` 默认 `'sequential'`，只有显式声明才并行；非法值注册时抛错 | `executionMode()`：未声明 / 未注册 / 判定抛异常一律 `exclusive`，只有精确 `true` 才算 `parallel`（`isConcurrencySafe` 还是**看参数的谓词**，我们暂用静态声明） |
| **分组按连续段** | `_run_group.fill()` 里的屏障（并行段撞上独占调用就停手），回传 `consumed` | `fillPool()` 里 `nextToStart > 0 && mode==='parallel' && executionMode(next)!=='parallel'` → `break`，`runGroup` 返回 `{consumed: started}` |
| **并发池上限** | `MAX_PARALLEL_TOOL_CALLS = 4` | `agentLoop.config.maxParallelToolCalls` |
| **结果按模型顺序落盘** | `slots` + `commit_ready()`：队首连续就绪才 `append` | `commitReady()`：`committed` 只跨连续槽位推进 |
| **取消补合成结果** | `_run_group` 的 `except CancelledError` + `_record_aborted_calls`（补 `tool/call` + is_error 的 `tool/result`） | `appendSkippedToolCall()` |

**未注册的工具名也走 fail-closed**：`ToolRegistry.mode()` 对不存在的工具返回
`'sequential'` 而不是抛 KeyError。分组发生在**执行之前**——在这里抛错的后果不是
"报个错"，而是 `_run_one` 里那段专门把"工具未注册"降级成 is_error 结果的兜底
永远没机会执行：整回合没有 `turn/end`，日志里只留下"请求了工具却没有结果"的
assistant 消息（wire 非法），模型什么都看不到（实测旧行为：`driver error: tool
'no_such_tool' is not registered`，`turn/end` 缺失）。对齐 harness：`executionMode()`
对未注册工具同样返回 `exclusive`，调用照旧派发，失败在派发阶段变成结果。

为什么"按模型顺序落盘"重要：并发只该改变**谁先跑完**，不该改变**日志顺序**。
顺序一乱，同一段对话每次重放出的前缀就不同——前缀缓存命中率下降、测试不稳定、
前端除了按 call_id 配对还得处理"结果早于调用"的畸形序列。

为什么取消必须补记：取消是用户随时可做的操作。若取消落在工具组执行中途，
模型请求过的调用会没有 `tool/result`，`derive_messages` 里就留下"带 tool_calls
却没有结果"的 assistant 消息——wire 格式要求每个 tool_call 都有对应的工具消息，
**下一轮请求直接 400**。注意 `tool/skipped` 只是审计痕迹（非 surface 事件），
进不了模型记忆，所以它不算记账。harness 先 drain 已派发的调用（拿到真结果）
再给剩下的补合成结果；**我们同样先 drain**：取消前已经跑完的调用认它的真结果，
拿不到结果的才补合成结果，文案按是否已派发分成 `aborted before dispatch` /
`aborted while running`。drain 那一步挡住二次取消（连点两次停止）——它不能让
后面的补记账被跳过，否则又回到"有调用没结果"。
（旧实现两边都没有：取消时两条 `tool/call` 已落盘、`tool/result` 一条没有。）
实测暴露面：真实会话 69 个多调用批次里有 7 个是 `[parallel…, sequential…]`
形状，旧实现会把它们整批塞进同一个并发池。

**两根正交的轴**：`execution_mode` 回答"能不能与其他调用并发"，`offload`
回答"会不会阻塞事件循环"。二者独立，按工具的真实性质推导：

| 工具 | 并发 | 卸载 | 依据 |
|---|---|---|---|
| read_file / list_files / grep / glob | ✅ | ✅ | 只读 + 同步盘 I/O |
| web_search | ✅ | ✗ | 只读 + 真异步（httpx 本身有挂起点） |
| todo_write | ✗ | ✗ | 写日志投影（共享状态），且无 I/O |
| bash / edit / write_file | ✗ | ✗ | 要人工把关；bash 本就异步（子进程要绑在运行中的循环上） |

不卸载的同步 executor 会让 `asyncio.wait_for` 的超时**永远不触发**：协程体
没有挂起点，会一口气跑完才把控制权还给事件循环，定时器回调根本没机会执行——
超时保护形同虚设，同进程的 SSE / 审批也被卡住。这一轴是 Python 侧的本地问题
（harness 在 Node 上，`fs/promises` 天然不阻塞），所以是我们加的，不是抄的。
卸载的边界同样是硬约束：**只给纯 I/O**——换了线程，`session`/`agent` 的内存
状态就没人在循环线程上守了（不变式 3 的"入队即记账"依赖单线程串行 append）。

### 3.16 会话自愈：崩溃留下的"悬空工具调用"

工具执行期间落两类事件：`tool/call`（痕迹，起跑前）和 `tool/result`（surface，
跑完后）。取消路径两者都补记账（§3.15），但**进程被 kill / 断电 / OOM / 宿主机崩**
时没有任何 Python 代码有机会运行——日志就停在这两个时刻之间。

**后果不是"少一条结果"**：`derive_messages` 会把那条"请求了工具"的 assistant
消息折进模型记忆，而 `_to_wire_messages` 是纯翻译、不修复，于是 wire 上成了
"assistant 带 `tool_calls`、却没有对应的 tool 消息"。实测（真发一次）：

```
HTTP 400 An assistant message with 'tool_calls' must be followed by tool messages
         responding to each 'tool_call_id'. (insufficient tool messages following tool_calls message)
```

而且**不自愈**：那条 assistant 消息永久留在日志里，之后每次发送都是同一个 400
（实测连发两次同样失败，且每次失败还往日志里再插一条 user 消息）。用户只剩两条
出路——新建会话（丢掉上下文）或手改 JSONL。UI 上还有一个更早的征兆：那条工具行
会**永远转圈**（前端建卡片时一律 `state:'running'`，只有拿到 `tool_result` 才翻）。

**修法**（`state/recovery.py`，恢复入口调用）：重放之后扫一遍投影，给缺结果的调用补一条
`is_error` 合成结果，并**先落一条 `session/repaired` 痕迹**说明这次自愈。两个刻意的
选择：

- **只在恢复时修，不在请求构造时补占位消息**。后者能让 400 消失，但会把"这里断过"
  从日志里抹掉——而日志是唯一事实源。合成结果的文案本身就说清它的来历
  （`no result was recorded — the session was interrupted …`），修复痕迹里记着
  `call_ids`，读日志的人一眼能看出哪几条结果是合成的。
- **修完即持久**（走 `session.append`，在 `bind_store` 之后调用）：从磁盘重放一次
  依然一致，不会每次打开都"修一遍内存"。

判据只看**投影**（`derive_messages`），不看痕迹事件：只有进得了模型记忆的调用才会
让 wire 非法；被 compaction 遮蔽掉的老调用不该被算进来。函数幂等（补完再扫就什么都不
缺），所以两个恢复入口（`web/sessions.open_session_seat`、`cli._prepare`）都可以无条件调一次。

实测修复效果：同一条崩溃日志，修完再发真请求 → `turn/end{reason: 'completed'}`，
模型正确复述"收到过一条失败的工具结果，提示可重新发起"，没有幻觉成"我读过那个文件"。
（反向的"孤儿结果"——有结果没有对应调用——当前没有任何路径能造出来：结果总是跟在
调用之后落盘，compaction 也按整段遮蔽，所以不修。）

### 3.17 工作区项目指令文件：探测 + live 注入

日志负责"这次会话发生过什么"，**项目指令文件**（`AGENTS.md` / `CLAUDE.md`）负责
"这个项目一直是怎么做的"：跨会话、跨工具、可进版本库。两者职责不同，不能互相替代
——会话结束后日志里那些发现（怎么跑测试、依赖方向、用户偏好）不会自动变成下个会话
的约定。

**注入通道的决定**：正文直接进 **system 的 live 段**（`app/instructions.py` + `app/factory.py`
的 order 20），而不是像 todo 那样作为 messages 末尾的合成消息。判据是"变化的频率"：

| 内容 | 变化频率 | 通道 | 为什么 |
|---|---|---|---|
| 通用纪律（discipline） | 永不变 | 静态 system 段 | 规则该在最稳的前缀里 |
| **项目指令文件正文** | 会话内几乎不变（除非 agent 自己改它） | **live system 段**（order 20） | 属于"每轮都该生效"的规则；字节稳定 → 不打碎缓存前缀 |
| 技能目录 | 会话内可变（新增/改写技能） | **live system 段**（order 95，stat 键控缓存） | 只放 name/description（**不列路径**） |
| 运行时状态（todo 状态栏等） | **每步都可能变** | messages 末尾的合成消息（注册制贡献者，见 §3.21） | 放 system 会把缓存前缀每请求打碎一次 |

live 段的语义（`prompt.render` 每次模型请求求值一次）正好覆盖 issue #6 的关键路径：
**agent 刚创建的 AGENTS.md，在同一回合的下一次请求里就能看到**——不需要等下一个回合，
也不需要 DSH 那套 baseline/delta 变更账（它注入一次，所以必须记增量；我们每请求重算，
重算替代版本账）。

**两级新鲜度**：注入的内容和子目录清单"保质期"不同，判据是代价而不是"越新越好"。

| 注入物 | 刷新时机 | 一次刷新的代价 | 为什么是这个粒度 |
|---|---|---|---|
| 根目录正文 | **每请求**（`(mtime_ns, size)` 键控缓存） | 2 次 `stat`（微秒级） | 便宜；而且"模型刚写下的 AGENTS.md"必须立刻生效——这正是 issue #6 的关键路径 |
| 子目录清单 | **每回合**（回合号当刷新纪元，`render(turn=…)`） | 一次 `os.walk` 全树遍历（本仓库实测 ~600 µs） | 贵三个数量级；"本回合新建的子目录约定下一回合可见"够用 |

**探测（确定性，不靠模型自觉）**：`InstructionLoader` 每次渲染读根目录候选文件
（`AGENTS.md`、`CLAUDE.md`，按候选顺序）并按 `(mtime_ns, size)` 缓存；子目录清单每回合
重扫一次（`os.walk` 原地剪枝隐藏目录/缓存，`dirnames` 排序后再走——不排的话提前
`break` 收前 N 条会因文件系统顺序不同而给出不同子集，段字节就不可复现）；候选路径先
`resolve()` 再判是否仍在工作区内——**指向工作区外的符号链接不注入**，按"读不到"报出来
（工具层已经用 `resolve_in_workspace` 拦住同一条路，指令读取是宿主的另一条路径，不拦就
等于开了一个把工作区外文件送进 system prompt 的口子）。

- 有文件 → `<workspace_instructions files="AGENTS.md">` + `Instructions from: <路径>`
  + 正文（单文件 8k / 整段 20k 字符预算，超预算截断并提示"用 read_file 读剩下的"；
  超过 1 MiB 的文件不读进内存，只留指引）；
- 没有文件 → 同一标签给 `files="none"` + "建议在掌握稳定项目知识后创建一份"，并写清
  "写入需要用户批准、不许写密钥/临时状态/未验证猜测"。这段提示是 issue #6 方案 C 的
  落地：**触发是确定的**（不依赖模型某轮想起这件事），内容则由 `discipline` 段的通用
  规则兜底（`Instructions:` 那条，每轮都生效）。

**探测是三态，不是两态**（对齐 DSH `ScopeInstructionProbe` 与 opencode
`SystemContext.unavailable`——后者的注释原话是"distinguishes confirmed absence from
provider failure"）：

| 态 | 判据 | 渲染 |
|---|---|---|
| **确认存在** | `lstat` 成功、目标是普通文件，读到了 | 注入正文（含"太大未内联"：算存在） |
| **确认不存在** | `lstat` 抛 `FileNotFoundError`（**条目本身**不在） | `files="none"` + 创建指引 |
| **读不到** | 权限拒绝、IO 错误、**同名目录**、**断链符号链接**、**读的时候文件在动**、**符号链接指向工作区外** | `files="unreadable"` + "内容未知、不要当成没有约定、不要提议创建" |

**为什么必须分开**：只有"确认不存在"才允许说"这个工作区没有项目指令文件"、才允许建议
创建；"读不到"时提议创建可能覆盖一份已存在（只是读不到）的文件，而且模型是在假前提上
行动。把"读不到"渲染成 `files="none"` 等于同时对用户和模型说同一句假话——这是不变式⑤
（失败降级为结果）与"宁炸勿静默"在**探测**上的对应物：**不要把"不知道"降级成"没有"**。
（真实案例：PI 的 CHANGELOG 记过一个叫 `AGENTS.md` 的**目录**导致 EISDIR，后来专门加了
`statSync(...).isFile()` 判断——正是"存在但不是文件"这一态。）

**三态落地的两个细节**（都是"读盘的顺序"问题，复盘见 issue #17）：

- **`lstat` 再 `stat`**：`stat()` 跟随符号链接，于是 `AGENTS.md -> 不存在的目标` 抛的
  `FileNotFoundError` 与"目录里没这个文件"长得一模一样——照旧写法会把断链当成"确认不
  存在"，模型据此提议创建一份其实**已经存在（只是目标没了）**的约定。先 `lstat()` 确认
  **条目**在不在，再 `stat()` 拿目标状态，两者分开才是三态。
- **读完再取一次缓存键**：键是读之前取的 `(mtime_ns, size)`。若读的过程中文件在变
  （编辑器保存 / agent 写文件 / git checkout），可能读到半截，而半截会被当成"这个键对应
  的内容"**缓存住并持续服务**——模型看到缺条目的约定还不自知。所以读完再取一次键，
  不一致就不写缓存、按"读不到"报出来，下一个请求自然重读（宁可这一轮说"内容未知"，
  也不给模型一份被削过的正文）。这条同时封住了"键精度粗的文件系统上等长改写"那条
  最坏组合。

**边界（与 DSH 的三处裁剪，理由见 `agent.md` §9）**：只在本工作区内发现（工具被沙箱
限制在 workspace 内，向上发现的文件模型读不到）；不做 user-global / `.local` 层级；
不做 baseline/delta 版本账。

**实测（真模型，两个方向都验过）**：全新工作区跑首轮任务后，模型主动问"要不要我把这条
测试命令写进 AGENTS.md？"且**没有静默创建**（文件确实不存在）；已有 AGENTS.md 的工作区
里，模型用的正是文件规定的命令（`python -m pytest -q test_calc.py -k add`，只按全局
`tool:bash` 提示套了 `conda run` 外壳），并在回答里说明"按 AGENTS.md 的约定只跑了
`-k add`"。
### 3.18 技能的两个来源：自带能力跟着 agent 走

技能正文不再是"只从工作区读"，而是**两个来源按名字合并**（对齐 DSH 的
project / user / bundled 与同名覆盖）：

| 来源 | 位置 | 谁维护 | 例子 |
|---|---|---|---|
| **bundled** | 包内 `agent_demo/bundled_skills/*.md`（`pyproject` package-data 保证随包发布） | agent 作者 | `project-instructions`（怎么写 AGENTS.md，任何工作区都适用） |
| **workspace** | `<workspace>/skills/*.md` | 项目 / 用户 | `gh-issue`（只对本仓库有意义的工作流） |

同名时 **workspace 覆盖 bundled**；合并后按名字排序——目录段进 system 的缓存稳定
前缀，字节必须可复现。

**为什么必须有 bundled 这一层**（issue #22 实测）：早期只扫工作区，于是"用户在自己
的项目里跑这个 agent"时，system 里没有「可用技能」段、`read_file` 读包内技能被沙箱
拒（`path outside workspace`）——**换个工作区自带能力归零**。根因是两种参考实现各取
了一半：PI 式"模型用 read 按路径取正文"要求技能文件在**模型读得到的地方**（PI 没有
workspace 沙箱）；DSH 式沙箱承诺又把可读范围锁在工作区内。两者组合，bundled 就够不着。

**取正文：`skill(name)` 工具，不是路径**。host 把名字解析到文件（自带技能是 host 自己
的资源），所以：

- **沙箱承诺一个字不用改**：工具入参只有名字，模型没有机会拼出路径；查不到就是一条
  `is_error` 结果（失败降级为结果，不变式⑤），不需要给沙箱开任何例外；
- **正文仍然作为 tool/result 进日志**：可重建、可被 compaction 折叠、前端画成工具卡
  ——与"读任何文件"机制一致，换的只是"怎么找到文件"；
- **目录与工具共用同一个 `SkillTable` 实例**（`factory` 构造一次、传两处），且工具在
  **执行时**才取表，"目录里有的"和"工具能取到的"永不漂移；目录段**不再列路径**（列了
  只会诱导 `read_file`，而 bundled 读了会被拒）。
- **目录段是 live 段，按请求新鲜**：表由 `SkillTable` 持有，每次求值只算一遍**内容
  指纹**（两个来源目录的 `*.md` 名单 + 每文件 `(mtime_ns, size)`，实测 ~70 µs），指纹
  变了才重扫重解析（~300 µs）。于是**会话中途新增/改写/删除技能，下一次模型请求就
  生效**——这正是早期"build 时算一次"版本的毛病：新技能要等新会话，更糟的是改写技能
  时**目录念旧描述、`skill` 工具给新正文**（同一份表在 system 与工具里说法不一）。
  文件没变时目录字节不变，所以缓存前缀照样命中。
- **刷新加锁 + 双检**：这张表被**两个线程**碰——live 段在循环线程求值，`skill` 工具
  声明了 `offload=True`、在工作线程执行。无锁时两次刷新可能交错成"指纹是新的、表是
  旧的"（一个线程写表之后、写指纹之前被抢占，另一个线程的整对赋值插进来），之后每次
  求值都以为"没变"，那份技能的描述就**永远**停在旧值。快路径（指纹没变）不碰锁，
  所以循环线程不会被工作线程的重扫阻塞。
- **指纹的匹配规则必须与 `scan_skills` 的 `glob('*.md')` 一致**，所以用
  `fnmatch.fnmatch`（glob 内部就是这套 `os.path.normcase` 语义）而不是
  `name.endswith('.md')`：Windows 上 glob **不区分大小写**，`Upper.MD` 会被扫成技能
  ——指纹漏掉它，"改了看不见"这个病就从另一条路回来了（实测踩过，测试里有覆盖断言）。
- **工作区来源必须做越界检查**（issue #25）：技能文件由宿主直接读、不走工具沙箱，
  所以 `skills/x.md -> 工作区外的 md` 与指令文件那条是**同一个洞**——放行的话，一份
  项目里的符号链接就能把工作区外的文件读给模型。判据与措辞两处共用
  （`sandbox.workspace_escape_reason`，一条规则一处实现）；**bundled 来源豁免**（包内
  技能本来就在工作区外，那正是 `skill` 工具存在的理由）；越界的那个**在读文件之前**
  就被跳过（否则等于"先泄后拦"）并打 stderr 诊断，于是目录与工具两边同时看不到它。
  少给边界（workspace 来源却忘了传）直接抛错：宁炸勿静默。

### 3.19 宿主直读文件的两条边界：越界 + 三态

`app/instructions.py`（指令文件）与 `app/skills.py`（工作区来源的技能）是**宿主自己发现并读取**
工作区文件的两条路——它们不经过工具沙箱（模型没参与，也没给路径），所以**边界要自己
守**。两条规则相同，实现也共用：

| 规则 | 判据 | 违反时的行为 |
|---|---|---|
| **越界不读** | `resolve()` 后仍在 `resolve()` 过的 workspace 内（`sandbox.workspace_escape_reason`） | 指令文件按"读不到"报出来；技能跳过 + stderr 诊断 |
| **不确定 ≠ 不存在** | 只有"条目本身不在"才算确认不存在；权限/IO/断链/同名目录/读取期间被改都是"读不到" | 按第三态渲染（`files="unreadable"`）或跳过，**绝不**降级成"没有" |

为什么值得单列一条：这两条路读的是**用户仓库里的内容**，而读出来的东西一条进
**system prompt**（指令文件正文）、一条进**对话**（技能正文）——都是"工作区里的文本
直接变成模型的输入"。persona 明写"工作区外不可读"，工具层也已经用
`sandbox.resolve_in_workspace` 拦住了模型给路径那条路；宿主直读这条路不自己拦，就等于
在后门留了同一个洞。（与 `app/sandbox.py` 同级：hardlink / TOCTOU 不设防，这是"防误用
保险"，不是 OS 级沙箱。）

### 3.20 每对话一个工作区：不可变、进日志、按 seat 隔离

`--workspace` 过去是**进程级唯一边界**。现在 Web 宿主允许**每个对话各自指定**工作区
（对齐 DSH 的 `SessionHeader.cwd`；三家对照与取舍见 `agent.md` §10）——这是"一个 Web
进程里同时开多个项目的对话"的前提。三条规则：

| 规则 | 落地 | 为什么 |
|---|---|---|
| **创建时定下，之后不可变** | `POST /sessions/new {"workspace": …}`；已有会话再给 workspace → 400 `workspace is fixed` | 半个对话换了沙箱根，前几轮读 A、后几轮写 B，语义上说不清楚。换目录 = 新建对话（DSH 同样靠 cwd 不可变绕开了"切换后重建什么"） |
| **写进日志（痕迹事件）** | `session/workspace` + `Session.workspace()` 从日志倒读 | 与 `session/title` 同构：**配置事实也从日志读回来**（"日志是唯一事实源"），于是换进程、隔几天再打开还回到同一个目录。它是 trace：不进 `derive_messages`（不变式 ②） |
| **每个 seat 一份 args** | `Seat.args`（复制宿主参数，只换 `workspace`） | 工具沙箱、指令探测、技能表、`{{workspace}}` 叙事全是 `build_agent` 当场从 `args.workspace` 派生的——所以"每会话一套"就是"每会话一份 args"，不需要给 Seat 挂派生对象 |

**旧会话与三态**：本功能之前建的会话日志里没有这条事件 → 跟随宿主默认工作区，
且**不回头改写它的日志**；日志里记着的工作区**不存在了** → 409 + 明确原因
（`this session's workspace is gone`），**绝不静默回退到宿主默认**——静默回退会让工具指向
另一个项目，而模型以为自己还在原来的目录里（这正是 DSH `session/conflict` 要防的事）。
两处细节（都来自 review）：**记录是空白**的按"没记录过"处理（空白不是路径，放它进
`resolve()` 会折叠成进程 cwd，那就是一次静默换根）；列表项带 `workspace_ok`（目录还在不在），
目录已失效的会话在界面上提前标出来，而**已经开着的 seat 不会因为目录被删就被打断**——
只有重新打开时才校验（运行中的对话不该被磁盘变动杀死）。

**新会话 id 用原子占坑**：`web-<秒级时间戳>` 撞车就顺延 `-2`/`-3`…，判据同时看磁盘文件与
内存里的 seat；真正防并发的是建立空文件时的 `exist_ok=False`（多 worker 各自独立内存、
只共享磁盘，两个进程同时判定"这个 id 还没人用"时只有一个能建成，另一个退回"已有会话"语义
——绝不拿自己的 workspace 去覆盖别人的根）。

**选择策略 = 信任界面使用者**（对齐 DSH：它也没有 allowlist）：只校验"存在 + 是目录"，
判据集中在 `app/workspace.py` 的 `resolve_workspace`。Web 默认只绑 `127.0.0.1`，
"能点这个界面的人"本来就等于"把该目录的读写交给 agent"；想收紧就在这一处加白名单，
调用方不用改。相对路径按**进程当前目录**解析（shell 直觉），空输入 = 宿主默认。

### 3.21 运行时状态：注册制贡献者（issue #19）

模型每轮请求看到的，除了日志推导出的历史（`derive_messages`），还有**当下才算得出来的
状态**——todo 清单是最典型的一个。这条通道原先硬编码在循环里
（`runtime/loop.py` 直接 `from ..tools.todo import build_todo_status`），是"框架层反向依赖
应用层"的典型：加一个来源就要改循环，审计字段也只能一个来源一个平铺字段。现在改成注册制：

| 角色 | 位置 | 职责 |
|---|---|---|
| 贡献者类型 | `state/runtime_status.py` | `RuntimeStatusRegistry`：`register(name, build)` —— `build(session) -> str \| None`（**无内容返回 None**）；空名/重名注册时刻抛错；返回注销函数 |
| 收集与贴尾 | `runtime/loop.py` | 每请求 `agent.runtime_status.collect(session)` → 非空者**各贴一条合成 user 消息**到 messages 末尾 → `request/header.runtime_status = {名字: 原文}` |
| 注册 | `app/factory.py` | `_runtime_status()` 里一行：`status.register('todo', build_todo_status)` |

三条设计约束：

1. **注册点必须在应用层**：`loop` 只认"贡献者"这个概念，所以 `runtime/` 不再 import 任何
   `tools.*`——`tests/test_architecture.py` 的白名单因此**清空**（那条"例外必须仍然真实存在"
   的断言在改造完成的瞬间会主动报红，逼你同步删条目）。
2. **不进 system、不进日志**：状态每步都变，写进日志会污染对话、放进 system 会打碎缓存
   前缀；所以它是"messages 末尾的合成消息"（模型可见）+ "`request/header` 里的原文"（可审计），
   **不是事件**。可重建性靠"贡献者必须是日志投影的纯函数"这一条纪律保证。
3. **多来源是一个映射，不是一堆平铺字段**：`runtime_status: {名字: 原文}`——加来源不必改
   审计形态，"这一轮模型被告知了哪些运行时状态"永远能用同一句话回答；**全部为空时这个字段
   整体缺席**（不是空映射，与改造前的 `todo_status` 一致）。

三条失败判据（容易搞反，单列）：

| 时刻 | 情形 | 行为 |
|---|---|---|
| 注册 | 空名 / 重名 / `build` 不可调用 | **当场抛**（宿主写错了代码，装配时就该响） |
| 求值 | 贡献者抛异常、或返回非 `str` | 记 ERROR（带名字）并跳过它——这一轮就是"没有这份状态"，审计只列真的被告知模型的项 |
| 求值 | 贡献者在求值期间注册/注销自己 | 迭代的是**快照**，本轮不受影响、**下一请求生效**（直接迭代 `items()` 会让迭代器抛 `RuntimeError` 并逃出兜底） |

这条通道是可选的状态展示，坏了不该让整个回合作废；对照：工具失败必须降级成 `is_error`
结果，因为那是"模型输入可能不合法"的通道。`except Exception` **不捕 `BaseException`**——
`CancelledError` / `KeyboardInterrupt` 照常穿透，"取消单向传播"不被这条兜底破坏（有测试钉住）。

与 DSH/opencode 的对照见 `agent.md` §4（DSH 的 runtime-context contributor 把动态上下文渲染成
user 角色以保住 system 前缀缓存；opencode 的 SystemContext 强调"不可用"的第三态——我们用
`None` 表达"这一轮没有"）。

---

## 4. 一条消息的完整生命周期

```
你发消息
 → Agent.followup → create_user_message（值层：不可变 Message）
 → send → inbox.append → _splice：
      ① 落 agent/inbox/spliced 日志（消息第一次进日志，水下）
      ② 进内存队列 next-turn
 → _wake 拍醒状态机（idle → running，拉起 driver）
 → run_turn：turn/start → claim 认领（spliced 记删除账）→ pre_step 钩子
 → 落 user/message surface 事件（浮上水面，变成记忆）
 → _run_step 内层循环：
      组请求（system 快照 + derive_messages 折叠的记忆 + 全部工具 schema）
      → request/header 落日志
      → 流式：有内容的 chunk 落 assistant/chunk，喂组装器；有思维链时另落
        assistant/reasoning/chunk，结束时落完整 assistant/reasoning（痕迹数据）
      → assistant/message 落日志（surface）
      → 有工具调用？按模式分组执行，结果落 tool/result（surface）
      → 回到 while 顶部：derive_messages 自动带上结果，再调模型
      → 纯文本 → 结束
 → step/end → turn/end{reason}
 → 跑空 inbox → 回 idle
 → 进程退出：.sessions/<id>.jsonl 是留下的唯一东西
 → 下次 --resume：load_events → adopt 重放 → 队列/记忆/回合号/模型路由
   全部还原——恢复 = 重放，零额外代码
```

---

## 5. 文件职责清单

包 `agent_demo/`（框架四层 + 应用内容）：

| 文件 | 角色 |
|---|---|
| `values/messages.py` | 值层：不可变 Message/SessionEvent/**ToolOutcome** + tagged dict 编解码 |
| `values/limits.py` | 跨层共享的常量（判据：有更低层要用就下沉到值层——`TOOL_RESULT_MAX_CHARS` 从应用层搬来，修掉 `state → app` 那条反向依赖） |
| `state/session.py` | 日志 + surface 折叠投影（append / derive_messages / adopt / request_header） |
| `state/inbox.py` | 双队列（next-turn / next-step）+ claim 语义 + 持久化重放 + `queued_items()` 队列投影 |
| `state/prompt.py` | sections 按 order 拼接 + `{{var}}` 严格插值 |
| `state/registry.py` | 工具类型（ToolSpec：schema + executor + 并发模式 + 卸载声明 + 超时 + requires_approval；返回值 `ToolOutcome` 在 `values/messages.py`） |
| `capability/llm.py` | 能力层：SSE 流式客户端 + FakeLlm + wire 双向翻译（含思维链字段解析） |
| `capability/hooks.py` | 三个决策钩子的类型 |
| `runtime/loop.py` | turn/step 两级循环 + 流组装 + 工具分组执行（自限池/有序提交/取消补记）+ 思维链痕迹落盘 + 四层兜底 |
| `runtime/agent.py` | 被动状态机：wake / kick / when_idle / cancel |
| `values/persistence.py` | JSONL 追加写 + 重放读 |
| `state/recovery.py` | 会话自愈：恢复时给崩溃留下的悬空工具调用补 is_error 合成结果 + `session/repaired` 痕迹 |
| `state/runtime_status.py` | **每轮叠给模型的运行时状态**：`RuntimeStatusRegistry`（注册制贡献者，"名字 + `build(session) -> str\|None`"）；贡献者由应用层注册，循环只负责收集与审计（issue #19） |
| `app/instructions.py` | 工作区项目指令文件（AGENTS.md/CLAUDE.md）：子目录清单扫描（每回合）+ 根文件三态探测（每请求；`lstat`/`stat` 分开 + 读完校验缓存键）+ 字符预算 + system live 段渲染 |
| `app/skills.py` | 按需技能：**两来源合并**（包内 `bundled_skills/` + `<workspace>/skills/`，workspace 同名覆盖，按名字排序）+ `SkillTable`（stat 键控缓存）+ 目录文本 + 按名字解析正文 + **工作区来源的越界检查**（`boundary`） |
| `bundled_skills/` | 随 agent 发布的技能正文（`pyproject` 的 package-data）；自带能力必须跟着 agent 走，不能跟着工作区走 |
| `tools/` | 应用工具（file_io 读写/编辑、search grep/glob、shell bash、todo、**web_search 联网搜索**、**skill 按名字取技能正文**）+ `build_tools(workspace, skills=…)` |
| `app/sandbox.py` | workspace 路径边界：工具入参的轻量沙箱（归一化 + 前缀匹配）+ **宿主直读文件的越界判据**（`workspace_escape_reason`，指令文件与技能共用） |
| `app/ui.py` | 终端渲染（_render_event / _paint，UI 是日志投影） |
| `app/factory.py` | build_agent / load_env（CLI 与 Web 共用组装） |
| `app/workspace.py` | 工作区选择策略：用户输入 → 绝对路径（空 = 宿主默认、相对路径按进程 cwd、`~` 展开、显式路径必须存在且是目录）。Web 的"每个对话一个工作区"唯一入口；想加白名单就加在这一处 |
| `cli.py` | CLI 入口（单次任务 / 无任务参数进 REPL） |
| `web/` | Web 宿主（入口层）：`app.py` FastAPI 路由 + `init_web` + `main`；`state.py` `Seat`/`WebState`/`state`；`sessions.py` seat 生命周期 + 会话文件 + 审批钩子 + **每会话工作区**（解析、落 `session/workspace` 事件、从日志读回）；`titles.py` 自动会话标题；`payload.py` 纯函数投影（不依赖 FastAPI）。seat 化并发隔离（每 seat 一份 `args`，**只有 workspace 不同**）；事件透传 turn/step + turn_start/user_message（带 message_id/rpc_id）/queue_update 帧供前端投影；队列项操作 `POST /queue/update`；`POST /sessions/new` 可带 `{"workspace": "…"}` |
| `app/compaction.py` | 上下文压缩引擎（四步事务 + checkpoint + 会话 token 累计账） |
| `show_memory.py` | 教学脚本：重放日志展示"记忆 = 投影" |
| `tests/` | 154 个架构测试，**按关注点分文件**（2026-09 从单文件 `test_demo.py` 拆出）：`test_values_session.py` 值/日志投影、`test_inbox.py` 队列、`test_prompt.py` 提示词、`test_llm.py` LLM 客户端/wire 格式、`test_loop.py` 框架循环（含运行时状态贡献者）、`test_tools.py` 工具、`test_todo.py`、`test_recovery.py` 自愈、`test_compaction.py` 压缩、`test_instructions.py` / `test_skills.py` 宿主直读、`test_web_search.py`、`test_web.py` Web 宿主（含每对话工作区）、`test_cli.py`；跨文件 helper 在 `conftest.py`；**`test_architecture.py`**（2 条：依赖方向 = 包结构——下层 import 上层当场红，白名单里的例外必须仍然真实存在，不许长僵尸）。3 条平台相关（Windows 建不了符号链接时 skip） |

---

## 6. 进化路线图

目标：**把 demo 进化成可用的本地编码 agent（mini opencode）**。

已定决策：
- 方向：实用编码 agent（能真干活：执行命令、搜索代码、编辑文件）
- 模型：DeepSeek 官方 API（deepseek-v4-flash）
- 节奏：一步步来，每步先讲设计再动手
- 定位：教学 demo → **个人工具 / 求职作品**（工程化重构规划见 NEXT_STEPS.md
  "架构重构（求职作品级）"——框架四层不动，拆 main.py 的应用内容为
  tools/ 包 + app/ui.py + app/sandbox.py，补打包 / lint / typecheck / CI）

### 阶段一：真实工具集（先能干活）

| 任务 | 说明 | 状态 |
|---|---|---|
| `bash` 工具 | 执行 shell 命令（超时 kill-on-cancel、输出截断、cwd 限工作区内、`[exit code: N]` 结果；执行后端接缝为 OS 级沙箱预留） | ✅ |
| `edit` 工具 | 精确字符串替换式编辑（替换 write_file 全量覆盖，省 token；字面量唯一匹配，零/多匹配拒绝，对齐 harness str_replace_editor） | ✅ |
| `grep` / `glob` 工具 | 代码搜索能力（标准库实现，对齐 harness 的 tool-fs-search 形状） | ✅ |
| `read_file` 升级 | 行号、偏移量、长度限制、`line_numbers` 开关 | ✅ |
| 轻量路径沙箱（workspace 边界） | `--workspace` 必填，工具只读写在界内（纯用户态路径校验：归一化 + 前缀匹配）；OS 级沙箱留给 bash 落地后的专题 | ✅ |
| **approval 确认门** | 敏感工具（bash/write_file/edit）执行前人工确认——`ToolSpec.requires_approval` 注册声明 + `Hooks.approval` 确认钩子；拒绝落 `tool/skipped` + is_error 结果（对应 harness 的 approval/权限桥） | ✅ |

> 实施进度与已定设计决策记录在 `NEXT_STEPS.md`（✅ 已完成 / 🔶 设计中 / ⬜ 待做）。

### 阶段二：交互式 REPL（能持续对话）

| 任务 | 说明 | 状态 |
|---|---|---|
| 多轮输入循环 | 持续对话，`/exit` 退出——替代"每次跑一次命令 + --resume" | ✅ CLI REPL（无任务参数启动）+ Web 持续对话 |
| Ctrl-C 取消 | 接到 `agent.cancel()`——CancelledError 传播链已就绪 | ✅ Web 停止按钮已接通 |
| `steer` 接入 | 运行中插入输入走 next-step 队列——双队列第二队首次启用 | ✅ Web `POST /steer`（同回合下一步即时生效；CLI 侧因 stdin/approval 竞争刻意不做，见 NEXT_STEPS） |
| 会话管理 | `/sessions` 列表、切换会话 | ✅ Web 会话列表/切换已完成 |
| Web 并发隔离 | 两会话并行跑互不干扰（每会话独立 agent/SSE/审批） | ✅ seat 化 `_seats[sid]`（见 NEXT_STEPS） |

### 阶段三：长会话保障（能干长任务）

| 任务 | 说明 | 状态 |
|---|---|---|
| max-tokens 粘性续写 | finish_reason=length 时自动继续（当前只记 max-tokens 收尾） | ⬜ 待做 |
| token 计数与成本显示 | usage 已落日志，重放日志即可统计——记忆机制的直接受益 | ✅ 会话累计消耗 token + Web 圆环显示（app/compaction.py 的 `session_token_totals`） |
| compaction 触发 | 上下文超限时压缩历史（harness 的 surface replace 区间遮蔽是方向） | ✅ 全套已完成：自动阈值 + 溢出恢复 + 手动按钮 |
| request_error 钩子启用 | RATE_LIMIT 退避重试——钩子插座插上第一个电器 | ✅ 溢出恢复（上下文过长 → 压缩重试）已占用该钩子；RATE_LIMIT 退避未做 |

### 阶段四：工程化打磨

| 任务 | 说明 | 状态 |
|---|---|---|
| 配置文件 | provider/model/max_tokens/工具白名单，替代纯 CLI 参数 | ⬜ 待做 |
| 错误恢复 | LlmError 分类处理、网络抖动重试 | 🔶 部分（溢出恢复已启用钩子） |
| 日志查看器 | 基于 JSONL 的可视化调试界面（日志本来就是调试器） | 🔶 Web 已能看历史/checkpoint；独立查看器未做 |
| 提示词调优 | identity/persona/tool 规则完善，利用首因/近因效应 | 🔶 持续演进 |

### 每阶段的验收标准

- 阶段一：一句话任务"找 bug 并修复、跑测试"端到端跑通
- 阶段二：连续对话完成多步任务，中途可打断改方向
- 阶段三：长任务（超上下文一半以上）稳定完成，成本可见
- 阶段四：配置驱动、崩溃可恢复、日志可诊断

### 实施约定（延续现有哲学）

1. 新增功能一律落日志——"没有状态不进日志"不变式不能破
2. 决策走钩子/走注册声明，不写死进循环
3. 失败降级为结果，不炸循环
4. 每个阶段结束跑测试 + 更新本文档与 README

---

## 7. 与 harness 的保真度对照（继承自 README）

| 学到并实现 | 简化/未实现（进化时的候选增量） |
|---|---|
| surface 事件标记 + 纯函数折叠投影；**replace 区间遮蔽（位置语义，compaction 用）** | 遮蔽区间溯源校验 |
| Inbox 双队列 + claim 语义 + 持久化重放；**steer 插队（同回合 next-step）；step = 一次模型请求（工具循环由 turn 循环驱动，插队一次往返内被吸收）** | 多宿主并发仲裁、turn-stopping 钩子（`agent/turn-stopping`）、`concludesTurn` 工具结果提前收尾 |
| sections + 严格 `{{var}}` 插值 | 作用域链 shadow、complete 段 |
| 工具分组执行 + 坏 JSON 兜底；**并发许可 fail-closed + 连续段分组（`consumed`）+ 池上限 + 按模型顺序提交 + 取消补合成结果（§3.15）**；**approval/权限桥 + `[exit code: N]` 跨调用准则** | 按参数的并发谓词（`isConcurrencySafe(args)`）、取消时 drain 已派发调用、OS 级沙箱、事件瀑布审批 |
| request/header 落日志 + resume 恢复路由；**checkpoint 策略（四步事务 + 结构化摘要）** | 持久化后端抽象、token 预算选段 |
| 三个钩子（回调版） | 事件总线（emit/serial/waterfall + 作用域过滤） |
| CancelledError 贯穿 + when_idle 收敛 | 三源 abort 熔合 |
| JSONL 追加 + adopt 重放 | 未知事件类型拒绝策略、ignorable 标记 |
| OpenAI function-call wire 格式；**compaction 触发（自动阈值 + 溢出恢复 + 手动）** | max-tokens 粘性续写、后台任务编排 |
