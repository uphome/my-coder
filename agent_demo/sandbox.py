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
