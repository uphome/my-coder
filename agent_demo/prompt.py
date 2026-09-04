"""状态层：提示词注册表——sections 按 order 拼接 + {{变量}} 严格插值。

插值规则（与 harness 一致）：引用必须是完整 {{name}}、名字匹配
[a-z][a-z0-9_]*、必须已注册且本轮有值，否则抛错；没有后续 }} 的
单个 {{ 是普通文本；替换出的值不再二次扫描。
"""
from __future__ import annotations

import re

# 模板变量名只允许小写字母/数字/下划线，且不能数字开头。
# 严格命名的目的：{{modl}} 这类拼写错误在组装时立刻抛错，
# 而不是把字面量静默发给模型（提示词 bug 是昂贵且难查的）。
VARIABLE_NAME = re.compile(r'^[a-z][a-z0-9_]*$')


class PromptRegistry:
    """提示词注册表：sections（按 order 排序的段落）+ variables（{{name}} 插值）。

    设计要点：
    - section 注册时带 order 数值，组装时排序——注册顺序无所谓，
      插件可以把自己的提示词插到任意位置而不动已有代码。
    - variable 注册的是 provider(ctx) 函数，每次 assemble 才求值，
      所以 cwd、model 等运行时信息永远是新鲜的。
    - 严格插值：未注册/无值的变量引用直接抛错，宁炸勿静默。
    """

    def __init__(self) -> None:
        self._sections: dict[str, tuple[float, object]] = {}
        self._variables: dict[str, object] = {}

    def section(self, name: str, order: float, text) -> object:
        """注册一段提示词。text 是静态字符串或 ctx -> str 的提供者。"""
        if name in self._sections:
            raise ValueError(f'prompt section {name!r} is already registered')
        self._sections[name] = (order, text)
        return lambda: self._sections.pop(name, None)

    def variable(self, name: str, provider) -> object:
        """注册一个 {{name}} 模板变量，provider(ctx) 每次组装时求值。"""
        if not VARIABLE_NAME.match(name):
            raise ValueError(f'invalid prompt variable name {name!r}')
        if name in self._variables:
            raise ValueError(f'prompt variable {name!r} is already registered')
        self._variables[name] = provider
        return lambda: self._variables.pop(name, None)

    def assemble(self, ctx=None) -> dict:
        """组装一次：求值所有变量 + 按 order 排序 sections。

        每个回合开头调用一次（loop.run_turn），得到本回合的提示词快照；
        之后 render 只是纯字符串拼接，不再求值。
        """
        variables = {}
        for name, provider in self._variables.items():
            variables[name] = provider(ctx) if callable(provider) else provider
        sections = []
        for name, (_, text) in sorted(self._sections.items(), key=lambda item: (item[1][0], item[0])):
            sections.append({'name': name, 'text': text(ctx) if callable(text) else text})
        return {'sections': sections, 'variables': variables}

    def render(self, assembly: dict) -> str:
        """把组装结果折叠成最终 system 提示词：逐个 section 插值，非空段用空行连接。"""
        parts = []
        for section in assembly['sections']:
            rendered = _interpolate(section['text'], assembly['variables'], f"section {section['name']!r}")
            if rendered:
                parts.append(rendered)
        return '\n\n'.join(parts)


def _interpolate(text: str, variables: dict, owner: str) -> str:
    """严格 {{name}} 插值（规则见模块 docstring）。

    与宽松模板引擎的区别：
    - 未注册/无值的引用抛错，而不是留空或原样输出；
    - 没有闭合 }} 的单个 {{ 是普通文本（提示词里可写 JSON 大括号）；
    - 替换值不再二次扫描（值里含 {{ 不会被误插值）。
    """
    result = ''
    last = 0
    while True:
        open_idx = text.find('{{', last)
        if open_idx < 0:
            break
        close_idx = text.find('}}', open_idx + 2)
        if close_idx < 0:
            result += text[last:open_idx + 2]
            last = open_idx + 2
            continue
        name = text[open_idx + 2:close_idx]
        if not VARIABLE_NAME.match(name):
            raise ValueError(f'malformed prompt variable reference in {owner}')
        if name not in variables:
            raise ValueError(
                f'unknown prompt variable {{{{ {name} }}}} in {owner}; '
                f'registered: {sorted(variables)}',
            )
        value = variables[name]
        if value is None:
            raise ValueError(f'prompt variable {{{{ {name} }}}} has no value for this assembly ({owner})')
        result += text[last:open_idx] + value
        last = close_idx + 2
    return result + text[last:]
