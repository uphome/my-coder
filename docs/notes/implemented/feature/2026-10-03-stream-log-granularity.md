# Agent Note: 日志粒度——流式帧不落盘，每步一条汇总

Status: implemented

## Problem

会话日志的体量与"模型看见的内容"严重脱钩。实测一个真实会话（`web-1788604766`，2026-10-03）：

| 指标 | 值 |
|---|---|
| 文件 | **116.3 MB / 425,854 行** |
| 模型可见消息 | **1,259 条**（每条可见消息 = 336 行 / 92 KB 日志） |
| 其中流式帧（`assistant/chunk` + `assistant/reasoning/chunk`） | **103.8 MB（89.3%）· 421,324 行（98.9%）** |
| 真正"有意义"的事件（用户话/助手话/工具调用/结果/边界） | 约 4,500 行、约 12 MB |

而且浪费的主体**不是内容**。对一个 step 逐帧量：

| 事件 | 帧数 | 文本字节 | 整行字节 | 固定开销 |
|---|---:|---:|---:|---:|
| `assistant/chunk` | 1,508 | 3,187 | 478,476 | **475,289（99%）** |
| `assistant/reasoning/chunk` | 1,285 | 5,714 | 257,948 | **252,234（98%）** |

一个 step 的 2,793 行 = 736 KB，**内容只有 8.9 KB**——98–99% 是逐行 JSON 的固定开销
（标签式包装 + seq/type/turn/step + 转义）。后果是全方位的：写入次数（平均每次模型请求
约 700 次 `open`/`close`+`write`）、冷启动（`GET /sessions` 冷态 **6.4 s**）、恢复重放
（425,854 事件 → 常驻内存约 3.4× 文本量）、以及"内存与活跃上下文无关、只与流过多少字节有关"。

## Decision

**流帧不是状态**——它只是"传输过程"。所以：

1. **帧不落日志**，改走新开的瞬时通道 `Session.on_stream` / `emit_stream`
   （`my_coder/state/session.py`）：只通知实时订阅者，**不进 seq、不进投影、不落盘**。
   值对象 `StreamFrame`（`values/messages.py`）与 `SessionEvent` 分开——两者的消费者不同：
   事件给日志/投影/重放，帧只给"此刻正在看的人"。
2. **每个 step 落一条** `assistant/stream` 汇总：`frames` / `reasoning_frames` /
   `text_chars` / `reasoning_chars` / `ms`。没有内容，只回答"这一步流了多久、多少帧"。
3. **内容完整性不靠帧**：`_BlockAssembler` 就是用同一批 chunk 拼出 `assistant/message`
   （正文 + 工具调用参数）与 `assistant/reasoning`（思维链全文）的——帧的内容是它们的
   真子集（构造上如此）。这一点有**机械证明**：`tests/test_stream_log.py` 把瞬时通道收到的
   帧拼接起来，与落盘事件的正文/参数/思维链逐字符比对。
4. **实时体验不变**：终端（`app/factory.py`）与 Web SSE（`web/app.py`）各多接一条
   `on_stream` 订阅；漏接只会丢打字机效果、不会丢信息，但会明显变难用，所以两处都写在
   测试与注释里。
5. **不 bump 格式版本**：按仓库规则（`values/messages.py` 的锚点说明），**词汇增长靠
   `ignorable` 标记，结构性变化才 bump**。为此补上了 `ignorable` 的**写入通路**
   （`Session.append(..., ignorable=True)` → `new_event(..., ignorable=...)`）——
   此前它只有序列化通路，写不进去。旧读取者遇到 `assistant/stream` 会**跳过**而不是拒绝重建。
6. **历史渲染换数据源**：历史 `history_payloads` 本来就是按角色出行、思维链从
   `assistant/reasoning` 折进 assistant 行——所以帧一停，历史照样有正文与思维链（#4 的
   显示不回退）。`assistant/chunk` 的映射保留在 `web/payload.py::event_to_payload`，
   供**旧日志**的实时/回放通道使用。

预期与实测（同一份 v1 日志做投影，零 API）：

| | 帧时代 | 帧聚合后（投影） |
|---|---:|---:|
| 字节 | 116.3 MB | **约 12.5 MB** |
| 行数 | 425,854 | **约 4,530** |

## Alternatives considered

- **只做写入批量化（常开句柄 + 行缓冲）**——**否决为单独方案**：它只减少系统调用，
  字节量一行不减（而浪费的主体恰恰是行数与其固定开销）。可以后续作为补充。
- **把帧删掉**（不落盘也不留汇总）——**部分否决**：内容确实不丢（子集关系），但"这次流式
  花了多久、多少帧"是有用的观测（长回合诊断、SSE 卡顿排查），留一条汇总的成本近乎为零。
- **帧按时间窗/帧数批量落盘**——**否决**：仍是"流帧进日志"的路子，只是把 700 行变 20 行；
  而 v2 想要的"紧凑 timed stream"本来就该是结构性的（内容归内容、过程归过程）。
- **把 timed stream 内嵌进 `assistant/message`**——**否决**：那会让一个 surface 事件携带
  非 surface 的观测字段，读侧要同时处理"消息"和"流式元数据"两件事；独立事件更干净，
  而且 `assistant/stream` 可以直接标 `ignorable`，内嵌字段做不到。

## Consequences

**换来的**：日志回到"账本"语义（内容 + 边界），体量与延迟都降一个数量级——写放大 111× 消失、
冷扫描从 6.4 s 掉到亚秒量级、内存里的事件对象从 42 万降到 4 千量级。**旧日志不受影响**
（帧类型仍在词表里，源文件不迁移）。

**付出的与风险**：

- **帧不再是"可重放的历史"**：想回看"当时逐帧的样子"已经不可能（日志里没有帧）。这是有意的
  取舍——它换来的是日志可用；帧本来就是过程而非事实。
- **漏挂 `on_stream` 就丢打字机**：新增宿主（又一个入口）必须记得挂。当前有测试与两处注释守着，
  但这是"人与 agent 都可能忘"的一类。
- **`ignorable` 的语义责任**：标错（把结构性变化标成可忽略）会让旧读取者静默少读一段历史。
  判据写在 `values/messages.py`：**只有"旧运行时跳过后仍语义正确"的词汇增长才能标**。
- **下一步**：`request/header` 已成日志里第二大项（12.6 KB/条 × 605 ≈ 7.6 MB），
  单独查它存了什么再决定怎么瘦身。

## Testing

证据类型：**单元测试（零 API，6 条）+ 真实日志的投影测量**

`tests/test_stream_log.py`：

1. 帧不落日志、每 step 恰好 1 条 `assistant/stream`（计数与耗时字段自洽）；
2. **内容等价（机械证明）**：瞬时帧拼接 == 落盘的正文 / 思维链；工具参数增量的拼接 ==
   `assistant/message` 里 `tool-call` 的参数（丢参数就是丢事实）；
3. **行数与帧数解耦**：4 帧与 40 帧的会话**行数相同**（这条是 issue 的核心回归）；
4. 实时通道仍逐帧送达（`on_stream` 收到帧）；
5. `assistant/stream` 在 JSON 里 `ignorable: true`，且用**不含该类型的旧词表**读同一文件
   会跳过它而不是抛 `UnknownSessionEventError`；
6. v2 历史（无帧）仍有正文与思维链（`history_payloads` 按角色出行、reasoning 折进 assistant 行）；
   旧日志的帧仍能经 `event_to_payload` 渲染。

测量（可重跑，零 API）：

```sh
# 逐帧开销与构成（把 SESSION 换成任一会话日志）
conda run --no-capture-output -n agent-demo python - <<'PY'   # 见本文表格的取数方式
PY
```

合并前的全量：**221 passed / 3 skipped**（`ruff` 含 `eval` 全绿、`mypy` 45 文件）。
