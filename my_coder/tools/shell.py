"""应用层工具：shell 执行（bash）+ 执行后端接缝。

执行能力是编码闭环"读→改→验证"的最后一步。_run_command 收执行细节
（shell 语义、合并输出、kill-on-cancel）——将来接 OS 级沙箱 runner
（restricted token / bwrap / seatbelt）只换这一个函数，executor 与循环
层不动。命令级刹车是 approval 门（已绑定），命令本身不过路径沙箱——
受限的是 cwd，命令里的路径无法校验。

**执行后端优先真正的 bash**（见 `pick_shell` 的实测依据）：工具叫 bash、
模型也按 Unix 写命令，用 `create_subprocess_shell` 落到 `cmd.exe` 会让
`ls`/`cat`/`tail` 一整类命令全部失败（评测里实测占全部工具失败的三分之一）。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from ..app.constants import BASH_MAX_OUTPUT_CHARS
from ..app.sandbox import resolve_in_workspace
from ..state.registry import ToolSpec
from ..values.messages import ToolOutcome


def pick_shell() -> tuple[list[str], str, str]:
    """挑执行后端 → `(argv 前缀, 给人看的名字, 给模型的提醒)`。

    **为什么必须显式挑**：`create_subprocess_shell` 在 Windows 上落到 `cmd.exe /c`，
    而工具名是 bash、模型的心智也是 Unix ——实测（`eval/recall` 的 15 个真 run、
    105 条工具结果）有 **22 条**失败是 `'ls' is not recognized` / `cat` / `tail` /
    `head` / `rm` / `pwd` 这类"命令不存在"。同一批模型真实命令的实测对照：

        bash -c : 12/13 通过（ls / cat / tail / head / rm / pwd / grep / 管道 /
                  `cd "E:\\..."` 都能用；conda / python / pytest / git 也在 PATH 上）
        cmd /c  :  5/13（上面这些全挂）

    回退顺序：**真正的 bash** → Windows 的 `cmd.exe`（并在工具描述里**明说**是 cmd，
    让模型改用 `dir`/`type`/`findstr`，而不是继续撞墙）→ POSIX 的 `/bin/sh`。
    **不把 PowerShell 当默认**：Windows 自带的是 5.1，不支持模型大量使用的 `&&`，
    换它会引入一整类新的语法失败（`pwsh` 7+ 才有）。
    """
    if sys.platform == 'win32':
        bash = shutil.which('bash')
        if bash:
            return [bash, '-c'], 'bash', ''
        return ['cmd.exe', '/c'], 'cmd.exe', (
            ' NOTE: this is Windows cmd.exe, NOT bash — use dir / type / findstr / copy / '
            'rmdir; ls, cat, tail, head, rm, pwd and grep do not exist here.')
    return ['/bin/sh', '-c'], '/bin/sh', ''


_SHELL_ARGV, SHELL_NAME, SHELL_HINT = pick_shell()


async def _run_command(command: str, cwd: Path) -> tuple[int, str]:
    """执行一条 shell 命令，返回 (退出码, 合并输出)。执行后端的接缝。

    - shell 语义由 `pick_shell()` 决定（优先真 bash），stdout/stderr 合并成顺序流
    - 取消（超时或用户 cancel）时 kill 掉子进程再传播：卡死的命令不能泄漏在后台
    """
    proc = await asyncio.create_subprocess_exec(
        *_SHELL_ARGV,
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        raw, _ = await proc.communicate()
    except asyncio.CancelledError:
        # kill-on-cancel：命令已不被需要，杀掉再让取消继续传播（记账在循环层）
        proc.kill()
        await proc.wait()
        # Windows Proactor 上取消的 communicate() 会留下未关闭的管道 transport，
        # 显式关闭避免 ResourceWarning 噪音（asyncio 已知怪癖）
        transport = getattr(proc, '_transport', None)
        if transport is not None:
            transport.close()
        raise
    # 与 read_file 同一宽容解码（Windows 子进程输出可能是 GBK，replace 不炸）
    output = raw.decode('utf-8', errors='replace')
    return proc.returncode or 0, output


def register(registry, workspace: Path, bash_timeout_s: float = 60.0) -> None:
    async def bash(args, agent, signal):
        # 注意：命令本身不过路径沙箱（无法校验命令里的路径），受限的是 cwd；
        # 命令级刹车是 approval 门（下一任务绑定，bash 声明 requires_approval）。
        command = args['command']
        if not command.strip():
            return ToolOutcome(content='command must not be empty', is_error=True)
        cwd, denied = resolve_in_workspace(args.get('cwd', '.'), workspace)
        if denied:
            return denied
        exit_code, output = await _run_command(command, cwd)
        if len(output) > BASH_MAX_OUTPUT_CHARS:
            # 截断 + 导航提示（read_file 哲学）：模型应学会重定向大输出到文件再分页读
            output = (
                output[:BASH_MAX_OUTPUT_CHARS]
                + f'\n(output truncated at {BASH_MAX_OUTPUT_CHARS} chars; '
                  'redirect to a file and use read_file for more)'
            )
        if exit_code != 0:
            # 退出码非 0 是"结果"不是异常：模型看到 [exit code: N] 自己判断怎么修
            # （README 对照表里 harness 的 "[exit code: N] 式跨调用准则"在此补上）
            content = f'[exit code: {exit_code}]\n{output}' if output else f'[exit code: {exit_code}]'
            return ToolOutcome(content=content, is_error=True)
        return ToolOutcome(content=output or '(no output)')

    registry.register(ToolSpec(
        name='bash',
        description=(
            f'Execute a shell command with {SHELL_NAME} and return its output.{SHELL_HINT} '
            'Non-zero exit code is reported as [exit code: N] with the output. '
            'The working directory is the workspace root unless `cwd` says otherwise. '
            'Output is capped at 8000 chars: redirect large outputs to a file and read it '
            'with read_file. Requires user approval before running.'),
        parameters={
            'type': 'object',
            'properties': {
                'command': {'type': 'string',
                            'description': f'Shell command to run (shell: {SHELL_NAME}).'},
                'cwd': {'type': 'string', 'description': 'Working directory (must be inside the workspace); defaults to the workspace root.'},
            },
            'required': ['command'],
        },
        execute=bash,
        timeout_s=bash_timeout_s,
        execution_mode='sequential',
        requires_approval=True,
    ))
