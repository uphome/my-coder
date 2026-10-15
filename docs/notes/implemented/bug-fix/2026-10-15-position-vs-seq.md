# Agent Note: 跳帧重放之后的"位置 ≠ seq"——一族三个 bug

Status: implemented

## Problem

`Session.from_path`（issue #46 ③ 阶段 2 引入）**跳帧重放**：打开会话时整类跳过流式帧
（老日志里帧占 **98.9%** 的事件）。这让打开大会话从 **377.7 MB / 14.5 s** 降到
**21.8 MB / 0.6 s**（见[粒度那篇](../feature/2026-10-03-stream-log-granularity.md)与
[分层提案](../../proposed/architecture/2026-10-03-session-log-tiering.md)）。

代价是一条**隐形前提被破坏**：日志密集时"**第 i 条事件的下标就是 i**"，跳帧之后不再成立
（5,617 条事件的 seq 散布在 0…425,984 之间）。而代码里有三处依赖这条前提，都**不会自己报错**
——只会偶尔 `IndexError`（吵），或者**静默给出错的数据**（危险）：

| 现场 | 症状 | 实测证据 |
|---|---|---|
| `Session.append` 用 `len(_log)` 当新事件的 seq | 跳帧会话里新 seq **从 4,661 重新开始** ⇒ 与已用 seq **重复** | 真实会话 `web-1788604766.jsonl` 出现 **957 个重复 seq**（详见 #57） |
| `app/compaction.py::_shadowed_messages` 与 `web/payload.py::surface_with_seq` 用 `events[seq]` 索引 tuple | **历史渲染**（`/history`）与**压缩取被压内容**要么 `IndexError`（整个 500），要么**取到错的事件** | 撤掉修复跑新建的穷举护栏：`payload.py:68` 与 `compaction.py:369` 各报 `IndexError: tuple index out of range` |
| `select_compact_range` 返回 `(surface[0], surface[first_keep_idx-1])`，两个消费者却按**数值区间**判定 | `replace`（checkpoint）的 seq **很大却坐在 surface 前部** ⇒ 端点**数值倒置**（实测 `(425851, 5501)`）⇒ **一条都选不中** | 真实会话（压过 5 次）上 `_shadowed_messages` 返回 **0 条** ⇒ 摘要是从**空对话**生成的，而 checkpoint 照样遮蔽那段 ⇒ **模型永久失去那段历史**（只能靠召回读回） |

第三条最严重：它不是崩溃，是**静默的内容损失**。

## Decision

三条修复，判据一致——**用"身份"（seq）或"位置"（下标）时必须认准是哪一种，不许混用**：

1. **`Session._next_seq` 游标**：`append` 从独立游标取 seq 并推进（`adopt` 推到"见过的最大 seq + 1"）。
   **不再用 `len(_log)`**——那只是"留下来的事件数"。
2. **`Session.by_seq(seq)`** 作为**唯一**的"按 seq 取事件"入口：稠密时走"下标即 seq"的快路
   （零额外开销），稀疏时查 `_sparse` 映射（**只在真稀疏时才填**，否则等于每个事件存两份）。
   两个消费点（压缩取被压内容、历史渲染）都改用它。
3. **选区端点按 surface 位置解释**：`_shadowed_messages` / `extract_file_ops` 先
   `surface.index(start_seq/end_seq)` 定位下标，再取 `surface[min:max+1]` 那段；
   `extract_file_ops` 用这段 seq 的 **min/max 包络**筛事件（文件操作只存在于 `tool/call`
   **痕迹**事件里，拿不到精确集合；宽一点的包络对"哪些文件被动过"这个启发式无害）。
   **日志格式不变**：`shadowed=(start_seq, end_seq)` 仍是那两个端点——
   `_apply_surface` 本来就用 `surface.index()` 位置化处理（它的 docstring 早就写明
   "在 surface 中的索引定位连续段"），**是那两个消费者写错了，不是格式错了**。

## Alternatives

- **把 seq 换成"稠密重编号"**（读时把稀疏 seq 压成 0..n，或写盘时按过滤后的日志重编号）：
  **否决**——seq 是日志里的**身份**，被 `shadowed` 区间、`_surface`、分析脚本、以及**已经写下的
  老日志**引用；重编号等于改写历史引用，比修三处调用点危险得多。
- **让 `Session.from_path` 保留帧**（回到稠密）：**否决**——那正是 #46 ③ 要解决的问题
  （377.7 MB / 14.5 s）。
- **给 `shadowed` 换成"位置区间"存法**（存下标而不是 seq）：**否决**——下标会随之后的
  `replace` 变动，seq 不会；格式改动还会牵动老日志兼容，收益只是让消费者少写两行。
- **只修崩溃的地方（加 try/except 或 `len()` 保护）**：**否决**——静默给错数据比崩溃更糟，
  必须修到"语义正确"，不能修到"不报错"。

## Consequences

- **读取路径的语义有了一条明确判据**：`event_count` / `events_since` 是**位置游标**（跳帧会话里
  必然更小），`by_seq` 才是按 seq 取；两者**不许互相替代**。这条写进了
  `tests/test_sparse_consumers.py` 的注释与断言里，
  免得以后有人把"位置游标在两种形态下不同"误当 bug 去"修"成相同。
- **`_shadowed_messages` 的修复让压缩重新"真的总结内容"**：真实会话上从 **0 条**变成
  **350 条 / 316,897 字符**。这不是性能改善，是**内容安全**修复。
- **被污染的那份日志**（`web-1788604766.jsonl`）在跳帧视图里仍有 **19 个重复 seq**、其中 **5 个在表面**；同 seq 的两条事件按"后出现者优先"解析，因此那 5 条的归属是歧义的。
- **代价**：`append` 多一个整数游标；`by_seq` 多一次 `_log[seq].seq == seq` 比较（稠密路径）；
  稀疏会话多一张 `_sparse` 映射（**只在稀疏时**）。

## Testing

- `tests/test_seq_continuity.py`：跳帧重放后追加的事件，
  seq **必须大于文件里的最大 seq**；继续写几条再整表重放，seq **必须唯一且严格递增**。
- `tests/test_sparse_consumers.py`：**穷举护栏**——
  同一份日志的整表重放与跳帧重放，**所有**读取路径（记忆 / 历史 / manifest / `read_turn` /
  压缩取被压内容 / L0 目录 / todo / workspace …）必须逐条一致；另有一条明确钉住
  "`event_count`/`events_since` 是位置游标，两种形态**本来就该不同**"。
- `tests/test_sparse_replay.py`：稀疏重放后取记忆可用；
  以及**payload 里出现 `"type": "assistant/chunk"` 时必须保留该事件**（防行首类型正则误判）。
- **反向验证**（判据本身也要验）：撤掉修复跑上述测试必须红——实测 `payload.py:68` /
  `compaction.py:369` 各报 `IndexError`；压缩选区那条在真实会话上从 **0 条 → 350 条**。
- 三绿：`ruff`（含 `eval`）· `mypy` · `pytest`。
