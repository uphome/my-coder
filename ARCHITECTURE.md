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
入口层  agent.py      被动状态机：send → inbox → wake → driver → idle
                        │
循环层  loop.py       turn/step 两级循环 + 三个钩子
                        │
状态层  session.py    追加式事件日志（唯一事实源）+ derive_messages 投影
        inbox.py      双队列 pending 消息（spliced 事件的持久化投影）
        prompt.py     sections 按 order 拼接 + {{变量}} 严格插值
        tools.py      工具注册表：schema + executor + 执行模式
能力层  llm.py        OpenAI 兼容 SSE 流式客户端 + 可脚本化 FakeLlm
        hooks.py      三个决策钩子的类型（属循环层接口）
                        │
值 层  values.py      不可变 Message/SessionEvent + JSONL 编解码
        persistence.py JSONL 追加写 + 重放读（横切状态层的 I/O 通道）
```

依赖方向只有一条：上层依赖下层，下层不感知上层。

---

## 3. 核心机制

### 3.1 日志是唯一事实源，记忆 = 日志的投影

整个架构最核心的一句话：

> **模型的记忆 = 日志的投影 = derive_messages() 的输出 = 下次 request 的 messages**

"记忆"不是一份独立存储的对话状态——它根本没被存下来。每次发起模型
请求前，循环实时地从日志折叠出来（`loop.py` 调 `session.derive_messages()`）：

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
   每循环一次：claim 一批消息 → 开一个 step → 干完
step 循环（内层，_run_step）    完成条件：模型给出纯文本 / max-tokens
   每循环一次：组请求 → 流式 → 有工具调用就执行 → 回来再调
```

> 外层循环回答"还有没有任务"，内层循环回答"这次应答完没完"。

关键点：
- 第 1 步认领 next-turn，后续步认领 next-step
- 认领到的消息在循环里落成 `user/message` surface 事件——从"水下"队列
  载荷变成模型记忆
- 工具结果**只落日志**，没有"把结果发给模型"的代码——回到 while 顶部，
  下一次 `derive_messages()` 自动带上。循环不保存对话状态，只写日志，
  记忆自己浮现
- 日志顺序就是因果顺序：step/start → user/message → request/header →
  chunks → assistant/message → tool/call → tool/result → step/end
- turn/end 五种结局：completed / blocked / aborted / error / max-tokens；
  aborted 和 error 记完账后必须重新抛
- config 三级 fallback：request 钩子 > agent.options > 上次 request/header
  （resume 恢复模型路由）

### 3.7 决策走钩子：循环是骨架，钩子是关节

三个决策点预留在循环里，策略由外部注入：

| 钩子 | 问题 | 典型决策 |
|---|---|---|
| `pre_step` | 这条消息该不该进模型？ | 安全审查拒绝、消息改写 |
| `request` | 这次请求怎么配置？ | 模型路由、动态 max_tokens |
| `request_error` | 模型报错怎么办？ | RATE_LIMIT 重试、401 放弃 |

钩子签名携带**默认实现**：钩子想放行就调 `default()`，想改写就自己返回，
想拒绝就返回 None。未挂载时循环走默认路径——**架构完整、插座已装**。
demo 运行时没挂任何钩子（只有测试验证语义），进化阶段会插上第一个。

钩子的改写也都落日志（pre_step 改过的消息落 user/message，request 结果
进 request/header）——不变式②"模型可见 ⟺ 可重建"没有破功。

### 3.8 失败降级为结果：工具失败是对话的一部分

"工具失败 → is_error 结果"是循环层和工具层共同维护的契约，四层兜底：

| 失败发生在哪 | 谁在兜底 |
|---|---|
| 坏 JSON（模型给了坏参数） | loop.py——连 execute 都不进 |
| 参数校验失败（缺 required / 多参数） | tools.py 抛 ValueError → loop.py 捕获降级 |
| 工具执行抛异常 | loop.py 捕获降级 |
| 工具卡死超时 | tools.py wait_for 兜底 |

模型看到 `ValueError: missing required argument` 这类结果，自己知道怎么
改。**任何异常都不能越过 `_run_group` 炸掉循环**，唯一能打断的只有用户
的取消。原则：失败被局部化、显式化，变成模型可以理解和修复的输入。

### 3.9 取消单向传播：CancelledError 即 AbortSignal

Python 的 `asyncio.CancelledError` 扮演 harness AbortSignal：

```
cancel() → driver.cancel() → 异常沿 await 链穿过
_run_group → _run_step → run_turn（记 turn/end aborted 后 re-raise）
→ _kick（吞掉，回 idle）
```

**每一层只记账，不拦截**——记账是义务，拦截是背叛。取消只往一个方向
传，没人半路吞掉。

### 3.10 能力层：双向翻译 + StreamChunk 契约

能力层（llm.py）是内部词汇表和外部协议之间的**双向翻译器**：

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

`values.py` 负责"单个事件 ↔ JSON"的纯转换（**没有文件 I/O**），
`persistence.py` 才是碰文件的通道。分工：Session 管状态、persistence
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
- **每次请求都带全部工具 schema**：模型在请求间无状态，工具描述是每轮
  的固定 token 成本——进化时加工具要算 token 账
- **提示词每回合快照一次**：`assemble` 在 turn 开头求值，整个 turn 内
  system 恒定，可预期、可调试
- **prompt sections 用 order 数值排序**：主序按 order 升序、平局按名字；
  main.py 用 -100/0/110 间隔留插队空间。排序本身是架构性的（插件插队），
  长提示词下首因/近因效应才变成真实的调优手段
- **usage 进日志**：成本统计将来是日志的投影，运行时不需要记账
- **模型是否每次看到全部工具**：是。每次请求全量携带 tools 清单

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

| 文件 | 角色 |
|---|---|
| `values.py` | 值层：不可变 Message/SessionEvent + tagged dict 编解码 |
| `session.py` | 日志 + surface 折叠投影（append / derive_messages / adopt / request_header） |
| `inbox.py` | 双队列（next-turn / next-step）+ claim 语义 + 持久化重放 |
| `prompt.py` | sections 按 order 拼接 + `{{var}}` 严格插值 |
| `tools.py` | 工具注册表（schema + executor + parallel/sequential + 超时） |
| `llm.py` | 能力层：SSE 流式客户端 + FakeLlm + wire 双向翻译（含思维链字段解析） |
| `hooks.py` | 三个决策钩子的类型 |
| `loop.py` | turn/step 两级循环 + 流组装 + 工具分组执行 + 思维链痕迹落盘 + 四层兜底 |
| `agent.py` | 被动状态机：wake / kick / when_idle / cancel |
| `persistence.py` | JSONL 追加写 + 重放读 |
| `main.py` | CLI + 示例工具 + 日志驱动 UI |
| `show_memory.py` | 教学脚本：重放日志展示"记忆 = 投影" |
| `tests/test_demo.py` | 20 个架构测试 |

---

## 6. 进化路线图

目标：**把 demo 进化成可用的本地编码 agent（mini opencode）**。

已定决策：
- 方向：实用编码 agent（能真干活：执行命令、搜索代码、编辑文件）
- 模型：DeepSeek 官方 API（deepseek-chat）
- 节奏：一步步来，每步先讲设计再动手

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

| 任务 | 说明 |
|---|---|
| 多轮输入循环 | 持续对话，`/exit` 退出——替代"每次跑一次命令 + --resume" |
| Ctrl-C 取消 | 接到 `agent.cancel()`——CancelledError 传播链已就绪 |
| `steer` 接入 | 运行中插入输入走 next-step 队列——双队列第二队首次启用 |
| 会话管理 | `/sessions` 列表、切换会话 |

### 阶段三：长会话保障（能干长任务）

| 任务 | 说明 |
|---|---|
| max-tokens 粘性续写 | finish_reason=length 时自动继续（当前只记 max-tokens 收尾） |
| token 计数与成本显示 | usage 已落日志，重放日志即可统计——记忆机制的直接受益 |
| compaction 触发 | 上下文超限时压缩历史（harness 的 surface replace 区间遮蔽是方向） |
| request_error 钩子启用 | RATE_LIMIT 退避重试——钩子插座插上第一个电器 |

### 阶段四：工程化打磨

| 任务 | 说明 |
|---|---|
| 配置文件 | provider/model/max_tokens/工具白名单，替代纯 CLI 参数 |
| 错误恢复 | LlmError 分类处理、网络抖动重试 |
| 日志查看器 | 基于 JSONL 的可视化调试界面（日志本来就是调试器） |
| 提示词调优 | identity/persona/tool 规则完善，利用首因/近因效应 |

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
| surface 事件标记 + 纯函数折叠投影 | surface replace 区间遮蔽 + 溯源校验（compaction 用） |
| Inbox 双队列 + claim 语义 + 持久化重放 | 多宿主并发仲裁、steer 中断当前步 |
| sections + 严格 `{{var}}` 插值 | 作用域链 shadow、complete 段 |
| 工具分组执行 + 坏 JSON 兜底 | approval/权限桥、跨调用准则 |
| request/header 落日志 + resume 恢复路由 | checkpoint 策略、持久化后端抽象 |
| 三个钩子（回调版） | 事件总线（emit/serial/waterfall + 作用域过滤） |
| CancelledError 贯穿 + when_idle 收敛 | 三源 abort 熔合 |
| JSONL 追加 + adopt 重放 | 未知事件类型拒绝策略、ignorable 标记 |
| OpenAI function-call wire 格式 | max-tokens 粘性、compaction 触发 |
