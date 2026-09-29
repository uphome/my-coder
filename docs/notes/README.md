# 设计记录（Agent Notes）

这里放**决定记录**：一个决定一篇，记的是"**为什么**这么定、放弃了什么、验证怎么跑"。
"这套东西现在怎么工作"不放这里——那是 `../architecture.md` 与 `../subsystems/`。

> 规则的总纲在 [`../AGENTS.md`](../AGENTS.md)（一个事实只有一个家）。本文件只管 note 本身。

## 一、路径编码两个轴

```
docs/notes/{lifecycle}/{class}/yyyy-mm-dd-主题.md
```

- **lifecycle**（第一层）= 状态，**文件在状态之间移动**：
  - `proposed/` —— 已评审、尚未落地（或只落了一半）。用将来时，可以有待办与开放问题。
  - `implemented/` —— 决定已随代码落地。用现在时陈述，**且必须跟着代码事实更新**（见第四节）。
  - `rejected/` —— 认真考虑过并否掉了。**只在"它的理由能防住一个真会犯的错"时保留**，
    否则删掉（被否决的方案若只是"我们选了另一个"，那属于落地那篇的
    `## Alternatives considered`，不单独建篇）。
- **class**（第二层）= 决定的种类，**闭集**（`tests/test_docs.py` 校验，加一类要同时改这里与测试）：

| class | 覆盖什么 |
|---|---|
| `feature` | 面向用户或面向模型的新能力 |
| `bug-fix` | 修缺陷，或补上复盘暴露的缺口 |
| `simplification` | 删代码/删行为/减表面积，不加能力 |
| `architecture` | 关于**已发布源码**的结构决定：包怎么分层、运行时的词汇表是什么 |
| `process` | **代码之外**的工具、政策、流程：门禁、包管理、文档标准 |
| `testing` | 测试基础设施与策略（含"怎么量、量什么"） |

  `architecture` 与 `process` 的分界：**architecture 管我们发布的源码，process 管围着它的工具与流程。**

- **日期 = 首次提出那天**（git 里该主题的第一个提交），**移动状态目录时不改名**。
  改动日期、给状态加日期或括号，都会被门禁拒绝——文件名有日期，其余交给 git。

## 二、头三行（固定）

```markdown
# Agent Note: <标题>

Status: <状态>
```

`Status:` 的三种形式，**必须与所在目录一致**（门禁交叉校验）：

- `Status: proposed`
- `Status: implemented`
- `Status: rejected — <一句话原因>`

`rejected` 是唯一带内容的 status：读者来就是为了拿这个判决。

## 三、正文骨架

第一段固定是 `## Problem`（动机，要能脱离方案独立读懂）。之后按 lifecycle：

**`proposed/`**

```markdown
## Problem
## Proposal
…按需的专门小节…
## Alternatives considered
## Acceptance criteria
## Risks
```

**`implemented/`**

```markdown
## Problem
## Decision
…按需的专门小节…
## Alternatives considered
## Consequences
```

- `implemented/` 里**禁止**出现 `## Proposal` / `## Plan` / `## Migration plan` /
  `## Acceptance criteria`——那是提案语域，出现在"已落地"的文档里会让读者分不清
  哪些已经发生（门禁拦）。
- `## Testing` / `## Deferred` / `## Related` 可以用，只要写的是现在时的事实。
- 专门小节（数据结构、协议、边界条件）自由命名，但**不要**把上面这些名字挪作他用。

## 四、什么时候写、怎么维护

- **同一个 PR 里写**：只要这个决定的理由**代码、测试、长文都装不下**。已经在别处的
  就更新那一篇，**不要新开重复篇**。
- **`implemented/` 的 note 必须跟着代码事实更新**：改了文件名、包名、键名、默认值，
  同一个改动里把 note 里的**事实**改对。**决定本身不许改**——决定要变就新写一篇，
  两篇互相链接（旧的若被完全取代，可以合并进新篇并删掉，但要先把独一无二的理由、
  被否决的方案、后果、验证方式全部保住）。
- 纯机械改动、纯 UI 微调、局部小修：不必写 note。
- **不建集中索引**：活跃的树 + 全文搜索就是清单。索引必然过期，而过期索引比没有索引更坏。
- `_template.md` 是骨架模板，门禁按文件名前缀 `_` 跳过它。

## 五、事实与证据

数字要给**复现命令**，不要把生成物（审计表、情境档案、汇总表、渲染样张）的内容抄进来：
那些是 L4 证据，留在 `.eval/**`（不入库），随要随重跑。note 里写的是**结论 + 命令**。
