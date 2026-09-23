"""工作区指令文件：三态探测、预算、live 段、子目录清单与越界检查。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**只搬家、不改断言**。）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_demo.instructions import (
    INSTRUCTION_MAX_FILE_CHARS,
    INSTRUCTION_MAX_SOURCE_BYTES,
    InstructionLoader,
)
from agent_demo.llm import (
    FakeLlm,
)
from agent_demo.session import Session


def test_instructions_section_reports_a_missing_file(tmp_path):
    """全新工作区：段里给"没有指令文件"的状态 + 创建指引（issue #6 方案 C）。"""
    rendered = InstructionLoader(tmp_path).render()
    assert 'files="none"' in rendered
    assert 'AGENTS.md' in rendered and 'propose writing' in rendered
    # 指引必须同时说清两条边界：写入要用户批准；不许写密钥/临时状态/未验证猜测
    assert 'approve' in rendered
    assert 'secrets' in rendered and 'unverified guesses' in rendered


def test_instructions_section_injects_an_existing_root_file(tmp_path):
    """已有指令文件：正文进段里（不是只给路径），并注明来源路径。"""
    (tmp_path / 'AGENTS.md').write_text('# 约定\n\n跑测试: pytest -q\n', encoding='utf-8')
    loader = InstructionLoader(tmp_path)
    loaded = loader.load()
    assert [(f.display, f.truncated) for f in loaded.files] == [('AGENTS.md', False)]

    rendered = loader.render()
    assert rendered.startswith('<workspace_instructions files="AGENTS.md">')
    assert 'Instructions from: AGENTS.md' in rendered
    assert '跑测试: pytest -q' in rendered


def test_instructions_injects_both_candidates_in_candidate_order(tmp_path):
    """AGENTS.md 与 CLAUDE.md 同时存在时都注入，顺序固定（AGENTS.md 在前）。"""
    (tmp_path / 'AGENTS.md').write_text('AGENTS 约定', encoding='utf-8')
    (tmp_path / 'CLAUDE.md').write_text('CLAUDE 约定', encoding='utf-8')
    rendered = InstructionLoader(tmp_path).render()
    assert 'files="AGENTS.md, CLAUDE.md"' in rendered
    assert rendered.index('AGENTS 约定') < rendered.index('CLAUDE 约定')


def test_instructions_truncates_oversized_file_and_points_to_read_file(tmp_path):
    """超预算：截断 + 明确告知"用 read_file 读剩下的"，不静默丢内容。"""
    (tmp_path / 'AGENTS.md').write_text('x' * (INSTRUCTION_MAX_FILE_CHARS + 500), encoding='utf-8')
    loaded = InstructionLoader(tmp_path).load()
    assert loaded.files[0].truncated is True
    content = loaded.files[0].content
    assert len(content) < INSTRUCTION_MAX_FILE_CHARS + 200     # 预算 + 提示，不是原文长度
    assert 'truncated at' in content and 'read_file' in content


def test_instructions_skips_a_file_over_the_source_cap(tmp_path):
    """超过读取上限（1 MiB）的文件不读进内存，只留一条指引。"""
    (tmp_path / 'AGENTS.md').write_text('x' * (INSTRUCTION_MAX_SOURCE_BYTES + 1), encoding='utf-8')
    loaded = InstructionLoader(tmp_path).load()
    assert loaded.files[0].truncated is False                  # 没截断——根本没读
    assert 'too large to inline' in loaded.files[0].content


def test_instructions_report_unreadable_instead_of_absent(tmp_path, monkeypatch):
    """第三态：文件在、但**读不到** → 说"不知道"，不说"没有"，也不提议创建。

    把"读不到"渲染成 files="none" 会同时对用户和模型撒谎（不变式⑤ / 宁炸勿静默
    在探测上的对应物：不要把"不知道"降级成"没有"）。对齐 DSH 的
    ScopeInstructionProbe 与 opencode 的 SystemContext.unavailable。
    """
    (tmp_path / 'AGENTS.md').write_text('约定正文', encoding='utf-8')
    original = Path.read_text

    def deny(self, *args, **kwargs):
        if self.name == 'AGENTS.md':
            raise PermissionError('denied by test')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', deny)
    loader = InstructionLoader(tmp_path)
    loaded = loader.load()
    assert loaded.found is False
    assert [f.display for f in loaded.unreadable] == ['AGENTS.md']
    assert loaded.unreadable[0].reason.startswith('cannot read: ')

    rendered = loader.render()
    assert 'files="unreadable"' in rendered
    assert 'could not be read' in rendered and '- AGENTS.md: cannot read' in rendered
    assert 'propose writing' not in rendered      # 存在性没确认 → 连"建议创建"都不该说


def test_instructions_treat_a_directory_named_agents_md_as_unreadable(tmp_path):
    """同名目录（PI 踩过 EISDIR 的那种）：也算"读不到"，不算"没有"。"""
    (tmp_path / 'AGENTS.md').mkdir()
    loader = InstructionLoader(tmp_path)
    loaded = loader.load()
    assert loaded.files == ()
    assert 'not a regular file' in loaded.unreadable[0].reason

    rendered = loader.render()
    assert 'files="unreadable"' in rendered
    assert 'propose writing' not in rendered


def test_instructions_list_unreadable_alongside_a_readable_file(tmp_path, monkeypatch):
    """一份读得到 + 一份读不到：正文照常注入，同时点名读不到的那份。"""
    (tmp_path / 'AGENTS.md').write_text('AGENTS 正文', encoding='utf-8')
    (tmp_path / 'CLAUDE.md').write_text('CLAUDE 正文', encoding='utf-8')
    original = Path.read_text

    def deny(self, *args, **kwargs):
        if self.name == 'CLAUDE.md':
            raise PermissionError('denied by test')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', deny)
    rendered = InstructionLoader(tmp_path).render()
    assert 'files="AGENTS.md"' in rendered and 'AGENTS 正文' in rendered
    assert 'Unreadable candidates' in rendered and 'CLAUDE.md (cannot read' in rendered


def test_instructions_count_an_empty_file_as_present(tmp_path):
    """空文件算"存在"：标题照常渲染——"这里有一份约定、内容为空"与"这里没有约定"不同。

    对齐 DSH 的注释（heading 存活即代表"存在"）与 opencode 的同名测试
    （keeps an empty AGENTS.md as available context）。
    """
    (tmp_path / 'AGENTS.md').write_text('', encoding='utf-8')
    assert InstructionLoader(tmp_path).load().found is True
    rendered = InstructionLoader(tmp_path).render()
    assert 'files="AGENTS.md"' in rendered
    assert 'Instructions from: AGENTS.md' in rendered
    assert 'files="none"' not in rendered


def test_instructions_cache_avoids_rereading_an_unchanged_file(tmp_path, monkeypatch):
    """缓存：文件没变时每次渲染只 stat、不读盘；且两次渲染字节相同（前缀缓存不失效）。"""
    (tmp_path / 'AGENTS.md').write_text('跑测试: pytest -q', encoding='utf-8')
    reads: list[str] = []
    original = Path.read_text

    def counting(self, *args, **kwargs):
        reads.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', counting)
    loader = InstructionLoader(tmp_path)
    first = loader.render()
    second = loader.render()
    assert reads == ['AGENTS.md']                 # 第二次没读盘（live 段每请求求值，读盘会变常态）
    assert first == second                        # 字节稳定 → 缓存前缀不因"重新渲染"失效

    (tmp_path / 'AGENTS.md').write_text('质量门: ruff + mypy + pytest', encoding='utf-8')
    third = loader.render()
    assert reads == ['AGENTS.md', 'AGENTS.md']    # 改了才重读
    assert '质量门' in third and '跑测试' not in third


def test_instructions_ignore_a_symlink_pointing_outside_the_workspace(tmp_path):
    """符号链接指向工作区外：不注入，并作为"读不到"报出来。

    工具层已经用 resolve+relative_to 拦住了这条（sandbox.resolve_in_workspace），
    指令读取是宿主的另一条路径——不拦的话 `AGENTS.md -> ~/.ssh/id_rsa` 就能把工作区
    外的文件塞进 system prompt 发给模型，与 persona 的"工作区外不可读"直接矛盾。
    """
    outside = tmp_path / 'outside-secret.md'
    outside.write_text('SECRET_OUTSIDE_WORKSPACE', encoding='utf-8')
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    try:
        (workspace / 'AGENTS.md').symlink_to(outside)
    except (OSError, NotImplementedError):        # Windows 无权限/未开开发者模式
        pytest.skip('symlink not permitted on this platform')

    loaded = InstructionLoader(workspace).load()
    assert loaded.files == ()
    assert 'outside the workspace' in loaded.unreadable[0].reason

    rendered = InstructionLoader(workspace).render()
    assert 'SECRET_OUTSIDE_WORKSPACE' not in rendered
    assert 'files="unreadable"' in rendered


def test_instructions_reject_a_candidate_that_resolves_outside(tmp_path, monkeypatch):
    """越界判定本身（不依赖平台能否建符号链接）：resolve 到工作区外 → 不注入。

    与上一条互补：符号链接那条是平台相关的集成验证（本机 Windows 可能 skip），
    这条直接换掉 resolve 的返回值，保证**安全分支在任何平台都被断言到**。
    """
    (tmp_path / 'ws').mkdir()
    (tmp_path / 'ws' / 'AGENTS.md').write_text('INSIDE_BODY', encoding='utf-8')
    outside = tmp_path / 'outside.md'                      # 工作区**外**（ws 的兄弟）
    outside.write_text('OUTSIDE_BODY', encoding='utf-8')
    original = Path.resolve

    def fake(self, *args, **kwargs):
        if self.name == 'AGENTS.md' and self.parent == tmp_path / 'ws':
            return outside
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', fake)
    loaded = InstructionLoader(tmp_path / 'ws').load()
    assert loaded.files == ()
    assert 'outside the workspace' in loaded.unreadable[0].reason
    rendered = InstructionLoader(tmp_path / 'ws').render()
    assert 'INSIDE_BODY' not in rendered and 'OUTSIDE_BODY' not in rendered
    assert 'files="unreadable"' in rendered


def test_instructions_do_not_cache_a_torn_read(tmp_path, monkeypatch):
    """撕裂读不进缓存（issue #17 缺陷 1）：读到半截时按"读不到"报出来，下个请求重读。

    复现手法：让 `read_text` 在**读的过程中**把磁盘内容改长，同时只返回半截——正是
    "编辑器保存 / agent 写文件 / git checkout 撞上这一读"的形状。修复前那半截会被按
    "读之前的键"缓存住，于是**后续每个请求都吃这份缺条目的约定**；修复后读完再取一次
    键发现不一致 → 不写缓存 + `files="unreadable"`，下一次渲染拿到盘上的完整内容。

    磁盘内容故意**每次读都变长**（而不是只动 mtime）：键必然不同，测试不依赖时间戳精度。
    """
    path = tmp_path / 'AGENTS.md'
    path.write_text('ORIGINAL-CONTENT-AAAA', encoding='utf-8')
    loader = InstructionLoader(tmp_path)
    original = Path.read_text
    calls: list[int] = []

    def tearing(self, *args, **kwargs):
        if self == path:
            calls.append(1)
            self.write_text('FULL-CONTENT-' + 'X' * (10 * len(calls)), encoding='utf-8')
            return 'HALF'                                   # 读到的只是半截
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', tearing)
    for _ in range(2):                                      # 第二次渲染 = 检验"有没有被缓存住"
        rendered = loader.render()
        assert 'files="unreadable"' in rendered
        assert 'file changed while reading' in rendered
        assert 'HALF' not in rendered                       # 半截既没进 system，也没被当成缓存内容
        assert 'propose writing' not in rendered            # 第三态，不是"没有约定"

    monkeypatch.setattr(Path, 'read_text', original)
    healed = loader.render()
    assert 'files="AGENTS.md"' in healed
    assert 'FULL-CONTENT-' in healed                        # 重读拿到盘上的完整内容


def test_instructions_treat_a_broken_symlink_as_unreadable(tmp_path, monkeypatch):
    """断链符号链接 = 第三态"读不到"，**不是**"确认不存在"（issue #17 缺陷 2）。

    平台无关的做法：文件真实存在（`lstat` 成功），只把 `stat()` 打成 `FileNotFoundError`
    ——这正是"条目在、目标不在"的形状。修复前它被映射成 `(None, '')`，渲染出
    "这个工作区没有约定 + 建议创建"，模型于是会在假前提上行动（顺着链接去创建目标）。

    注意（本机 Python 3.13 实测）：`Path.lstat()` 的实现就是 `self.stat(follow_symlinks=False)`
    ——假函数必须放行 `follow_symlinks=False` 那一支，否则它会把"条目在不在"也一起打掉，
    测试就变成了在验证"不存在"。
    """
    path = tmp_path / 'AGENTS.md'
    path.write_text('REAL_BODY', encoding='utf-8')
    original = Path.stat

    def broken(self, *args, **kwargs):
        if self == path and kwargs.get('follow_symlinks', True):
            raise FileNotFoundError(2, 'No such file or directory')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', broken)
    loaded = InstructionLoader(tmp_path).load()
    assert loaded.files == ()
    assert 'broken symlink' in loaded.unreadable[0].reason
    rendered = InstructionLoader(tmp_path).render()
    assert 'REAL_BODY' not in rendered
    assert 'files="unreadable"' in rendered
    assert 'propose writing' not in rendered


def test_instructions_report_a_real_broken_symlink_as_unreadable(tmp_path):
    """真·断链符号链接的平台集成验证（Windows 无权限建链接 → skip）。"""
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    try:
        (workspace / 'AGENTS.md').symlink_to(tmp_path / 'does-not-exist.md')
    except (OSError, NotImplementedError):
        pytest.skip('symlink not permitted on this platform')

    loaded = InstructionLoader(workspace).load()
    assert loaded.files == ()
    assert 'broken symlink' in loaded.unreadable[0].reason
    rendered = InstructionLoader(workspace).render()
    assert 'files="unreadable"' in rendered
    assert 'propose writing' not in rendered


def test_instruction_budget_can_hold_every_candidate():
    """预算自洽：单文件上限 × 候选数 必须塞进总预算，否则多出来的会被静默丢掉。"""
    from agent_demo.instructions import (
        INSTRUCTION_FILE_CANDIDATES,
        INSTRUCTION_MAX_TOTAL_CHARS,
    )
    assert INSTRUCTION_MAX_TOTAL_CHARS >= INSTRUCTION_MAX_FILE_CHARS * len(INSTRUCTION_FILE_CANDIDATES)


def test_instructions_render_is_live(tmp_path):
    """live 段语义：每次渲染重新求值——文件出现/被改，下一次渲染就看得到。"""
    loader = InstructionLoader(tmp_path)
    assert 'files="none"' in loader.render()

    (tmp_path / 'AGENTS.md').write_text('跑测试: pytest -q', encoding='utf-8')
    assert '跑测试: pytest -q' in loader.render()               # 出现即生效

    (tmp_path / 'AGENTS.md').write_text('质量门: ruff + mypy + pytest', encoding='utf-8')
    assert '质量门: ruff + mypy + pytest' in loader.render()     # 改过即失效重读


def test_nested_instruction_files_are_listed_but_not_inlined(tmp_path):
    """子目录清单只列路径（正文按需 read_file），隐藏目录不算数。"""
    (tmp_path / 'web').mkdir()
    (tmp_path / 'web' / 'AGENTS.md').write_text('WEB_BODY_SHOULD_NOT_BE_INLINED', encoding='utf-8')
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'AGENTS.md').write_text('GIT_BODY', encoding='utf-8')

    loader = InstructionLoader(tmp_path)
    assert loader.nested == ('web/AGENTS.md',)                  # .git 被剪枝
    rendered = loader.render()
    assert 'web/AGENTS.md' in rendered
    assert 'WEB_BODY_SHOULD_NOT_BE_INLINED' not in rendered      # 只列路径
    assert 'GIT_BODY' not in rendered


def test_nested_instruction_list_refreshes_per_turn(tmp_path, monkeypatch):
    """子目录清单按**回合**刷新：同一回合内不重扫（省一次全树遍历），新回合看得见。

    根目录正文是每请求新鲜的（上面那条测过），清单不是——它要一次 `os.walk`，
    比 stat 贵三个数量级，所以拿回合号当刷新纪元。
    """
    from agent_demo import instructions as instructions_module

    scans: list[Path] = []
    original = instructions_module.scan_nested_instruction_files

    def counting(workspace):
        scans.append(workspace)
        return original(workspace)

    monkeypatch.setattr(instructions_module, 'scan_nested_instruction_files', counting)
    loader = InstructionLoader(tmp_path)
    assert len(scans) == 1                                      # 构造时那份

    assert 'Subdirectory instruction files' not in loader.render(turn=1)
    assert len(scans) == 2                                      # 新纪元 → 重扫一次
    assert loader.render(turn=1) == loader.render(turn=1)        # 同一回合：不再扫
    assert len(scans) == 2

    web = tmp_path / 'web'
    web.mkdir()
    (web / 'AGENTS.md').write_text('WEB', encoding='utf-8')
    assert 'web/AGENTS.md' not in loader.render(turn=1)          # 本回合新建 → 还看不见
    assert 'web/AGENTS.md' in loader.render(turn=2)              # 下一回合 → 进清单
    assert len(scans) == 3

    (web / 'AGENTS.md').unlink()
    assert 'web/AGENTS.md' in loader.render(turn=2)              # 同一回合：清单不动
    assert 'web/AGENTS.md' not in loader.render(turn=3)          # 删掉 → 下一回合消失
    assert len(scans) == 4


async def test_build_agent_injects_workspace_instructions_before_tool_sections(tmp_path):
    """factory 级全链路：通用规则（discipline）在前，注入的正文在后，工具段最后。"""
    from argparse import Namespace

    from agent_demo import factory

    (tmp_path / 'AGENTS.md').write_text('# 项目约定\n\nRUN: pytest -q\n', encoding='utf-8')
    args = Namespace(fake=True, model='fake-model', workspace=tmp_path, hide_reasoning=False,
                     session='id', sessions=str(tmp_path), prompt='x', resume=False, verbose=False)
    session = Session(id='id')
    agent = factory.build_agent(session, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
    agent.followup('hi')
    await agent.when_idle()

    system = [e.data for e in session.events if e.type == 'request/header'][0]['system']
    assert 'Instructions:' in system                             # 通用规则（discipline 段）
    assert 'RUN: pytest -q' in system                            # 注入的正文（instructions 段）
    assert system.index('Instructions:') < system.index('RUN: pytest -q') < system.index('Use bash')


async def test_instruction_section_follows_the_agent_turn_number(tmp_path):
    """factory 把 `agent.last_turn` 接进 live 段：新建的子目录指令文件下一回合进清单。

    用**真回合**（不是手改 `_last_turn`）验接线：factory 忘了传 `turn=` 的话，加载器
    就永远停在构造时那份清单上，最后那条断言必然失败。
    """
    from argparse import Namespace

    from agent_demo.factory import build_agent

    args = Namespace(fake=True, model='fake-model', workspace=tmp_path, hide_reasoning=False,
                     session='id', sessions=str(tmp_path), prompt='x', resume=False, verbose=False)
    agent = build_agent(Session(id='turn-wired'), args,
                        {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
    agent.llm = FakeLlm(script=[
        {'text': 'one', 'finish_reason': 'stop'},
        {'text': 'two', 'finish_reason': 'stop'},
    ], provider='fake', model='fake-model')

    def last_system() -> str:
        header = agent.session.request_header()
        assert header is not None
        return header['system']

    agent.followup('one')
    await agent.when_idle()
    assert 'Subdirectory instruction files' not in last_system()   # 回合 1：还没有这份约定

    web = tmp_path / 'web'
    web.mkdir()
    (web / 'AGENTS.md').write_text('WEB_BODY', encoding='utf-8')

    agent.followup('two')
    await agent.when_idle()
    assert 'web/AGENTS.md' in last_system()                        # 回合 2：进清单
    assert 'WEB_BODY' not in last_system()                         # 仍然只列路径
