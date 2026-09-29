# Agent Note: `bash` 工具在 Windows 上必须真的跑 bash

Status: implemented

## Problem

工具叫 `bash`，系统提示词里的例子也是 Unix，而实现用的是
`asyncio.create_subprocess_shell`——**在 Windows 上它落到 `cmd.exe /c`**。
于是模型按自己的心智发 `ls` / `cat` / `tail` / `head` / `rm` / `pwd`，
拿回来的是 `'ls' is not recognized as an internal or external command`。

实测（15 个真实评测 run、105 条工具结果）：**22 条失败属于这一类，占全部失败的三分之一**。
同一批命令换后端对照：**真 bash 12/13 通过、cmd 5/13**。

这不只是"少几条命令能用"：它把每一次探索都变成一次试错，**请求数与步数被系统性抬高**，
于是任何"成本 / 步数"数字都被这个噪音污染，实验之间也没法比。

## Decision

**显式挑后端，并且让工具描述与后端不漂移。**

- `tools/shell.py` 的 `pick_shell()` 返回 `(argv, name, hint)`：优先真 bash
  （Windows 上找 `Git\bin\bash.exe` 等常见位置），找不到才回退 `cmd.exe`；
  结果缓存进模块级的 `_SHELL_ARGV` / `SHELL_NAME` / `SHELL_HINT`。
- 执行改走 `create_subprocess_exec(*_SHELL_ARGV, command, cwd=...)`——**不再让 shell 解析决定后端**。
- 工具描述用 `SHELL_NAME` 插值，并在回退时**明确写清**：
  "this is cmd.exe, not bash — use `dir` / `type` / `findstr`; `ls` / `cat` / `tail` do not exist here"。
- 不用 PowerShell 当默认：Windows 自带 5.1，不支持模型大量使用的 `&&`（`pwsh` 7+ 才有），
  换它等于引入一整类新的语法失败。

判据是"**工具名 + 模型心智**"：工具承诺什么后端，就必须是那个后端；
回退不是错，**不告诉模型**才是错。

## Alternatives considered

- **把工具改名（`shell` / `exec`）并保持 cmd**：否决。工具名与系统提示词的例子仍然是 Unix 味的，
  改名不能消除"模型按 Unix 心智发命令"这件事，只是把矛盾藏起来。
- **自己实现一层 Unix 兼容层（把 `ls` 映射到 `dir`）**：否决。命令的语义差异（引号、
  通配、管道、退出码）会造成更难查的错，而"一个真 bash"是免费的（Git for Windows 自带）。
- **默认 PowerShell**：否决（见上，5.1 无 `&&`）。
- **给模型两个工具（bash 与 cmd）**：否决。工具面翻倍而收益只是"另一种方言"，
  而且模型仍会在错误的场景选错那个。

## Consequences

**换来的**：Windows 上模型用 Unix 命令能真的跑通（同一批命令 12/13 vs 5/13）；
工具描述与后端由同一个 `pick_shell()` 产出，两处不会漂移；实验的步数/成本数字变干净。

**付出的**：多了一个环境依赖（没有真 bash 时回退 cmd），因此**描述必须是动态的**
——把 "use dir / type / findstr" 写死在描述里，就会在真的有 bash 的机器上误导模型。
测试把两处一起钉住（后端选择 + 描述一致性）。

## Testing

- `tests/test_tools.py::test_shell_backend_prefers_a_real_bash`
- `tests/test_tools.py::test_bash_tool_runs_unix_commands`（没有真 bash 时 skip）

**含义（写在评测结论里）**：这个缺陷修复**之前**跑出来的所有 run，其请求数/步数都被
系统性抬高，成本类数字必须在修复后重测——定性发现（"R1 上限臂揭穿坏题""三题是对照题"）
不受影响。
