"""应用层：按需技能（skill）——目录静态注入 system，正文按名字取。

技能 = `*.md` 文件 + YAML frontmatter（name/description），正文是操作指南
（纯指令，不携带执行代码）。

**两个来源，按名字合并（对齐 DSH 的 project / user / bundled 分层与同名覆盖）**：

- **bundled**：随 agent 发布，包内 `agent_demo/bundled_skills/*.md`——agent 自带的
  通用能力（如"怎么写 AGENTS.md"）。它必须跟着 **agent** 走，而不是跟着工作区走：
  换一个工作区就消失的能力不算自带能力。
- **workspace**：`<workspace>/skills/*.md`——项目自己的约定与工作流（如本仓库的
  `gh-issue`，只对本项目有意义）。
- 合并规则：同名时 **workspace 覆盖 bundled**（项目可以定制通用手册），按名字排序，
  所以目录字节可复现（缓存前缀稳定）。

**正文按名字取（`skill` 工具），不按路径取**。这是被 bundled 逼出来的机制决定：
本仓库早期沿用 PI 式的"模型用 read_file 按目录里的路径读技能文件"，那条路要求技能
文件**在模型读得到的地方**；而 `read_file` 被沙箱锁在工作区内，包内技能根本读不到
（实测：`path outside workspace`）。DSH 的模型侧 `skill` 工具正是为解决这件事存在的
——**模型给名字，host 负责把名字解析到文件**，自带技能是 host 自己的资源，沙箱承诺
一个字都不用改（模型没有机会拼出路径；查不到就是一个 is_error 结果）。

**新鲜度：技能文件改了，下一次模型请求就生效**。目录段是 system 的 live 段
（provider 是 `SkillTable.skills()`），`skill` 工具在**执行时**取同一张表——所以会话
中途新增/改写/删除技能，目录与工具**同时**看见，不会出现"目录说着旧描述、工具却返回
新正文"这种自相矛盾（早期目录段是 build 时算好的静态字符串，就有这个毛病：新技能要等
新会话，改过的描述永远停在旧值）。代价用 stat 键控缓存压住：每次求值只做两轮
`scandir` + 每文件一次 `stat`（实测 ~80 µs），文件真的变了才重扫重解析（~430 µs）；
文件没变时目录字节也不变，缓存前缀照样命中。

模块职责：
1. `Skill` 值对象（frozen）：目录行需要的一切 + `source`
2. `scan_skills(root, source=…)`：扫一个技能根目录（bundled 与 workspace 共用）
3. `load_skills(workspace)`：两个来源合并（workspace 覆盖 bundled，按名字排序）
4. `SkillTable(workspace)`：合并结果的 **stat 键控缓存**（目录段与 `skill` 工具共用
   同一个实例，所以两处永不漂移）
5. `resolve_skill(skills, name)` / `read_skill_body(skill)`：`skill` 工具用
6. `format_catalog(skills)`：目录 → system 注入文本（只有名字与一句话，正文不进）
"""
from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# 技能名只允许小写字母/数字/连字符（对齐 Agent Skills 约定的名字规则）。
_NAME = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')

# 包内自带技能目录。**故意不叫 `skills/`**：那会和同目录的 `skills.py` 模块同名，
# Python 虽然仍能解析到模块（源码加载器优先于命名空间包），但读者和打包工具会被绕。
BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent / 'bundled_skills'

BUNDLED = 'bundled'
WORKSPACE = 'workspace'


@dataclass(frozen=True)
class Skill:
    """一条技能：目录里的一行（name/description）+ 取正文的位置。

    source 区分 `bundled`（随 agent 发布）与 `workspace`（项目自带）；同名时
    workspace 胜出（见模块 docstring）。
    """
    name: str
    description: str
    path: Path
    source: str = WORKSPACE


def scan_skills(root: Path, *, source: str = WORKSPACE) -> list[Skill]:
    """扫描一个技能目录：每个 .md 文件即一个技能（解析 frontmatter 取 name/description）。

    只扫顶层 *.md（扁平形态，教学够用）；SKILL.md 目录形态等有需要再加。
    解析容错：坏技能（不可读/无 frontmatter/缺 name 或 description/名字非法）
    **跳过并打诊断**（对齐项目"宁炸勿静默"——技能作者要能发现自己写坏了；
    但技能坏了不炸 system，只提示，正文由 `skill` 工具按需取）。
    """
    skills: list[Skill] = []
    if not root.is_dir():
        return skills
    for path in sorted(root.glob('*.md')):
        skill, error = _parse_skill(path, source)
        if skill is not None:
            skills.append(skill)
        elif error:
            # 诊断走 stderr：不污染 CLI 的 stdout 正常输出流（对齐项目里
            # print(..., flush=True) 的诊断风格，但分离到错误流）
            print(f'[skill] skipped {path.name}: {error}', file=sys.stderr, flush=True)
    return skills


def load_skills(workspace: Path) -> list[Skill]:
    """两个来源合并：bundled（随包）→ workspace（工作区，同名覆盖），按名字排序。

    排序不只是好看：目录段进 system 的缓存稳定前缀，字节必须可复现。
    """
    merged: dict[str, Skill] = {}
    for skill in scan_skills(BUNDLED_SKILLS_DIR, source=BUNDLED):
        merged[skill.name] = skill
    for skill in scan_skills(workspace / 'skills', source=WORKSPACE):
        merged[skill.name] = skill
    return sorted(merged.values(), key=lambda skill: skill.name)


class SkillTable:
    """技能表 + **stat 键控缓存**（目录段的 live provider 与 `skill` 工具共用同一个实例）。

    `load_skills` 是纯函数，想知道"现在有哪些技能"直接调它就对；这个类只多回答一个
    问题：**什么时候需要重算**。每次 `skills()` 先算一遍内容指纹（两个来源目录的
    `*.md` 名单 + 每个文件的 `(mtime_ns, size)`），指纹没变就把上次那份原样返回。

    指纹覆盖三种变化：**新增 / 删除 / 改名**（改的是名单）与**改写内容**（改的是
    mtime/size）。读不到目录时指纹算作"未知"→ 每次都重扫：宁可多扫几次，也不拿一个
    可能过期的指纹把新技能挡在门外（对齐指令文件加载器的"不要把不知道降级成没有"）。
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._signature: tuple | None = None
        self._skills: tuple[Skill, ...] = ()

    def skills(self) -> tuple[Skill, ...]:
        """当前技能表（按名字排序）——目录段与 `skill` 工具看到的是同一份。"""
        signature = _skills_signature(self._workspace)
        if signature is None or signature != self._signature:
            self._skills = tuple(load_skills(self._workspace))
            self._signature = signature
        return self._skills


def _skills_signature(workspace: Path) -> tuple | None:
    """两个技能来源的内容指纹；任一来源"读不到"（不是"不存在"）时返回 None = 未知。"""
    parts = []
    for root in (BUNDLED_SKILLS_DIR, workspace / 'skills'):
        signature = _dir_signature(root)
        if signature is None:
            return None
        parts.append(signature)
    return tuple(parts)


def _dir_signature(root: Path) -> tuple | None:
    """一个技能目录的指纹：`(*.md 名单, 每文件 (mtime_ns, size))`。

    用 `os.scandir` 而不是 `glob` + 逐个 `stat`：一次系统调用就把名字和 stat 都拿到
    （Windows 上 `DirEntry.stat()` 还自带缓存）——这个函数跑在每请求的 live 段上。

    目录不存在是**确定状态**（这个来源没有技能，指纹是空元组）；读不到（权限/IO）才是
    "未知"。同名目录与坏符号链接不参与指纹（`scan_skills` 会给它们打诊断）。
    """
    try:
        with os.scandir(root) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
    except FileNotFoundError:
        return ()
    except OSError:
        return None
    signature = []
    for entry in entries:
        if not entry.name.endswith('.md'):
            continue
        try:
            if not entry.is_file():
                continue
            stat = entry.stat()
        except OSError:
            return None                        # stat 都失败了 → 指纹未知，只能重扫
        signature.append((entry.name, stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def resolve_skill(skills: Sequence[Skill], name: str) -> Skill | None:
    """按名字查技能（`skill` 工具的唯一入口：模型给的是名字，不是路径）。"""
    for skill in skills:
        if skill.name == name:
            return skill
    return None


def read_skill_body(skill: Skill) -> str:
    """读技能正文（剥掉 frontmatter）。

    目录行已经给过 name/description，正文再带一遍 YAML 只是噪声；模型拿到的应该是
    一份可以直接照着做的指南。
    """
    text = skill.path.read_text(encoding='utf-8', errors='replace')
    span = _frontmatter_span(text)
    return text[span[1]:].lstrip('\n') if span else text


def format_catalog(skills: Sequence[Skill]) -> str:
    """技能目录 → system 注入文本。

    内容：引导句（教模型"任务匹配就用 skill 工具按名字取正文"）+ 每技能一行
    （名字 + 一句话）。**不再列路径**——取正文走工具，列路径只会诱导模型去
    read_file（而 bundled 技能在沙箱外，读了会被拒）。

    这段文本进 system 的缓存稳定前缀，所以**字节必须可复现**：输入按名字排序，
    同一批文件每次渲染字节相同（`SkillTable` 的指纹也因此能把"没变"判准）。
    """
    if not skills:
        return ''
    lines = [f'- {skill.name}: {skill.description}' for skill in skills]
    head = '可用技能（任务匹配描述时调用 skill 工具按名字取正文）：'
    return head + '\n' + '\n'.join(lines)


def _frontmatter_span(text: str) -> tuple[int, int] | None:
    """frontmatter 的 (开始, 结束) 下标；没有合法 frontmatter 返回 None。

    开始固定是 0（文件必须以 `---` 开头），结束指向收尾 `---` 的下一行之前。
    """
    if not text.startswith('---'):
        return None
    end = text.find('\n---', 3)
    if end < 0:
        return None
    return 0, end + 4


def _parse_skill(path: Path, source: str) -> tuple[Skill | None, str]:
    """解析一个技能文件 → (Skill | None, 跳过原因)。

    返回 (None, '') = 正常跳过（非技能文件，如目录说明文档），不打诊断；
    返回 (None, reason) = 写坏了要提示（缺 name/description、名字非法、frontmatter 没闭合）。
    只取 name/description（目录需要），正文不读——正文由 `skill` 工具按需取。
    """
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as error:
        return None, f'unreadable: {error}'
    span = _frontmatter_span(text)
    if span is None:
        # 以 `---` 开头但没闭合 = 写坏了，要提示；否则是普通文档，静默跳过
        return None, 'frontmatter not closed (missing second ---)' if text.startswith('---') else ''
    meta: dict[str, str] = {}
    for line in text[3:span[1] - 4].splitlines():
        if ':' not in line:
            continue
        key, _, value = line.partition(':')
        key = key.strip()
        if not key:
            continue
        # 值取第一个冒号后的全部剩余（支持 description 值内含冒号），
        # 剥掉两端空白与成对引号；多行 YAML（> / | 块）不在契约内
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        meta[key] = value
    name = meta.get('name', path.stem)
    description = meta.get('description', '').strip()
    if not description:
        return None, 'missing description in frontmatter'
    if not _NAME.match(name):
        return None, (
            f'invalid name {name!r} (must be lowercase letters/digits/hyphens; '
            'set `name:` in frontmatter or rename the file)'
        )
    return Skill(name=name, description=description, path=path, source=source), ''
