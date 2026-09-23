"""依赖方向测试：分层不是靠纪律，而是靠这条断言。

四层单向（数字越小越底层）：

```
0 值(agent_demo.values) ── 1 能力(capability) ── 2 状态(state) ── 3 框架循环(runtime)
   ── 4 应用与工具(app / tools) ── 5 入口(web / cli)
```

规则：**一个模块只能 import 同层或更低层**。为什么值得一条测试——`AGENTS.md` 早就写着
依赖方向，但 21 个模块平铺时没人拦得住，实测真的长出了两处反向依赖
（`loop → tools.todo`、`registry → app.constants`）。分层目录化之后"包 = 层"，这条断言
就能机械化：谁 import 反了，当场红。

白名单只收**带 issue 号**的例外，并且**断言它们仍然存在**——修好之后必须同步删掉条目，
不然白名单会悄悄长成一张废纸（僵尸条目 = 测试在替一段已经不存在的代码开脱）。
"""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / 'agent_demo'

# 包名 → 层号（没有列出的包按入口层处理：`agent_demo/__init__.py` 与 `cli.py`）
LAYERS = {
    'values': 0,
    'capability': 1,
    'state': 2,
    'runtime': 3,
    'app': 4,
    'tools': 4,
    'web': 5,
}
ENTRY_LAYER = 5

# 已知例外：(源模块, 目标模块) → 为什么留着
#
# 曾经的第二条是 `state.registry → app.constants`（状态层要用应用层的
# `TOOL_RESULT_MAX_CHARS`）：第 4 步把它下沉到 `values/limits.py` 之后就不再越界，
# 于是按"白名单不许长僵尸"的规矩从这里删掉了——这条注释就是那次删除留下的记录。
KNOWN_VIOLATIONS = {
    ('agent_demo.runtime.loop', 'agent_demo.tools.todo'):
        'issue #19：把"每轮叠给模型的状态"做成注册制贡献者之后清掉',
}


def module_name(path: Path) -> str:
    """文件 → 绝对模块名（`agent_demo/state/session.py` → `agent_demo.state.session`）。"""
    return '.'.join(path.relative_to(PACKAGE_ROOT.parent).with_suffix('').parts)


def layer_of(module: str) -> int:
    """模块 → 层号（`agent_demo` 自己与未知包按入口层算）。"""
    parts = module.split('.')
    if parts[0] != 'agent_demo' or len(parts) < 2:
        return ENTRY_LAYER
    return LAYERS.get(parts[1], ENTRY_LAYER)


def unresolved_target(source_module: str, node: ast.ImportFrom) -> str:
    """import 的目标模块的绝对名（相对 import 按源模块所在包解析）。"""
    if node.level == 0:
        return node.module or ''
    base = source_module.rpartition('.')[0]          # 自己的模块名去掉 → 所在包
    for _ in range(node.level - 1):
        base = base.rpartition('.')[0]
    return f'{base}.{node.module}' if node.module else base


def import_edges() -> set[tuple[str, str]]:
    """(源模块, 目标模块) 的全部**包内**依赖边。"""
    edges: set[tuple[str, str]] = set()
    for path in sorted(PACKAGE_ROOT.rglob('*.py')):
        module = module_name(path)
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.ImportFrom):
                target = unresolved_target(module, node)
                if target.startswith('agent_demo'):
                    edges.add((module, target))
                    for alias in node.names:         # from agent_demo import x → 也是模块依赖
                        sub = f'{target}.{alias.name}'
                        if (PACKAGE_ROOT / Path(*sub.split('.')[1:])).with_suffix('.py').exists():
                            edges.add((module, sub))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith('agent_demo'):
                        edges.add((module, alias.name))
    return edges


def violations() -> set[tuple[str, str]]:
    """反向依赖：源层号 **小于** 目标层号（下层不该知道上层）。"""
    return {
        (src, dst) for src, dst in import_edges()
        if layer_of(src) < layer_of(dst)
    }


def test_layers_only_depend_downwards():
    found = violations()
    unexpected = sorted(found - set(KNOWN_VIOLATIONS))
    assert not unexpected, (
        '依赖方向反了（下层 import 上层）：\n'
        + '\n'.join(f'  {src} → {dst}'
                    for src, dst in unexpected)
        + '\n\n要么改成按依赖倒置（钩子/注册声明），要么在 KNOWN_VIOLATIONS 里带上 issue 号。'
    )


def test_known_violations_are_still_real():
    """白名单不许有僵尸条目：修好之后必须同步删掉，否则它会替不存在的代码开脱。"""
    found = violations()
    stale = sorted(set(KNOWN_VIOLATIONS) - found)
    assert not stale, (
        '这些例外已经不再越界了，请从 KNOWN_VIOLATIONS 删掉：\n'
        + '\n'.join(f'  {src} → {dst}' for src, dst in stale)
    )
