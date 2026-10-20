# 架构游历：一条消息走完全程

> 事实源：[`../architecture.md`](../architecture.md)。本文件只做**压缩与串联**，
> 目的是让主线可以一次读完、并能对着代码复核。行号会随代码漂移，**用函数名检索**。

## 一、一页纸

```
┌─ 5 入口 ────────────────────────────────────────────────┐
│  cli.py（单次任务 / REPL）     my_coder/web/（FastAPI + SSE）│
└───────────────┬─────────────────────────────────────────┘
                │ factory.build_agent() 装配
┌─ 4 应用 ──────▼─────────────────────────────────────────┐
│  app/（factory·compaction·recall·instructions·skills·     │
│        sandbox·workspace·ui）   tools/（12 个工具）        │
└───────────────┬─────────────────────────────────────────┘
┌─ 3 框架 ──────▼─────────────────────────────────────────┐
│  runtime/agent.py  被动状态机（wake / kick / when_idle）   │
│  runtime/loop.py   turn + step 两级循环 + 工具分组执行      │
└───────────────┬─────────────────────────────────────────┘
┌─ 2 状态 ──────▼─────────────────────────────────────────┐
│  state/session.py 唯一事实源 + surface 投影                │
│  state/inbox.py   双队列（spliced 事件的投影）              │
│  state/registry.py  ToolSpec = schema + executor + 声明    │
│  state/prompt.py   sections 按 order 拼接 + 严格插值        │
│  state/recovery.py（自愈） · runtime_status.py（每轮状态）   │
└───────────────┬─────────────────────────────────────────┘
┌─ 1 能力 ──────▼─────────────────────────────────────────┐
│  capability/llm.py   SSE 流式客户端 + FakeLlm + wire 翻译   │
│  capability/hooks.py 三个决策钩子的类型                     │
└───────────────┬─────────────────────────────────────────┘
┌─ 0 值 ────────▼─────────────────────────────────────────┐
│  values/messages.py 不可变 Message/SessionEvent/编解码      │
│  values/persistence.py JSONL 追加写 + 重放读               │
│  values/limits.py 跨层常量                                 │
└─────────────────────────────────────────────────────────┘
       依赖方向：只能向下。`tests/test_architecture.py` 用 AST 断言。
```

**"四层"还是"六层"**：两种说法都对，指的是不同的东西。**框架四层** =
values → capability → state → runtime（[`../architecture.md`](../architecture.md) 的 §2
历史叫法）；加上后来长出来的**应用层**（app / tools）与**入口层**（my_coder/web / cli）
是六层。`tests/test_architecture.py` 的 `LAYERS` 字典是唯一权威（0–5）。
**面试时先说六层，再解释"四层"指框架部分**——这个反问很常见，答得清说明你动过结构。

## 二、一条消息的十二跳

以下行号写作"文件:函数"，便于 `grep` 定位。

| # | 位置 | 发生了什么 | 进日志吗 |
|---|---|---|---|
| 1 | `runtime/agent.py:followup` | `create_user_message` 造不可变消息（值层） | 否 |
| 2 | `state/inbox.py:_splice` | **先** `append('agent/inbox/spliced')`，**再**改内存队列 | 是（痕迹） |
| 3 | `runtime/agent.py:_wake` | idle → running，拉起 `_driver`；忙则置标记，回 idle 补拉 | 否 |
| 4 | `runtime/loop.py:run_turn` | `turn/start`；**每回合求值一次** system 快照（`prompt.assemble`） | 是 |
| 5 | `runtime/loop.py:run_turn` | `peek` 只看候选 → 跑两个钩子（`pre_step` / `request`，**都可能 await**）→ **再** `claim` + 落 `user/message`；最后这一段无 await | 是（surface） |
| 6 | `runtime/loop.py:_run_step` | 组请求：`system`（live 段此刻求值）+ `derive_messages()` + 运行时状态合成消息 + 全部工具 schema | 否 |
| 7 | `runtime/loop.py:_run_step` | `request/header`（含 system 全文、工具名、模型路由） | 是（痕迹） |
| 8 | `capability/llm.py:stream` | SSE → `StreamChunk`；循环层喂组装器，并发**瞬时帧**给 UI | **帧不落盘** |
| 9 | `runtime/loop.py:_run_step` | `assistant/reasoning`（思维链全文）→ `assistant/message`（正文+工具参数，surface）→ `assistant/stream`（帧数/耗时汇总） | 是 |
| 10 | `runtime/loop.py:_run_group` | 按声明分组执行工具；`tool/call` 起跑前落，`tool/result` **按模型顺序**落 | 是 |
| 11 | `runtime/loop.py:run_turn` | `step/end`；有工具调用则回到第 5 跳再发一次请求 | 是 |
| 12 | `runtime/loop.py:run_turn` | `turn/end{reason}`；跑空 inbox 回 idle | 是 |

**第 5 跳的原子性**是取消不丢消息的关键：两个 `await` 全部提到 `claim` **之前**，
所以取消点落在钩子里时队列一个字没动；而 `claim → step/start → user/message` 之间没有
`await`，提交要么没发生、要么已完整落盘。

**第 10 跳的后半句是整个设计的枢纽**：工具结果只落日志，**没有"把结果发给模型"的代码**。
下一次请求的 `derive_messages()` 自动带上它。循环不保存对话状态——记忆自己浮现。

## 三、日志长什么样

一条日志 = 第一行会话头 + 每行一个事件（`values/messages.py:SessionEvent`）。
一次"模型要读文件"的最小序列（注意 **seq0 是入队记账**：`followup` 先落 spliced 再
`_wake`；而 `claim` 也走同一个 `_splice`，所以**认领也会落一条 spliced**）：

```
{"session": true, "format_version": 1, "id": "web-…"}          ← 头：不是事件、不占 seq
{"seq":0,"type":"agent/inbox/spliced","data":{"target":"next-turn","removed_count":0,"inserted":[…]}  ← 入队记账
{"seq":1,"type":"turn/start",   "data":{"turn":1}}
{"seq":2,"type":"agent/inbox/spliced","data":{"target":"next-turn","removed_count":1,"inserted":[]}}   ← 认领记账（不是丢弃，无 outcome）
{"seq":3,"type":"step/start",   "data":{"turn":1,"step":1}}
{"seq":4,"type":"user/message", "data":{…},"surface_op":"append"}   ← 浮上水面
{"seq":5,"type":"request/header","data":{"system":…,"tools":[…],"model":…}}
{"seq":6,"type":"assistant/message","data":{"message":…},"surface_op":"append"}
{"seq":7,"type":"assistant/stream", "data":{"frames":…,"ms":…},"ignorable":true}
{"seq":8,"type":"tool/call",    "data":{"call_id":…,"name":"read_file",…}}
{"seq":9,"type":"tool/result",  "data":{…},"surface_op":"append"}
{"seq":10,"type":"step/end",    "data":{"turn":1,"step":1}}
…
{"seq":N,"type":"turn/end",     "data":{"reason":"completed"}}
```

三个可以立刻讲的点：

- **两类 surface 校验都要响**：三类 surface 事件（`user/message`、`assistant/message`、
  `tool/result`）**必须**带 `surface_op`（缺了抛 `requires surface_op`）；
  **其它类型带了**就抛 `cannot carry surface_op`。错误暴露在写入现场，而不是折叠时
  才发现（`state/session.py:append`）。
- **`ignorable: true`** 是给旧读取者的通行证：词汇增长不 bump 版本，结构性变化才 bump。
- **会话头不是事件**：`_log[seq]` 依赖"下标即 seq"，让头占掉 seq 0 会把整个域平移。

## 四、三个核心结构，别混

`Session` 内部有三个东西，面试最爱在这里追问：

| 结构 | 是什么 | 谁写 | 用途 |
|---|---|---|---|
| `_log` | 全部 `SessionEvent`（含痕迹） | `append` / `adopt` | 事实源 |
| `_surface` | **seq 的列表**（不存事件本体） | `_apply_surface` | 模型可见顺序 |
| `_listeners` / `_stream_listeners` | 订阅者（事件 / 瞬时帧） | `on_event` / `on_stream` | 落盘、UI、SSE |

`_surface` 是"索引"不是"内容"：`derive_messages()` 按它逐个 `by_seq` 取事件折叠。
`replace` 就作用在这个索引上——**删掉一段连续节点、在原位插入 checkpoint 的 seq**。

`derive_messages()` 是纯函数：同一段日志永远折叠出同一份消息序列。这条性质是整个
"恢复 = 重放"的前提，也是面试里"你怎么保证记忆不会漂"的标准答案。

## 五、五个不变式 → 违反后果 → 谁钉住它

| 不变式 | 一句话 | 违反了会怎样 | 证据 |
|---|---|---|---|
| ① 没有状态不进日志 | 新状态必须走 `session.append` | 重启后状态回不来，且没人能解释它从哪来 | `tests/test_values_session.py` |
| ② 模型可见 ⟺ 可重建 | 只有三类 surface 事件进消息，`derive_messages` 纯函数 | 模型看到的东西无法从日志复原 → 调试与恢复都失效 | `tests/test_values_session.py`（surface_op 校验、replace 折叠）· `tests/test_session_events_cache.py`（缓存不改变重放语义） |
| ③ 入队即记账 | 先落 `spliced` 再改内存 | 崩溃窗口里排队消息消失（用户原话静默丢失） | `tests/test_inbox.py` |
| ④ 决策走钩子/声明 | 循环里不写业务 `if` | 加一个能力要改框架，框架开始认识应用 | `tests/test_architecture.py` |
| ⑤ 失败降级为结果 | 工具失败 → `is_error`；取消单向传播 | 一次工具异常炸掉整轮对话；或取消留下非法 wire | `tests/test_loop.py` |

**讲的时候配一个具体事故，说服力会翻倍**：不变式③的来历是"取消点落在 claim 与落盘
之间，消息三处都不在"；不变式⑤的来历是"取消时两条 `tool/call` 已落盘、`tool/result`
一条没有，下一轮请求直接 400"。

> 注意别把 `tests/test_session_format.py` 当成不变式②的证据——它钉的是**会话头与
> 格式版本**（方向感知拒绝、未知事件守卫），不是 surface 折叠。

## 六、system 提示词怎么装配的

`factory.build_agent` 按 `order` 注册 section（`state/prompt.py` 升序拼接、空段省略、
`{{var}}` 严格插值——未注册变量当场抛错，不把 `{{modl}}` 静默发给模型）：

| order | 段 | 性质 |
|---|---|---|
| -100 | `identity` | 静态：角色与项目定位 |
| 0 | `persona` | 静态：行为准则 |
| 10 | `discipline` | 静态：六条通用纪律（Scope / Economy / Evidence / Cleanup / Instructions / Continuity） |
| 20 | `instructions` | **live**：工作区 `AGENTS.md` / `CLAUDE.md` 正文（每请求按 `(mtime_ns, size)` 重算） |
| 95 | `skill:catalog` | **live**：技能目录（只放 name + description，按内容指纹缓存） |
| 105 / 106 / 107 | `tool:bash` / `tool:web_search` / `tool:recall` | 静态：工具专属规则 |
| 110 | `tool:todo` | 静态 |

两条判据：① **通用规则与工具规则不互串**——反复被踩的坑从文档上移成通用段（文档只有
愿意读的 agent 才看得到，system 才是每轮都生效的通道）；② **变化频率决定通道**——
永不变的进静态段，每请求变的进 live 段，每步变的贴 messages 末尾。

## 七、三个最容易讲错的点

**1. `tool/result` 是 `user` 角色**
内部词汇表里工具结果就是一条 `role='user'` 的消息（"工具替你说话"），
到 wire 层才翻译成 `role:'tool'`（`capability/llm.py` 的 `_to_wire_messages`）。
如果面试官问"为什么工具结果算用户消息"——因为内部只需要一种"非助手输入"的角色，
且它天然带 `ToolSource(call_id)` 可以回指。

**2. `replace` 是位置语义，不是 seq 区间**
checkpoint 的 seq 一定**大于**它顶替的旧 seq（追加式），所以 `replace` 之后 `_surface`
不再单调。若按 `start<=seq<=end` 数值过滤，第二次压缩会把范围内的无关节点误吞。
正确做法是拿两端 seq 在 `_surface` 里的**下标**定位连续段（`state/session.py:_apply_surface`）。
——这条有个专门的 bug-fix 记录，是最能体现"踩过坑"的素材。

**3. 流式帧不是状态**
帧只喂实时 UI，不落盘、不占 seq。一步结束时落一条 `assistant/stream` 汇总。
内容完整性由 `assistant/message`（正文 + 工具参数全文）与 `assistant/reasoning`
（思维链全文）保证。实测把帧落盘时，日志 98.9% 的行、89.3% 的字节都是帧。

> **这条改造的可见代价**：新日志不再有 chunk 帧，而 CLI 渲染器当初是跟着帧渲染的——
> 所以 `--resume` 一度看不到历史助手正文（已修：渲染器加 `assistant/message` 分支，
> 并用"本 (turn,step) 已流式打印"标记与实时路径去重）。**讲优化一定要能讲它的代价。**

## 八、数字速查（不确定就别报数）

| 事实 | 位置 |
|---|---|
| 12 个工具 | `README.md` 工具表 |
| 工具超时 60s | `state/registry.py:ToolSpec.timeout_s` |
| 工具结果统一上限 20000 字符（`bash` 自己更早截到 8000） | `values/limits.py:TOOL_RESULT_MAX_CHARS`、`app/constants.py:BASH_MAX_OUTPUT_CHARS` |
| `read_file` 默认 200 行 / 上限 2000 行 / 16000 字符 | `app/constants.py` |
| 上下文窗口 1M、压缩阈值 524288（窗口一半） | `app/constants.py:DEFAULT_COMPACT_TOKENS` |
| 并发池上限 4 | `runtime/loop.py:MAX_PARALLEL_TOOL_CALLS` |
| 收敛软阈值 8 / 硬阈值 16 | 生效值 `app/constants.py:READONLY_NUDGE_AT/READONLY_CLOSE_AT`（经 `factory._progress_policy` 注入）；默认兜底在 `state/progress.py:ProgressPolicy` |
| 系统注入预算：单文件 8000 字符、整段 20000 | `app/instructions.py` |
| 召回 L0 每请求 0.1–0.2 ms | [`../notes/implemented/testing/2026-09-19-context-recall-evaluation.md`](../notes/implemented/testing/2026-09-19-context-recall-evaluation.md)（复现命令在那篇，不在 architecture.md） |

> **测试数与代码行数不在这里写**：它们的权威副本在被门禁扫描的 `README.md` /
> `docs/architecture.md` 里（`tests/test_doc_numbers.py` 只扫 L0/L1/L1.5/L3，
> 不扫 `docs/interview/`）。要报就当场跑
> `conda run -n agent-demo python -m pytest --collect-only -q | tail -1`——
> 写死在讲稿里，迟早说错。
