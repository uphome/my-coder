"""技能：两来源合并、目录注入、按名字取正文、新鲜度缓存与越界检查。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**只搬家、不改断言**。）
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from agent_demo.llm import (
    FakeLlm,
)
from agent_demo.session import Session
from agent_demo.tools import build_tools


def test_skill_catalog_scan_and_format(tmp_path, capsys):
    """技能目录扫描/格式化（AGENTS.md 约定：目录只放 name+description）。

    - scan_skills：解析 skills/*.md 的 frontmatter；坏技能跳过并打诊断
    - format_catalog：纯文本目录行，**不列路径**（取正文走 skill 工具按名字）
    - 正文绝不进目录（正文由 skill 工具按需取，见后面的测试）
    """
    from agent_demo.skills import format_catalog, scan_skills

    skills_dir = tmp_path / 'skills'
    skills_dir.mkdir()
    (skills_dir / 'gh-issue.md').write_text(
        '---\nname: gh-issue\ndescription: 处理 GitHub issue：读全文、独立验证\n'
        '---\n# 正文\n这是一段不该出现在目录的技能正文。\n',
        encoding='utf-8',
    )
    # 描述含成对引号 → 应剥离引号保留内容
    (skills_dir / 'quoted.md').write_text(
        '---\nname: quoted\ndescription: "任务匹配时使用，含:冒号"\n---\n正文\n',
        encoding='utf-8',
    )
    # 坏技能（无 description）：应被 scan 跳过 + stderr 诊断，不进目录
    (skills_dir / 'broken.md').write_text(
        '---\nname: broken\n---\n没有 description。\n', encoding='utf-8')
    # 非法技能名（文件名中文且无 name）：跳过 + 诊断
    (skills_dir / '处理问题.md').write_text(
        '---\ndescription: 中文文件名技能\n---\n正文\n', encoding='utf-8')
    # 非技能 md（无 frontmatter）：静默跳过（不算坏，不打诊断）
    (skills_dir / 'README.md').write_text('目录说明，不是技能。\n', encoding='utf-8')

    skills = scan_skills(skills_dir, boundary=tmp_path)
    names = [s.name for s in skills]
    assert names == ['gh-issue', 'quoted'], names
    # 引号剥离 + 值内冒号保留
    quoted = next(s for s in skills if s.name == 'quoted')
    assert quoted.description == '任务匹配时使用，含:冒号'
    gh = next(s for s in skills if s.name == 'gh-issue')
    assert gh.description == '处理 GitHub issue：读全文、独立验证'

    # 诊断：broken 与中文名各一条 [skill] skipped（README 静默）
    err = capsys.readouterr().err
    assert '[skill] skipped broken.md' in err
    assert '[skill] skipped 处理问题.md' in err
    assert 'README' not in err

    catalog = format_catalog(skills)
    assert 'gh-issue' in catalog
    assert '处理 GitHub issue' in catalog
    assert '不该出现在目录' not in catalog           # 正文不进目录
    assert 'broken' not in catalog                    # 坏技能被跳过
    assert 'README' not in catalog
    # 不再列路径：列了只会诱导模型去 read_file，而 bundled 技能在沙箱外读不到
    assert 'skills/' not in catalog
    assert 'skill 工具' in catalog                    # 引导句指向按名字取正文

    # 无技能/空目录 → 空目录文本（render 自动省略，零 token）
    assert format_catalog([]) == ''
    assert scan_skills(tmp_path / 'no-such-dir', boundary=tmp_path) == []


@pytest.mark.asyncio
async def test_skill_catalog_injected_into_system(tmp_path):
    """技能目录静态注入 system（端到端：build_agent → 一步请求）。

    - request/header 的 system 含目录（name/description/路径），随 system 落
      日志可重建
    - 正文不进 system——正文只在模型 read_file 后作为 tool/result 进上下文
    - 目录在 todo:state 等动态段之前（order 95 < 100，缓存稳定前缀）
    """
    from argparse import Namespace

    from agent_demo.factory import build_agent
    from agent_demo.session import Session

    skills_dir = tmp_path / 'skills'
    skills_dir.mkdir()
    (skills_dir / 'gh-issue.md').write_text(
        '---\nname: gh-issue\ndescription: 处理 GitHub issue（读全文、独立验证）\n'
        '---\n# 正文\n秘密技能正文，绝不该进 system。\n',
        encoding='utf-8',
    )

    session = Session(id='skill-cat')
    args = Namespace(fake=True, model='fake-model', workspace=tmp_path,
                     hide_reasoning=False, session='id', sessions=str(tmp_path),
                     prompt='x', resume=False, verbose=False)
    llm = FakeLlm(script=[{'text': 'done', 'finish_reason': 'stop'}])
    agent = build_agent(session, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
    agent.llm = llm
    agent.followup('hi')
    await agent.when_idle()

    header = session.request_header()
    assert header is not None and 'system' in header
    system = header['system']
    assert '可用技能' in system
    assert 'gh-issue' in system
    assert 'skill 工具' in system                    # 目录引导模型按名字取正文
    assert '秘密技能正文' not in system              # 正文始终不进 system

    # 目录段在工具提示段之前（todo 为空时无 todo 段，取 bash 提示为序界）
    assert system.index('可用技能') < system.index('Use bash')


def test_bundled_skills_reach_an_arbitrary_workspace(tmp_path):
    """自带技能必须跟着 agent 走：工作区里**没有** skills/ 也要看得到目录。"""
    from agent_demo.skills import BUNDLED, load_skills

    skills = load_skills(tmp_path)
    names = [skill.name for skill in skills]
    assert 'project-instructions' in names                     # 来自包内
    assert names == sorted(names)                              # 排序 → 目录字节可复现
    bundled = next(skill for skill in skills if skill.name == 'project-instructions')
    assert bundled.source == BUNDLED
    assert bundled.path.is_relative_to(tmp_path) is False      # 正文在包内，工作区之外

    from agent_demo.skills import format_catalog
    catalog = format_catalog(skills)
    assert 'project-instructions' in catalog


def test_workspace_skill_overrides_bundled_by_name(tmp_path):
    """同名时 workspace 覆盖 bundled：目录里只出现一次，且是项目自己那份。"""
    from agent_demo.skills import WORKSPACE, load_skills, read_skill_body, resolve_skill

    skills_dir = tmp_path / 'skills'
    skills_dir.mkdir()
    (skills_dir / 'project-instructions.md').write_text(
        '---\nname: project-instructions\ndescription: 本项目的定制版手册\n'
        '---\n# 定制正文\n本仓库的指令文件放在 docs/ 下。\n', encoding='utf-8')

    skills = load_skills(tmp_path)
    names = [skill.name for skill in skills]
    assert names.count('project-instructions') == 1             # 不是两条
    winner = resolve_skill(skills, 'project-instructions')
    assert winner is not None and winner.source == WORKSPACE
    assert '定制正文' in read_skill_body(winner)


async def test_skill_tool_loads_bundled_body_from_outside_the_workspace(tmp_path):
    """机制的核心：bundled 正文在工作区之外，read_file 拒绝、skill 工具取得到。"""

    registry = build_tools(workspace=tmp_path)
    skill = await registry.execute('skill', {'name': 'project-instructions'}, None)
    assert skill.is_error is False
    assert '维护项目指令文件' in skill.content              # 正文到手
    assert '---\nname:' not in skill.content               # frontmatter 已剥离

    # 对照：同一个文件用 read_file 读 → 沙箱拒绝（所以必须走 skill 工具）
    from agent_demo.skills import BUNDLED_SKILLS_DIR
    denied = await registry.execute(
        'read_file', {'file_path': str(BUNDLED_SKILLS_DIR / 'project-instructions.md')}, None)
    assert denied.is_error is True
    assert 'outside workspace' in denied.content


def test_workspace_skill_pointing_outside_the_workspace_is_not_a_skill(tmp_path, monkeypatch, capsys):
    """工作区来源的技能越界 → 不当技能（issue #25）：目录里没有、工具也取不到。

    与指令文件那条（`AGENTS.md -> 工作区外` 不注入）是**同一条判据、同一个模块**
    （`sandbox.workspace_escape_reason`）。放行的后果比指令文件更直接：技能正文是
    `skill` 工具按需读进对话的，等于把工作区外的文件读给模型。

    平台无关做法：直接换掉 `resolve` 的返回值（真符号链接那条见下一条，Windows 会 skip）。
    """
    from agent_demo.skills import SkillTable, format_catalog, load_skills

    body = '---\nname: leak\ndescription: 外面的技能\n---\nSECRET_OUTSIDE_WORKSPACE\n'
    outside = tmp_path / 'outside' / 'secret.md'
    outside.parent.mkdir()
    outside.write_text(body, encoding='utf-8')
    workspace = tmp_path / 'ws'
    (workspace / 'skills').mkdir(parents=True)
    (workspace / 'skills' / 'leak.md').write_text(body, encoding='utf-8')

    original = Path.resolve

    def fake(self, *args, **kwargs):
        if self.name == 'leak.md' and self.parent.name == 'skills':
            return outside
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', fake)
    skills = load_skills(workspace)
    names = [skill.name for skill in skills]
    assert 'leak' not in names                             # 不当技能（越界）
    assert 'project-instructions' in names                 # bundled 不受影响
    assert 'leak' not in format_catalog(skills)            # 目录里也没有
    assert 'SECRET_OUTSIDE_WORKSPACE' not in format_catalog(skills)

    err = capsys.readouterr().err
    assert '[skill] skipped leak.md' in err and 'outside the workspace' in err

    # 工具侧同样取不到（目录与工具共用同一张表 → 不会"目录没有、工具却有"）
    registry = build_tools(workspace=workspace, skills=SkillTable(workspace))
    denial = asyncio.run(registry.execute('skill', {'name': 'leak'}, None))
    assert denial.is_error is True and 'no skill named' in denial.content


def test_workspace_skill_symlink_outside_is_skipped(tmp_path):
    """真·符号链接的平台集成验证（Windows 无权限建链接 → skip）。"""
    body = '---\nname: leak\ndescription: 外面的技能\n---\nSECRET\n'
    outside = tmp_path / 'secret.md'
    outside.write_text(body, encoding='utf-8')
    workspace = tmp_path / 'ws'
    (workspace / 'skills').mkdir(parents=True)
    try:
        (workspace / 'skills' / 'leak.md').symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip('symlink not permitted on this platform')

    from agent_demo.skills import load_skills

    names = [skill.name for skill in load_skills(workspace)]
    assert 'leak' not in names
    assert 'project-instructions' in names


async def test_skill_tool_unknown_name_is_a_result(tmp_path):
    """未知技能名 → is_error 结果（并列出可用名字），不炸循环。"""

    registry = build_tools(workspace=tmp_path)
    outcome = await registry.execute('skill', {'name': 'no-such-skill'}, None)
    assert outcome.is_error is True
    assert 'no skill named' in outcome.content
    assert 'project-instructions' in outcome.content        # 列出可用名字，模型能自我纠正


def test_web_skill_catalog_survives_reload(tmp_path):
    """技能目录可重建：新会话（reload）重新扫描，目录照旧注入。

    目录是磁盘文件的静态投影——会话重建不丢（与 todo 的 fold 同理，
    都是"从持久状态折叠"，只是来源是文件而非日志）。
    """
    from argparse import Namespace

    from agent_demo.factory import build_agent
    from agent_demo.session import Session

    skills_dir = tmp_path / 'skills'
    skills_dir.mkdir()
    (skills_dir / 'gh-issue.md').write_text(
        '---\nname: gh-issue\ndescription: 处理 GitHub issue\n'
        '---\n正文\n', encoding='utf-8')

    def system_of():
        args = Namespace(fake=True, model='fake-model', workspace=tmp_path,
                         hide_reasoning=False, session='id', sessions=str(tmp_path),
                         prompt='x', resume=False, verbose=False)
        s = Session(id='reload')
        a = build_agent(s, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
        return a.prompt.render(a.prompt.assemble(ctx={'agent': a}), ctx={'agent': a})

    first = system_of()
    assert 'gh-issue' in first
    # 第二个会话重建 → 目录仍在（来源是磁盘文件，与日志无关）
    assert system_of() == first


def test_skill_catalog_is_live_for_new_edited_and_deleted_skills(tmp_path):
    """新鲜度：会话中途新增/改写/删除技能，**下一次请求**的目录就是新的。

    早期目录段是 build 时算好的静态字符串，于是新技能要等新会话；更糟的是改写：
    目录还念着旧描述，`skill` 工具却返回新正文——同一份表在 system 和工具里说法不一。
    现在目录是 live 段、工具在执行时取同一张表，两边同时更新。
    """
    from argparse import Namespace

    from agent_demo.factory import build_agent

    args = Namespace(fake=True, model='fake-model', workspace=tmp_path, hide_reasoning=False,
                     session='id', sessions=str(tmp_path), prompt='x', resume=False, verbose=False)
    agent = build_agent(Session(id='live-skills'), args,
                        {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})

    def system() -> str:
        assembly = agent.prompt.assemble(ctx={'agent': agent})
        return agent.prompt.render(assembly, ctx={'agent': agent})

    assert 'gh-issue' not in system()                       # 这份技能还不存在

    skills_dir = tmp_path / 'skills'
    skills_dir.mkdir()
    skill_file = skills_dir / 'gh-issue.md'
    skill_file.write_text(
        '---\nname: gh-issue\ndescription: 初版描述\n---\n初版正文\n', encoding='utf-8')
    assert 'gh-issue: 初版描述' in system()                  # 新增 → 下一次请求可见

    skill_file.write_text(
        '---\nname: gh-issue\ndescription: 二版描述\n---\n二版正文\n', encoding='utf-8')
    rendered = system()
    assert 'gh-issue: 二版描述' in rendered
    assert '初版描述' not in rendered                       # 描述跟着改（不再停在 build 时刻）
    body = asyncio.run(agent.tools.execute('skill', {'name': 'gh-issue'}, agent))
    assert body.is_error is False and '二版正文' in body.content   # 工具给的也是新的

    skill_file.unlink()
    assert 'gh-issue' not in system()                       # 删除 → 下一次请求消失
    gone = asyncio.run(agent.tools.execute('skill', {'name': 'gh-issue'}, agent))
    assert gone.is_error is True and 'no skill named' in gone.content


def test_skill_table_rescans_only_when_files_change(tmp_path, monkeypatch):
    """缓存是 stat 键控的：目录段每请求求值，但文件没变时一次都不重扫。

    这条守着成本：`load_skills` 要读并解析每个技能文件（~300 µs），而 `scandir` +
    每文件 `stat`（~70 µs）每次请求都做也没关系。指纹来自"名单 + (mtime, size)"，
    所以增删改名与改写内容都判得出来。
    """
    from agent_demo import skills as skills_module

    scans: list[Path] = []
    original = skills_module.scan_skills

    def counting(root, **kwargs):
        scans.append(root)
        return original(root, **kwargs)

    monkeypatch.setattr(skills_module, 'scan_skills', counting)
    table = skills_module.SkillTable(tmp_path)

    first = table.skills()
    assert len(scans) == 2                                  # 两个来源各扫一次
    assert table.skills() == first and len(scans) == 2      # 没变 → 命中缓存，不重扫

    skills_dir = tmp_path / 'skills'
    skills_dir.mkdir()
    skill_file = skills_dir / 'new-skill.md'
    skill_file.write_text('---\nname: new-skill\ndescription: 新技能\n---\n正文\n', encoding='utf-8')
    added = table.skills()
    assert [skill.name for skill in added] != [skill.name for skill in first]
    assert len(scans) == 4                                  # 名单变了 → 重扫
    assert table.skills() == added and len(scans) == 4       # 再求值又命中

    skill_file.write_text('---\nname: new-skill\ndescription: 改过的描述\n---\n正文二\n',
                          encoding='utf-8')
    edited = table.skills()
    assert any(skill.description == '改过的描述' for skill in edited)
    assert len(scans) == 6                                  # 内容变了 → 重扫（size/mtime 变）
    assert table.skills() == edited and len(scans) == 6


def test_skill_table_refresh_is_serialized_across_threads(tmp_path, monkeypatch):
    """这张表被两个线程碰（live 段在循环线程、`offload=True` 的 skill 工具在工作线程）。

    两次刷新交错的那种时序很难手工复现，但"刷新有没有串行化"是可断言的结构性质：让 4
    个线程同时求值（指纹已过期 → 都想重扫），断言 `load_skills` **不会被并发进入**。
    没有锁时两次刷新会交错，可能留下"指纹是新的、表是旧的"——之后每次求值都以为没变，
    那份技能的描述就**永远**停在旧值（本 PR 要治的病换了个入口回来）。
    """
    import threading

    from agent_demo import skills as skills_module

    table = skills_module.SkillTable(tmp_path)
    table.skills()                                          # 建立缓存（此时还没有技能目录）
    skills_dir = tmp_path / 'skills'
    skills_dir.mkdir()
    (skills_dir / 'a.md').write_text(
        '---\nname: a\ndescription: 初版\n---\n正文\n', encoding='utf-8')

    guard = threading.Lock()
    active = 0
    peak = 0
    original = skills_module.load_skills

    def counting(workspace):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.05)                                # 给别的线程插进来的机会
            return original(workspace)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(skills_module, 'load_skills', counting)
    threads = [threading.Thread(target=table.skills) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak == 1                                        # 串行：双检之后只有第一个真去扫
    assert [skill.name for skill in table.skills()] == ['a', 'project-instructions']


def test_skill_fingerprint_covers_every_file_scan_skills_finds(tmp_path):
    """指纹必须覆盖 `scan_skills` 会扫到的每个文件——否则"改了看不见"。

    实测踩过：Windows 的 `Path.glob('*.md')` **不区分大小写**，`Upper.MD` 会被扫成
    技能；而指纹最初用 `name.endswith('.md')` 判大小写——于是改这份技能的描述永远不会
    让指纹变化，目录永远停在旧描述（正是本 PR 要治的病换了条路回来）。现在指纹改用
    `fnmatch.fnmatch`（glob 内部就是这套 `os.path.normcase` 语义）。
    """
    from agent_demo.skills import SkillTable, _dir_signature, scan_skills

    skills_dir = tmp_path / 'skills'
    skills_dir.mkdir()
    for name in ('Upper.MD', 'lower.md'):
        (skills_dir / name).write_text(
            f'---\nname: {Path(name).stem.lower()}\ndescription: 初版\n---\n正文\n',
            encoding='utf-8')

    scanned = {skill.path.name for skill in scan_skills(skills_dir, boundary=tmp_path)}
    covered = {name for name, _, _ in _dir_signature(skills_dir) or ()}
    assert scanned <= covered                               # 扫得到的都在指纹里

    # 行为面：只改 `Upper.MD`（Windows 上它会被扫到，改了就必须刷新缓存）。
    # Linux 的 glob 区分大小写、根本扫不到它，这条在那边不成立也不该成立。
    if 'Upper.MD' in scanned:
        table = SkillTable(tmp_path)
        before = table.skills()
        (skills_dir / 'Upper.MD').write_text(
            '---\nname: upper\ndescription: 改过的描述\n---\n正文二\n', encoding='utf-8')
        assert table.skills() != before
