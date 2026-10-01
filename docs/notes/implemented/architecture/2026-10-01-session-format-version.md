# Agent Note: 日志格式有锚点——会话头 + 方向感知拒绝 + 未知事件守卫

Status: implemented

## Problem

日志是唯一事实源，但**格式本身没有版本**。`.sessions/main.jsonl` 的第一行直接是事件：

```json
{"seq": 0, "time": 1790140299.11, "type": "agent/inbox/spliced", ...}
```

后果分两种，都不可见：

- **升级之后**：运行时增删事件类型、改语义，旧进程仍能"成功"读回一份日志——只是它理解的
  与写它的进程不同。恢复出一份**语义不同**的会话，没有任何信号。
- **降级之后**：新格式的日志落到旧读取者手里，同样静默重建。

另外 `ignorable` 字段是**死字段**：`values/messages.py` 定义了它、序列化/反序列化都带着，
但**读取端零检查**（`grep` 全仓 `ignore` 相关读取路径 = 0 处）。也就是说"这条事件可以跳过"
这个声明写了没人听。

DSH 早在 0.1 时代就有这套机制（`.agents/notes/implemented/architecture/2026-08-10-session-log-version-mechanism.md`），
三条原则值得抄：**单调整数**、**写入者决定 bump**、**按方向读**。

## Decision

**会话头是文件级元数据，不是事件**（`values/messages.py` 的 `SessionHeader`）：

```json
{"session": true, "format_version": 1, "id": "main", "time": 1790847285.53}
```

为什么不复用事件信封：`Session._log[seq]` 依赖"seq == 下标"（`derive_messages` 直接按下标取），
让它占掉 seq 0 会把整库序号平移，并坏掉所有"按 seq 取事件"的读取方（`eval/recall` 的
审计脚本就是按下标取的）。头与事件流正交。

头只带三个字段。**工作区不进头**：它已经有 `session/workspace` 痕迹事件这个家，
再放一份就是第二个会漂的副本（`docs/AGENTS.md` 第一节：一个事实只有一个家）。

**写入**：`Session.bind_store(path)` 顺带调用 `save_header`——只在文件为空时写，
已有内容不动（头在文件出生时写一次；追加第二条头没有意义）。

**读取**（`values/persistence.py`）：

1. **方向感知拒绝**：`check_compatible` 比较 `declared_version(path)` 与
   `SESSION_FORMAT_VERSION`。日志更新 → `SessionFormatUnsupportedError`
   （**独立异常类型**，与"损坏"分开：文件什么都没坏）；相等 → 正常；更旧 →
   返回旧版本号（v0 与 v1 事件形状相同，无需迁移）。**没有头的老文件按 v0 读**。
2. **未知事件默认"必读"**：`KNOWN_SESSION_EVENT_TYPES` 是 22 个类型的闭集，
   `load_events` 遇到不在集合里、且 `ignorable != True` 的事件 → `UnknownSessionEventError`；
   标了 `ignorable` 的跳过、其余照常重建。

**只有结构性变化才 bump 版本；词汇增长靠 `ignorable`，不 bump**——因为"能否自动升级"
是那一步 upgrader 的属性，不该由版本号形态预先承诺。版本由**写入者**决定，判据不是
"旧运行时能否解析"，而是"还能否**语义正确**地处理"。

**所有读取方同步改造**（头会改变"第一行是事件"的隐含契约）：`web/sessions.py` 的会话列表、
`app/recall.py` 的 L0 行内快扫、`eval/recall/build_dataset.py` 与 `export_sessions.py` 的
逐行读。判别统一用 `line.startswith('{"session": true')`（快扫）或
`is_session_header_line()`（解析后）。

## Alternatives considered

- **`format_version` 做成一条事件（占 seq 0）**：否决。会把整库 seq 平移一格，
  且 `derive_messages` 的"seq == 下标"前提立刻失效——那是修一个可见问题、造三个不可见问题。
- **`format_version` 单独放一个 `.meta` 旁挂文件**：否决。日志是唯一事实源，
  版本是日志的属性；旁挂文件会在复制/导出/删除时与日志失配（而"日志丢了"正是最需要版本锚点的场景）。
- **主次版本号 `1.2.3`**：否决。DSH 的理由直接可用：能否自动升级是那一步 upgrader 的
  有无，不该由版本号形态预先承诺；单调整数足够。
- **读取者主动 bump（"我能解析就兼容"）**：否决。判据必须是**写入时刻的语义判断**——
  读取者无法知道"新运行时是否还按老语义处理这条事件"。
- **未知事件默认忽略**：否决。这是本机制里唯一的安全方向问题：
  忘标记 → 过度拒绝（用户看到明确报错，麻烦）；默认忽略 → **静默恢复出一份被掏空的会话**
  （安全事故）。方向反了代价不对称。
- **把 `ignorable` 一起删掉（既然是死字段）**：否决。它正是"词汇增长不 bump 版本"的载体，
  现在读取端真的用它了。
- **头带上 workspace**：否决，见上（第二个会漂的副本）。

## Consequences

**换来的**：

- 日志格式有了锚点；旧读取者遇到新日志**明确报错**并指明升级方向与文件路径，
  而不是静默重建。
- `ignorable` 从死字段变成生效机制——词汇增长不必 bump 版本。
- 全仓三处"按行数事件"的计数（会话列表的 `events`、L0 目录的事件数、eval 导出的条数）
  从此与真实事件数一致（头不再被算进去）。

**付出的 / 边界**：

- **v0 与 v1 没有格式差异**——这次只是把机制装上，bump 的是"从此有锚点"。
  真正的迁移链（`v1 → v2`）还没有实现；`check_compatible` 对"更旧"只返回版本号。
- **写入端仍无人设置 `ignorable`**：新增可跳过的事件类型要显式传 `ignorable=True`
  才享受这条通道，目前没有调用方这么做（`Session.append` 也不收这个参数）。
- **存量日志不受影响**：没有头的老文件按 v0 读，`--resume` 照常工作——
  本仓库 `.sessions/` 下那 4 个文件都不需要改写。
- **拒绝是 fail-closed 的**：日志比读取者新时，**用户能做的只有升级**——
  这符合"宁炸勿静默"，但对"临时用旧版本看一眼"不友好（页面/CLI 会直接报错）。

## Testing

```sh
conda run -n agent-demo python -m pytest tests/test_session_format.py -q   # 7 passed
conda run -n agent-demo python -m pytest -q                                # 210 passed, 3 skipped
```

七条契约测试逐条对应计划 §P0-3 的验收表：新建会话有头且版本正确 / 无头老文件按 v0 读 /
更新的日志被拒且消息含升级方向与路径 / 未知且不可忽略 → 拒绝 / 未知但 `ignorable` → 跳过其余重建 /
**有头时 `derive_messages()` 与加头之前逐字节一致** / 闭集不许漏也不许留僵尸
（最后一条扫描 `my_coder/` 的字符串字面量，双向断言）。

真实链路自检：`conda run --no-capture-output -n agent-demo python -m my_coder.cli --fake
--workspace . --session fmt-check --sessions <tmp> "read README.md and summarize"`——
第一行是 `{"session": true, "format_version": 1, …}`，`--resume` 可继续。
