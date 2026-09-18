# 上下文召回与预算管理：改动设计（issue #3）

> **状态：🔶 设计中，未实现。本文是这次改动的执行说明**（怎么写、先写哪个、怎么验）。
>
> 分工：调研与决策档案在 `agent.md` §7；**落地后**把规则提炼进 `AGENTS.md`、机制摘要进
> `ARCHITECTURE.md`、进度进 `NEXT_STEPS.md`、前端映射进 `web/PROJECTION_DESIGN.md`。
> 按项目惯例，那些文档在实现时同步，本文不作规范。
>
> **本文的中心是"召回"，不是"压缩"。** issue #3 提了三件事（预算感知 / `new_context`
> 硬切换 + notes / history 回溯原始会话）。经过一轮设计与实测后确认：**只有第三件直接
> 消除信息损失**，前两件是"知道自己要丢了"和"丢得更彻底"，属于配套。所以实施顺序按
> **M1 召回 → M2 预算感知 → M3 切换与笔记**。

---

## 1. 目标与设计原则

### 1.1 一句话目标

**让 agent 能回到原始对话取回被压缩丢掉的信息**——压缩只改变"模型看得见什么"，
不改变"存在什么"；召回把这个差补上。

### 1.2 三条设计原则（本文其余部分都从它们推出）

| 原则 | 含义 | 为什么 |
|---|---|---|
| **P1 一切都是日志的投影** | 清单、检索面都是纯函数 fold 出来的，可重建、不新增事实源 | 不变式①②：没有状态不进日志；模型可见 ⟺ 可重建 |
| **P2 不做相关性排序，让 LLM 自己导航** | 主路径是"目录 → 清单 → 明细"的结构化导航；关键词检索只作兜底 | 语料是**自述的时间线**（对话），不是文档堆；排序参数没有标注数据、调不准；Codex 的做法证实了这条路（§3.3） |
| **P3 检索面 = 曾经进过模型上下文的事件** | 即 `user/message` + `assistant/message`（含其工具调用）+ `tool/result` 的**全集**（live + 已被遮蔽） | 只有进过上下文的才会"丢"；痕迹事件占日志 99.6% 的行数（实测），放进检索面只会稀释结果；可重建的投影（todo/context 状态栏）随时能重算，不算损失 |

### 1.3 非目标（本期明确不做，及其理由）

| 不做 | 理由 |
|---|---|
| `trace` 检索面（reasoning / request-header / chunk） | 从未进过上下文，不算损失 |
| 子块切分 / small-to-big 的 child chunk | 实测单个事件最大 17k 字符（§2.2），以事件为单位已足够 |
| 向量（dense）检索 | 语料 1 MB；当前技术栈没有 embedding 端点；代码标识符类查询 lexical 更强。**保留后端接缝**，等评测证明 lexical 必挂再谈 |
| FTS5 / sqlite 索引 | 同上：657 条事件线性打分是亚毫秒级；索引接缝保留 |
| N2 真新窗口 + 会话血缘 | M3 之后再评估（§7） |
| `spill` / tool-result pruner | 我们的工具本身已有输出上限（`BASH_MAX_OUTPUT_CHARS` / `read_file` 分页 / grep 250 上限） |
| 相关性排序、IDF/BM25 调参 | P2：主路径不需要；兜底检索用 coverage 排序即可 |

---

## 2. 现状与实测（本文所有数字都来自本仓库真实日志）

### 2.1 已有的家底

`agent_demo/compaction.py`（473 行）：阈值自动压缩（`DEFAULT_COMPACT_TOKENS = 524288`）、
溢出→压缩→重试、四步事务（start/summary/replace/end）、8 段结构化 checkpoint +
由代码拼入的 `fileOps` 文件清单、`estimate_context_tokens` / `session_token_totals` /
`cache_hit_rate`、`POST /compact` + UI「压缩旧对话」按钮 + 上下文圆环。

本次要复用的已有结构：`Session.surface`（surface 投影 + replace 遮蔽）、`(turn, step)`
坐标、`session/title` 事件、`tool/call` 痕迹、`turn/end.reason`。

### 2.2 语料实测

```
会话 4 个 / 45.1 MB / 175,007 行
  └─ surface 事件：657 条（575 live + 82 已被压缩遮蔽）
       类型分布：tool/result 311 · assistant/message 276 · user/message 70
  └─ 痕迹事件：占 99.6% 的行数
```

| 单位 | 中位 | p95 | 最大 | 备注 |
|---|---|---|---|---|
| 回合 字符数 | 1,340 | **102,801** | **219,323** | 回合内 step 数最大 **61**、事件数最大 131；5/54 个回合 > 60k |
| 步 字符数 | 900 | 11,858 | **219,150** | 一步可达 77 个 surface 事件（并行工具批次） |
| 事件 user | 16 | 101 | 4,623 | |
| 事件 assistant | 244 | 1,833 | 15,427 | 含工具调用参数 |
| 事件 tool/result | 1,160 | 8,920 | **17,035** | 32 条 > 8k 字符 |

**结论：事件是唯一有界的单位**（最大 17k 字符）→ 明细读回以事件为最小单位；
回合太大（p95 10 万字符）→ 绝不能当检索单位或返回单位。

扫描成本实测（同一语料）：

| 做法 | 耗时 |
|---|---|
| 原始字节 `find()` 全扫 | 73 ms（622 MB/s） |
| 逐行 + 类型预过滤 | 172 ms |
| **全量 `json.loads`（反面教材）** | **842 ms** |
| 只解析命中行 | 140 ms |

**结论：只解析命中行；不要把日志全量 parse。**

### 2.3 用户话清单（manifest）的成本实测

| 会话 | 用户话 | 全文 | 截断到 120 字符 | 已压缩遮蔽 | 超长(>120) |
|---|---|---|---|---|---|
| `web-1788604766` | 55 | 10,981 字符 | **1,124 字符** | 7 | 3 |
| `web.jsonl` | 13 | 607 字符 | 607 字符 | 0 | 0 |
| `web-1789573652` | 2 | 9 字符 | 9 字符 | 0 | 0 |

**结论：清单便宜到可以常驻**——44 回合会话的全部用户话，截断后仅 ≈1.1k 字符（≈550 token），
全文也只有 ≈11k 字符（≈5.5k token）。

### 2.4 三个必须处理的既有问题

**① checkpoint 本身是一条 `user/message`**（压缩用 `surface_op='replace'` 写它）。朴素地
"收集所有用户话"会把合成消息混进清单：

```
[turn 6] This is an automatically generated checkpoint condensing an earlier span …
```

→ 清单必须**只收真人发言**（排除带 `<compacted-summary>` 的合成消息），插队消息标注来源。

**② 自动压缩只挂 `turn/end`**（`wire_auto_compaction`，外加 `request_error` 被动兜底）。
而 step 粒度已是"一次模型请求"，一个回合可发 61 次请求（实测）——
**单回合内膨胀到溢出时主动压缩没有机会跑**。属于 M2 的修复项（挂 `pre_step`）。

**③ 压缩成功率**：某会话 4 次 `compaction/start` 只成功 1 次，另外 3 次都是
`compaction/end{error: "empty summary"}`（时间上都在成功那次之前，很可能是
`max_tokens=600` 被思维链吃光的老 bug 残留）。**做损失评测前必须确认压缩本身成功**，
否则测的是别的东西。

### 2.5 压缩到底丢了什么（唯一的 1 次成功压缩）

原文 167,999 字符 → 摘要 4,280 字符（**压缩比 2.5%**）。按"原文有、摘要没有"的具体片段统计：

| 类别 | 原文 | 摘要保留 | 丢失 | 丢掉的例子 |
|---|---|---|---|---|
| 路径/文件 | 71 | 24 | **47（66%）** | `agent_demo/persistence.py`、`agent_demo/compaction.py` |
| 数字/阈值 | 26 | 1 | **25（96%）** | `1_000_000`、`1000 行`、`1500 行`、`2.29s` |
| 标识符/测试名 | 177 | 18 | **159（90%）** | `test_surface_replace_shadows_and_derives_in_place` |
| 大写常量/错误码 | 55 | 9 | **46（84%）** | `DEFAULT_COMPACT_TOKENS`、`ALLOW_PARALLEL_IN_PROGRESS` |
| URL/出处 | 5 | 0 | **5（100%）** | `https://docs.pytest.org/...` |

**摘要保留叙事，丢掉证据。** 而且是**代码侧注入的结构化信息（`fileOps` 那 24 个路径）
活下来了**——凡是交给摘要自由发挥的具体信息，丢了 66%~100%。推论：**能让代码注入的，
就不要指望摘要。**

> ⚠️ 这张表是**代理指标**（信息片段留存率），**不是损失本身**：压缩比 2.5% 不能读成
> "丢了 97.5% 的信息"；正则抽取也有噪声。它的正当用途是**出题器**（§5.4）。
> 损失的严格定义与量化见 §5。

---

## 3. 对标：DSH / PI / OpenCode / Codex

### 3.1 四家一览（前三个在本地，Codex 读了源码）

| 能力 | DSH | PI | OpenCode | Codex |
|---|---|---|---|---|
| 压缩触发 | 窗口 0.8 + 溢出 + `/compact` | `manual`/`threshold`/`overflow` 三态 | `compaction: {auto, tail_turns, preserve_recent_tokens, reserved}` | 预采样 + 回合中 roll-over |
| 保留量口径 | `retainRatio 0.16` | `retainedTail` / `tokensBefore` | **`preserve_recent_tokens` + `reserved`（token）** | 可只算"前缀之后"的增长，基线锚在服务端实测 |
| 工具输出剪枝 | 独立 pruner 插件 | — | 有（`[Tool output truncated for compaction]`） | — |
| 历史结构化读取 | `session-query-sqlite`：**FTS5** | `scanBranch(stopAtType:'compaction')` | 消息分页 / `filterCompacted` | **history/notes 九个工具（闭源后端）** |
| 模型可见预算 | ✗ | ✗ | ✗ | ✓（`get_context_remaining` + 一次性 reminder） |
| 模型发起新窗口 | ✗ | ✗ | ✗ | ✓（`new_context`，**无参数、不摘要**） |
| 跨窗口笔记 | ✗（`.agents/notes` 是仓库约定） | ✗ | ✗ | ✓（服务端 notes，虚拟路径） |

**四家都在做"交接"，只有 Codex 做了"回到原文"。**

### 3.2 DSH 的 FTS5 实测（为什么不照抄）

`packages/session-query/session-query-sqlite/src/schema.ts:127,135` 用 `tokenize = 'unicode61'`，
且全包没有任何 CJK 处理。实测后果：

| tokenizer | `message` | `宽度`(2字) | `宽度钉`(3字) |
|---|---|---|---|
| `unicode61` | **0**（"messages" 是整词，子串不命中） | **0** | 0 |
| `trigram` | 1 | **0**（<3 字符不索引） | 1 |

且 `unicode61` 下**一整串连续中文是一个 token**（`列宽度钉死` 整段命中、`宽度钉死` 不命中）。
→ **照抄会在中文上失效**；我们若上 FTS5，必须先把中文 bigram 化再入库。

### 3.3 Codex 的三段式（我们要抄的形态）

**① 主动注入一小段（不是工具）**：后端返回 `thread_hint`，塞进 `<context_window>` 这个
developer 消息（连同窗口血缘 first/current/previous window id）。
证据：`token_budget_context.rs:60-75`、`core/src/session/mod.rs:4272-4321`；
测试断言 `notes.thread_hint` **不作为工具暴露**（`app-server/.../history_notes_extension.rs:206-225`）。

**② 清单 + 结构过滤（按需）**：

| 工具 | 关键参数 | 回到上下文的是什么 |
|---|---|---|
| `history.list_windows` | `limit` / `recent_first` | 窗口 ID + 条目数 |
| `history.list_items` | `role` / `tool_name` / `window_id` / `recent_first` / **`max_chars_per_item`** | 条目清单 + 每条截断内容 |
| `history.read_item` | `window_id` + `item_id` + **`offset_chars` / `limit_chars`** | 单条原文的精确字符区间 |
| `history.search_contents` | **大小写敏感的"字面量子串"** + 同样的结构过滤 | 命中的条目 |

（`ext/history-notes/src/tools.rs:143-236`；`ToolExposure::DirectModelOnly` 在 `:311-313`。）

**③ 返回的是原始条目**，且**命中内容尾部带 `[id: ...]` 标记**（`read_item` 的 `item_id` 说明），
模型据此引用与回读。

**关键观察：Codex 基本没做相关性排序**——`search_contents` 只有"字面量子串 + 结构过滤 +
`recent_first`"，接口层不承诺相关性。排序交给了**结构 + 时间序 + 模型自己多轮导航**。

### 3.4 抄什么 / 不抄什么

| 抄 | 理由 |
|---|---|
| 两段式：**清单/预览 → 精确区间读** | 便宜地看"有什么"，再按坐标精读 |
| **结构过滤是一等公民** | 比关键词更有效 |
| **命中带稳定 ID/坐标标记** | 模型要能引用并回读 |
| 返回体带**截断预算** | 上下文成本由调用方控制 |
| **主动注入一段入口（不是工具）** | 新窗口的"目录页"；我们要做成**结构化清单**，而不是一段自由文本 |
| **浏览模式**（无 query 的清单调用） | "最近发生了什么"比强行编关键词自然 |

| 不抄 | 理由 |
|---|---|
| 字面量子串、无分词 | 我们中文工况实测会 0 命中（§4.5） |
| 无排序 **且无清单** | Codex 有服务端索引兜底；我们靠**清单**替代排序（P2） |
| `encrypted_output` + "never disclose to the user" | **与我们的哲学相反**：日志是唯一事实源、UI 是投影，召回必须落日志、可审计 |
| 一切走服务端 | 我们本地、零网络 |

---

## 4. M1：三层目录导航（核心）

### 4.0 总体形状

```
L0 会话层      【状态栏常驻】最近 N 个会话 + 当前标记      ← 模型据此知道"我有历史"
      ↓ 模型挑一个会话
L1 用户话清单   session_manifest(sid)                     ← 真人发言 + 每回合足迹（≈1~4k 字符）
      ↓ 模型自己从清单里挑出"哪一回合"
L2 完全层       read_turn(sid, turn[, step])              ← 该回合的原文事件（按需、有界）
```

**没有相关性排序，主路径也不需要检索器**：没有倒排索引、没有 sqlite、没有向量。
要写的只有**三个投影 + 两个工具**（L0 的状态栏贡献者、L1/L2 的投影与渲染）。

兜底的字面检索（`search_history`）**v1 不做**——只有"清单找不到"时才需要
（跨会话、或某句话只出现在工具输出里），等导航评测证明它必要再加。

**导航靠**：会话标题 + 回合编号 + 用户自己的措辞 + **足迹线索**（工具/文件/结局/结论摘录）。

### 4.1 检索面（P3）

- 集合 = 全部 `user/message` + `assistant/message` + `tool/result`（**含被 replace 遮蔽的**）；
- 每条带 **live / shadowed** 标记（shadowed = 已被折叠进某次 checkpoint，并记下是哪次）；
- 痕迹事件不进检索面，但**可以在读回时作为"相邻痕迹"附带**（reasoning / request-header
  能解释"当时为什么这么决定"，作为上下文有价值、作为检索目标没有）。

### 4.2 L0 会话层：**放状态栏**（不是工具）

L0 常驻在**运行时状态栏**里（与 todo 并列的 runtime status 贡献者），模型不用调工具就知道
"有过哪些会话"：

```
<context_sessions>
[web-1788604766] 2026-09-13 · 55 条用户话 · 「加一个 web_search 工具」 · 44 回合 · [已压缩×4]
[web-1789573652] 2026-09-16 · 2 条用户话 · 「你好」 · 2 回合  ← 当前
</context_sessions>
```

- 数据来自 `session/title`、首条用户话、用户话计数、文件 mtime、`compaction/start` 次数；
- **只放最近 N 个**（默认 5）+ 当前会话永远在内；总数超限时补一行 `…（另有 K 个更早会话）`；
- 成本：每会话一行（≈60~100 字符），可忽略；而且是 append-only 的稳定前缀，利于缓存；
- **风险**：常驻状态会变墙纸（模型很快不看）→ 所以 L0 只给"目录"，真正的导航靠 L1。

> 为什么 L0 常驻而 L1 按需：模型**需要知道自己有历史**（否则不会想到去查，
> 这是"模型不知道自己不知道"的那一面）；但**不需要**常驻 55 行的用户话清单。

### 4.3 L1 用户话清单（本设计的入口，工具）

```python
session_manifest(session_id: str = '', include_shadowed: bool = True,
                 with_footprint: bool = True, max_chars_per_line: int = 120) -> str
```

**只收真人发言**，三条过滤/标注规则：

1. **排除合成消息**：带 `<compacted-summary>` 的 checkpoint 不入清单（它们是压缩产物，
   不是用户语言）；
2. **插队要标注**：同一回合内第 2 条起的用户消息标 `插队`；
3. **被遮蔽的保留并标记** `[已压缩]`——**这正是清单的价值**：用户话是约束的来源，
   绝不能因为压缩就从目录里消失。

**富清单**一行 = 用户话（截断）+ 该回合的结构足迹：

```
[turn  7] 处理 issues/1 · 5 step · bash,edit,read_file · web/index.html · done
          用户：处理 https://github.com/uphome/my-coder/issues/1
[turn  3] 手动(插队) · 1 step · — · — · done
          用户：手动
[turn  1] 你先探索这个项目… · 12 step · read_file,grep · — · done [已压缩]
          用户：你先探索这个项目。后续你需要为这个agent添加一个工具。代码风格按照原来的进行。
```

足迹字段全部可从日志现算（复用 `extract_file_ops` 的抽取器）：

| 字段 | 来源 | 作用 |
|---|---|---|
| `N step` | `step/start` 计数 | 一眼看出这回合是大活还是小活（实测最大 61） |
| 工具名（去重） | `tool/call` | "只读调查"还是"真干活" |
| 涉及文件 | 工具参数（同 `fileOps`） | 直接指向工作区 |
| 结局 | `turn/end.reason` | `done / error / aborted / max-tokens` |
| **结论摘录** | 该回合最后一条 assistant 文本前 ~60 字符 | **把"推测 agent 做了什么"换成"有据可查"** |

> **足迹不是锦上添花，是导航的必需项。** 因为导航题（§5.4）不会用用户原话当题干
> （那样在清单里直接搜就行，测不出东西），而要从 **agent 侧的工作内容**去问——
> 模型必须靠"工具 / 文件 / 结局 / 结论摘录"把"用户当时说了什么"和"我现在要找什么"桥起来。

**为什么要富清单**：让模型"自己推断哪一回合"这一步有据可依，而不是凭记忆猜自己做过什么。
成本（实测口径）：截断用户话 ≈1.1k 字符 + 足迹与结论 ≈3.3k 字符 ≈ **4.4k 字符/会话**。

### 4.4 L2 完全层：回合明细读回

```python
read_turn(session_id: str = '', turn: int = 0, step: int | None = None,
          scope: str = 'surface') -> str
```

- **坐标对模型暴露的是 `turn`（可加 `step`）**，seq 只在内部使用——模型按回合思考，
  日志按 seq 存储（对齐 Codex 暴露 window_id + item_id，而不是内部偏移）；
- 渲染成可读文本，**不是原始 JSONL**（后者带 `$dict`/`$list`/`$message` 包装与转义）：

```
[turn 7 · step 2 · user] 处理 https://github.com/uphome/my-coder/issues/1
[turn 7 · step 2 · assistant] 我先读一下 issue 的内容…
[turn 7 · step 2 · tool_call] bash({"command": "…"})
[turn 7 · step 2 · tool_result] …
```

- **尺寸警告与截断规则**（实测：回合字符数 中位 1,340 / **p95 102,801** / 最大 219,323；
  一个回合最多 61 步、131 个事件）。所以"把整回合都给模型"会一次灌掉约 5 万 token：
  - `step` 为空 → 默认给 **"回合头 + 步骤目录（每步一行）+ 装得下的步"**；
  - 字符/事件双上限，超限**明确告知被截断**并给出步骤目录，让模型按 `step` 精读；
  - **无游标/offset/limit**（对齐 DSH 的 cursor-free）；
- `scope='trace'` 可选附带痕迹事件（reasoning / request-header），默认关——它们能解释
  "当时为什么这么决定"，作为**读回时的上下文**有价值，作为检索目标没有。

### 4.5 兜底：字面检索（**v1 不做，留接口**）

只有在"清单找不到"时才需要：跨会话、或某句话只出现在某个工具输出里。**先不做**——
等导航评测（§5.3）证明模型确实会遇到"清单里没有"的情况，再加这把钥匙。届时实现要点
（已在 §4.5 下方保留，含实测反例）如下：

```python
search_history(query: str, session_id: str = '', kind: str = '', limit: int = 20) -> str
```

用于"猜不到是哪一回合"的场景（某句话只出现在工具输出里、跨会话"我们讨论过 X 吗"）。
**不做相关性排序**，但要做**词元化**，否则中文实测直接 0 命中。基线实测（真实日志）：

| query | 类型 | 全行字面 | 仅 surface | 词元 AND |
|---|---|---|---|---|
| `插入信息不起作用` | 中文短语 | 1 | 1 | 1 |
| `气泡右缘` | 中文短语 | 9 | 4 | 4 |
| **`右缘漂移`** | 中文短语 | **0** | **0** | **0** |
| **`messages 列宽度`** | 中英混 | **0** | **0** | 1 |
| `select_compact_range` | 标识符 | 4 | 4 | 4 |
| **`plan_compaction`**（不存在） | 标识符 | 0 | 0 | **5（假命中）** |
| `new-context-already-used`（负样本） | 代码串 | 0 | 0 | 0 |

词元 df 分布：`入信`=1 · `右缘`=4 · `漂移`=6 · `压缩`=59 · `compaction`=60 · `messages`=96。

四条由实测得出的规则：

1. **中文短语不做 bigram-AND**——`右缘漂移` 因 `缘漂` 在语料里不存在（df=0）而全灭；
   改 **bigram 覆盖率打分**，**分母只算语料中实际存在的词元**；
2. **标识符整体优先**——`plan_compaction` 切词后变 `plan`+`compaction` 产生 5 条假命中；
   只有整体 0 命中才降级为子词，并在返回里说明"已降级"；
3. **只收窄到 surface 就是一次大清洗**（`steer` 87→47、`宽度` 32→6、`气泡右缘` 9→4）；
4. **排序用 coverage 优先**（先按"命中了几个存在的词元"排），**不用 IDF/BM25**——见 P2。

空结果必须**干净且可解释**：说明搜了什么、跳过了什么、"没有找到"，并建议换词或改用清单。

### 4.6 投放策略与缓存

| 时机 | 内容 | 理由 |
|---|---|---|
| **窗口切换 / 压缩之后** | 注入一次清单 | 新窗口的"目录页"（Codex `thread_hint` 的位置，我们是结构化清单） |
| **模型调用工具时** | 按需返回 | 主路径 |
| **不常驻全文清单** | — | 理由不是 token（1~4k 字符可忽略），是**会变墙纸**：模型很快就不看了 |

缓存说明：清单是 **append-only**（只往后长），天生是缓存稳定前缀；若将来要常驻，
用截断版（≈1.1k 字符）放消息尾部，并接受它只是"目录"。

### 4.7 与压缩的挂钩（消除损失的闭环）

1. **原文永不删**（已满足：`replace` 只动 `Session._surface` 索引列表，`_log` 一个字不动）；
2. **checkpoint 写指针**：`<compacted-source session="…" range="a..b"/>` ——让模型知道可以去哪儿捞；
3. **checkpoint 写线索**：`fileOps`（已有）+ 事件数 + 回合范围 ——治"模型不知道自己不知道"；
4. **清单 ↔ 遮蔽**：清单是**全量投影**（含已遮蔽的用户话，带 `[已压缩]` 标记），
   checkpoint 是**被压缩后的叙事**——两者互补，不重复。

### 4.8 改动/新增文件

| 文件 | 动作 | 内容 |
|---|---|---|
| `agent_demo/recall.py` | **新增** | 检索面投影（surface 全集 + live/shadowed）、L0 会话目录、L1 清单与足迹、L2 回合渲染 |
| `agent_demo/tools/recall.py` | **新增** | 两个工具：`session_manifest` / `read_turn`（兜底 `search_history` v1 不做） |
| `agent_demo/runtime_status.py` + `factory.py` | 改 | L0 会话目录做成 runtime status 贡献者（与 todo 并列） |
| `agent_demo/tools/__init__.py` | 改 | 注册新工具 |
| `agent_demo/compaction.py` | 改 | checkpoint 增加 `<compacted-source …/>` 指针与线索字段 |
| `agent_demo/constants.py` | 改 | 上限常量（清单行宽、明细事件/字符上限、检索封顶） |
| `agent_demo/factory.py` | 改 | 注册工具；`discipline` 增加"恢复历史"的通用规则（§4.9） |
| `tests/test_demo.py` | 改 | §4.10 测试 |

### 4.9 提示词（通用规则，不写事故细节）

`discipline` 段增加一条通用规则（通用行为，不属于任何具体工具）：

> **Recall**：your context is a summary of a longer history that is still on disk. When a
> detail you need is missing — an exact path, number, command, error string, or what the
> user originally asked — look it up instead of guessing or redoing the work; when you
> quote something from an earlier turn, read it first.

工具描述里另给"怎么用"（清单优先、找不到再搜、按回合读）。

### 4.10 测试（M1）

| 测试 | 断言 |
|---|---|
| `test_recall_surface_includes_shadowed_events` | 被 replace 遮蔽的事件仍在检索面，且标 `shadowed` |
| `test_manifest_excludes_checkpoint_synthetic_messages` | 带 `<compacted-summary>` 的 user/message 不进清单 |
| `test_manifest_marks_steer_and_shadowed` | 插队标 `插队`、被遮蔽标 `[已压缩]` |
| `test_manifest_footprint_from_log` | step 数 / 工具名 / 文件 / 结局 / 结论摘录 与日志一致 |
| `test_read_turn_renders_text_not_raw_jsonl` | 输出不含 `$dict` / `$list` |
| `test_read_turn_rejects_unknown_turn` | 不存在的回合 → is_error（不炸循环） |
| `test_search_identifier_not_split_by_default` | `plan_compaction` 不产生 `plan`+`compaction` 假命中 |
| `test_search_cjk_coverage_ignores_absent_bigrams` | `右缘漂移` 能命中 `右缘`/`漂移` 所在事件 |
| `test_search_shadowed_marked_and_live_first` | 命中带标记、live 排在 shadowed 前 |
| `test_search_empty_result_is_explicit` | 负样本返回"未找到 + 已搜索范围"，无假命中 |

---

## 5. M1 的验收：损失度量规格

### 5.1 损失的定义

**损失不是"文本没了"，是"能力掉了"**，三条约束：

1. **相对性**：`Loss = E_{q~Q}[ ℓ(q, ctx_after) ]`——同一段遮蔽，对"继续当前任务"可能零损失，
   对"三小时前那个决定为什么这么定"可能全损。**必须先固定查询分布 Q**（这就是清单/明细
   两级问题的来源）；
2. **行为性**：判据是 agent 会不会因此做错事（违反约束 / 重蹈覆辙 / 答错事实）；
3. **可恢复性**：能被重算/重读的信息（工作区里还有、命令可重跑）**不是损失，是成本**：

```
f 在 ctx_after 中仍可获得？
 ├─ 能原样读到（live 事件里还有）        → 无损失
 ├─ 能从工作区重算（文件还在/命令可重跑） → 不是损失，记"恢复成本"
 └─ 不可获得（已遮蔽 + 不可重算）        → 真损失
```

### 5.2 三臂实验（**端到端**，不是答题）

**主指标 = 任务成功率**：给 agent 一个**探针任务**，看它能不能做对。
三臂的差别只在"它手上有什么"：

| 臂 | 上下文 | 召回工具 | 用途 |
|---|---|---|---|
| **R1 上限** | 压缩前的完整原文（**用日志重建**） | 不需要 | 这个任务本来能不能做对 |
| **R2 基线** | 只剩摘要 | **不注册** | 当前系统的真实水平 |
| **R3 处理** | 摘要 + L0 状态栏 | 注册 `session_manifest` / `read_turn` | 召回救回多少 |

```
Loss      = acc(R1) − acc(R2)      # 压缩毁了多少
Recovered = acc(R3) − acc(R2)      # 召回挽回多少
Residual  = acc(R1) − acc(R3)      # 还剩多少救不回来
```

> **为什么必须端到端**（评审定的）：真实链路是五步——
> ① **意识到"我这里缺了东西"** → ② 自己把信息需求表述出来 → ③ 用清单/工具定位 →
> ④ 读回原文 → ⑤ 把内容正确用进回答/行动。
> 只测 ③（"我们告诉它要什么知识，让它挑回合"）**漏掉了最容易失败的第 ① 步**
> （unknown-unknowns：模型不知道自己不知道，于是自信地瞎猜），而且那种题面是
> **神谕式配好的**、天然可答。所以③只能当**组件诊断**（见 §5.3）。

### 5.3 指标：端到端为主，组件诊断为辅

**主指标（端到端）**：

| 指标 | 定义 | 为什么 |
|---|---|---|
| **任务成功率** | 探针任务是否做对（逐字串 / 程序化判定） | 唯一的"它到底有没有用" |
| **幻觉率** | 答错但**很自信**（尤其编出一个具体值） | "记忆漂移"的直接证据 |
| **过程指标** | 有没有调召回工具 / 调了几次 / 花了多少 token | 区分"没查就答对"与"查了才答对" |
| **恢复成本** | 达到同等正确率所需的调用数与 token | "能恢复但多花 10 次调用"也是损失 |

**四种探针（判分从硬到软）**：

| 探针 | fact F 的样子 | 探针任务 | 判分 |
|---|---|---|---|
| **约束型（最强）** | 用户说过"这一步不许提交" | "收尾这个任务" | **程序化**：查工具痕迹里有没有 commit |
| **纠正型** | "上次那个修法失败了，因为 X" | "再试一次" | 程序化：有没有重试失败路线 |
| **精确值型** | "端口定的是 8123" | "把端口写进配置" | 逐字串判定 |
| **避免重复型** | "已经试过 Y 了" | "怎么解决 Z" | 程序化：有没有再做 Y |

**组件诊断（辅，便宜，不进主结论）**：把信息需求直接交给模型、只测"用清单挑回合"
（`eval/recall/build_navigation.py` + `run_selection.py`）。它的价值是**定位故障**：
主指标掉了时，用它能分清是"没去查"（①失败）还是"查了但挑错回合"（③失败）。
首轮 13 题的实测表见 §5.4 末尾——**不作为主结论**。

**成本（诚实数字）**：一次端到端 run ≈ 3~10 次模型请求，每次请求重发上下文
（实测单次 input 中位上百 k，缓存命中 96%）。12 题 × 2 臂 × 2 次重复 ≈ **48 次 run**、
**1~2M token** + 一两小时机时。端到端方差大，**每题至少重复 2 次**，单次数字不可信。

### 5.4 数据构造：受控会话 + 探针任务（主路径）

**为什么不能靠挖历史日志**：实测那唯一一次成功压缩，2271 个候选事实经过"只留真噪音"
的硬过滤后，**0 条**真正"只能靠召回"（细节见本节末尾的挖矿实测）——历史里可挖的太少、
且 F 是否被摘要保住不受我们控制。所以**主路径是受控构造**：

```
make_controlled_session.py
  ① 构造一个**真实格式**的会话（可以真跑 agent，也可以直接 append 事件）：
     里面藏着 fact F（约束/失败原因/精确值/已试过的做法）
  ② 真跑一次压缩（真事务、真摘要）
  ③ 校验 F 确实：已被遮蔽 ∧ 不在摘要里 ∧ 不在工作区里（三道标签，见下）
  ④ 生成**探针任务**（新的一轮用户输入），其正确完成必须用到 F
run_endtoend.py
  ⑤ 同一份压缩后的日志跑两遍：R2（不注册召回工具）/ R3（注册）
  ⑥ 判分 + 记录过程指标（调了几次、token、有没有幻觉）
```

**难度可控**：F 埋的深度可调（放在长工具输出的中段 vs 放在助手的结论句里），
摘要保不保得住由真实摘要决定——这正是我们要测的。

**导航题生成器（组件诊断用，保留）**：从 agent 侧工作内容出题，锚点用该回合**独有短语**
保证良定义，校验三条（逐字包含锚点 / 无指代词 / 干扰回合足够），按与清单的词元重叠打
T1（撞词）/ T2（桥接）标签。

**生成配方**（四步，全部可自动）：

1. **抽目标事实**：对每个回合，从 **agent 侧内容**里抽一个有辨识度的东西
   （改了哪个文件 / 跑了什么命令 / 得出什么结论 / 哪个错误串）。
2. **写 query**：给模型"该回合的原文 + 该回合在清单里的那一行"，
   要求它写一句真人风格的提问，**且不得使用清单行里出现过的词**——逼出"桥接"而不是"抄词"。
   （模板版可在没有 LLM 时先用：`"哪一回合我们处理过 {文件}？"`，但那是**简单档**。）
3. **校验**（自动，全部可程序化）：
   - **不与清单撞词**：query 的词元不出现在任何清单行里（否则退化成字符串匹配）；
   - **回合唯一**：目标事实只在该回合出现（否则金标不唯一，弃题）；
   - **非空干扰**：该会话至少还有 K 个其它回合（默认 K≥5），否则这题太送分。
4. **难度分层**（题集自带标签，评测按层报数）：

| 难度 | 定义 | 考什么 |
|---|---|---|
| **T1 简单** | query 的词出现在目标回合的**清单行**里（用户话/足迹） | 会不会读清单 |
| **T2 桥接** | query 的词**不在任何清单行**里，但能靠足迹语义关联（"CSS 宽度" → 那次改了 `web/index.html`） | **真正的导航能力** |
| **T3 跨会话** | 需要先选对会话（L0） | L0 目录有没有用 |

**三类数据集**：

| 集 | 题目 | 要不要三道过滤（§5.4 修正后的标签） |
|---|---|---|
| **navigation（主）** | query → 正确回合号 | **不需要**：金标是回合号，与摘要/工作区无关。实测 70 条用户话即刻可用，规模上不封顶 |
| **recovery（次）** | query → 只在原文里的细节 | 需要：`shadowed` 且不在摘要/工作区（用 §5.4 的标签筛） |
| **negative** | 历史里没有 | — |

**受控补足**仍然要做（挖矿够不到的类别）：被压缩掉的**用户约束**、被否决的方案、
运行时实测值 → `eval/recall/make_controlled_session.py` 注入已知 fact 再触发真实压缩。

**实测（2026-09，`eval/recall/build_dataset.py`）**：拿那唯一一次历史压缩挖矿。

第一版把"摘要里有 / 工作区里有 / live 里有"当**硬过滤**，结果几乎全被滤掉（2271 → 0）。
**这个做法是错的**（评审时被指出并纠正）：

> **摘要里有 ≠ 不用查询。** 摘要本身是被压缩过的信息：它可能保住了"用户要建分支"这个
> **要点**，却丢掉了原话里的**具体约束**；用短语做包含判断，测的是"这个词有没有被引用"，
> 而不是"信息还在不在"。更糟的是它滤掉了**最该测的那一类**——**要点在摘要里、细节只在
> 原文里**，那正是模型"以为自己知道"然后按粗粒度行动（记忆漂移）的情形。

**修正后的口径**：

| 类别 | 处置 | 理由 |
|---|---|---|
| 行号/行数、临时夹具路径（`a.py`）、泄露（答案在题干里）、重复 | **硬过滤** | 真噪音，与召回无关 |
| `in_summary` / `in_workspace` / `in_live` | **标签，不过滤** | 它们衡量的是"**端到端**是否需要召回"；是否真有区分度要靠 R2 实测，不能靠字符串包含猜 |
| `gold_all`（所有含该答案的坐标，含 live 副本） | 命中集 | 检索器返回 live 副本是**正确行为**，不该判错 |

修正后同一份日志产出：**detail 28 + manifest 5 + negative 3 = 36 条**，
其中 **16 条 `in_summary=true`**（就是上面说的最该测的那一类）。

**两条结论**：

1. **"片段留存率"严重高估损失**：§2.5 那张"路径丢 66% / 数字丢 96%"的表里，被丢掉的
   绝大多数是行号、工作区可读的文件名、以及摘要已保留的内容——**可恢复性三分类把损失
   从 ~97% 拉回到很低**。这条结论不受上面修正影响（行号+夹具+工作区可读合计约 90%）。
2. **query 质量是瓶颈，不是数量**：模板的"最近中文短语"启发式不可靠
   （实测出题 `当时处理「隐藏目录跳过」的是哪个文件？` → 答案 `tests/test_a.py`）。
   所以要两条腿走：
   - **v0.2 LLM 改写**：给"事实 + 上下文"，让模型写一句自然提问（答案必须恰好是它），
     噪声则回答 SKIP——既过滤垃圾又产出自然措辞（构建期一次性成本）；
   - **受控构造**：挖矿够不到的类别（**被压缩掉的用户约束**、被否决的方案、运行时实测值）
     靠 `make_controlled_session.py` 注入已知 fact 再触发真实压缩。

实现注意（踩过的坑）：

- `compaction/summary` 在 `replace` **之前**落日志（四步事务），生成器要用 pending 暂存摘要；
- 工作区索引必须**排除 `eval/`**，否则上一轮写出的 `questions.jsonl` 含答案，
  下一轮会被"工作区里有"这条过滤掉（**自我污染**）。

**导航题首轮实测（2026-09，`build_navigation.py` + `run_selection.py`）——组件诊断，非主结论**：
13 道导航题（12 T1 / 1 T2，两个真实会话；题库 `eval/recall/navigation.jsonl`）。
把 L1 清单 + 一道题交给模型，要求给出回合号：

| 策略 | top-1 精确 | **±1 回合** | top-3 |
|---|---|---|---|
| LLM 选择 | **38%** | 46% | 38% |
| 词元重叠基线 | **69%** | 85% | 69% |

**不能读成"LLM 导航不行"**——它暴露了三个混淆因素（都是这一版实现的锅）：

1. **数据集偏向字面匹配**：出题时**要求 query 逐字包含**该回合的"独有短语"，
   而清单行的**结论摘录**常常就含这个词 → 字符串匹配天然占优；
2. **清单泄露答案**：结论摘录取自 assistant 自己的正文，query 又从同一段文本造 ——
   **同一段文字既是题面又是线索**；
3. **选择调用关掉了 thinking**（`llm_util.ask` 默认 `thinking=False`），
   而多候选消歧恰恰是最需要推理的那类任务。

它**确实**证明了另一件对设计有利的事：**L1 清单的区分度足够**——连最朴素的词元重叠都能到
69%/85%，说明"用清单导航"这条路信号是够的，问题在题面与提示词，不在清单结构。

**下一轮修三处**（按优先级）：

1. **去掉"逐字包含锚点"的硬要求**，改成从 **agent 侧工作内容**提问（"改 `web/index.html`
   那次"、"跑 pytest 那次"），并**禁止 query 与清单撞词** → 才是真 T2；
2. **加消融对照**：清单**去掉结论摘录**再跑一遍 → 直接回答"结论摘录该不该进清单"；
3. **选择调用打开 thinking**（或换更强模型）→ 把"模型能力"与"任务偏向"分开。

### 5.5 判分阶梯（优先客观）

1. **逐字串命中**（数字解析后比较、路径 basename 比较）——客观、零成本、可回归；
2. **程序化行为判定**（约束型：查工具痕迹里有没有 commit；纠正型：查有没有重试失败路线）
   ——**最强的一档**，不依赖任何模型判分；
3. **LLM 判定**——只在"决策理由/被否决方案"这类无逐字答案时用，需抽样人工校准。

### 5.6 已知陷阱（做评测时不要踩）

1. 拿压缩比当损失（错，压缩比 2.5% ≠ 丢 97.5%）；
2. 把可重算的信息算成损失（用 5.1 的三分类修正）；
3. 正则抽取噪声（`site-packages` 路径、中文散文里的 "error"）→ 加信息量过滤 + 人工抽检；
4. `n=1`（真实日志只有 1 次成功压缩）→ 必须受控构造；
5. 摘要质量本身有方差 → 每题跑多次取分布；
6. 压缩本身可能失败（§2.4 ③）→ 先确认成功再评测；
7. **端到端方差远大于选择题**：单次 run 的数字不可信 → 每题 ≥2 次，报告分布而不是一个点；
8. **"做对"不等于"查了"**：模型可能靠猜对 → 必须同时记**过程指标**（有没有调召回工具）；
   靠猜对的那部分要单独算，否则会把运气记成方法的功劳；
9. **题面泄露**：探针任务不能提到 F 本身（否则 R2 也能"答对"）；
10. **工作区泄露**：F 不能同时存在于工作区里（否则读文件就行，测的不是召回）——
    用 §5.4 的三道标签在构造时校验。

---

## 6. M2：预算感知（配套，不是核心）

有了 M1，"约束不会静默丢失"已经由清单解决，M2 的价值降为"让 agent 知道自己快没空间了"。

### 6.1 预算投影（锚定法，只估增量）

```python
@dataclass(frozen=True)
class Budget:
    used: int; window: int; ratio: float
    basis: str            # 'measured' | 'projected' | 'estimated' | 'unknown'
    pressure: int | None; sampled_seq: int | None
```

- `pressure` = 最后一条带 usage 的 `assistant/message` 的 `prompt_tokens`（**连它的 seq**）；
- `delta` = 该采样点之后 surface 的增删（用同一次请求的 `request/header.context_estimate` 对照）；
- `used = pressure + delta`；**只估增量、总量锚在 provider 实测**，误差不累积；
- `window` 从 `request/header.context_window` 读（换模型自动跟上），常量兜底；
- 无采样 / 采样点已被遮蔽 → `basis='unknown'`，**不要用 0 或全量估算假装知道**。

### 6.2 每请求卡点 + 水位状态栏

- 压缩检查挂 `pre_step`（每请求，同步），`turn/end` 降为兜底，`request_error` 溢出恢复保留；
- 状态栏走**注册制 runtime contributor**（todo 与 context 各一个），审计字段从
  `todo_status` 改为 `runtime_status: {name: 原文}`；
- **水位触发**：`tight ≥ 0.7`、`critical ≥ 0.9`，低于水位不叠（零成本）；
- 规则与事实分层：**"该收尾"是 system 的通用规则**（`discipline` 的 Convergence），
  **"什么时候算紧张"是运行时事实**（状态栏给）。

### 6.3 `discipline` 新增 Convergence 条款

> An open-ended request still has to converge: investigate until the question is answered,
> then answer — don't keep widening scope. When the runtime tells you the context is tight,
> converge: state conclusions, record what the next window needs, and stop starting new work.

---

## 7. M3：模型发起的窗口切换 + 笔记

### 7.1 `new_context`（工具）

- **无参数**（对齐 Codex `new_context`：*"Start a new context window. Does not clear,
  reset, or otherwise affect environment state."*）；
- **选段权在代码**（模型看不到 seq，指定区间只会猜错）；
- **闸门**：一个回合最多一次（第二次 → `is_error: new-context-already-used`）——
  能无限重置上下文的模型可以靠"重开"假装收敛（issue #2 的逃逸口）；
- **因果完整**：选段边界落 step 边界 + 保住当前 step → surface 顺序是
  `[checkpoint][assistant: 调了 new_context][tool_result: 已切换]`；
- **对人类可见**：新增 **`context/switch`** 事件 `{trigger: model|auto|manual, compaction_id,
  range, freed_tokens_estimate}`，前端画分隔线并给 checkpoint 卡标注来源；
- **两种形态**：默认"模型自写交接 = checkpoint"（安全版）；**M1 落地并验证之后**，
  再评估 Codex 式的"纯重置、不摘要"（Codex 敢这么做，前提正是它有 history 工具）。

### 7.2 笔记

- **载体 = 工作区文件**（跨窗口唯一可续的载体）；约定**非隐藏**目录 `notes/`
  （隐藏目录被 `sandbox.iter_files` 跳过，`glob`/`grep` 发现不了——下个窗口的模型会
  "不知道自己有笔记"）；
- **索引注入 > 搜索**：`notes/INDEX.md`（每条一行：标题 + 路径 + 一句话）在窗口切换时注入，
  正文按需 `read_file`——**与技能目录（`skill:catalog`）同一个模式**；
- **不加新工具**（`write_file`/`edit`/`read_file`/`grep` 已覆盖读写查）。

---

## 8. 不变式对照

| 不变式 | 本设计怎么满足 |
|---|---|
| ① 没有状态不进日志 | 清单/检索面/预算全是**投影**（纯函数 fold），不新增状态；压缩与切换走 `session.append` |
| ② 模型可见 ⟺ 可重建 | 清单 = fold 日志；明细 = 日志渲染；审计原文进 `request/header.runtime_status` |
| ③ 入队即记账 | 不涉及（inbox 不动） |
| ④ 决策走钩子/声明 | 每请求压缩检查挂 `pre_step`；状态栏走注册；工具走 `ToolRegistry` |
| ⑤ 失败降级为结果 | 读不存在的会话/回合、超限、路径非法一律 `is_error`；压缩失败落 `compaction/end{error}` 并放行 |

---

## 9. 工程量与分期

| 档 | 内容 | 工期 | 换来什么 |
|---|---|---|---|
| **A 最小可用** | 检索面投影 + L0 状态栏 + `session_manifest` / `read_turn` + checkpoint 指针（§4 全部） | ~2 天 | agent 能"回到原文"；**还没有数字证明** |
| **B 可信（推荐）** | A + `make_controlled_session.py`（注入 fact + 真压缩）+ `run_endtoend.py`（R2/R3 双臂，R1 抽几题校验上限） | ~4 天 | **主结论**：探针任务成功率 R2 → R3 |
| **C 研究级** | B + 四类探针全覆盖 + 每题重复 ≥5 次 + 消融（清单去掉结论摘录）+ 多后端 | +1~2 周 | 学术级严谨；对作品边际收益小 |

**实施顺序（端到端口径定下来之后的顺序）**：

```
①  M1 实现（A）——不实现就没法端到端跑，这是硬前提
②  受控构造器（注入 fact + 触发真实压缩 + 三道标签校验）
③  端到端 runner（R2 vs R3，判分 + 过程指标）→ 主结论表
④  M2（预算感知）→ M3（new_context + notes）
```

组件诊断（`build_navigation.py` / `run_selection.py`）**随时可跑**：主指标掉了时用它定位
是"没去查"还是"查了挑错回合"。它便宜（每题 1 次调用），但不单独作为结论。

每步三绿（`ruff`/`mypy`/`pytest`）后提交，并同步 `ARCHITECTURE.md` / `AGENTS.md` /
`NEXT_STEPS.md` / `README.md` / `web/PROJECTION_DESIGN.md`（写通用规则，不写针对本次事故的补丁）。

---

## 10. 待定问题

1. **探针任务从哪来**：手写（可控、但可能不自然）还是让模型按 fact 生成（自然、要校验）？
2. **受控会话怎么造**：直接 `session.append` 事件（完全可控）还是真跑 agent（更真实、难控）？
   （建议：主集用前者，抽几题用后者验证真实性）
3. **R2 的"不注册工具"怎么实现**：改 `build_tools` 的注册集合，还是用一个开关？
4. **每题重复几次**：端到端方差大，2 次够不够？（C 档定 ≥5）
5. **`read_turn` 的粒度**：默认整个回合（p95 10 万字符）还是"回合头 + 步骤目录"再按需展开？
   （§4.4 倾向后者）
6. **清单投放**：只在窗口切换注入一次，还是每轮在消息尾部常驻截断版？
7. **`context/switch` 事件 vs 给 `compaction/*` 加 `author` 字段**（§7.1 选了前者，确认）
