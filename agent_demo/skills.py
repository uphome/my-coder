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
    解析容错：文件不可读 / frontmatter 缺 name / description 为空 → 跳过
    （技能坏了不该让整个 system 组装崩掉——宁缺毋滥，静默丢弃并留痕）。
    """
    skills: list[Skill] = []
    if not root.is_dir():
        return skills
    for path in sorted(root.glob('*.md')):
        skill = _parse_skill(path)
        if skill is not None:
            skills.append(skill)
    return skills


def _parse_skill(path: Path) -> Skill | None:
    """解析一个技能文件：YAML frontmatter 的 name/description + 正文文件路径。

    只取 name/description（目录需要），正文不读——正文由模型按需 read，
    目录格式化阶段绝不把正文拖进 system（设计决策，见模块 docstring）。
    """
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return None
    if not text.startswith('---'):
        return None
    end = text.find('\n---', 3)
    if end < 0:
        return None
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ':' in line:
            key, _, value = line.partition(':')
            meta[key.strip()] = value.strip().strip('"\'')
    name = meta.get('name', path.stem)
    description = meta.get('description', '').strip()
    if not _NAME.match(name) or not description:
        return None
    return Skill(name=name, description=description, path=path)


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
    head = (
        '可用技能（任务匹配其描述时，用 read_file 读取对应文件全文再按其执行）：'
    )
    return head + '\n' + '\n'.join(rel_lines)
