"""应用层：按需技能（skill）——技能 = 文件，目录静态注入 system，正文按需 read。

对齐三家（详见 agent.md §1/§3，落地规则以 AGENTS.md 约定节为准）：

- 技能 = `skills/<name>.md` 文件 + YAML frontmatter（name/description），
  正文是操作指南（纯指令，不携带执行代码）
- **目录（catalog）静态注入 system**：只放 name + description + 相对
  workspace 的路径，正文绝不进 system——目录字节稳定，处于缓存稳定前缀
- **正文 = 工具结果注入**：模型判断任务匹配某技能后，用现有 read_file
  按目录里的路径读文件 → 正文作为 tool/result 进 derive_messages，与读
  任何文件机制一致（落日志可重建、可被 compaction 折叠）
- 不新增 skill() 专用加载工具（对比 DSH/opencode 的取舍背景见 agent.md）

本模块三个职责：
1. Skill 值对象：frontmatter 解析结果（frozen，供扫描与目录格式化共用）
2. scan_skills(dir)：扫目录解析技能（dir 参数化——为将来多 agent 各自
   传技能根目录留缝，不用重构）
3. format_catalog(skills)：目录 → system 注入文本（纯文本行 + read 指引）
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

# 技能名只允许小写字母/数字/连字符（对齐 Agent Skills 约定的名字规则）。
_NAME = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')


@dataclass(frozen=True)
class Skill:
    """一条技能：目录里的一行（name/description）+ 取正文的路径。

    path 是文件绝对路径；相对 workspace 的路径由调用方（目录格式化）生成，
    因为只有它知道 workspace 根。
    """
    name: str
    description: str
    path: Path


def scan_skills(root: Path) -> list[Skill]:
    """扫描技能目录：每个 .md 文件即一个技能（解析 frontmatter 取 name/description）。

    只扫顶层 *.md（扁平形态，教学够用）；SKILL.md 目录形态等有需要再加。
    解析容错：坏技能（不可读/无 frontmatter/缺 name 或 description/名字非法）
    **跳过并打诊断**（对齐项目"宁炸勿静默"——技能作者要能发现自己写坏了；
    但技能坏了不炸 system，只提示，正文由模型按需 read）。
    """
    skills: list[Skill] = []
    if not root.is_dir():
        return skills
    for path in sorted(root.glob('*.md')):
        skill, error = _parse_skill(path)
        if skill is not None:
            skills.append(skill)
        elif error:
            # 诊断走 stderr：不污染 CLI 的 stdout 正常输出流（对齐项目里
            # print(..., flush=True) 的诊断风格，但分离到错误流）
            print(f'[skill] skipped {path.name}: {error}', file=sys.stderr, flush=True)
    return skills


def _parse_skill(path: Path) -> tuple[Skill | None, str]:
    """解析一个技能文件 → (Skill | None, 跳过原因)。

    返回 (None, '') = 正常跳过（非技能文件，如目录说明文档），不打诊断；
    返回 (None, reason) = 写坏了要提示（缺 name/description、名字非法）。
    只取 name/description（目录需要），正文不读——正文由模型按需 read，
    目录格式化阶段绝不把正文拖进 system（设计决策，见模块 docstring）。
    """
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as error:
        return None, f'unreadable: {error}'
    if not text.startswith('---'):
        return None, ''  # 非技能文件（如 README）：不算坏，静默
    end = text.find('\n---', 3)
    if end < 0:
        return None, 'frontmatter not closed (missing second ---)'
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
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
    return Skill(name=name, description=description, path=path), ''


def format_catalog(skills: list[Skill], workspace: Path) -> str:
    """技能目录 → system 注入文本（静态，字节稳定）。

    内容：引导句（教模型"任务匹配就 read_file 读正文"）+ 每技能一行
    （名字 + 一句话 + 相对 workspace 路径）。正文不在这里——模型按路径
    自己读，读完作为 tool/result 进上下文。
    """
    if not skills:
        return ''
    rel_lines = []
    for skill in skills:
        try:
            rel = skill.path.resolve().relative_to(workspace)
        except ValueError:
            rel = skill.path  # 技能在 workspace 外（理论不会）：给绝对路径兜底
        # as_posix：路径统一正斜杠——模型传给 read_file 的跨平台规范写法
        # （Windows 下反斜杠也能读，但正斜杠在 CLI/JSON 里不用转义、不会歧义）
        rel_lines.append(f'- {skill.name}: {skill.description} ({rel.as_posix()})')
    # 引导句：短 + 给足操作细节。正文不是代码，行号纯浪费——read_file 默认
    # line_numbers=true，这里明确要 false；limit 默认 200 行足够技能正文。
    head = '可用技能（任务匹配描述时用 read_file 读对应文件，line_numbers=false）：'
    return head + '\n' + '\n'.join(rel_lines)
