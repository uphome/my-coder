"""应用层：轻量路径沙箱——工具路径的 workspace 边界（纯用户态）。

原理（对齐 harness 的 canonicalPath / fs-sandbox fence）：相对路径锚定到
workspace → resolve() 归一化（折叠 ..、解符号链接）→ relative_to() 逐段
前缀匹配 → 越界返回 is_error 结果。这是"防误用保险"，不是 OS 级沙箱
（TOCTOU 等不设防，README 有安全警告）；OS 级强制留给 bash 的沙箱 runner。
"""
from __future__ import annotations

import os
from pathlib import Path

from .registry import ToolOutcome


def resolve_in_workspace(raw: str, workspace: Path) -> tuple[Path | None, ToolOutcome | None]:
    """把工具给的路径解析到 workspace 内；越界返回错误结果（轻量沙箱边界）。

    - 相对路径一律相对 workspace 解析（生产环境 workspace=cwd，与历史行为一致）
    - 先 resolve() 再 relative_to()：解掉 .. 和符号链接，杜绝"看似在内实则在外"
    - 越界不是异常而是 is_error 结果：模型看到原因（path outside workspace）
      自己会改正——失败降级为结果，不炸循环
    """
    path = Path(raw)
    if not path.is_absolute():
        path = workspace / path
    try:
        path.resolve().relative_to(workspace)
    except ValueError:
        return None, ToolOutcome(
            content=f'path outside workspace: {path} (workspace is {workspace})',
            is_error=True,
        )
    return path, None


def workspace_escape_reason(path: Path, resolved_workspace: Path) -> str | None:
    """**宿主自己读文件**前的越界检查：`None` = 在工作区内，否则返回"读不到"的原因。

    与 `resolve_in_workspace` 是两件事，不是重复：

    - `resolve_in_workspace` 管**工具入参**（模型给的字符串 → 解析 + 越界 → is_error 结果）；
    - 这条管**宿主直读**的两条路——指令文件（`instructions.py`）与工作区来源的技能
      （`skills.py`）。它们不走工具沙箱（读的是宿主自己发现的文件），但必须做**同样的**
      检查：否则一个 `AGENTS.md -> ~/.ssh/id_rsa` 就能把工作区外的文件塞进 system prompt，
      或者一份 `skills/x.md -> 工作区外的 md` 被当成技能读进对话——两者都与 persona 的
      "工作区外不可读"直接矛盾。

    判据与措辞两处共用（一条规则一处实现）；`resolve()` 失败（循环链接、权限）也算越界：
    **读不确定的文件，不如不读**。

    `resolved_workspace` 必须传 **`resolve()` 过的**工作区根（两条调用方都已经持有），
    否则"工作区自己就是符号链接"的平台上会把区内的文件误判成越界。
    """
    try:
        resolved = path.resolve()
    except OSError as error:
        return f'cannot resolve: {error}'
    if not resolved.is_relative_to(resolved_workspace):
        return 'outside the workspace (symlink?)'
    return None


def iter_files(root: Path):
    """递归产出 root 下的普通文件（相对路径显示用），跳过隐藏条目与 __pycache__。

    隐藏条目（. 开头，如 .git/.sessions/.codegraph/.env）是噪音甚至敏感数据
    （.env 里有 API key），grep/glob 搜"代码"不该看到它们。
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and d != '__pycache__']
        for name in filenames:
            if name.startswith('.'):
                continue
            yield Path(dirpath) / name


def occurrence_lines(content: str, search: str) -> list[int]:
    """search 每次出现处的 1-based 起始行号（同一行多次出现产生重复行号，调用方去重）。"""
    lines: list[int] = []
    offset = 0
    while True:
        idx = content.find(search, offset)
        if idx < 0:
            return lines
        lines.append(content.count('\n', 0, idx) + 1)
        offset = idx + len(search)
