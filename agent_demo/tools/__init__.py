"""应用层工具包：build_tools() 组装全部工具进共享注册表。

workspace 必须显式指定（调用者声明边界）：CLI 用 --workspace（必填），
测试注入 tmp_path。安全边界是"注册时的声明"——每个 executor 在注册时
闭包捕获 workspace，所有路径先过 resolve_in_workspace 校验。

skill 工具是两个"例外"里较轻的那个：它的入参只有**技能名**，由 host 把名字解析到
文件（bundled 技能在包内、工作区之外），所以它读得到工作区外的自带资源，却**不需要**
给沙箱开例外——模型没有机会拼出路径。web_search 是另一个例外：它读公共互联网。

安全模型（README 安全警告如实声明）：工作区内全裸、TOCTOU 竞态、
非 OS 级沙箱——bash 的 OS 级沙箱 runner 是演进预留（tools/shell.py 的
_run_command 是唯一接缝）。
"""
from __future__ import annotations

from pathlib import Path

from ..registry import ToolRegistry
from ..skills import SkillTable
from . import file_io, search, shell, skill, todo, web_search


def build_tools(
    workspace: Path | None, bash_timeout_s: float = 60.0, skills: SkillTable | None = None,
) -> ToolRegistry:
    """工具注册表：全部文件工具共用一个 workspace 边界（轻量沙箱）。

    web_search 与 skill 是例外（见模块 docstring）。`skills` 可注入
    `factory.build_agent` 构造好的技能表：**同一个实例**同时喂给目录段（live 段
    provider）与 skill 工具，两处永不漂移，会话中途新增的技能也两边同时可见；
    不传就自己建一个（测试与单独用 build_tools 的场景）。
    """
    if workspace is None:
        raise ValueError('build_tools requires an explicit workspace（安全边界必须显式声明）')
    workspace = workspace.resolve()
    skills = SkillTable(workspace) if skills is None else skills
    registry = ToolRegistry()
    file_io.register(registry, workspace)
    search.register(registry, workspace)
    skill.register(registry, skills)
    shell.register(registry, workspace, bash_timeout_s=bash_timeout_s)
    todo.register(registry)
    web_search.register(registry)
    return registry
