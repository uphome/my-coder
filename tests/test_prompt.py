"""prompt 注册表：sections 组装、严格插值，以及 system 里的通用纪律位置。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**断言与用例体一字未改**；
唯一差异是 26 处函数内冗余 import 被 ruff 的 F401/F811 删掉——那是拆分暴露出来的旧问题。）
"""
from __future__ import annotations

import pytest

from my_coder.state.prompt import PromptRegistry
from my_coder.state.session import Session


def test_interpolation_strict():
    registry = PromptRegistry()
    registry.section('persona', 0, 'You run on {{model}} in {{cwd}}.')
    registry.variable('model', lambda ctx: 'deepseek-chat')
    registry.variable('cwd', lambda ctx: '/tmp')
    assert registry.render(registry.assemble({})) == 'You run on deepseek-chat in /tmp.'

    bad = PromptRegistry()
    bad.section('p', 0, '{{nope}}')
    with pytest.raises(ValueError, match='unknown prompt variable'):
        bad.render(bad.assemble({}))


def test_interpolation_keeps_literal_braces():
    registry = PromptRegistry()
    registry.section('p', 0, 'a { b and an unclosed {{ brace')
    assert registry.render(registry.assemble({})) == 'a { b and an unclosed {{ brace'


def test_interpolation_rejects_nested_braces():
    registry = PromptRegistry()
    registry.section('p', 0, '{{{model}}}')
    registry.variable('model', lambda ctx: 'M')
    with pytest.raises(ValueError, match='malformed prompt variable'):
        registry.render(registry.assemble({}))


@pytest.mark.asyncio
async def test_identity_prompt_is_neutral(tmp_path):
    """agent 身份 = 中性编码助手：request/header 的 system 里不含品牌/血缘词。

    之前 identity 写的是 'powered by DeepSeek Harness (Python demo)'，模型
    会照抄自我介绍——作品集项目不该把功劳归给被复刻对象，锁住文案防回归。
    """
    from argparse import Namespace

    from my_coder.app import factory
    from my_coder.state.session import Session

    args = Namespace(fake=True, model='fake-model', workspace=tmp_path, hide_reasoning=False,
                     session='id', sessions=str(tmp_path), prompt='x', resume=False, verbose=False)
    session = Session(id='id')
    agent = factory.build_agent(session, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
    agent.followup('hi')
    await agent.when_idle()
    headers = [e.data for e in session.events if e.type == 'request/header']
    assert headers, 'expected a request/header event'
    system = headers[0]['system']
    assert 'coding agent' in system
    # {{model}} 变量已渲染成实际模型名，且身份即模型名（不许自称别的模型）
    assert 'fake-model' in system
    for banned in ('DeepSeek Harness', 'powered by', 'Python demo',
                   'Claude', 'Anthropic', 'GPT', 'OpenAI'):
        assert banned not in system, f'identity must not mention {banned!r}'


@pytest.mark.asyncio
async def test_system_prompt_carries_general_discipline(tmp_path):
    """通用行为纪律必须在 system 里，且**排在工具专属规则之前**。

    为什么守这条：system 是唯一"每轮都生效"的通道，文档（AGENTS.md）只有愿意读的
    agent 才看得到——反复被踩的坑如果不提成 system 里的通用规则，agent 每次都要
    重新踩一遍。四条纪律（范围 / 成本 / 自证 / 项目指令）与具体工具无关，所以放在
    通用段（identity/persona/discipline）而不是某个 tool:* 段；这里断言它们真的
    渲染进了 system，且位置在工具段之前（顺序错位会让"通用性"名存实亡）。
    """
    from argparse import Namespace

    from my_coder.app import factory

    args = Namespace(fake=True, model='fake-model', workspace=tmp_path, hide_reasoning=False,
                     session='id', sessions=str(tmp_path), prompt='x', resume=False, verbose=False)
    session = Session(id='id')
    agent = factory.build_agent(session, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
    agent.followup('hi')
    await agent.when_idle()
    headers = [e.data for e in session.events if e.type == 'request/header']
    assert headers, 'expected a request/header event'
    system = headers[0]['system']

    for label in ('Scope:', 'Economy:', 'Evidence:', 'Instructions:'):
        assert label in system, f'通用纪律缺 {label}（应提成 system 规则，而不是只写在文档里）'
    # 通用段排在工具段之前；且"验证"不再等同于"执行"（理解代码不必跑命令）
    assert system.index('Scope:') < system.index('Use bash')
    assert 'reading code needs no execution' in system
