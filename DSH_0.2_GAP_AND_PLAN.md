# DSH 0.2 差异对照与改造建议（供 agent 执行）

> **读者**：接手本仓库改造的 AI agent。
> **执行方式**：按 P0 → P1 → P2 顺序做，每项独立提交、独立验收；不要跨项合并。
> **本文性质**：**执行计划**，不是设计规范。落地后的规则按本仓库五层分工提炼
> （见 `docs/AGENTS.md`）：判据一句话进 **L1**（根 `AGENTS.md`），长理由进 **L2**（`docs/notes/`），
> 现状描述进 **L3**（`docs/*.md`、`docs/subsystems/`），进度进 `NEXT_STEPS.md`。

---

## 0. 背景与证据基线

- **本仓库（MyCoder）**：`E:\codeall\agent-demo`，架构是 **deepseek-harness 0.1.x 的复刻**（2026-08/09 期间抄的）。
- **母本（DSH）**：`E:\codeall\deepseek-harness`，当前 HEAD = **0.2.0-rc.2**（从 0.1.2-alpha.5 起跨 **5,552 个提交 / 588 个主线合并**）。
- **结论一句话**：本仓库精确停在 DSH 0.1.x 的位置；本次落差分三级——
  **A 级**（改变了架构形状，值得补）、**B 级**（能力广度，按目标决定）、**C 级**（本仓库持平甚至更好，不要动）。

### ⚠️ 基线复核（2026-09-29 仓库重组后，执行前必读）

本文写于重组之前，**路径与部分结论已过期**。

**① 文档已重组**（commit `5429705 docs: 设计文档统一到 docs/`）。本文里的旧路径一律按下表换算；权威对照表在 `docs/AGENTS.md` §七：

| 本文引用的旧路径 | 现在在哪 |
|---|---|
| `ARCHITECTURE.md` | `docs/architecture.md`（L3 现状长文） |
| `agent.md` | `docs/prior-art.md`（L3） |
| `CONTEXT_BUDGET_DESIGN.md` | 拆成三篇设计记录：`docs/notes/implemented/feature/2026-09-19-context-recall.md`（机制 + agent 供给面）· `docs/notes/implemented/testing/2026-09-19-context-recall-evaluation.md`（验收方法学）· `docs/notes/implemented/bug-fix/2026-09-29-silent-render-truncation.md`（渲染截断） |
| `web/PROJECTION_DESIGN.md` | `docs/subsystems/web-projection.md` |
| `eval/recall/RENDERING_SAMPLE.md` | 不再入库，输出到 `.eval/recall/RENDERING_SAMPLE.md` |

**② 分工已升级为五层**（`docs/AGENTS.md` 第一节）：L0 入口（`README.md`/`USAGE_zh.md`）→ L1 规范（根 `AGENTS.md`，**≤8000 字符注入预算**）→ L2 设计记录（`docs/notes/{lifecycle}/{class}/`）→ L3 现状长文（`docs/*.md`、`docs/subsystems/`）→ L4 证据（`.eval/**`，不入库 + note 里写复现命令）。本文第 7 节说的"文档四方分工"按这张表理解。

**③ 已完成项（不要重做）**

| 项 | 状态 | 证据 |
|---|---|---|
| P1-3 渲染静默截断 | ✅ **已完成** | commit `54db8c8 fix(recall): 渲染不再静默截断（按行分页）+ 补三段提示词供给面`（同 commit 增 204 行测试） |

**④ 已被仓库决策掉的项（本文相应调整）**

| 项 | 仓库的决定 | 调整 |
|---|---|---|
| P0-1 `.eval/` 入库 | **判据 = 重建成本**：要花 API 才能重建的（`eval/recall/questions.jsonl`、`navigation.jsonl`）入库；零成本能重跑的（审计表、情境档案、汇总表、渲染样张）**不入库**，note 里只留结论 + 复现命令 | P0-1 已改写为"**核对结论是否落在 note 里**"，不再是"把 `.eval/` 搬进仓库" |
| 双语 sidecar / 冻结归档清单 | **明确不抄**（`docs/AGENTS.md` 开头："只抄结构，不抄它的双语 sidecar 与冻结清单机制"） | §5、§6.4、P2-5 不再建议这两项 |

**⑤ 数字必须现场重测**：本文引用的 `175 / 85 / 182` 个测试**均已过期**（重组后：测试文件 **18** 个、`def test_` 约 **128** 个）。P0-2 的正确做法是"脚本现场测量并断言文档一致"——**不要把本文的数字抄进任何文档**。

### 复核本文所有证据的命令

```sh
# demo 侧
grep -rn "request/header" my_coder --include=*.py | wc -l        # 期望 12
grep -rn "assistant/chunk" my_coder --include=*.py | wc -l       # 期望 8
grep -rn "system/message\|assistant/attempt\|prepare_call" my_coder --include=*.py | wc -l   # 期望 0
grep -n "SURFACE_EVENT_TYPES" my_coder/state/session.py          # 期望 3 类
head -1 .sessions/main.jsonl                                      # 期望第一行直接是事件（无会话头）
grep -c "^def test_\|^    def test_" tests/*.py | awk -F: '{s+=$2} END {print s}'
git ls-files .eval | wc -l                                        # 期望 0（未入库）

# 母本侧（DSH 仓库内）
grep -n "SESSION_FORMAT_VERSION = " packages/core/session/src/types.ts        # 期望 = 4
sed -n '94,100p' packages/core/session/src/types.ts                            # SessionHeader.version
sed -n '439,445p' packages/core/session/src/types.ts                           # SurfaceEventType 成员
ls .agents/notes/implemented/architecture/2026-08-10-session-log-version-mechanism.md
ls .agents/notes/implemented/architecture/2026-09-01-v2-embedded-assistant-streams.md
ls .agents/notes/implemented/architecture/2026-09-02-system-prompt-as-surface-node.md
```

### 必读的母本资料（按顺序）

| 资料（DSH 仓库内相对路径） | 读它解决什么 |
|---|---|
| `.agents/notes/implemented/architecture/2026-08-10-session-log-version-mechanism.md` | 会话日志版本机制的全部理由（**P0-3 的权威依据**） |
| `docs/session-format-status.md` | 写入者版本 vs 已发布版本的权威记录（当前 writer=4 / released=3） |
| `.agents/notes/implemented/architecture/2026-09-01-v2-embedded-assistant-streams.md` | assistant 流尝试的持久化设计（P1-1） |
| `.agents/notes/implemented/architecture/2026-09-02-system-prompt-as-surface-node.md` | 系统提示进表面的完整设计（P3 项参考） |
| `docs/architecture.md` | 母本当前 turn flow（"Turn flow" 一节） |
| `packages/core/session/src/types.ts` | 事件类型、`SessionHeader`、`SurfaceEventType` 的现行定义 |
| `.agents/notes/proposed/feature/2026-07-06-recallable-compaction.md` | 召回式压缩**提案（未实现）**——见 §6.4 |
| `packages/session-query/session-query/README.md`、`session-query-sqlite/README.md`、`tool-session-query/README.md` | DSH 0.2 **已落地**的会话历史检索路线（§6.4 的对照面） |

---

## 1. 逐机制对照表（含证据）

| # | 机制 | 本仓库现状（证据） | DSH 0.2 现状（证据） | 级别 | 建议 |
|---|---|---|---|---|---|
| 1 | 表面事件类型 | `state/session.py:22`：`{user/message, assistant/message, tool/result}` = **3 类** | `types.ts:439`：`system/message, developer/message, user/message, assistant/message, tool/result` = **5 类** | A | P3（见 §6.1） |
| 2 | 系统提示存放 | 提示 = sections 拼接，走请求/header（同 DSH 0.1） | `system/message` 是**表面节点 0**，`deriveMessages()` 折出来即 wire 消息 0 | A | P3（见 §6.1） |
| 3 | `request/header` | ✅ 有（12 处命中） | ✅ 有，但 **`EpochHeader` 不再含 `system` 字段** | — | 随 P3 联动 |
| 4 | 流式持久化 | `assistant/chunk` 独立事件（8 处命中） | v2：`assistant/message` **内嵌紧凑 timed stream** | A | P3（见 §6.2） |
| 5 | 失败尝试留档 | ❌ **0 处**（失败/重试/取消在日志里不留痕） | `assistant/attempt`：log-only，保留尝试但**不进模型历史** | A | **P1-1** |
| 6 | 路由准备与取消原子性 | ❌ 无 `prepare_call`（0 处） | `agent/request` → `prepareCall()`；取消时 **system 与 users 都不提交** | A | **P1-2** |
| 7 | 进程内流帧 | ❌ 0 处 | `agent/assistant-stream` 的 start/chunk/end 帧（唯一远程消费者是 Web Session-follow） | B | 暂不做 |
| 8 | 日志格式锚点 | ❌ **无会话头、无版本字段**；`.sessions/main.jsonl` 第一行直接是事件 | `SessionHeader.version`（`types.ts:99`）+ `SESSION_FORMAT_VERSION = 4`（`types.ts:89`）+ 方向感知拒绝 | A | **P0-3** |
| 9 | 未知事件守卫 | `ignorable` **字段有**（`values/messages.py:228/345/359`）但**读取端零检查** | `KNOWN_SESSION_EVENT_TYPES` 白名单 + 默认"必读"（未知且无 `ignorable` → 拒绝重建） | A | **P0-3** |
| 10 | 投影接缝 | `runtime_status` 注册制贡献者（已有，很好） | `ctx.sessionProjections` **强制**：host 读取者必须 require 或显式失败 | B | 暂不做 |
| 11 | 插件消息投影 | ❌ 无 | 插件可注册 pure message projections 改现有消息内容 | B | 暂不做 |
| 12 | Agent 创建初始化 | ❌ 无 | `agent/created` 串行初始化；失败回滚创建 | B | 暂不做 |
| 13 | 沙箱 | **纯用户态路径边界**（README 已如实声明"不是 OS 级沙箱"） | OS 级：bwrap / Landlock / Seatbelt / Windows 受限令牌 + fail-closed + 方言签名 | B | **不做**（理由见 §7） |
| 14 | 多 agent | ❌ 无（单 agent） | subagent（4 类 provider）+ workflow 扇出 + Ralph + agent-team | B | **不做** |
| 15 | 能力接缝 | 钩子 + 注册声明（够用） | Service Definition / Provider / Consumer 三件套，全能力可换 | B | **不做** |
| 16 | 形态 | CLI + Web | web / headless / sdk / sdk-minimal / acp + 桌面 Electron | B | **不做** |
| 17 | 召回设计 | **L0/L1/L2 三层 + 三臂评测**（本仓库原创，实测后明确不抄 DSH 的 FTS5） | session-query（FTS5 检索路线） | **C** | **保持** |
| 18 | 会话自愈 | **自研**（悬空工具调用补合成结果） | `repair.ts`（torn JSONL 恢复），方向一致 | **C** | **保持** |
| 19 | 三态探测哲学 | 明确成规则："不要把'不知道'降级成'没有'" | 分散在 `docs/defensive-patterns.md` | **C** | **保持** |

---

## 2. P0 — 立刻做（成本极低，收益最大）

### P0-1 核对"结论是否落在 note 里"（L4 证据政策已定，**不要**再把 `.eval/` 搬进仓库）

**仓库已有的决定**（`docs/AGENTS.md` §七末）：生成物入库判据 = **重建成本**——要花 API 才能重建的
（`eval/recall/questions.jsonl`、`navigation.jsonl`）入库；零成本能重跑的（审计表、情境档案、汇总表、
渲染样张）**不入库**，note 里只写结论 + 复现命令。**这条决定是对的；P0-1 不再是"把 `.eval/` 入库"。**

**剩下的真实风险**：结论是否真的落在库里？评审者**不花 API** 能不能判断"这套召回有效"？

**改动（核对，不是搬运）**

1. 确认 `docs/notes/implemented/testing/2026-09-19-context-recall-evaluation.md` 里有：
   三臂设计（R1/R2/R3）、判据分类、**关键结论数字**、以及**复现命令**（完整形式，含
   `conda run -n agent-demo …`、是否需要 `DEEPSEEK_API_KEY`、大致预算与耗时）。
2. 若关键结论只存在于 `.eval/**` 的生成物里，把它们**摘要进那篇 note**（数字 + 命令），
   **不要**把生成物的表格内容抄进去（`docs/AGENTS.md` §四.3）。
3. 核对工具链已入库：`git ls-files eval` 应非空（`eval/recall/*.py`）；缺生成器就补。
4. 可选：若要评审者**零 key 复核**，可把某一次冻结运行的 `RESULTS.md` **结论段**（不是 raw JSONL）
   入库，并在 note 里注明"这是某次运行的快照，重跑会变"。

**验收**

```sh
git ls-files eval | wc -l                     # > 0：工具链与题库在库里
grep -n "conda run" docs/notes/implemented/testing/2026-09-19-context-recall-evaluation.md
```

---

### P0-2 数字校验脚本 + 清 5 处漂移

**现状证据**

> ⚠️ 仓库已在 2026-09-29 重组，本文写时的数字**全部过期**。执行时**一律现场测量**，
> 不要把本文（或任何文档）里的数字直接抄走。

写本文时观察到的**漂移类型**（具体值以现场测量为准）：

| 漂移类型 | 写本文时的实例 | 现在去哪查 |
|---|---|---|
> ⚠️ **表中数字是 2026-09-29/30 的基线，执行前请重新测量**（`conda run -n agent-demo python -m pytest --collect-only -q | tail -1`、`grep -c "^### 3\." docs/architecture.md`、`git ls-files 'my_coder/**/*.py' | xargs wc -l`）——照抄过期数字去"修正"文档，会把对的改成错的。

| 测试数不一致 | `AGENTS.md`/`README.md` 说 175；`NEXT_STEPS.md` 说 85；**2026-09-30 实测 200 passed / 3 skipped** | `grep -rn "个测试\|passed" AGENTS.md README.md NEXT_STEPS.md docs/` |
| 机制数 | `README.md` 说"21 个核心机制"，实际 `§3.1–§3.22` = **22** | `grep -c "^### 3\." docs/architecture.md` |
| 代码行数 | `docs/architecture.md`（旧 `ARCHITECTURE.md`）说"约 1000 行"，实测 `my_coder` **7,673 行** | 现场统计 `my_coder/**/*.py` |
| 过期无标注 | `docs/prior-art.md`（旧 `agent.md`）里 "pytest 131 passed" | `grep -rn "passed" docs/prior-art.md` |

**为什么做**

数字自相矛盾是评审者最容易抓的破绽；且 `NEXT_STEPS.md:28` 已经明确承诺过同步，属于"承诺失效"。

**改动**

新增 `tests/test_doc_numbers.py`：

1. 真实测试数：`subprocess` 跑 `python -m pytest --collect-only -q`，解析条数。
2. 真实行数：统计 `my_coder/**/*.py`、`web/index.html`。
3. 断言文档中出现的 `N passed` / `N 行` / `N 个核心机制` 与实测一致（或重构为
   "文档从单一来源引用 + 脚本生成"）。
4. **反静默通过**设计：学 `tests/test_architecture.py:102-110` 的 `assert scanned >= 30`——
   脚本必须先证明"确实扫到了 N 个待校验点"，否则"0 个待校验点"会让门无声消失。

**验收**

- 故意把某个数字改错 → 测试必须红。
- 5 处漂移全部修正；顺手把 `NEXT_STEPS.md:28` 那句失效承诺改成"由 `tests/test_doc_numbers.py` 机械保证"。

---

### P0-3 日志格式锚点（`formatVersion` + 方向感知拒绝 + 用上 `ignorable`）

**现状证据**

- `.sessions/main.jsonl` 第一行：
  ```json
  {"seq": 0, "time": 1790140299.11, "type": "agent/inbox/spliced", "data": {...}, "surface_op": null, "shadowed": null, "ignorable": false}
  ```
  **没有会话头、没有版本字段。**
- 全代码搜 `version` / `SESSION_FORMAT` / `KNOWN_EVENT` / `unsupported` → 与日志无关的命中之外，**0 处**。
- `ignorable` 字段存在（`values/messages.py:228` 定义、`:345` 序列化、`:359` 反序列化），但**没有任何读取端检查**——是"声明了但没人用"的死字段。
- 存量数据：`.sessions/` 下 4 个文件、约 **63MB**。

**母本机制（DSH 在 0.1 时代就有，不是 0.2 新增）**

三条设计原则（摘自 `2026-08-10-session-log-version-mechanism.md`）：

1. **一个单调整数，不分主次版本**——"能否自动升级"是那一步 upgrader 是否存在的属性，不该由版本号形态预先承诺。
2. **写入者决定 bump，不是读取者**——判据不是"能否解析"，而是"旧运行时还能否**语义正确地**处理"；只有结构性变化（头结构、事件信封、核心事件语义、表面机制）才 bump。**拿不准就 bump**：近乎恒等的 upgrader 几乎免费，漏 bump 会静默毁掉旧读取者。
3. **按方向读**：
   - 相等 → 正常读
   - **日志版本 > 读取器 → 拒绝**（指明"这是更新版本写的，请升级"，并给出原始日志路径让用户至少能看文本；用独立异常类型，与"损坏"区分，因为什么都没坏）
   - **日志版本 < 读取器 → 在内存里跑完相邻迁移链**，源文件字节 / inode 不动

配套第二轴：**未知事件默认"必读"**。不认识的、又没标 `ignorable: true` 的事件 → 拒绝重建。
理由（原文）："忘标记 → 过度拒绝（麻烦）"远好于"默认忽略 → 静默恢复被掏空的会话（安全事故）"。
**只有结构变化才 bump 版本；词汇增长靠 `ignorable` 标记，不 bump。**

**改动（建议实现）**

```python
# values/messages.py（或 state/session.py）
SESSION_FORMAT_VERSION = 1          # 只在结构性变化时 +1

# 新增会话头事件（新建会话时写第一行）
{"type": "session", "formatVersion": SESSION_FORMAT_VERSION,
 "id": session_id, "workspace": workspace, "time": now}

# 新增异常（与"损坏"分开）
class SessionFormatUnsupportedError(Exception): ...

# 读取：首行必须是会话头；无头老文件 → 视作 v0
```

步骤：

1. `SESSION_FORMAT_VERSION` 常量 + 会话头事件类型。
2. 新建会话时先写会话头；读入时解析版本：
   `> 当前` → 抛 `SessionFormatUnsupportedError`（消息含"升级"方向 + 文件路径）；
   `== 当前` → 正常；`< 当前` → 走迁移函数（本期可先 `NotImplementedError` 占位，因为尚无旧版本）。
3. **未知事件守卫**：维护 `KNOWN_EVENT_TYPES`（从现有全部事件类型汇总），
   读取时未知且 `ignorable != True` → 拒绝重建。
4. **同步所有读取方**（会话头会改变"第一行是事件"的隐含契约）：
   `state/session.py`（surface 判定处约 `:92`）、`show_memory.py`、`web/` 会话列表、
   `.eval/` 下的重放/审计脚本、`eval/recall/render_sample.py`。

**验收（契约测试，逐条钉死）**

| 用例 | 期望 |
|---|---|
| 版本相等 | 正常读，事件数不变 |
| 日志版本 > 读取器 | 抛 `SessionFormatUnsupportedError`，消息含"升级"与文件路径 |
| 无版本头的老文件 | 按 v0 正常读（不需要迁移） |
| 未知事件 + `ignorable=false` | 拒绝重建（明确报错） |
| 未知事件 + `ignorable=true` | 跳过该事件，其余正常重建 |
| 会话头存在时 | `derive_messages()` 输出与加头之前**逐字节一致** |

---

## 3. P1 — 架构正确性 + 本仓库已登记的债务

### P1-1 `assistant/attempt`：失败/重试/取消尝试留档

- **现状**：0 处；失败与取消的尝试在日志里不留痕。
- **母本依据**：`2026-09-01-v2-embedded-assistant-streams.md`——每次尝试要么结算为
  `assistant/message`（成功，内嵌流），要么结算为 `assistant/attempt`（失败/重试/取消/流错误，
  **log-only，不进模型历史**）；硬进程丢失在结算前 → 无尝试流可恢复（已知代价）。
- **为什么**：失败可见性是**架构级可观测性**——长任务需要回答"它试了几次、为什么失败"。
- **改动**：事件类型 + `runtime/loop.py` 的异常/取消分支各落一条；`derive_messages()` 明确忽略该类型。
- **验收**：制造一次模型失败与一次取消 → 日志各出现 `assistant/attempt`；
  `derive_messages()` 输出不变；`--resume` 正常。

### P1-2 `prepareCall` 取消原子性

- **现状**：无 `prepare_call`；请求准备与用户消息提交的边界不明确。
- **母本依据**：`docs/architecture.md` 的 turn flow——`agent/request → prepareCall`；
  **取消发生在任一异步阶段时，system 与 users 都不提交**。
- **为什么**：半提交会产生"日志里有请求头/用户消息，但请求从未发出"的错位状态，
  破坏"模型可见 ⟺ 可重建"。
- **改动**：`runtime/loop.py` 把"组装 → 提交 user 消息"放进同一事务边界；
  取消时回滚（或改为"先 prepare 完成、再一次性提交"）。
- **验收**：在 prepare 阶段取消 → 该 step 无新增 `user/message`（或明确记录取消边界事件）。

### P1-3 单块截断静默 → 按设计规则明说 ✅ **已完成（不要重做）**

**完成证据**：commit `54db8c8 fix(recall): 渲染不再静默截断（按行分页）+ 补三段提示词供给面`
（`my_coder/app/recall.py`、`my_coder/tools/recall.py`、`tests/test_recall.py`，共 +395/−70，
其中测试 +204 行）；决定已入档
`docs/notes/implemented/bug-fix/2026-09-29-silent-render-truncation.md`。

**遗留核对（可选，成本很小）**：`eval/recall` 的审计脚本是否已识别**新的**截断标记——
否则"真损失"分类会把这些仍算作渲染器缺陷。

### P1-4 题库可复现性缺口

- **现状**：`docs/notes/implemented/testing/2026-09-19-context-recall-evaluation.md` 自记（原 `CONTEXT_BUDGET_DESIGN.md:645`）：`questions.jsonl`（36 条）与
  `navigation.jsonl`（13 条）的生成/冻结方式未闭环。
- **改动**：把生成脚本 + 版本号 + 校验入库；或冻结为不可变快照并加校验测试。
- **验收**：`git ls-files` 能看到题库与生成器；校验测试能拒绝被手改的题库。

### P1-5 收敛压力（开放任务回合不收敛）✅ **已完成（2026-09-30，不要重做）**

> 落地：`ToolSpec.cacheable` 声明 + `my_coder/state/progress.py`；设计与证据见 `docs/notes/implemented/feature/2026-09-30-tool-convergence.md`。
> 判据是"**自上次变更以来连续只读调用数**"（**不是**总步数——按步数会砍掉合法大任务）；
> 软阈值 8 / 硬阈值 16（收尾步不带工具面），开关 `--readonly-nudge/--readonly-close/--no-convergence`。
> 实测 A/B：请求 9/10→**7/6**、工具调用 12/19→**11/9**，结论质量不降。

- **原记录（勿照做）** · **现状**：`docs/prior-art.md` §4 问题 3（**已量准并落地**，见 P1-5）——单回合模型连续 15+ 次请求、24 次工具调用才收敛。
- **为什么**：这是**体验问题**，比工程问题更影响观感（也影响 token 成本）。
- **建议方向**（供决策，勿直接照抄）：在 system 的 `discipline` 段增加"何时够了先汇报"的
  收敛条款；或对开放任务加 step 预算的软提醒。
- **验收**：同一条开放任务实测请求数下降，且答案质量不降（记录前后对比与 token 数）。

---

## 4. P2 — 防腐（文档工程）

| # | 事项 | 做法 | 验收 |
|---|---|---|---|
| P2-1 | 文档路径 + `§N` 交叉引用检查 | 脚本提取文档中的路径与 `§N` 引用，断言存在（本仓库互引很密） | 故意写错路径 → 脚本红 |
| P2-2 | 主文档字数上限 | 学 DSH `scripts/doc-budgets.manifest.json`：给 5 份主文档设上限 | 超限 → 脚本红 |
| P2-3 | 术语表 | 新建 `GLOSSARY.md`：22 个机制 + surface / 投影 / 钩子 / seat 等核心词 | 与 ARCHITECTURE §3 互链 |
| P2-4 | 恢复 CI | 只跑：文档校验（P2-1/2）+ ruff + mypy + pytest | 见 `NEXT_STEPS.md:551-553` 记录的移除原因，先解决跨版本 glob 语义问题 |

> ⚠️ **P2-2 部分已完成**：`docs/AGENTS.md` §五 已给 L1（根 `AGENTS.md` ≤8000 字符，**门禁强制**）
> 与单篇 note（≤300 行，未强制）设了预算；**未覆盖 L3 长文**（`docs/architecture.md` 885 行、
> `docs/prior-art.md` 985 行）——补预算时只管 L3。
> **P2-3 术语表**：注意与 L3 分工（`docs/AGENTS.md` §一）——术语表属 L3，不要塞进 L1。

### P2-5 Agent Notes 制度补齐（本仓库已实现 ~90%；三个门禁缺口 + 一个已拍板 A）

**为什么值得补**：这套制度是本仓库最有说服力的工程资产之一，而现在它的**门禁强度低于它的规则文本**——
规则写在 `docs/notes/README.md` 与 `docs/AGENTS.md` 里，但最容易漂的两条没有门禁。
（本仓库自己已经证明过这个道理：`tests/test_docs.py` 的 docstring 写着"规则写在 AGENTS.md 里并不等于
agent 看得到"，并因此把注入预算变成了门禁。）

**已做对、不要动**：路径两轴 `docs/notes/{lifecycle}/{class}/yyyy-mm-dd-主题.md`、闭集六类、
头三行（`# Agent Note: ` / 空行 / `Status: `）、两套骨架（`implemented/` 禁提案语域）、
`rejected` 强制带原因、相对链接可解析、不建集中索引、`_template.md`、README 规则、
以及 `tests/test_docs.py` 的四条门禁。**其中有一条是 DSH 没有的原创**——
`test_agents_md_fits_the_system_prompt_budget`（"渲染后的注入文本不许被截断"），保留并在文档里强调。

**母本规则**：DSH `.agents/notes/README.md`（`Alternatives considered — mandatory` 与
`Archiving and deletion` 两节）+ `scripts/verify-agent-note-format.ts`。
**本仓库已明确不抄**（`docs/AGENTS.md` 开头："只抄结构，不抄它的双语 sidecar 与冻结清单机制"）：
双语 `.zh.md`/`.i18n.yaml`、`archived/` 冻结树 + manifest + hash —— **这两项不必补**。

#### P2-5a 门禁强制 `## Alternatives considered`

- **现状**：6 篇 note 都手写了这一节，但门禁对它 **0 处检查**（`grep -c "Alternatives" tests/test_docs.py` = 0）
  → 下一篇漏写不会被拦。
- **母本理由**（原文）：*"Every Agent Note carries an `## Alternatives considered` section… A decision
  recorded without what it beat invites re-litigation — the failure Agent Notes exist to prevent."*
- **改动**：在 `test_note_header_and_body_skeleton_match_the_lifecycle` 加断言——每篇正文必须含
  `## Alternatives considered`；错误信息点名"记录被否决的方案是 note 存在的首要理由"
  （对齐 `docs/AGENTS.md` §四.2）。
- **验收**：临时删掉任一篇的该节 → 门禁红。用行首匹配（`'\n## Alternatives considered'`）以免
  误匹配正文里的提及。

#### P2-5b 门禁校验正文里反引号包住的仓库路径

- **现状**：门禁只查 markdown 链接 `[...](...)`；正文里大量 `my_coder/app/recall.py`、
  `tests/test_docs.py` 这类**反引号路径**不受校验。
- **为什么**：`docs/notes/README.md` §四 要求"`implemented/` 的 note 必须跟着代码事实更新"，
  而最常见的漂移就是**文件改名 / 包改名之后正文路径没改**。
- **改动**：正则提取 `` `…` `` 中形如 `[\w./-]+\.(py|md|json|toml|yml|html|js|css)` 且含 `/` 的
  仓库相对路径，断言 `(REPO / p).exists()`。
  **排除**：以 `http`/`~`/`/`/`.` 开头；含空格或 `{}<>*$`；`.sessions/`、`.eval/`、`.pytest_tmp/`；
  纯命令片段（内含空格）。
- **反静默通过**：先 `assert scanned >= 10`（学 `tests/test_architecture.py` 的做法）——
  否则"一条都没匹配到"会伪装成通过。
- **验收**：把某篇 note 里的 `my_coder/app/recall.py` 改成不存在的路径 → 门禁红；扫描计数 > 0。

#### P2-5c README 补"删除 vs 保留"规则（轻量版，**不建归档树**）

- **现状**：只有一句"被完全取代可合并删除"；没有"什么时候可以删一篇 implemented note"的判据。
- **母本规则（轻量化后）**：
  - **可删**：只描述小型 UI 调整或纯机械改动的 implemented note（连入链一起修）。
  - **保留**：它的"被否决方案 / 所有权边界 / 否定性保证 / 持久化或 wire 语义 / 安全规则 /
    复活条件"仍有用时，留着。
  - **删除前置条件**：必须先保住独一无二的理由、被否决方案、后果、验证方式（README 已有这句，保留）。
  - **部分取代不算**：保留两篇并互链（**这条现在没有，补上**）。
- **明确不做**：`archived/` 冻结树 + manifest + hash（DSH 为约 800 篇笔记设计；本仓库 6 篇，
  重机械不划算）。把这个取舍与**触发条件**写进 README，例如"implemented 超过 ~30 篇、
  且真的出现'理由已无指导价值但仍有历史价值'的实例时再评估"。
- **验收**：README 有该节；门禁无需改动（这是策略文本）。

#### P2-5d `rejected/` 的语域 → **已决定 A**（作者 2026-09-30 拍板：保留提案语域）

**决定**：`rejected/` 是**被冻结的提案**，保留 `## Proposal`（可以保留提案期的
`## Acceptance criteria` / `## Plan`）；判决只写在 `Status: rejected — <原因>` 行上。
理由：本仓库自己把 `## Decision` 定义为"现在时、陈述**已发布**的事实"（`docs/AGENTS.md` §四.6），
而被否决的方案从未发布；且 rejected 的价值恰恰是**完整保留"当初想做什么"**。

**要改三处**

1. **门禁**（`tests/test_docs.py` 的 `test_note_header_and_body_skeleton_match_the_lifecycle`）：
   现在是一条 `assert lifecycle == 'proposed' or '## Decision' in body`（`tests/test_docs.py:74`），
   改成按 lifecycle 分别要求：
   - `proposed/` → 必须有 `## Proposal`
   - `implemented/` → 必须有 `## Decision`，且**禁**提案语域（现有 4 个禁词的检查只作用于这类）
   - `rejected/` → 必须有 `## Proposal`（允许同时保留 `## Acceptance criteria` / `## Plan`）
2. **那篇 note**：`docs/notes/rejected/feature/2026-09-19-llm-selected-turns.md` 把 `## Decision`
   改成 `## Proposal`，正文时态/人称相应调整（**内容与结论不动**，`Status:` 行不变）。
3. **README**：`docs/notes/README.md` §三 现在只给了 `proposed/` 与 `implemented/` 两套骨架，
   补上 `rejected/` 的说明（冻结的提案 + 判决在 Status 行）。

**验收**

```sh
conda run -n agent-demo python -m pytest tests/test_docs.py -q          # 绿
grep -rn "^## Proposal" docs/notes/rejected/                            # 命中
grep -rn "^## Decision" docs/notes/rejected/                            # 无命中
```

### P2-6 提交信息与 PR 的模板（**本仓库目前完全没有**）

**现状**：本仓库**没有 `.github/` 目录**——没有 PR 模板、没有 Issue 模板、没有任何 PR 自动化。
提交信息已经接近 Conventional Commits（`feat(eval):`、`fix(recall):`、`docs:`、`chore(recall):`），
但有例外（如 `自审（第二轮）：修四个缺陷 + 一处文档/实现漂移`），而且**没有任何地方写清这条约定**，
更没有门禁——纯靠习惯。

**母本有什么（可直接借鉴的形态）**

| 母本文件 | 作用 |
|---|---|
| `.github/pull_request_template.md` | 三段式：`## Motivation`（一句话 + `Fixes #N` / `Related #N`）、`## Changes`（命令/配置/API/协议/持久化变化 + 用户或模型可观察行为的变化，没有就写 `None`）、`## Testing`（一种方法一个条目 + 可折叠 `<details><summary>Proof</summary>` 放可复核证据） |
| `.github/ISSUE_TEMPLATE/{bug,feature,task}.md` + `config.yml` | 三类 issue 表单 |
| `.github/issue-management/` | **自动化强制**：合格 PR 必须"≥1 条同仓库 Issue 引用 + 恰好 1 个 `kind/*` + ≥1 个 `area/*`"；HTML 注释/代码块里的引用不算；`Fixes/Closes/Resolves #N` 才算关闭引用 |
| `.github/review-ownership/` | 审批策略自动化（本仓库规模不需要） |
| `.agents/skills/`（`dsh-pre-push-checks`、`dsh-merging-stacked-prs`） | 给 agent 的工作流技能：推送前选最小检查集、堆叠 PR 的落地顺序 |

**给本仓库的建议（按五层分工落点）**

**① L1 一句话判据**（根 `AGENTS.md`；注意 ≤8000 字符预算，只放判据）：

> 提交信息用 `type(scope): 主题`（`type ∈ {feat,fix,docs,refactor,test,chore}`）；非平凡改动必须
> 同一个 PR 里附设计记录（`docs/notes/`）；PR 正文按 `.github/pull_request_template.md` 写。

**② `.github/pull_request_template.md`**（新建；本仓库是中文文档，模板也用中文，字段对齐现有习惯）：

```markdown
## Motivation

<!-- 一句话说明要解决的问题，并引用同仓库 Issue：Fixes #N 或 Related #N；没有 Issue 就写原因。 -->

## Changes

<!-- 机制 / 接口 / 持久化 / 配置的变化；没有就写 None。 -->
<!-- 用户或模型可观察行为的变化；没有就写 None。 -->

设计记录：<!-- 链接本次同 PR 附的 note（docs/notes/{lifecycle}/{class}/…）；纯机械改动写"免（机械）" -->

## Testing

<!-- 一种方法一个条目；把可复核证据放进 Proof（命令 + 输出）。 -->

- <!-- 命令或步骤，以及它覆盖的行为 -->

  <details>
  <summary>Proof</summary>

  <!-- 测试输出、渲染样张、截图等 -->

  </details>
```

> "设计记录"一行是本仓库特有的：母本没有这一行，但本仓库的规则是"非平凡改动必须同 PR 附 note"，
> PR 正文正是**证明这条规则被执行**的地方。

**③ `.github/ISSUE_TEMPLATE/`（可选，但本仓库已经在用 issue：#3 / #9 / #19 / #22 / #32 / #39）**：
三个最简表单 `bug.md` / `feature.md` / `task.md` + `config.yml`（`blank_issues_enabled: false`）。
每个表单只问三件事：现象与证据（复现命令）、期望、影响面。

**④ `skills/gh-pr.md`（agent 工作流技能，与现有 `skills/gh-issue.md` 同构）**：
开 PR 的步骤、正文各段写什么、**必须核对设计记录 note 是否存在**、证据要求（只写真跑过的命令）、
以及"不要自行合并"。

**⑤ 可选门禁**：commit message 格式检查适合放 **commit-msg 钩子**，不是 pytest。
本仓库已移除 CI（`NEXT_STEPS.md:551-553`），所以这类检查**只在本地生效**——文档里要写清这一点，
别让读者以为"有门禁"。

**验收**

```sh
ls .github/pull_request_template.md
git log --format='%s' -20 | grep -vE '^(feat|fix|docs|refactor|test|chore)(\([a-z0-9-]+\))?: '   # 期望无输出
```

**明确不做**：`review-ownership` 式审批自动化、堆叠 PR 自动重定位（本仓库单人单线，不需要）。

---

## 5. 明确不做（连同理由，将来要动先看这里）

| 不做的事 | 理由 |
|---|---|
| OS 级沙箱（bwrap/Landlock/Seatbelt/受限令牌） | 成本高、教学价值低；且 README 已**如实声明**"纯用户态路径边界，不是 OS 级沙箱"——**诚实比假装有更值钱** |
| 多 agent（subagent / workflow / agent-team） | 单机教学场景不需要；会稀释"把一件事讲清楚"的核心优势 |
| 能力接缝三件套 / 五形态 profile / PTC 运行时 | 为 316 个包与多发布渠道设计，本规模用不上 |
| 全量双语 | 除非目标改成投英文岗位（那时优先译 README，而不是全量双语） |
| 追赶 DSH 0.2 的全部演化 | 它的多数变更是为"已发布用户数据 + 第三方插件生态"服务的，与本项目处境不同 |
| 把 54MB 会话语料入库 | 仓库会爆；只入库生成器 + 报告 + 题库 |
| **零成本可重跑的生成物入库**（`.eval/**` 的审计表 / 情境档案 / 汇总表 / 渲染样张） | 判据 = **重建成本**（`docs/AGENTS.md` §七末）：要花 API 才能重建的题库入库，零成本能重跑的不入库、note 里只留结论 + 复现命令（P0-1 已按此改写） |
| **双语 sidecar**（`.zh.md` + `.i18n.yaml`）与 **`archived/` 冻结树 + manifest + hash** | 本仓库**已明确不抄** DSH 的这两项（`docs/AGENTS.md` 开头）；6 篇 note 的规模用不上冻结清单与哈希校验。触发条件见 P2-5c |

---

## 6. P3 — 可选（先与作者确认目标再动）

### 6.1 系统提示进表面（`system/message`）

若决定做，母本完整设计见 `2026-09-02-system-prompt-as-surface-node.md`。最小改动路径：

1. `state/session.py:22` 的 `SURFACE_EVENT_TYPES` 加 `system/message`（3 类 → 4 类）。
2. 新投影器（对照母本 `runtime-context.ts` 的 `SystemPromptProjection`）：
   读当前表面上存活的 `system/message` 节点，决定 **append / 精确替换那一个节点 / 不操作**。
3. 首次渲染**即使提示为空也要占位 node 0**（否则提示later变非空时会追加到 user 历史之后，
   角色语义错误）。
4. 请求构造去掉 system 字段：`messages` 的 `derive_messages()` 首项即 wire 消息 0。
5. **node 0 保护**：替换操作覆盖表面节点 0 时，除非替换者本身是正好覆盖该节点的
   `system/message`，否则拒绝——保证 **compaction 永远吞不掉提示**。
6. 收益：单一表示（"模型看到什么"只折表面一处）；提示变更与工具/配置变更在日志里可区分。

### 6.2 内嵌 timed stream（v2）

母本依据 `2026-09-01-v2-embedded-assistant-streams.md`：`assistant/message` 内嵌产生它的
紧凑 timed stream，取代"chunk* + message 分开"的 v1 布局。**做完 P0-3 之后再考虑**——
它是**结构性变化**，正好是版本锚点存在的理由。

### 6.3 会话格式迁移骨架

再做 §6.2 或任何结构变更时：相邻迁移包各负责一步 `vN → vN+1`，已发布代际**永不改名/替换/删除**，
迁移结果**另存为新代际**。母本依据 `2026-08-31-released-session-format-migrations.md`。

### 6.4 召回与压缩：DSH 提案 vs 已落地 vs 本仓库实现

**动手前必读本节**，否则容易犯两个错：以为"DSH 已有 recall 可以照抄"，或"提案未实现所以整条路都不成立"。

#### (a) DSH 提案（**未实现**）

`.agents/notes/proposed/feature/2026-07-06-recallable-compaction.md`（`Status: proposed`）

**核心洞察**：压缩不可逆的根因是**一个产物扮演两个冲突角色**——索引要冻结、按时间顺序、便宜；
工作记忆要全局视野、可重排序、可变。一份摘要两个都做不好。

**三件套**：

1. **冻结索引 checkpoint（stub，~100–200 token）**：陈旧历史按确定性策略切块（累积到
   `chunkTokens`、工具配对平衡、优先回合边界），每块压成一个 stub = 2–3 行"发生了什么"
   + **一行低频字面锚点**（精确错误串 / 值 / 配置键，按类别分组）
   + **代码生成的 footer**：`[checkpoint c<seq>: shadows conversation span #<a>–#<b>; originals retrievable via history_read]`。
   已提交的 stub **永不重写、永不进入后续压缩区间** → 前缀字节稳定 → 缓存命中。
2. **可变状态 checkpoint**：一份工作记忆文档（决策 / 现状 / 约束 / 下一步），位于所有 stub 之后、
   保留尾部之前；每轮由"旧状态 + 本轮新增"重写（O(旧+新)）；被取代的 state 折叠进下一轮首个
   chunk（**无墓碑**）。
3. **召回工具**：`history_read(checkpoint, offset?)`（把任意 checkpoint——含已被取代的——的遮蔽
   区间渲染成 `User:`/`Assistant:`/`Tool result:` 转录，分页 + 游标）、
   `history_search(query, checkpoint?, limit?)`（对全部被遮蔽区间做**字面**扫描，返回片段 +
   checkpoint id + 覆盖元数据 `scanned`/`matched`/`truncated`）。

**配套**：膨胀守卫（压缩后不小于压缩前就**什么都不提交**）、两阶段 pass（并发摘要 → 左到右提交）、
缓存经济学（前缀 miss 从 position-zero 降到 O(新增)）、**无 sidecar 索引**（日志即索引）、
召回产物以普通 `tool/result` 落在上下文尾部（可重建性不变）。

**未实现的复核命令**：

```sh
codegraph query history_search     # 期望 No results
codegraph query stubTokens         # 期望 No results
codegraph query tool-recall        # 期望 No results
grep -rn "shadowed span" .agents/notes | grep -v recallable-compaction   # 期望无输出
ls packages/compaction             # 期望无 compact-recallable
```

#### (b) DSH 已落地的相邻路线（**已实现**）：`packages/session-query/`

四个包：服务 `session-query`、后端 `session-query-sqlite`（**FTS5 字面短语检索**）、
模型工具 `tool-session-query`、`session-log-export`。

模型可见的 5 个只读工具：

| 工具 | 作用 |
|---|---|
| `session_search` | 会话级检索，**总是排除调用者自己的会话**（用于查过去别的会话） |
| `session_event_search` | **事件级检索，可在单个逻辑会话内** → 这一条能查自己会话里被遮蔽的事件 |
| `session_event_read` | 按 seq 读精确事件数据 |
| `session_event_trace` / `session_trace` | 追踪事件/会话关系 |

关键事实：

- **默认索引 `current`、`shadowed`、`log-only` 全部事件**（`session-query-sqlite` README 明说）
  → **被遮蔽内容可检索**这条已经通了。
- 跨会话授权：目标会话的 `cwd` 必须与调用者**完全相等**；无 `cwd` 只能查自己
  （与本仓库"只允许读同一工作区"是同一条规则）。
- 查询是**字面短语**（FTS5 的 `OR`/`NEAR`/`*` 当数据不当语法）；排序确定性（匹配跨度 → 文档长度
  → 时间/id/seq 破平）；分页用不透明 cursor，语料变化即失效。
- `tool-session-query` 是 **opt-in** 包（默认不挂）。

**仍缺（= 提案独有的部分）**：按 **checkpoint id** 寻址读回、压缩闭环内的召回、冻结索引、
状态/索引两分、膨胀守卫、缓存经济学、`dsh-compact-recallable` 后端。

#### (c) 提案自己已经重估过索引选型

提案原文（Alternatives considered 末条）：

> "**FTS/vector index sidecar** — the rejection based on a resident, bounded live log is superseded by
> the event-read policy. **Reassess the index choice against paged historical reads before implementing recall.**"

即：DSH 自己承认索引选型的拒绝理由**已被 2026-09-09 的事件读取政策取代**，实现 recall 前要对照
已落地的分页历史读取（就是 session-query）重新评估。

#### (d) 本仓库现状与立场

**已实现**：L0 会话目录（状态栏常驻，零工具调用）/ L1 用户话清单 `session_manifest`（原话 + 足迹：
结论摘录、step 数、用过的工具、动过的文件、结局、是否被压缩）/ L2 回合明细 `read_turn`
（按回合号读回，超限**明说**被截断，`scope='trace'` 带推理痕迹）；只读同一工作区；
**明确不做相关性排序**（语料是自述的时间线，模型看清单自己挑）。

**与已落地 session-query 的关系**：同类路线（确定性 + 工作区受限 + 只读），
但本仓库有两处它没有的：**L0 常驻状态栏**、**"自述时间线"取向**（清单而非排序检索）。
本仓库另有它没有的**实测证据**（三臂评测 + 16 条针的损失审计）。

**立场保留**：`docs/notes/implemented/feature/2026-09-19-context-recall.md`（原 §3.2） 记录了对 DSH FTS5 的实测与"为什么不照抄"——
**评审时应保留并强化该论证**，并补一条新事实：DSH 已在 0.2 落地 FTS5 路线
（`session-query-sqlite`），而本仓库是**评估过之后主动拒绝**，不是不知道。

**本仓库的决定（正式记录）：召回走"模型自选 episode"，不引入字面检索实现**

- **决定**：召回路径为 **L0 目录**（状态栏常驻，零工具调用）→ **L1 清单**
  （`session_manifest`：用户原话 + 足迹）→ **L2 读回**（`read_turn`，超限明说，`scope='trace'`）。
  **本期不实现任何字面/排序检索**；DSH 提案的 `history_search` 类工具**只保留接口**
  （本仓库 `docs/notes/implemented/feature/2026-09-19-context-recall.md`（原 §4.5） "兜底：字面检索（v1 不做，留接口）"已经是这个决定）。
- **理由（精确版）**：编码会话的召回单位是**回合（episode）**，不是字符串命中。片段检索会返回
  脱离情境的碎片，并由**排序算法代替模型判断重要性**；清单把判断权交回模型——即"信任模型在
  给定自述时间线上的选择能力"。注意与 DSH 的区分：拒绝的是**排序检索（FTS5）**，
  而 **DSH 提案自己**用的是**无索引字面扫描**（其立场同为"recall path stays a pure function of
  the log"）——本仓库与提案的距离，小于与已落地实现的距离。
- **接受的失败模式（明确写下，不回避）**：
  1. **unknown unknowns**：事实只存在于被遮蔽区间、且清单足迹未覆盖 → 模型不知道去哪读。
     （DSH 提案同样承认：*"a detail absent from summaries and keywords draws no recall"*。）
  2. **精确串定位**：模型知道某个标识符/值存在、但不知道它在哪个回合 → 逐回合读的成本高于一次搜索。
  3. **缓解**：L1 的足迹字段（用过的工具 / 动过的文件 / 结论摘录 / step 数 / 结局 / 是否被压缩）
     是对"关键词锚点"的**结构化替换**——覆盖"哪一回做了什么"，而非"哪个字符串在哪"。
- **回头做的触发条件（可观察、可反驳；满足任一才考虑实现字面检索）**：
  1. 出现 **≥3 个**实测案例：缺口真实存在 + 清单足迹未覆盖 + 模型因此失败；
  2. `read_turn` 的**平均读取次数/回合**超过阈值（说明在盲目翻回合，导航失效）；
  3. 某类任务的 **R3 成功率显著低于 R1 上限**，且归因到"找不到"而非"读不回"。
- **若将来做，只做这一种**：**无索引字面扫描**（DSH 提案的 `history_search` 形态：扫描被遮蔽
  区间、返回 `scanned`/`matched`/`truncated` 覆盖元数据），**不做 FTS5 排序检索**——
  保持"召回是日志的纯函数"。

#### (e) 可选借鉴四项（若做，各自独立提交）

| # | 借鉴项 | 母本依据 | 成本 | 收益 | 验收 |
|---|---|---|---|---|---|
| 1 | **膨胀守卫** | 提案 "The inflation guard" | 极小 | 你实测里有"空摘要失败 3 次"的现场；防压缩反而变大 | 构造 `post ≥ pre` 的场景 → 不提交且不让回合失败 |
| 2 | **代码生成 footer 指针** | 提案 footer | 小 | 把"可达"变成"确信可达"：模型确知被哪个 checkpoint 遮蔽、可用 `read_turn` 读回 | footer 由代码从事件 `seq` 组装（模型绝不手写）；回放字节一致 |
| 3 | **`history_search`（字面扫描 + coverage）** | 提案召回工具 | 中 | 补上"知道有这回事、不知道在哪个回合"；确定性、可回放 | 零命中给"这是字面扫描"提示；返回 `scanned`/`matched`/`truncated`；能命中仅存在于被遮蔽区间的内容 |
| 4 | **冻结 stub / 可变 state 两分** | 提案核心 | 大（重写压缩） | 根本解法：缓存不再全 miss、不再"摘要的摘要" | 前缀为 `[stubs…][state][tail]`；stub 跨轮字节稳定；被取代 state 折叠**无墓碑** |

> ⚠️ 第 3 项（字面检索）**不按普通可选任务执行**：它受 §6.4(d) 的"回头做的触发条件"约束，
> 触发条件未满足前**不实现**（保留接口即可）。第 1、2、4 项不受此约束。

#### (f) 明确不做

- ❌ **排序检索（FTS5 / 向量 sidecar）**：与 §5"确定性召回"一致；本仓库已有实测理由
  （`docs/notes/implemented/feature/2026-09-19-context-recall.md`（原 §3.2）），**不是没评估过**。
- ❌ **字面检索的实现（本期）**：`docs/notes/implemented/feature/2026-09-19-context-recall.md`（原 §4.5） 的接口**保留但不动**；
  仅在 §6.4(d) 的三个触发条件之一满足时才考虑，且只做无索引字面扫描形态。
- ❌ 让 LLM / embedding 参与召回路径（提案与本仓库**双方原则一致**：召回必须是日志的纯函数）

---

## 7. 执行约定（硬规则）

1. **三绿才提交**：`python -m ruff check my_coder tests` + `python -m mypy my_coder` + `python -m pytest`。
2. **一改一测**：每项改动附回归测试（本仓库已有 182 个测试，测试/代码 ≈ 0.75:1，保持这个密度）。
3. **不许破坏分层**：`tests/test_architecture.py` 必须绿（下层 import 上层当场红）。
4. **文档同步（按本仓库既有的五层分工，见 `docs/AGENTS.md` §一）**：
   调研/决策档案 → `docs/prior-art.md`；设计记录 → `docs/notes/{lifecycle}/{class}/`（规则见 `docs/notes/README.md`）；
   规范 → 根 `AGENTS.md`（**≤8000 字符注入预算**）；机制现状 → `docs/architecture.md`；
   前端映射 → `docs/subsystems/web-projection.md`；进度 → `NEXT_STEPS.md`；用法 → `USAGE_zh.md`。
   **规则一句话进 L1、长理由进 L2**——不要把长文塞回 `AGENTS.md`（`tests/test_docs.py` 会红）。
5. **每完成一项**在 `NEXT_STEPS.md` 追一条（日期 + 验收结果），格式对齐现有条目。
6. **不许新增依赖**，除非在提交说明里写明理由并获得作者同意。
7. **报告格式**：改了什么 / 证据（命令与输出）/ 遗留问题。
8. **每项独立提交**，提交信息用本仓库现有风格（如 `feat(session): 日志格式锚点与方向感知拒绝`）。

---

## 8. 验收总清单

```text
P0-1  .eval/ 入库（生成器 + 报告 + 题库，不含大语料）+ .eval/README.md      [ ]
P0-2  tests/test_doc_numbers.py + 5 处漂移清零 + 反静默通过设计              [x]  ✅ commit 759f4cd
P0-3  SESSION_FORMAT_VERSION + 会话头 + 方向感知拒绝 + 未知事件守卫          [x]  ✅ commit 2604eee
      + 6 条契约测试全绿 + 所有读取方同步改造                                [ ]
P1-1  assistant/attempt（失败/重试/取消留档，不进模型历史）                  [x]  ✅ commit eded733
P1-2  prepareCall 取消原子性                                                 [x]  ✅ commit 951592d
P1-3  渲染静默截断                                        [x] ✅ 已完成（commit 54db8c8）
      └ 遗留核对：eval/recall 审计脚本是否识别新截断标记                      [ ]
P1-4  题库可复现性闭环（生成器 + 校验入库）                                  [ ]
P1-5  收敛压力改善（含前后实测对比）                                         [x]  ✅ commit e5b5968（PR #41 已合并）
P2-1  文档路径 + §N 引用检查                                                 [ ]
P2-2  **L3 长文**字数上限（L1 ≤8000 字符与 note ≤300 行已有，见 docs/AGENTS.md §五） [ ]
P2-3  术语表（属 L3，不要塞进 L1）                                           [ ]
P2-4  恢复 CI（文档校验 + 三绿）                                             [ ]
P2-5a 门禁强制 `## Alternatives considered`                                  [ ]
P2-5b 门禁校验反引号里的仓库路径（含 `assert scanned >= 10` 反静默通过）      [ ]
P2-5c README 补"删除 vs 保留"规则（**不建归档树**，写触发条件）              [ ]
P2-5d rejected 语域改为提案语域（**已决定 A**：改门禁 + 那篇 note + README 骨架说明）  [ ]
P2-6a 根 AGENTS.md 加一句提交/PR 判据（不超注入预算）                        [ ]
P2-6b 新建 `.github/pull_request_template.md`（含"设计记录"一行）            [ ]
P2-6c 新建 `.github/ISSUE_TEMPLATE/`（bug/feature/task，可选）               [ ]
P2-6d 新建 `skills/gh-pr.md`（与 gh-issue.md 同构的 agent 工作流）           [ ]
P2-6e commit-msg 钩子（可选；**CI 已移除，只在本地生效**，文档要写明）       [ ]

可选（§6.4(e)，各自独立提交，先与作者确认）
P3-1  膨胀守卫（压缩后不小于压缩前 → 不提交）                                 [ ]
P3-2  代码生成 footer 指针（模型确知被哪个 checkpoint 遮蔽）                  [ ]
P3-3  字面检索（**仅在 §6.4(d) 触发条件满足时**才考虑；形态=无索引字面扫描）  [ ]
P3-4  冻结 stub / 可变 state 两分（大改动，先确认是否需要缓存经济学）         [ ]

已完成（不要重做）
```

> **P0-1 的复核行**（改写后）：
> `git ls-files eval | wc -l` > 0；且
> `docs/notes/implemented/testing/2026-09-19-context-recall-evaluation.md` 里含三臂设计、
> 判据分类、关键结论数字、完整复现命令。

---

## 9. 与母本的对应关系速查

| 本仓库改动 | 母本对应位置 |
|---|---|
| 会话头 + `formatVersion` | `packages/core/session/src/types.ts`（`SessionHeader.version`、`SESSION_FORMAT_VERSION`） |
| 方向感知拒绝 | `.agents/notes/implemented/architecture/2026-08-10-session-log-version-mechanism.md` |
| 未知事件守卫 + `ignorable` | 同上（`KNOWN_SESSION_EVENT_TYPES` 与 `ignorable` 语义） |
| `assistant/attempt` | `.agents/notes/implemented/architecture/2026-09-01-v2-embedded-assistant-streams.md` |
| `system/message`（P3） | `.agents/notes/implemented/architecture/2026-09-02-system-prompt-as-surface-node.md` |
| 版本/发布状态权威 | `docs/session-format-status.md`（writer 4 / released 3） |
| turn flow 现状 | `docs/architecture.md` 的 "Turn flow" 一节 |
| **Agent Notes 制度**（路径两轴、闭集分类、头三行、两套骨架、`Alternatives` 强制） | `.agents/notes/README.md` |
| **Note 格式门禁** | `scripts/verify-agent-note-format.ts`（属 `doc-sync`） |
| **归档 / 删除判据** | `.agents/notes/README.md` 的 "Archiving and deletion" 一节（本仓库**不抄**冻结树，见 P2-5c） |
| **L1 注入预算**（本仓库原创门禁） | 母本对应 `docs/AGENTS.md` 的字数预算与 `verify-doc-budgets`；本仓库另有"渲染后不许截断"的运行时门禁 |

---

> **交接记录**：2026-10-03 起由架构会话**接管执行**（前一执行会话已停手，交接前核对：无其它写入进程、工作树无在飞改动）。执行顺序：账本对齐 → 第 4 项流式持久化（A）→ P1-4 → P2-1/P2-2 → 内存席位置换。**每落地一项就打勾 + 写一篇 `docs/notes/` 的设计记录**（L1.5 规则，见 `docs/AGENTS.md` §一）。

*本文由架构对照会话产出；证据均可用第 0 节的复核命令重新验证。若发现本文与代码不符，以代码为准并在 `NEXT_STEPS.md` 记一条漂移。*
