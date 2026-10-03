# Agent Note: 文档里的数字由门禁保证，不靠"记得同步"

Status: implemented

## Problem

同一个数字在文档里有多个版本，而且**都没有跟着代码走**：

- 测试数：`README.md` 与 `docs/architecture.md` 说 **175**、`NEXT_STEPS.md` 说 **85**、
  `docs/prior-art.md` 说 **131**；实测 `pytest --collect-only -q` = **203**（`206` 含新增门禁自身）。
- 代码行数：`docs/architecture.md` 开头写"约 **1000** 行"，实测 `my_coder/**/*.py` = **7,991** 行。

`NEXT_STEPS.md` 还留着一句**已失效的承诺**："当前全量测试：85 passed（AGENTS.md 里的数字
保持同步）"——它自己就是不同步的证据。

这类漂移之所以必须机械拦，理由和 [`2026-09-29-agents-md-injection-budget.md`](2026-09-29-agents-md-injection-budget.md)
写在 `tests/test_docs.py` 开头的那条一样：**"规则写进文档"不等于它会生效**。数字自相矛盾
又是评审者最容易抓的破绽：它不需要读代码就能发现，一旦发现就会怀疑其余结论。

## Decision

新增 `tests/test_doc_numbers.py`：**现场测量 + 断言文档声明一致**。

**扫描面**只覆盖"描述现状"的层（`docs/AGENTS.md` 第一节的分层）：

| 扫 | 文件 |
|---|---|
| L0 入口 | `README.md` / `USAGE_zh.md` |
| L1 规范 | `AGENTS.md` |
| L1.5 台账 | `NEXT_STEPS.md` |
| L3 现状长文 | `docs/architecture.md` / `docs/prior-art.md` |

**不扫**：L2 `docs/notes/**`（那是"某一天成立的决定"，数字是当时快照，改它就是篡改历史）
与 `DSH_0.2_GAP_AND_PLAN.md`（它自己用醒目标注声明"本文数字全部过期，执行时现场重测"）。

**两类声明**：`约 N` 前缀允许 ±10%（本来就是近似）；裸 `N` 必须精确相等。

**被校验的量**：测试数（spawn `pytest --collect-only -q` 解析）、`my_coder/**/*.py` 行数、
`tests/*.py` 行数、`web/index.html` 行数、`docs/architecture.md` 的 `### 3.x` 节数。

**反静默通过**：`assert len(claims) + exempted >= 8`——扫描面塌掉（文件搬家、正则失效）时，
下面的循环会空转全绿，门禁无声消失。这条照抄 `tests/test_architecture.py`
的 `assert scanned >= 30`，那里实测过"把 root 指到不存在的目录 → 2 passed"。

**历史叙述**不删也不改，登记进 `HISTORICAL`（当前两条：`NEXT_STEPS.md` 的
`31 passed in 1.12s` 验收实录、`docs/prior-art.md` 的 `pytest 131 passed` 门禁快照），
每条写明理由；`test_historical_exemptions_are_still_real` 保证豁免表不长僵尸条目
（对齐 `KNOWN_VIOLATIONS` 的纪律）。

**已知取舍（正向）**：`passed` 只在文档里数**当前**测试数时算声明；**行内代码示例里的
pytest 输出**（如 `31 passed in 1.12s`）会误命中，靠 `HISTORICAL` 豁免而不是靠"改历史数字"。

## Alternatives considered

- **文档从单一来源生成**（如数字由脚本注入占位符）：否决。它把文档变成生成物，
  改一个数字要跑生成器、review 时看不到 diff 的语义；而真正需要的是"漂了就红"，
  不是"永远不可能漂"。门禁的改法小得多。
- **在 `docs/AGENTS.md` 里写一条"数字要保持同步"的规定**：否决。这正是被证明无效的做法
  （那条承诺已经失效过一次），规则文本不产生约束力。
- **全文扫描所有数字**：否决。`DSH_0.2_GAP_AND_PLAN.md` 里全是"当初的数字"，
  `note` 里全是"当时的快照"；把扫描面扩到那儿，要么天天误报，要么把豁免表写成一张
  大名单（等于没有门禁）。**限定扫描面**才是这条门禁能长期活着的原因。
- **只校验一个"权威数字"（如测试数）**：否决。行数、机制数同样在漂
  （`约 1000 行` vs 实测 7,991），只钉一个等于放掉另外几个。
- **把数字写死进测试**：否决。被测对象就是"文档与代码是否一致"，写死数字等于测试
  自己成了第三个漂移源。

## Consequences

**换来的**：文档里任何测试数/行数与代码不符，`pytest` 直接红并点名文件:行号；
`NEXT_STEPS.md` 那句失效承诺改成"由 `tests/test_doc_numbers.py` 机械保证"；
本次同时清零 4 处漂移（`README.md` 两处、`NEXT_STEPS.md` 一处、`docs/architecture.md` 两处）。

**付出的**：

- 测试会 **spawn 一个 `pytest --collect-only` 子进程**（约 0.1s），并且它**必须在测试函数体里
  惰性调用**——模块级调用会让内层 pytest 再次导入本模块、再次 spawn，**无限递归**
  （实测：进程炸开、60s 不返回）。函数用 `@lru_cache(maxsize=1)` 保证一次运行只 spawn 一次。
- 近似声明的 ±10% 是**拍的**，没有实测依据；若将来某个"约 N"长期贴着边界晃，就该改成精确值。
- 它只管**被列进 `CLAIM_PATTERNS` 的六种声明形**；新写一种表述（如"N 条用例"）不会被覆盖，
  要同时加模式——门禁的覆盖面靠这份模式表维护。

## Testing

```sh
conda run -n agent-demo python -m pytest tests/test_doc_numbers.py -q     # 3 passed
conda run -n agent-demo python -m pytest -q                              # 203 passed, 3 skipped
```

**故意改错即红的验证**：把 `README.md` 的 `206 个测试` 改成 `175` → 门禁报
`README.md:202 175 ≠ 实测 206（tests）`；把 `HISTORICAL` 里的 `31 passed in 1.12s`
那段文字删掉 → `test_historical_exemptions_are_still_real` 报"豁免条目作废"。
