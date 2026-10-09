# Agent Note: 评测题库冻结——sha256 锚在 git 里，且明说"不可原地复现"

Status: implemented

## Problem

`eval/recall/` 里的两份题库是**评测的历史基线**，而它们此前**完全不受保护**：

| 文件 | 体量 | 重建成本 |
|---|---|---|
| `eval/recall/questions.jsonl` | 36 条 | 纯本地计算（`build_dataset.py`，零 API） |
| `eval/recall/navigation.jsonl` | 13 条 | **要 `DEEPSEEK_API_KEY`**（`build_navigation.py` 让 LLM 造题） |

两者都已入库（`git ls-files eval` = 12），题数（36 / 13）被评测 note 反复引用——
但**没有任何自动检查**会发现"有人改了一行、删了两条、重排了顺序"。
`git diff` 能看见，可它不会在 CI 或提交前替你说"评测基线变了"。
第二份尤其危险：**它重建要花钱**，所以"以后重跑一遍就行"这条退路并不便宜，
一旦被手改而没被注意，挂在墙上的召回率/成功率结论就静默对不上了。

## Decision

新增 `eval/recall/freeze.json` + `tests/test_eval_freeze.py`（5 条），
把"当时那份题库"钉死。四条设计要点：

### 1. 冻结值锚在 **git 的 HEAD 内容**上，不是工作区

校验同时算两个哈希：工作区那份、以及 `git show HEAD:<path>` 那份，
**要求两者都等于 freeze.json 里的值**。否则"改坏题库 → 顺手改 freeze.json"就能自证通过。
工作区与 HEAD 不一致时，错误信息会区分是"未提交的改动"还是"已提交但冻结清单过期"。

### 2. 哈希前先做**换行归一**（`\r\n` → `\n`）

本仓库 `core.autocrlf=true`：Windows 检出是 CRLF、git 里存 LF。直接哈希工作区字节
会和 HEAD 对不上——那是**检出层的噪声**，不是内容改动（实测：`navigation.jsonl` 工作区
`6f7ca1c5…` vs HEAD `85f37288…`，同一份文件两个哈希）。归一后两种形态同哈希，
而任何真实内容改动一定变哈希。

### 3. 冻结"形状"而不只是哈希

除 sha256 外还记 `lines` / `bytes` / `fields`（两份分别 11、12 个键）——
哈希只能证"不同"，行数与字段集才能**说清哪里不同**（少了两条题？某个字段改名了？）。

### 4. 把"**不能原地复现**"这个事实也钉住

题库是**旧版** `render_turn` 的产物。产品渲染其后改过：v1 加回合头、截断时列可用 step；
2026-09 删掉单块静默截断（改按行分页 `offset`）、删 `max_events`。
所以"重跑生成器得到同一份"是**假命题**——`build_navigoation.py` 还是 LLM 造题，
连旧渲染下都未必得到同一份。

`freeze.json` 里因此有一节 `render_contract: {status: 'drifted', rebuildable_in_place: false}`
+ 说明，并有 `test_freeze_admits_the_question_bank_cannot_be_rebuilt_in_place`
盯着它。这条测试**不检查代码行为，它防的是误读**：后来人若打算"重跑生成器来更新 freeze"，
会先在这里读到为什么不行、以及要复现旧数字得回到产出它的提交
（`46bbfb8` / `6be54e4`）。

## Alternatives considered

- **只记 sha256、不记行数/字段**：否决。哈希对不上时无法给出可操作的诊断——
  审阅者需要知道"少了两条"还是"顺序变了"，否则只能整个重建（而重建要 API）。
- **把题库内容直接抄进测试**（内联期望值）：否决。两份共 22 KB，
  塞进测试文件会让"改题库"变成"改测试"，而且 diff 里再也看不清改的是题还是断言。
- **冻结成只读**（`git update-index --skip-worktree` / 文件权限位）：否决。
  跨平台不可靠（Windows 的权限位对 git 无意义），而且它防的是"不小心写"，
  防不住"有意改"——门禁要防的是**没人发现**，不是**没法改**。
- **让生成器输出确定性化**（固定种子 / 排序稳定）再记哈希：**方向对但不解决本项**。
  `build_dataset.py` 已经是确定性的（纯本地计算），问题在**渲染层**——
  `render_turn` 的输出变了，生成器的输入就变了。要真正可复现得先冻结渲染契约，
  那是更大的一步（见下）。
- **不记 `render_contract`，只留 sha256**：否决，这是本次最重要的一条。
  只留哈希会让读者默认"能重跑出同一份"；事实相反，且这个误解会直接导致
  下一次有人重跑生成器、看到全红、然后**顺手把 freeze.json 改成新值**——
  把一次"基线变更"伪装成"维护性更新"。把不可复现写在结构里，就是不让那条路走通。
- **顺带做渲染契约版本化**（给 `render_turn` 加版本号，题库记版本）：**没做，但记在这**。
  它是对的下一步，代价是要给产品渲染加一个"输出契约"概念，超出 P1-4 的范围
  （P1-4 的验收是"校验测试能拒绝被手改的题库"）。触发条件：真的需要复现旧数字时。

## Consequences

**换来的**：题库被手改（无论是否已提交）会在 `pytest` 里响亮地红；
重建成本、生成器路径、"不可原地复现"三件事都写在 `freeze.json` 里，不必靠口口相传。

**付出的 / 边界**：

- **多了一个要同步的东西**：有意重建题库后必须手动更新 `freeze.json`（哈希/行数/字段）。
  这是刻意的摩擦——它把"改动评测基线"变成一次明确的动作，而不是一次静默的编辑。
  错误信息里写了该怎么做。
- **门禁不覆盖"题库内容是否合理"**：题出得好不好、答案对不对，它一概不管。
  它只回答"还是不是当初那一份"。
- **换行归一意味着"只改行尾"不会被拦**——这是有意的（见决策 2），
  行尾差异不该让评测基线报警。

## Testing

```sh
conda run -n agent-demo python -m pytest tests/test_eval_freeze.py -q   # 5 passed
conda run -n agent-demo python -m pytest -q                             # 251 passed / 3 skipped
```

**篡改实测**（不是推断）：把 `navigation.jsonl` 第一题的 `id` 改成 `TAMPERED-…`
（合法 JSON、行数与字段集不变）→ 门禁红，报出两个哈希；还原后恢复绿。

五条测试：冻结清单覆盖全部题库（含反静默通过 `>= 2`）、工作区与提交内容双哈希一致、
行数与字段集一致、`render_contract` 仍标 `drifted` 且 `rebuildable_in_place=False`、
`generator` 路径指向的脚本仍然存在。
