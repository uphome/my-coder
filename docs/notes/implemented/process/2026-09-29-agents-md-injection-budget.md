# Agent Note: 规范有注入预算——`AGENTS.md` 必须留在 8000 字符以内

Status: implemented

## Problem

`AGENTS.md` 是 agent **每轮都读**的规范入口：`app/instructions.py` 把工作区根的
`AGENTS.md` / `CLAUDE.md` 正文**直接注入 system**（每请求都在）。但它有预算：
`INSTRUCTION_MAX_FILE_CHARS = 8000`（每文件），超了**截断 + 追加一句提示**。

这个文件一路长到 **22,553 字符**（401 行），于是实测（真实渲染，离线）：
**只有前 8,000 字符进 system**，后面是
`(…truncated at 8000 chars; use read_file to read the rest of this file)`。
逐块核对 13 个位置递进的规则块，**9 个模型从来没看到过**：

| 规则块 | 在 `AGENTS.md` 的位置 | 进 system |
|---|---:|---|
| 项目定位 / 五个不变式 / 六条纪律 | 13 / 2,842 / 3,565 | ✅ |
| 工具并发调度 / `bash` 必须真跑 bash | 6,050 / 7,230 | ✅ |
| runtime status 注册制 | 8,004 | ❌ |
| 上下文召回落地规则 | 9,407 | ❌ |
| 每对话一个工作区 / 工作区指令文件 | 11,553 / 14,433 | ❌ |
| 按需技能 / inbox 队列 | 17,264 / 19,190 | ❌ |
| 发现缺口时怎么办 / 入口与工具 | 20,557 / 21,980 | ❌ |

本仓库那句格言是"**写在文档里挡不住**"（一条临时文件的规则写在 `NEXT_STEPS.md` 里多年，
真实会话照样在仓库根留下扫描脚本）。这份测量说明它**同样适用于 `AGENTS.md` 自己**：
规则写进了规范文件，不等于模型看得到。

## Decision

**规范只放"判据一句话 + 链接"，长文搬进 `docs/`；并且用测试钉住预算。**

- `AGENTS.md` 目标 ≤ **8000 字符**：每条规则保留**能照做的判据**（一句到三句），
  动机、实测、事故、被否决的方案全部沉到 `docs/notes/**` 与 `docs/subsystems/**`。
- 文档体系按"一个事实只有一个家"分五层（入口 / 规范 / 设计记录 / 现状长文 / 证据），
  规则见 `docs/AGENTS.md`，设计记录的格式见 `docs/notes/README.md`。
- `tests/test_docs.py::test_agents_md_fits_the_system_prompt_budget` 用
  `InstructionLoader(REPO).render(turn=None)` 渲染真实注入文本，断言**不含截断提示**。
  它红了就是把长文搬出去、只留判据与链接的信号。

**为什么用测试而不是"记得看一眼"**：这份测量本身就是证据——22,553 字符不是一次写成的，
是几十次"顺手补一句"累积的，期间没有任何东西拦。

## Alternatives considered

- **提高 `INSTRUCTION_MAX_FILE_CHARS`（8000 → 30000）**：否决。它是**每请求都付**的成本
  （system 前缀会进每次请求的 prompt），而且长规范本身会稀释注意力：
  模型的"每轮预算"是有限的，把 22k 字符塞进去不等于它读了 22k。
- **把规范拆成多个文件（`AGENTS.md` + `CONTRIBUTING.md` + …）**：否决（部分）。
  `INSTRUCTION_FILE_CANDIDATES` 只注入这两个名字，多出来的文件**默认不进 system**——
  那正是现在的问题的另一种形式：文件更小，但模型看不到。要修的是**分层**，不是分片。
- **把规则全写进 system 的 `discipline` / `tool:*` 段**：否决。那些段是**每轮生效**的通道，
  但它们属于**产品**（所有工作区通用），而 `AGENTS.md` 是**本仓库**的工程规范。
  两者的读者不同：前者是"跑起来的 agent"，后者是"改这个仓库的 agent"。
- **`AGENTS.md` 只留一句"去读 docs/"**：否决。那样模型每轮都要多发一次 `read_file`
  才知道判据，把成本从"前缀 token"换成了"一轮请求"。

## Consequences

**换来的**：模型每轮真的能看到全部判据；长文有了唯一的家（`docs/notes/**`），
且状态由目录编码（proposed / implemented / rejected），不会再出现"一个文件里混着
已落地、已否决、待办"的局面；`AGENTS.md` 的每次增长都会有人拦。

**付出的**：

- 读规则的人（含 agent）有时要多跳一次链接去看动机——这是刻意的取舍：
  **判据**必须每轮都在，**理由**按需读。
- 中英混排的规则块被压缩后，措辞更硬（少了"为什么"的铺垫），
  这会让人误以为规则是凭空定的——所以每条判据后面都挂了 `docs/` 链接。
- `docs/notes/**` 必须跟着维护：`implemented/` 的 note **要随代码事实更新**
  （改了文件名/默认值就同 PR 改 note），否则它会变成新的"过期声明"来源。

## Testing

- `tests/test_docs.py`（五条）：路径↔状态一致、头三行、`implemented/` 里禁提案语、
  相对链接可解析、**`AGENTS.md` 渲染后不含截断提示**。
- 渲染验证可直接跑：
  `python -c "from my_coder.app.instructions import InstructionLoader; from pathlib import Path; print(len(InstructionLoader(Path('.')).render(turn=None)))"`
