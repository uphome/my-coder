"""应用层工具：文件读写（read_file / list_files / write_file / edit）。

每个模块提供 `register(registry, workspace)`：把工具 executor + ToolSpec
注册进共享注册表——workspace 边界在注册时刻注入（executor 闭包捕获），
安全边界是"注册时的声明"。executor 拿到的每个路径都先过
resolve_in_workspace 校验，越界拒绝（轻量沙箱）。
"""
from __future__ import annotations

from pathlib import Path

from ..registry import ToolOutcome, ToolSpec
from ..sandbox import occurrence_lines, resolve_in_workspace


def register(registry, workspace: Path) -> None:
    async def read_file(args, agent, signal):
        path, denied = resolve_in_workspace(args['file_path'], workspace)
        if denied:
            return denied
        if not path.is_file():
            return ToolOutcome(content=f'file not found: {args["file_path"]}', is_error=True)
        # 行号分页：offset（1-based 起始行）+ limit（最大行数）。
        # 行号让模型能引用"第 N 行"（edit 工具的前置）；分页防止大文件一次读爆上下文。
        try:
            offset = int(args.get('offset', 1))
            limit = int(args.get('limit', 200))
        except (TypeError, ValueError):
            return ToolOutcome(content='offset and limit must be integers', is_error=True)
        if offset < 1:
            return ToolOutcome(content=f'offset must be >= 1, got {offset}', is_error=True)
        if limit < 1:
            return ToolOutcome(content=f'limit must be >= 1, got {limit}', is_error=True)
        # line_numbers 开关：LLM 自己决定要不要行号——读代码要坐标（引用"第 N 行"），
        # 读文档/日志时行号是纯 token 浪费，传 false 拿裸行。
        raw = args.get('line_numbers', True)
        if isinstance(raw, bool):
            line_numbers = raw
        elif isinstance(raw, str) and raw.strip().lower() in ('true', '1', 'yes'):
            line_numbers = True
        elif isinstance(raw, str) and raw.strip().lower() in ('false', '0', 'no'):
            line_numbers = False
        else:
            return ToolOutcome(content='line_numbers must be a boolean', is_error=True)
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        total = len(lines)
        if total == 0:
            return ToolOutcome(content='(empty file)')
        if offset > total:
            # 越界是明确错误：告诉模型文件总行数，让它自己调整（宁炸勿静默）
            return ToolOutcome(
                content=f'file has {total} lines, offset {offset} out of range', is_error=True)
        end = min(offset + limit - 1, total)
        selected = lines[offset - 1:end]
        if line_numbers:
            content = '\n'.join(f'{i:>4}: {line}' for i, line in enumerate(selected, start=offset))
        else:
            content = '\n'.join(selected)
        if end < total:
            # 截断提示是关键 UX：模型必须知道"后面还有"，否则会以为文件就这些
            content += (
                f'\n(file has {total} lines; showing lines {offset}-{end}; '
                'increase offset to continue)'
            )
        return ToolOutcome(content=content)

    async def list_files(args, agent, signal):
        path, denied = resolve_in_workspace(args.get('dir_path', '.'), workspace)
        if denied:
            return denied
        if not path.is_dir():
            return ToolOutcome(content=f'not a directory: {path}', is_error=True)
        names = sorted(entry.name for entry in path.iterdir())
        return ToolOutcome(content='\n'.join(names) if names else '(empty)')

    async def write_file(args, agent, signal):
        path, denied = resolve_in_workspace(args['file_path'], workspace)
        if denied:
            return denied
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args['content'], encoding='utf-8')
        return ToolOutcome(content=f'wrote {path}')

    async def edit(args, agent, signal):
        # 精确字符串替换式编辑：替换 write_file 全量覆盖（省 token——不用把整个
        # 文件重新写给模型）。对齐 harness str_replace_editor 的 str_replace 语义：
        # 字面量精确匹配（缩进/空白敏感），零匹配或多匹配都拒绝，刻意没有 replace_all。
        path, denied = resolve_in_workspace(args['file_path'], workspace)
        if denied:
            return denied
        if not path.is_file():
            return ToolOutcome(content=f'file not found: {args["file_path"]}', is_error=True)
        old_string = args['old_string']
        new_string = args.get('new_string', '')  # 缺省空 = 删除片段（对齐 harness）
        if not old_string:
            return ToolOutcome(content='old_string must not be empty', is_error=True)
        before = path.read_text(encoding='utf-8', errors='replace')
        count = before.count(old_string)
        if count == 0:
            # 零匹配：提示模型检查空白/缩进，或先用 read_file 看准确内容（宁炸勿静默）
            return ToolOutcome(content=(
                f'old_string did not appear verbatim in {args["file_path"]}. '
                'Check whitespace/indentation, or use read_file to see the exact content.'
            ), is_error=True)
        if count > 1:
            # 多匹配：报所有出现行号，要求扩大上下文使其唯一（对齐 harness 的
            # FS_AMBIGUOUS_EDIT）——改错位置比拒绝更危险
            lines = ', '.join(str(n) for n in sorted(set(occurrence_lines(before, old_string))))
            return ToolOutcome(content=(
                f'old_string appears {count} times (lines {lines}). '
                'Make old_string unique by including more context.'
            ), is_error=True)
        offset = before.index(old_string)
        line_no = before.count('\n', 0, offset) + 1  # 1-based 起始行号
        after = before[:offset] + new_string + before[offset + len(old_string):]
        path.write_text(after, encoding='utf-8')
        # 返回行号：模型可以用 read_file 验证改动（闭环）
        return ToolOutcome(content=f'edited {args["file_path"]} (replaced at line {line_no})')

    registry.register(ToolSpec(
        name='read_file',
        description='Read a UTF-8 text file. Line numbers on by default (pass line_numbers=false for plain text); use offset/limit to page large files.',
        parameters={
            'type': 'object',
            'properties': {
                'file_path': {'type': 'string'},
                'offset': {'type': 'integer', 'description': '1-based start line, default 1'},
                'limit': {'type': 'integer', 'description': 'max lines to return, default 200'},
                'line_numbers': {'type': 'boolean', 'description': 'prepend line numbers, default true'},
            },
            'required': ['file_path'],
        },
        execute=read_file,
    ))
    registry.register(ToolSpec(
        name='list_files',
        description='List entries in a directory.',
        parameters={
            'type': 'object',
            'properties': {'dir_path': {'type': 'string'}},
        },
        execute=list_files,
    ))
    registry.register(ToolSpec(
        name='edit',
        description='Replace an exact string in a file. Saves tokens vs write_file full rewrite; old_string must appear verbatim EXACTLY once (whitespace matters). Requires user approval before running.',
        parameters={
            'type': 'object',
            'properties': {
                'file_path': {'type': 'string'},
                'old_string': {'type': 'string', 'description': 'Exact text to replace; must appear verbatim and uniquely (whitespace matters).'},
                'new_string': {'type': 'string', 'description': 'Replacement text; omit or pass empty to delete the old_string.'},
            },
            'required': ['file_path', 'old_string'],
        },
        execute=edit,
        execution_mode='sequential',
        requires_approval=True,
    ))
    registry.register(ToolSpec(
        name='write_file',
        description='Create or overwrite a UTF-8 text file. Requires user approval before running.',
        parameters={
            'type': 'object',
            'properties': {
                'file_path': {'type': 'string'},
                'content': {'type': 'string'},
            },
            'required': ['file_path', 'content'],
        },
        execute=write_file,
        execution_mode='sequential',
        requires_approval=True,
    ))
