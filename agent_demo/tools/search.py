"""应用层工具：代码搜索（grep 按内容 / glob 按路径模式）。

按内容搜（正则）与按路径找（glob）是互补的两个方向：grep 拿到"位置"、
glob 拿到"路径清单"。预算常量（GREP_MAX_MATCHES / GLOB_MAX_RESULTS）
集中在 constants——上限不进模型 schema，模型只看到"前 N 条 + 截断提示"。
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from ..constants import GLOB_MAX_RESULTS, GREP_MAX_MATCHES
from ..registry import ToolOutcome, ToolSpec
from ..sandbox import iter_files, resolve_in_workspace


def register(registry, workspace: Path) -> None:
    async def grep(args, agent, signal):
        # 模型知道"内容片段"，拿"位置"——和 read_file 方向相反。
        # 输出对齐 harness：Found N of M matches + 按文件分组 Line N: <text>。
        pattern = args['pattern']
        include = args.get('include')
        if include is not None:
            # include 必须是单个正向 glob：拒绝否定（!）和逗号列表（对齐 harness 的校验）
            if not include.strip() or include.startswith('!') or ',' in include:
                return ToolOutcome(
                    content='include must be one positive glob (e.g. "*.py"); negation and lists are not supported',
                    is_error=True,
                )
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            # 坏正则是模型给的坏输入：降级为结果，模型拿到原因自己改（宁炸勿静默）
            return ToolOutcome(content=f'invalid regex: {exc}', is_error=True)
        include_re = None
        if include is not None:
            # 预编译 include：fnmatch 每次调用都编译，预编译一次更快，
            # 且坏模式（如未闭合的 [）在编译时刻显式报错，而不是搜索中途炸
            try:
                include_re = re.compile(fnmatch.translate(include))
            except re.error as exc:
                return ToolOutcome(content=f'invalid include glob: {exc}', is_error=True)
        target, denied = resolve_in_workspace(args.get('path', '.'), workspace)
        if denied:
            return denied
        if target.is_file():
            # grep.path 可以是单个文件（对齐 harness：grep 目标是文件或目录）
            files = [(target, target.name)]
        elif target.is_dir():
            files = [(p, p.relative_to(target).as_posix()) for p in iter_files(target)]
        else:
            return ToolOutcome(content=f'not found: {args.get("path", ".")}', is_error=True)
        matches = []
        for path, rel in files:
            if include_re is not None and not include_re.search(path.name):
                continue
            for lineno, line in enumerate(
                    path.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
                if regex.search(line):
                    matches.append((rel, lineno, line))
        if not matches:
            return ToolOutcome(content='(no matches)')
        kept = matches[:GREP_MAX_MATCHES]
        # 按文件分组：每个文件一段 rel 路径 + Line N: <text> 行
        sections = []
        by_file = {}
        for rel, lineno, line in kept:
            by_file.setdefault(rel, []).append((lineno, line))
        for rel, rows in by_file.items():
            sections.append(f'{rel}\n' + '\n'.join(f'Line {n}: {text}' for n, text in rows))
        body = '\n\n'.join(sections)
        if len(matches) > GREP_MAX_MATCHES:
            # 截断页脚：模型必须知道"还有更多"，否则会以为搜索就这些
            header = f'Found {len(kept)} of {len(matches)} matches'
            body += f'\n\n(Showing first {GREP_MAX_MATCHES}; narrow pattern, path, or include to see more.)'
        else:
            header = f'Found {len(matches)} matches'
        return ToolOutcome(content=f'{header}\n\n{body}')

    async def glob_tool(args, agent, signal):
        # 模型知道"名字形状"，拿"路径清单"——与 grep 方向相反。
        pattern = args['pattern']
        target, denied = resolve_in_workspace(args.get('path', '.'), workspace)
        if denied:
            return denied
        if not target.is_dir():
            return ToolOutcome(content=f'not a directory: {target}', is_error=True)
        try:
            paths = sorted(
                p.relative_to(target).as_posix()
                for p in target.glob(pattern)
                if p.is_file()
                and not any(part.startswith('.') or part == '__pycache__'
                            for part in p.relative_to(target).parts)
            )
        except (re.error, ValueError) as exc:
            # 坏 glob 模式（如未闭合的 [）同样降级为结果
            return ToolOutcome(content=f'invalid glob pattern: {exc}', is_error=True)
        if not paths:
            return ToolOutcome(content='(no paths match)')
        kept = paths[:GLOB_MAX_RESULTS]
        content = '\n'.join(kept)
        if len(paths) > GLOB_MAX_RESULTS:
            content += f'\n\n(Showing {len(kept)} of {len(paths)} paths; narrow the pattern to see more.)'
        return ToolOutcome(content=content)

    registry.register(ToolSpec(
        name='grep',
        description='Search file contents with a regular expression. Returns matching lines with line numbers, grouped by file.',
        parameters={
            'type': 'object',
            'properties': {
                'pattern': {'type': 'string', 'description': 'Regular expression to search for.'},
                'path': {'type': 'string', 'description': 'File or directory to search; defaults to the workspace, relative paths resolve against it.'},
                'include': {'type': 'string', 'description': 'One glob filter for which files to search (e.g. "*.py", "*.{js,jsx}"). Not a list; negation is not supported.'},
            },
            'required': ['pattern'],
        },
        execute=grep,
    ))
    registry.register(ToolSpec(
        name='glob',
        description='Find files whose paths match a glob pattern. Returns one relative path per line.',
        parameters={
            'type': 'object',
            'properties': {
                'pattern': {'type': 'string', 'description': 'Glob pattern relative to path (e.g. "**/*.py", "tests/**/test_*.py").'},
                'path': {'type': 'string', 'description': 'Directory to search in; defaults to the workspace.'},
            },
            'required': ['pattern'],
        },
        execute=glob_tool,
    ))
