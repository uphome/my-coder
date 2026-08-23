"""CLI 入口：一句话任务 → 跑完整个 agent → 流式打印。

演示"UI 是日志的投影"：屏幕上的输出不是从模型回调来的，
而是订阅 session 事件（assistant/chunk、tool/call）渲染的。
"""
from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import logging
import os
import re
from pathlib import Path

from agent import Agent
from llm import FakeLlm, OpenAiCompatibleLlm
from persistence import load_events
from prompt import PromptRegistry
from session import Session
from tools import ToolOutcome, ToolRegistry, ToolSpec
from values import TextBlock

# 终端颜色：思维链用青色 + 暗淡样式，和正式回答区分开。
# 后续如果做 Web UI，这里可以换成真正的可折叠组件。
_REASONING_COLOR = '\033[36m'   # cyan
_REASONING_DIM = '\033[2m'      # dim
_RESET = '\033[0m'

# 搜索预算（对齐 harness 的 tool-fs-search，也是 Claude Code 的默认值）：
# 常规上限不进模型 schema，模型只看到"前 N 条 + 截断提示"。
GREP_MAX_MATCHES = 250   # grep 内联保留的最大匹配数
GLOB_MAX_RESULTS = 100   # glob 内联保留的最大路径数

DEMO_SCRIPT = [
    {
        'reasoning': '用户让我总结 README，先读取文件内容再回答。',
        'tool_calls': [{'id': 'call-1', 'name': 'read_file', 'arguments': json.dumps({'file_path': 'README.md'})}],
        'finish_reason': 'tool_calls',
    },
    {
        'reasoning': 'README 已经读完，核心是四层架构，现在整理成简短总结。',
        'text': 'README 讲的是这个 demo 的四层架构。任务完成。',
        'finish_reason': 'stop',
    },
]


def load_env(path: Path) -> None:
    """把 .env 里的 KEY=VALUE 注入进程环境；已存在的环境变量优先（不覆盖）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def build_tools(workspace: Path) -> ToolRegistry:
    """工具注册表：全部文件工具共用一个 workspace 边界（轻量沙箱）。

    workspace 必须显式指定（调用者声明边界）：CLI 用 --workspace（必填），
    测试注入 tmp_path。安全边界是"注册时的声明"：executor 拿到的每个路径
    都先过 _resolve_in_workspace 校验，越界拒绝——这是纯用户态的路径沙箱
    （归一化 + 前缀匹配，对齐 harness 的 canonicalPath / fs-sandbox fence），
    不是 OS 级沙箱（TOCTOU 竞态等不设防，README 有安全警告）。
    """
    if workspace is None:
        raise ValueError('build_tools requires an explicit workspace（安全边界必须显式声明）')
    workspace = workspace.resolve()
    registry = ToolRegistry()

    async def read_file(args, agent, signal):
        path, denied = _resolve_in_workspace(args['file_path'], workspace)
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
        path, denied = _resolve_in_workspace(args.get('dir_path', '.'), workspace)
        if denied:
            return denied
        if not path.is_dir():
            return ToolOutcome(content=f'not a directory: {path}', is_error=True)
        names = sorted(entry.name for entry in path.iterdir())
        return ToolOutcome(content='\n'.join(names) if names else '(empty)')

    async def grep(args, agent, signal):
        # 按内容搜（正则）：模型知道"内容片段"，拿"位置"——和 read_file 方向相反。
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
        target, denied = _resolve_in_workspace(args.get('path', '.'), workspace)
        if denied:
            return denied
        if target.is_file():
            # grep.path 可以是单个文件（对齐 harness：grep 目标是文件或目录）
            files = [(target, target.name)]
        elif target.is_dir():
            files = [(p, p.relative_to(target).as_posix()) for p in _iter_files(target)]
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
            body += '\n\n(Showing first %d; narrow pattern, path, or include to see more.)' % GREP_MAX_MATCHES
        else:
            header = f'Found {len(matches)} matches'
        return ToolOutcome(content=f'{header}\n\n{body}')

    async def glob_tool(args, agent, signal):
        # 按路径模式找文件（如 **/*.py）：模型知道"名字形状"，拿"路径清单"。
        pattern = args['pattern']
        target, denied = _resolve_in_workspace(args.get('path', '.'), workspace)
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

    async def write_file(args, agent, signal):
        path, denied = _resolve_in_workspace(args['file_path'], workspace)
        if denied:
            return denied
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args['content'], encoding='utf-8')
        return ToolOutcome(content=f'wrote {path}')

    async def edit(args, agent, signal):
        # 精确字符串替换式编辑：替换 write_file 全量覆盖（省 token——不用把整个
        # 文件重新写给模型）。对齐 harness str_replace_editor 的 str_replace 语义：
        # 字面量精确匹配（缩进/空白敏感），零匹配或多匹配都拒绝，刻意没有 replace_all。
        path, denied = _resolve_in_workspace(args['file_path'], workspace)
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
            lines = ', '.join(str(n) for n in sorted(set(_occurrence_lines(before, old_string))))
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

    async def todo_write(args, agent, signal):
        agent.session.append('todo/write', {'todos': args.get('todos', [])})
        return ToolOutcome(content='todo list updated')

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
    registry.register(ToolSpec(
        name='edit',
        description='Replace an exact string in a file. Saves tokens vs write_file full rewrite; old_string must appear verbatim EXACTLY once (whitespace matters).',
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
    ))
    registry.register(ToolSpec(
        name='write_file',
        description='Create or overwrite a UTF-8 text file.',
        parameters={
            'type': 'object',
            'properties': {
                'file_path': {'type': 'string'},
                'content': {'type': 'string'},
            },
            'required': ['file_path', 'content'],
        },
        execute=write_file,
    ))
    registry.register(ToolSpec(
        name='todo_write',
        description='Record and update a structured task list. The ENTIRE list replaces the previous one.',
        parameters={
            'type': 'object',
            'properties': {
                'todos': {'type': 'array', 'items': {'type': 'object'}},
            },
            'required': ['todos'],
        },
        execute=todo_write,
    ))
    return registry


def _iter_files(root: Path):
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


def _occurrence_lines(content: str, search: str) -> list[int]:
    """search 每次出现处的 1-based 起始行号（同一行多次出现产生重复行号，调用方去重）。"""
    lines: list[int] = []
    offset = 0
    while True:
        idx = content.find(search, offset)
        if idx < 0:
            return lines
        lines.append(content.count('\n', 0, idx) + 1)
        offset = idx + len(search)


def _resolve_in_workspace(raw: str, workspace: Path) -> tuple[Path | None, ToolOutcome | None]:
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


def build_agent(session: Session, args) -> Agent:
    prompt = PromptRegistry()
    prompt.section('identity', -100, 'You are a coding agent powered by DeepSeek Harness (Python demo).')
    prompt.section('persona', 0, 'You run on the {{model}} model. Your workspace is {{workspace}}; tool paths resolve relative to it, and nothing outside it is readable or writable.\nVerify work by running code or tests. Keep answers brief.')
    prompt.section('tool:todo', 110, 'Use todo_write to plan multi-step work before you start.')
    prompt.variable('model', lambda ctx: ctx['agent'].options.get('model', ''))
    prompt.variable('workspace', lambda ctx: str(args.workspace))

    if args.fake:
        llm = FakeLlm(script=DEMO_SCRIPT, provider='fake', model='fake-model')
        options = {'provider': 'fake', 'model': 'fake-model'}
    else:
        api_key = os.environ.get('DEEPSEEK_API_KEY')
        if not api_key:
            raise SystemExit('missing DEEPSEEK_API_KEY (set it in the environment or run with --fake)')
        llm = OpenAiCompatibleLlm(
            base_url=os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com'),
            api_key=api_key,
            model=args.model,
            provider='deepseek',
        )
        options = {'provider': 'deepseek', 'model': args.model}

    agent = Agent(session=session, llm=llm, prompt=prompt, tools=build_tools(workspace=args.workspace), options=options)

    reasoning_started = False

    def _close_reasoning() -> None:
        nonlocal reasoning_started
        if reasoning_started:
            print(f'{_RESET}\n', end='', flush=True)
            reasoning_started = False

    def on_event(event) -> None:
        nonlocal reasoning_started
        if event.type == 'request/header':
            # 每次新请求前关闭可能未闭合的思维链（例如失败重试场景）。
            _close_reasoning()
        elif event.type == 'assistant/chunk':
            text = event.data['chunk']['text']
            if text:
                _close_reasoning()
                print(text, end='', flush=True)
        elif event.type == 'assistant/reasoning/chunk':
            if not args.hide_reasoning and event.data['reasoning']:
                if not reasoning_started:
                    print(
                        f'{_REASONING_COLOR}{_REASONING_DIM}[思考] ',
                        end='', flush=True,
                    )
                    reasoning_started = True
                print(event.data['reasoning'], end='', flush=True)
        elif event.type == 'assistant/reasoning':
            if not args.hide_reasoning and event.data['reasoning']:
                # 思维链流式片段已经实时打印，这里补一个换行，避免和正式回答粘在一起。
                _close_reasoning()
        elif event.type in ('step/end', 'turn/end'):
            _close_reasoning()
        elif event.type == 'tool/call':
            _close_reasoning()
            print(f'\n[tool] {event.data["name"]}({event.data["arguments"]})', flush=True)
        elif event.type == 'tool/result':
            result = event.data
            block = result.content[0]
            print(f'[result] {block.content[:200]}{"…" if len(block.content) > 200 else ""}', flush=True)

    session.on_event(on_event)
    return agent


async def run(args) -> None:
    load_env(Path(__file__).parent / '.env')
    session_path = Path(args.sessions) / f'{args.session}.jsonl'
    if session_path.exists() and not args.resume:
        raise SystemExit(
            f'session {args.session!r} already exists at {session_path} — '
            f'pass --resume to continue it, pick a new --session id, or delete the file',
        )
    session = Session(id=args.session)
    if args.resume and session_path.exists():
        for event in load_events(session_path):
            session.adopt(event)
        print(f'resumed {args.session}: {len(session.events)} events restored')
    session.bind_store(session_path)
    agent = build_agent(session, args)
    agent.followup(args.prompt)
    await agent.when_idle()
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description='Python demo of the harness agent architecture')
    parser.add_argument('prompt', help='the task to run')
    parser.add_argument('--session', default='main', help='session id (JSONL file under --sessions)')
    parser.add_argument('--sessions', default='.sessions', help='directory for JSONL session logs')
    parser.add_argument('--workspace', type=Path, required=True,
                        help='workspace root directory — tools may only read/write inside it (required)')
    parser.add_argument('--model', default='deepseek-chat', help='model id for the OpenAI-compatible API')
    parser.add_argument('--resume', action='store_true', help='resume the session from its JSONL log')
    parser.add_argument('--fake', action='store_true', help='offline scripted model (architecture demo)')
    parser.add_argument('--hide-reasoning', action='store_true',
                        help='折叠（隐藏）思维链，只记录到日志，不打印到终端')
    parser.add_argument('--verbose', action='store_true', help='debug logging')
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
