"""文档门禁：设计记录的路径与格式、交叉链接、以及 `AGENTS.md` 的注入预算。

**为什么这些规则要写成测试而不是写在文档里**：仓库已经吃过这个亏——
"规则写在 AGENTS.md 里"并不等于"agent 看得到"（`AGENTS.md` 长到 22,553 字符时，
只有前 8,000 进 system，13 个规则块里 9 个模型从没见过，见 `docs/AGENTS.md` 第二节）。
文档规则本身同理：写得再清楚，没有东西拦就会漂移。所以这里把三条最容易漂的钉死：

1. **路径与状态一致**（`docs/notes/{lifecycle}/{class}/yyyy-mm-dd-主题.md`，闭集）；
2. **头三行与正文骨架**（`implemented/` 里不许出现提案语域的标题）；
3. **相对链接可解析**（移动文件时不会烂在半路）+ `AGENTS.md` **渲染后不含截断提示**。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NOTES = REPO / 'docs' / 'notes'

LIFECYCLES = ('proposed', 'implemented', 'rejected')
CLASSES = ('feature', 'bug-fix', 'simplification', 'architecture', 'process', 'testing')
# `implemented/` 里禁止出现的标题：那是提案语域，出现在"已落地"的文档里会让读者
# 分不清哪些已经发生（`docs/notes/README.md` 第三节）
PROPOSAL_HEADINGS = ('## Proposal', '## Plan', '## Migration plan', '## Acceptance criteria')
FILENAME_RE = re.compile(r'^\d{4}-\d{2}-\d{2}-[a-z0-9]+(-[a-z0-9]+)*\.md$')


def note_files() -> list[Path]:
    """所有设计记录（跳过 `_` 前缀的模板/规则文件）。"""
    if not NOTES.exists():
        return []
    return sorted(path for path in NOTES.rglob('*.md')
                  if not path.name.startswith('_') and path.name != 'README.md')


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO)).replace('\\', '/')


def test_notes_live_in_a_closed_lifecycle_and_class_tree():
    """路径编码两个轴：lifecycle 与 class 都是闭集，日期 = 首次提出那天。"""
    for path in note_files():
        parts = path.relative_to(NOTES).parts
        assert len(parts) == 3, f'{_rel(path)}：必须是 notes/{{lifecycle}}/{{class}}/文件.md'
        lifecycle, klass, name = parts
        assert lifecycle in LIFECYCLES, f'{_rel(path)}：lifecycle 只能是 {LIFECYCLES}'
        assert klass in CLASSES, f'{_rel(path)}：class 只能是 {CLASSES}（加类要同时改测试）'
        assert FILENAME_RE.match(name), (
            f'{_rel(path)}：文件名要是 yyyy-mm-dd-主题.md（小写短横线，日期=首次提出那天）')


def test_note_header_and_body_skeleton_match_the_lifecycle():
    """头三行固定；`Status:` 与目录一致；`implemented/` 里不许出现提案语。"""
    for path in note_files():
        lifecycle = path.relative_to(NOTES).parts[0]
        lines = path.read_text(encoding='utf-8').splitlines()
        assert lines[0].startswith('# Agent Note: '), f'{_rel(path)}：第一行必须是 "# Agent Note: …"'
        assert lines[1].strip() == '', f'{_rel(path)}：标题后要空一行'
        status = lines[2]
        assert status.startswith('Status: '), f'{_rel(path)}：第三行必须是 "Status: …"'
        value = status[len('Status: '):].strip()
        if lifecycle == 'rejected':
            # 被否决的 note 里，读者就是来拿这个判决的，所以唯一带内容的 status
            assert value.startswith('rejected — '), (
                f'{_rel(path)}：rejected 的 status 必须写原因："Status: rejected — <一句话>"')
        else:
            assert value == lifecycle, (
                f'{_rel(path)}：status {value!r} 与目录 {lifecycle!r} 不一致')
        body = '\n'.join(lines[3:])
        assert body.lstrip().startswith('## Problem'), (
            f'{_rel(path)}：正文第一段固定是 "## Problem"（要能脱离方案独立读懂）')
        assert lifecycle == 'proposed' or '## Decision' in body, (
            f'{_rel(path)}：implemented 的 note 要有 "## Decision"（现在时陈述已发布事实）')
        if lifecycle == 'implemented':
            for heading in PROPOSAL_HEADINGS:
                assert heading not in body, (
                    f'{_rel(path)}：implemented 里不许出现 {heading!r}——那是提案语域；'
                    '已落地的决定要写成现在时的事实')


def test_note_relative_links_resolve():
    """交叉引用一律用相对链接，且必须能解析（移动文件时不会烂在半路）。"""
    link_re = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
    for path in note_files() + [NOTES / 'README.md', REPO / 'docs' / 'AGENTS.md']:
        if not path.exists():
            continue
        for target in link_re.findall(path.read_text(encoding='utf-8')):
            if target.startswith(('http://', 'https://', '#')):
                continue
            anchorless = target.split('#', 1)[0]
            if not anchorless:
                continue
            assert (path.parent / anchorless).exists(), (
                f'{_rel(path)}：相对链接 {target!r} 指向不存在的路径')


def test_agents_md_fits_the_system_prompt_budget():
    """`AGENTS.md` 渲染进 system 时**不许被截断**。

    它是 agent 每轮都读的规范入口，而 `app/instructions.py` 每文件只注入 8000 字符。
    实测（2026-09-29，`AGENTS.md` = 22,553 字符）：13 个规则块里 9 个模型从来没看到过。
    这条测试红了就说明"又长出去了"——把长文搬进 `docs/`，在 `AGENTS.md` 里留判据 + 链接。
    """
    from my_coder.app.instructions import InstructionLoader

    rendered = InstructionLoader(REPO).render(turn=None)
    assert 'truncated at' not in rendered, (
        f'AGENTS.md 超出注入预算被截断了（渲染出 {len(rendered)} 字符）：'
        '模型看不到后半部分，把长文搬进 docs/，只留判据与链接')


@pytest.mark.parametrize('name', ['README.md', 'AGENTS.md'])
def test_root_entry_documents_still_exist(name: str):
    """入口（L0）与规范（L1）是文档体系的两个端点，不能因为整理而消失。"""
    assert (REPO / name).is_file()
