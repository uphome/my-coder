"""应用层工具包：build_tools() 组装全部工具进共享注册表。

workspace 必须显式指定（调用者声明边界）：CLI 用 --workspace（必填），
测试注入 tmp_path。安全边界是"注册时的声明"——每个 executor 在注册时
闭包捕获 workspace，所有路径先过 resolve_in_workspace 校验。

安全模型（README 安全警告如实声明）：工作区内全裸、TOCTOU 竞态、
非 OS 级沙箱——bash 的 OS 级沙箱 runner 是演进预留（tools/shell.py 的
_run_command 是唯一接缝）。
"""
from __future__ import annotations

from pathlib import Path

from ..registry import ToolRegistry
from . import file_io, search, shell, todo


def build_tools(workspace: Path | None, bash_timeout_s: float = 60.0) -> ToolRegistry:
    """工具注册表：全部文件工具共用一个 workspace 边界（轻量沙箱）。"""
    if workspace is None:
        raise ValueError('build_tools requires an explicit workspace（安全边界必须显式声明）')
    workspace = workspace.resolve()
    registry = ToolRegistry()
    file_io.register(registry, workspace)
    search.register(registry, workspace)
    shell.register(registry, workspace, bash_timeout_s=bash_timeout_s)
    todo.register(registry)
    return registry
