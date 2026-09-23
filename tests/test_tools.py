"""工具层：read_file/grep/glob/edit/bash 的行为、路径沙箱边界与结果预算。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**断言与用例体一字未改**；
唯一差异是 26 处函数内冗余 import 被 ruff 的 F401/F811 删掉——那是拆分暴露出来的旧问题。）
"""
from __future__ import annotations

import json

import pytest

from agent_demo.agent import Agent
from agent_demo.constants import READ_FILE_MAX_CHARS, READ_FILE_MAX_LIMIT, TOOL_RESULT_MAX_CHARS
from agent_demo.hooks import Hooks
from agent_demo.llm import FakeLlm
from agent_demo.prompt import PromptRegistry
from agent_demo.registry import ToolOutcome, ToolRegistry, ToolSpec
from agent_demo.session import Session
from agent_demo.tools import build_tools


@pytest.mark.asyncio
async def test_read_file_pages_with_line_numbers(tmp_path):
    registry = build_tools(workspace=tmp_path)
    path = tmp_path / 'code.py'
    path.write_text(''.join(f'line {i}\n' for i in range(1, 6)), encoding='utf-8')

    # 默认：全部行 + 行号，窗口没截断时没有提示行
    full = await registry.execute('read_file', {'file_path': str(path)}, None)
    assert full.is_error is False
    assert full.content == (
        '   1: line 1\n   2: line 2\n   3: line 3\n   4: line 4\n   5: line 5'
    )

    # offset + limit 组合：从第 3 行开始只给 2 行，且必须告知后面还有
    paged = await registry.execute(
        'read_file', {'file_path': str(path), 'offset': 3, 'limit': 2}, None)
    assert paged.content == (
        '   3: line 3\n   4: line 4\n'
        '(file has 5 lines; showing lines 3-4; increase offset to continue)'
    )

    # 窗口恰好覆盖到文件末尾：没有截断提示
    tail = await registry.execute(
        'read_file', {'file_path': str(path), 'offset': 5, 'limit': 10}, None)
    assert tail.content == '   5: line 5'

    # line_numbers=false：裸行输出（读文档/日志省 token）；JSON 布尔和字符串写法都接受
    plain = await registry.execute(
        'read_file', {'file_path': str(path), 'line_numbers': False}, None)
    assert plain.content == 'line 1\nline 2\nline 3\nline 4\nline 5'
    plain_str = await registry.execute(
        'read_file', {'file_path': str(path), 'line_numbers': 'false'}, None)
    assert plain_str.content == 'line 1\nline 2\nline 3\nline 4\nline 5'


@pytest.mark.asyncio
async def test_read_file_errors_are_results(tmp_path):
    registry = build_tools(workspace=tmp_path)
    path = tmp_path / 'code.py'
    path.write_text('a\nb\nc\n', encoding='utf-8')

    missing = await registry.execute('read_file', {'file_path': str(tmp_path / 'nope.txt')}, None)
    assert missing.is_error and 'file not found' in missing.content

    oob = await registry.execute('read_file', {'file_path': str(path), 'offset': 10}, None)
    assert oob.is_error and 'file has 3 lines, offset 10 out of range' in oob.content

    bad = await registry.execute('read_file', {'file_path': str(path), 'offset': 'abc'}, None)
    assert bad.is_error and 'integers' in bad.content

    zero = await registry.execute('read_file', {'file_path': str(path), 'limit': 0}, None)
    assert zero.is_error and 'limit must be >= 1' in zero.content

    too_many = await registry.execute(
        'read_file', {'file_path': str(path), 'limit': READ_FILE_MAX_LIMIT + 1}, None)
    assert too_many.is_error and f'limit must be <= {READ_FILE_MAX_LIMIT}' in too_many.content

    weird = await registry.execute(
        'read_file', {'file_path': str(path), 'line_numbers': 'maybe'}, None)
    assert weird.is_error and 'line_numbers must be a boolean' in weird.content

    empty = tmp_path / 'empty.txt'
    empty.write_text('', encoding='utf-8')
    result = await registry.execute('read_file', {'file_path': str(empty)}, None)
    assert result.is_error is False and result.content == '(empty file)'


@pytest.mark.asyncio
async def test_registry_caps_long_tool_result():
    registry = ToolRegistry()

    async def long_tool(args, agent, signal):
        return ToolOutcome(content='x' * (TOOL_RESULT_MAX_CHARS + 1000), is_error=True)

    registry.register(ToolSpec(
        name='long_tool',
        description='returns a very long result',
        parameters={'type': 'object', 'properties': {}},
        execute=long_tool,
    ))
    out = await registry.execute('long_tool', {}, None)
    assert out.is_error is True
    assert len(out.content) <= TOOL_RESULT_MAX_CHARS
    assert 'output truncated at' in out.content


@pytest.mark.asyncio
async def test_read_file_caps_chars_with_paging_hint(tmp_path):
    registry = build_tools(workspace=tmp_path)
    path = tmp_path / 'big.txt'
    line = 'x' * 200
    path.write_text('\n'.join(line for _ in range(100)), encoding='utf-8')

    out = await registry.execute(
        'read_file', {'file_path': str(path), 'line_numbers': False}, None)
    assert out.is_error is False
    # 正文（不含提示行）必须落在字符预算内
    body = out.content.split('\n(file has ', 1)[0]
    assert len(body) <= READ_FILE_MAX_CHARS
    assert 'output truncated at' in out.content
    assert 'increase offset to continue' in out.content


@pytest.mark.asyncio
async def test_grep_matches_and_groups(tmp_path):
    registry = build_tools(workspace=tmp_path)
    (tmp_path / 'a.py').write_text('def foo():\n    return 1\n', encoding='utf-8')
    (tmp_path / 'b.py').write_text('x = foo()\n', encoding='utf-8')
    (tmp_path / 'notes.md').write_text('nothing here\n', encoding='utf-8')

    out = await registry.execute('grep', {'pattern': 'foo', 'path': str(tmp_path)}, None)
    assert out.is_error is False
    assert out.content.startswith('Found 2 matches')
    assert 'a.py\nLine 1: def foo():' in out.content
    assert 'b.py\nLine 1: x = foo()' in out.content
    assert 'notes.md' not in out.content  # 没命中的文件不出现在分组里

    # include 过滤：只搜 *.py
    only_py = await registry.execute(
        'grep', {'pattern': 'foo', 'path': str(tmp_path), 'include': '*.py'}, None)
    assert 'notes.md' not in only_py.content

    # 大小写敏感（对齐 harness：不公开大小写参数）
    case = await registry.execute('grep', {'pattern': 'FOO', 'path': str(tmp_path)}, None)
    assert case.content == '(no matches)'

    # 坏正则 / 坏 include / 路径不存在：一律降级为 is_error
    bad = await registry.execute('grep', {'pattern': '(unclosed', 'path': str(tmp_path)}, None)
    assert bad.is_error and 'invalid regex' in bad.content
    bad_inc = await registry.execute(
        'grep', {'pattern': 'foo', 'path': str(tmp_path), 'include': '*.py,*.md'}, None)
    assert bad_inc.is_error and 'one positive glob' in bad_inc.content
    missing = await registry.execute('grep', {'pattern': 'foo', 'path': str(tmp_path / 'nope')}, None)
    assert missing.is_error and 'not found' in missing.content

    # path 可以是单个文件（对齐 harness：grep 目标是文件或目录）
    single_file = await registry.execute(
        'grep', {'pattern': 'foo', 'path': str(tmp_path / 'a.py')}, None)
    assert single_file.content.startswith('Found 1 match')
    assert 'a.py\nLine 1' in single_file.content


@pytest.mark.asyncio
async def test_grep_skips_hidden_and_truncates(tmp_path):
    registry = build_tools(workspace=tmp_path)
    (tmp_path / '.hidden.py').write_text('secret = 1\n', encoding='utf-8')
    git = tmp_path / '.git'
    git.mkdir()
    (git / 'config').write_text('secret = 2\n', encoding='utf-8')
    big = tmp_path / 'big.py'
    big.write_text(''.join(f'match {i}\n' for i in range(260)), encoding='utf-8')

    # 隐藏条目（. 开头目录/文件）不进搜索结果
    hidden = await registry.execute('grep', {'pattern': 'secret', 'path': str(tmp_path)}, None)
    assert hidden.content == '(no matches)'

    # 超过上限：头部 Found 250 of 260 + 截断页脚（模型必须知道还有更多）
    trunc = await registry.execute('grep', {'pattern': 'match', 'path': str(tmp_path)}, None)
    assert trunc.content.startswith('Found 250 of 260 matches')
    assert 'narrow pattern' in trunc.content


@pytest.mark.asyncio
async def test_glob_recursive_and_skip_hidden(tmp_path):
    registry = build_tools(workspace=tmp_path)
    (tmp_path / 'a.py').write_text('x\n', encoding='utf-8')
    tests = tmp_path / 'tests'
    tests.mkdir()
    (tests / 'test_a.py').write_text('x\n', encoding='utf-8')
    (tests / 'test_b.txt').write_text('x\n', encoding='utf-8')
    hidden = tmp_path / '.hidden'
    hidden.mkdir()
    (hidden / 'h.py').write_text('x\n', encoding='utf-8')

    out = await registry.execute('glob', {'pattern': '**/*.py', 'path': str(tmp_path)}, None)
    assert out.is_error is False
    # 递归匹配 + 相对路径 + 排序输出 + 隐藏目录跳过
    assert out.content.splitlines() == ['a.py', 'tests/test_a.py']
    assert '.hidden' not in out.content

    no_match = await registry.execute('glob', {'pattern': '**/*.rs', 'path': str(tmp_path)}, None)
    assert no_match.content == '(no paths match)'

    # 畸形模式（如 [z-a]）：3.13 的 pathlib 宽容处理（按字面量，不抛错）——
    # 与 rg 的 invalid-regex 行为不同；工具如实返回空结果，模型自己会修正。
    # executor 仍保留 re.error/ValueError 兜底（防御其他平台/版本的异常）。
    bad = await registry.execute('glob', {'pattern': '[z-a]', 'path': str(tmp_path)}, None)
    assert bad.is_error is False and bad.content == '(no paths match)'

    # 超过上限：截断页脚
    many = tmp_path / 'many'
    many.mkdir()
    for i in range(105):
        (many / f'f{i:03}.txt').write_text('x\n', encoding='utf-8')
    trunc = await registry.execute('glob', {'pattern': 'many/**', 'path': str(tmp_path)}, None)
    assert trunc.content.endswith('(Showing 100 of 105 paths; narrow the pattern to see more.)')


@pytest.mark.asyncio
async def test_workspace_boundary_blocks_escape(tmp_path):
    registry = build_tools(workspace=tmp_path)
    outside = tmp_path.parent / 'outside.txt'
    outside.write_text('secret\n', encoding='utf-8')

    # 绝对路径越界 → is_error（模型看到原因能自己改正）
    denied = await registry.execute('read_file', {'file_path': str(outside)}, None)
    assert denied.is_error and 'outside workspace' in denied.content

    # 相对路径 .. 逃逸 → is_error（resolve 折叠后前缀不符）
    escaped = await registry.execute('read_file', {'file_path': '../outside.txt'}, None)
    assert escaped.is_error and 'outside workspace' in escaped.content

    # 越界写入不落盘
    write = await registry.execute('write_file', {'file_path': str(outside), 'content': 'x'}, None)
    assert write.is_error and 'outside workspace' in write.content
    assert outside.read_text(encoding='utf-8') == 'secret\n'

    # grep / glob 的 path 越界同样拒绝
    grep = await registry.execute('grep', {'pattern': 'x', 'path': str(outside.parent)}, None)
    assert grep.is_error and 'outside workspace' in grep.content
    glb = await registry.execute('glob', {'pattern': '**', 'path': str(outside.parent)}, None)
    assert glb.is_error and 'outside workspace' in glb.content

    # workspace 内一切正常
    inner = tmp_path / 'inner.txt'
    inner.write_text('ok\n', encoding='utf-8')
    ok = await registry.execute('read_file', {'file_path': str(inner)}, None)
    assert ok.is_error is False and ok.content == '   1: ok'


def test_build_tools_requires_explicit_workspace():
    # 安全边界必须显式声明：不传 workspace 直接抛错（严格校验哲学）
    with pytest.raises(ValueError, match='workspace'):
        build_tools(None)


@pytest.mark.asyncio
async def test_edit_replaces_exact_string(tmp_path):
    registry = build_tools(workspace=tmp_path)
    path = tmp_path / 'code.py'
    path.write_text('def foo():\n    return 1\n', encoding='utf-8')

    out = await registry.execute(
        'edit',
        {'file_path': str(path), 'old_string': '    return 1', 'new_string': '    return 2'},
        None)
    assert out.is_error is False
    assert out.content == f'edited {path} (replaced at line 2)'
    assert path.read_text(encoding='utf-8') == 'def foo():\n    return 2\n'

    # new_string 缺省 = 删除片段（对齐 harness）
    out2 = await registry.execute(
        'edit', {'file_path': str(path), 'old_string': 'def foo():\n'}, None)
    assert out2.is_error is False
    assert path.read_text(encoding='utf-8') == '    return 2\n'


@pytest.mark.asyncio
async def test_edit_requires_unique_verbatim_match(tmp_path):
    registry = build_tools(workspace=tmp_path)
    path = tmp_path / 'code.py'
    path.write_text('x = 1\ny = x\nx = 2\n', encoding='utf-8')

    # 零匹配（字面量、空白敏感）：提示模型检查空白/缩进
    missing = await registry.execute(
        'edit', {'file_path': str(path), 'old_string': 'x=1'}, None)
    assert missing.is_error and 'did not appear verbatim' in missing.content

    # 多匹配：报所有出现行号，拒绝替换——改错位置比拒绝更危险（宁炸勿静默）
    ambiguous = await registry.execute(
        'edit', {'file_path': str(path), 'old_string': 'x = ', 'new_string': 'z = '}, None)
    assert ambiguous.is_error and 'appears 2 times' in ambiguous.content
    assert 'lines 1, 3' in ambiguous.content
    assert path.read_text(encoding='utf-8') == 'x = 1\ny = x\nx = 2\n'  # 文件未被改动

    # 空 old_string 拒绝（严格校验）
    empty = await registry.execute(
        'edit', {'file_path': str(path), 'old_string': '', 'new_string': 'z'}, None)
    assert empty.is_error and 'must not be empty' in empty.content

    # 文件不存在 / 越界路径（沙箱自动继承）
    missing_file = await registry.execute(
        'edit', {'file_path': str(tmp_path / 'nope.py'), 'old_string': 'x'}, None)
    assert missing_file.is_error and 'file not found' in missing_file.content
    outside = tmp_path.parent / 'outside.txt'
    outside.write_text('x\n', encoding='utf-8')
    denied = await registry.execute(
        'edit', {'file_path': str(outside), 'old_string': 'x'}, None)
    assert denied.is_error and 'outside workspace' in denied.content


@pytest.mark.asyncio
async def test_bash_runs_and_reports_exit_code(tmp_path):
    registry = build_tools(workspace=tmp_path)

    ok = await registry.execute('bash', {'command': 'echo hello'}, None)
    assert ok.is_error is False
    assert ok.content.strip() == 'hello'

    # 退出码非 0 → is_error + [exit code: N] 前缀（模型看到结果自己修）
    fail = await registry.execute('bash', {'command': 'exit 3'}, None)
    assert fail.is_error
    assert fail.content.startswith('[exit code: 3]')

    # 成功但无输出 → (no output)
    empty = await registry.execute('bash', {'command': 'cd .'}, None)
    assert empty.is_error is False and empty.content == '(no output)'

    # 空命令拒绝
    blank = await registry.execute('bash', {'command': '   '}, None)
    assert blank.is_error and 'must not be empty' in blank.content


@pytest.mark.asyncio
async def test_bash_cwd_and_truncation(tmp_path):
    registry = build_tools(workspace=tmp_path)
    sub = tmp_path / 'sub'
    sub.mkdir()
    (sub / 'probe.txt').write_text('x\n', encoding='utf-8')

    # cwd 生效：命令在指定目录下跑
    listed = await registry.execute(
        'bash', {'command': 'python -c "import os; print(sorted(os.listdir()))"', 'cwd': str(sub)}, None)
    assert listed.is_error is False and 'probe.txt' in listed.content

    # cwd 越界 → is_error（沙箱继承）
    denied = await registry.execute('bash', {'command': 'echo hi', 'cwd': str(tmp_path.parent)}, None)
    assert denied.is_error and 'outside workspace' in denied.content

    # 大输出截断 + 重定向导航提示
    big = await registry.execute('bash', {'command': 'python -c "print(\'x\' * 20000)"'}, None)
    assert big.is_error is False
    assert big.content.startswith('x' * 100)
    assert 'truncated at 8000 chars' in big.content
    assert 'redirect to a file' in big.content


@pytest.mark.asyncio
async def test_bash_timeout_kills_process(tmp_path):
    # 注入短超时：wait_for 取消 → executor kill 子进程 → TimeoutError → is_error
    registry = build_tools(workspace=tmp_path, bash_timeout_s=0.3)
    slow = await registry.execute('bash', {'command': 'python -c "import time; time.sleep(5)"'}, None)
    assert slow.is_error and 'timed out' in slow.content


@pytest.mark.asyncio
async def test_approval_gate_approves_and_skips(tmp_path):
    # 批准路径：确认钩子返回 True → 工具执行、文件写入、无 skipped
    decisions = []
    approve_hooks = Hooks()

    async def approve(name, args):
        decisions.append(name)
        return True

    approve_hooks.approval = approve
    agent = Agent(
        session=Session(id='s'),
        llm=FakeLlm(script=[
            {
                'tool_calls': [{'id': 'c1', 'name': 'write_file',
                                'arguments': json.dumps({'file_path': 'out.txt', 'content': 'hi'})}],
                'finish_reason': 'tool_calls',
            },
            {'text': 'done', 'finish_reason': 'stop'},
        ]),
        prompt=PromptRegistry(), tools=build_tools(workspace=tmp_path),
        options={'provider': 'fake', 'model': 'fake-model'}, hooks=approve_hooks,
    )
    agent.followup('write a file')
    await agent.when_idle()
    assert decisions == ['write_file']
    assert (tmp_path / 'out.txt').read_text(encoding='utf-8') == 'hi'
    assert not [e for e in agent.session.events if e.type == 'tool/skipped']

    # 拒绝路径：确认钩子返回 False → tool/skipped + is_error result + 文件未写入 + 循环正常收尾
    reject_hooks = Hooks()

    async def reject(name, args):
        return False

    reject_hooks.approval = reject
    agent2 = Agent(
        session=Session(id='s2'),
        llm=FakeLlm(script=[
            {
                'tool_calls': [{'id': 'c1', 'name': 'write_file',
                                'arguments': json.dumps({'file_path': 'evil.txt', 'content': 'evil'})}],
                'finish_reason': 'tool_calls',
            },
            {'text': 'ok, skipped', 'finish_reason': 'stop'},
        ]),
        prompt=PromptRegistry(), tools=build_tools(workspace=tmp_path),
        options={'provider': 'fake', 'model': 'fake-model'}, hooks=reject_hooks,
    )
    agent2.followup('write a file')
    await agent2.when_idle()
    events = agent2.session.events
    skipped = [e for e in events if e.type == 'tool/skipped']
    assert skipped and skipped[0].data['reason'] == 'not-approved'
    results = [e for e in events if e.type == 'tool/result']
    assert results and 'skipped' in str(results[0].data)
    assert not (tmp_path / 'evil.txt').exists()  # 拒绝后文件未写入
    assert events[-1].data['reason'] == 'completed'  # 循环正常完成，不是炸掉
