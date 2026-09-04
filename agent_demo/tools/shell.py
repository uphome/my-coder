"""应用层工具：shell 执行（bash）+ 执行后端接缝。

执行能力是编码闭环"读→改→验证"的最后一步。_run_command 收执行细节
（shell 语义、合并输出、kill-on-cancel）——将来接 OS 级沙箱 runner
（restricted token / bwrap / seatbelt）只换这一个函数，executor 与循环
层不动。命令级刹车是 approval 门（已绑定），命令本身不过路径沙箱——
受限的是 cwd，命令里的路径无法校验。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ..constants import BASH_MAX_OUTPUT_CHARS
from ..registry import ToolOutcome, ToolSpec
from ..sandbox import resolve_in_workspace


async def _run_command(command: str, cwd: Path) -> tuple[int, str]:
    """执行一条 shell 命令，返回 (退出码, 合并输出)。执行后端的接缝。

    - shell 语义（Windows 上是 cmd.exe /c），stdout/stderr 合并成顺序流
    - 取消（超时或用户 cancel）时 kill 掉子进程再传播：卡死的命令不能泄漏在后台
    """
    proc = await asyncio.create_subprocess_shell(
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
        description='Execute a shell command and return its output. Non-zero exit code is reported as [exit code: N] with the output. Output is capped at 8000 chars: redirect large outputs to a file and read it with read_file. Requires user approval before running.',
        parameters={
            'type': 'object',
            'properties': {
                'command': {'type': 'string', 'description': 'Shell command to run.'},
                'cwd': {'type': 'string', 'description': 'Working directory (must be inside the workspace); defaults to the workspace root.'},
            },
            'required': ['command'],
        },
        execute=bash,
        timeout_s=bash_timeout_s,
        execution_mode='sequential',
        requires_approval=True,
    ))
