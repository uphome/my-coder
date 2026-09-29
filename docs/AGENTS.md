# 文档标准（`docs/AGENTS.md`）

本文件是**文档的总规则**：一份知识该放哪、写成什么样、怎么被校验。
机制本身的入口在 `../README.md`；每轮必须生效的规则在 `../AGENTS.md`（agent 每轮都读它）。

> 参考：deepseek-harness 的 `.agents/notes/` 与 `docs/AGENTS.md`（它把这套做到了 ~800 篇）。
> 我们只抄**结构**（状态即目录、一篇一个决定、格式门禁），不抄它的双语 sidecar 与冻结清单机制。

## 一、一个事实只有一个家

**同一条知识在两处各写一遍，两处必然漂移；而漂移的那一份会被当成事实源。**
所以先问"它属于哪一层"，再动笔：

| 层 | 位置 | 装什么 | 谁读它 | 过期了怎么办 |
|---|---|---|---|---|
| **L0 入口** | 根 `README.md` / `USAGE_zh.md` | 这是什么、怎么跑、安全警告 | 第一次来的人 | 直接改（它就是门面） |
| **L1 规范** | 根 `AGENTS.md`（**≤ 8000 字符**）+ system 的 `discipline` / `tool:*` 段 | 每轮必须生效的**判据一句话** + 指向设计记录的链接 | **agent 每轮** | 改判据；长理由进设计记录 |
| **L2 设计记录** | `docs/notes/{lifecycle}/{class}/yyyy-mm-dd-主题.md` | **一个决定**：问题、决定、被否决的方案、后果、验证方式 | 人 + agent 按需 `read_file` | 移动目录即改状态；决定变了就新写一篇 |
| **L3 现状长文** | `docs/architecture.md`、`docs/prior-art.md`、`docs/subsystems/*.md` | 机制总览、三家对照、工具目录——**描述现状**，不是记录决定 | 人 + agent 按需 | 跟着代码事实改 |
| **L4 证据** | `.eval/**`（**不入库**）+ 设计记录里写**复现命令** | 审计表、情境档案、汇总表、渲染样张 | 谁要谁重跑 | 重跑即新，没有"过期"问题 |

**判据：写之前问一句"这句话在别处已经有了吗？"** 有就改成链接，不要复制。

## 二、L1 为什么有 8000 字符上限（这条是实测，不是审美）

`app/instructions.py` 把工作区根的 `AGENTS.md` **正文注入 system**（每请求都在），
但**每文件预算 8000 字符**。实测（2026-09-29）：`AGENTS.md` 长到 22,553 字符时，
**只有前 8,000 字符进了 system**，后面接一句
`(…truncated at 8000 chars; use read_file to read the rest of this file)`——
13 个规则块里 **9 个模型从来没看到过**（含召回、技能、inbox、"发现缺口时怎么办"）。

所以：

- **L1 只放判据**（一句能照做的规则）+ 链接；动机、证据、事故全部沉到 L2/L3。
- `tests/test_docs.py` 钉住这件事：**渲染后的注入文本里不许出现截断提示**。
  它一旦红，就是把长文搬出 L1 的信号——不要再靠"记得看一眼"。

## 三、设计记录（L2）与长文（L3）的分界

- **决定**（选了 A 而不是 B、放弃了什么）→ `docs/notes/`。
- **现状**（这套东西现在怎么工作）→ `docs/*.md` 与 `docs/subsystems/`。
- 一篇 note 里可以有一段"现状"，但它的重点是**为什么**；长文里可以有一段理由，
  但它的重点是**是什么**。两边都要时：note 链到长文，长文链回 note。

## 四、写作规则

1. **具体**：写清哪个文件、哪个函数、哪个数字。不要"某处""相关逻辑""若干"。
2. **记录被否决的方案与原因**——这是 note 存在的首要理由；代码和长文都装不下它。
3. **只写当时成立的事实**；数字要给**复现命令**，不要把生成物的内容抄进来（见 L4）。
4. **交叉引用一律用相对链接**（`../implemented/feature/2026-….md`），不要写裸文件名或编号
   ——`tests/test_docs.py` 会校验链接可解析，移动文件时不会烂在半路。
5. **中文**；命令、代码、标识符、专有名词保持原文。
6. **不写"计划/将来/待办"**——那是 `proposed/` 的语域；已落地的东西用现在时陈述。
7. **命令必须能跑**：写进文档的命令要给出完整形式（含 `conda run …`），
   并且**只写你真的跑过的**。

## 五、预算

- `AGENTS.md` ≤ **8000 字符**（硬上限，见第二节）。
- 一篇 note 目标 **≤ 300 行**；超了通常说明它讲了不止一个决定 → 拆。
- `docs/notes/README.md` 是规则，不是清单：**不要建集中索引**。活跃的树 + 全文搜索就是清单
  （理由见 `implemented/process/` 下关于"不做索引"的那篇 note）。

## 六、slop 清单（出现即删）

- "众所周知 / 显而易见 / 需要注意的是 / 值得一提的是"
- 复述代码能直接看出来的东西（大段实现粘贴）
- "本次 / 这次 / 今天 / 最近"——读者不知道你是哪天写的
- 没有出处的断言（"测试通过了" → 给命令与输出）
- 索引清单、目录树复制件（会过期）
- 把同一段理由在 note 与长文里各写一遍

## 七、已迁移的路径（2026-09-29 整理）

旧的引用（PR 正文、issue、聊天记录里的链接）不会跟着改，所以在这里留一张对照表：

| 旧路径 | 现在在哪 |
|---|---|
| `ARCHITECTURE.md` | [`docs/architecture.md`](architecture.md)（L3 现状长文） |
| `agent.md` | [`docs/prior-art.md`](prior-art.md)（L3；改名是为了不再与 `AGENTS.md` 一字之差） |
| `web/PROJECTION_DESIGN.md` | [`docs/subsystems/web-projection.md`](subsystems/web-projection.md)（L3） |
| `CONTEXT_BUDGET_DESIGN.md` | 拆成三篇设计记录：[机制 + agent 供给面](notes/implemented/feature/2026-09-19-context-recall.md) · [验收方法学 + 四道情景](notes/implemented/testing/2026-09-19-context-recall-evaluation.md) · [渲染静默截断](notes/implemented/bug-fix/2026-09-29-silent-render-truncation.md) |
| `eval/recall/RENDERING_SAMPLE.md` | 仍是生成物，只是**不再入库**：输出改到 `.eval/recall/RENDERING_SAMPLE.md`，跑 `python eval/recall/render_sample.py` 重生成 |

**生成物入库的判据是"重建成本"**：要花 API 才能重建的（`eval/recall/navigation.jsonl`、
`questions.jsonl`）留在仓库里当数据存档；零成本能重跑的（审计表、情境档案、汇总表、渲染样张）
不入库，note 里只写结论与复现命令。
