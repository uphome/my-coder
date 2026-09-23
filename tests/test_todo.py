"""todo：整表写入、状态栏折叠成 messages 末尾的合成消息、跨回合持续。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**断言与用例体一字未改**；
唯一差异是 26 处函数内冗余 import 被 ruff 的 F401/F811 删掉——那是拆分暴露出来的旧问题。）
"""
from __future__ import annotations

import asyncio
import json

import pytest

from my_coder.capability.llm import FakeLlm
from my_coder.state.prompt import PromptRegistry
from my_coder.state.registry import ToolSpec
from my_coder.state.session import Session
from my_coder.tools import build_tools
from my_coder.values.persistence import load_events, save_event


def test_todo_write_folds_and_injects_into_prompt(tmp_path):
    """todo_write 全链路：写整表 → 折叠读回 → 作为 live 段注入下次请求的 system。"""

    from my_coder.state.session import Session
    from my_coder.tools import build_tools
    from my_coder.tools.todo import fold_todos

    session = Session(id='todo-test')
    registry = build_tools(tmp_path)
    spec: ToolSpec = registry._tools['todo_write']

    class FakeAgent:  # todo_write executor 需要 agent.session 落日志
        def __init__(self, s): self.session = s
    agent = FakeAgent(session)

    # 1) 规划 3 步：一步 in_progress
    out = asyncio.run(spec.execute({'todos': [
        {'content': '读代码', 'status': 'in_progress'},
        {'content': '写修复', 'status': 'pending'},
        {'content': '跑测试', 'status': 'pending'},
    ]}, agent, None))
    assert out.is_error is False and '2 pending, 1 in progress' in out.content

    # 2) 折叠读回 = 最后一次 todo/write 快照
    folded = fold_todos(session)
    assert [t['content'] for t in folded] == ['读代码', '写修复', '跑测试']
    assert folded[0]['status'] == 'in_progress'

    # 3) 事件是痕迹（非 surface）：不进模型记忆，但可重放
    assert [e.type for e in session.events] == ['todo/write']
    assert session.derive_messages() == []

    # 4) prompt live 段注入：assemble 一次，两次 render 拿到两次新鲜清单
    prompt = PromptRegistry()
    prompt.section('todo:state', 0, lambda ctx: _fmt_todos(ctx['agent']))
    prompt.section('identity', -10, 'static preamble')
    prompt.variable('x', lambda ctx: 'v')
    from my_coder.tools.todo import fold_todos as _fold
    def _fmt_todos(agent):
        todos = _fold(agent.session)
        if not todos:
            return ''
        return 'TODOS: ' + '; '.join(f"{t['content']}={t['status']}" for t in todos)
    assembly = prompt.assemble({'agent': type('A', (), {'session': session})()})
    assert 'TODOS: 读代码=in_progress; 写修复=pending; 跑测试=pending' in prompt.render(assembly, {'agent': type('A', (), {'session': session})()})

    # 5) 更新清单（完成一项）→ 下次 render 看到新状态（live 段不锁死在快照）
    asyncio.run(spec.execute({'todos': [
        {'content': '读代码', 'status': 'completed'},
        {'content': '写修复', 'status': 'in_progress'},
    ]}, agent, None))
    rendered = prompt.render(assembly, {'agent': type('A', (), {'session': session})()})
    assert '读代码=completed' in rendered and '写修复=in_progress' in rendered
    assert '跑测试' not in rendered


def test_todo_write_rejects_bad_inputs(tmp_path):
    """todo 校验降级为 is_error（不炸循环）：空 content / 重复 / 多 in_progress。"""


    registry = build_tools(tmp_path)
    spec = registry._tools['todo_write']
    cases = [
        [{'content': '  ', 'status': 'pending'}],                 # 空 content
        [{'content': 'a', 'status': 'pending'}, {'content': 'a', 'status': 'completed'}],  # 重复
        [{'content': 'a', 'status': 'in_progress'}, {'content': 'b', 'status': 'in_progress'}],  # 双活动
    ]
    for todos in cases:
        out = asyncio.run(spec.execute({'todos': todos}, None, None))
        assert out.is_error is True, f'should reject {todos}'


@pytest.mark.asyncio
async def test_todo_status_bar_in_messages(tmp_path):
    """方案 A：todo 状态栏从 system 迁到 messages 末尾（合成 user 消息）。

    FakeLlm 两步：第一步 todo_write 规划 2 项，第二步纯文本。断言：
    - 第一步请求（规划前）：无状态栏（fold 无清单）
    - 第二步请求（规划后）：request/header.runtime_status 里记了 `todo`（审计字段，
      注册制贡献者的映射形态，见 issue #19），
      且 system **不含** todo 清单（system 全静态）
    - 状态栏 XML 含两项与状态
    """
    from argparse import Namespace

    from my_coder.app.factory import build_agent
    from my_coder.state.session import Session

    session = Session(id='todo-status')
    args = Namespace(fake=True, model='fake-model', workspace=tmp_path, hide_reasoning=False,
                     session='id', sessions=str(tmp_path), prompt='x', resume=False, verbose=False)
    llm = FakeLlm(script=[
        {
            'tool_calls': [{'id': 't1', 'name': 'todo_write', 'arguments': json.dumps({
                'todos': [
                    {'content': 'step one', 'status': 'in_progress'},
                    {'content': 'step two', 'status': 'pending'},
                ]})}],
            'finish_reason': 'tool_calls',
        },
        {'text': 'planned done', 'finish_reason': 'stop'},
    ])
    agent = build_agent(session, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
    agent.llm = llm
    agent.followup('do the multi-step work')
    await agent.when_idle()

    headers = [e.data for e in session.events if e.type == 'request/header']
    assert len(headers) == 2, f'expected 2 model requests, got {len(headers)}'

    # 规划前：无状态栏
    assert 'runtime_status' not in headers[0]
    # system 里不再有 todo 清单（方案 A：system 全静态）
    assert 'step one' not in headers[0]['system']
    assert 'todo:state' not in headers[0]['system']

    # 规划后：audit 字段带 XML 状态栏（映射形态：贡献者名 → 原文）；system 仍不含清单
    status = headers[1].get('runtime_status', {}).get('todo')
    assert status is not None, 'second request must carry runtime_status["todo"] audit field'
    assert status.startswith('<todo_status>') and status.endswith('</todo_status>')
    assert '[in_progress] step one' in status
    assert '[pending] step two' in status
    assert 'step one' not in headers[1]['system']      # system 保持静态

    # 每轮都叠（第二轮也带了）；且 derive_messages 里没有状态栏（历史零污染）
    derived_texts = []
    for message in session.derive_messages():
        for block in message.content:
            if getattr(block, 'type', '') == 'text':
                derived_texts.append(block.text)
    assert not any('todo_status' in text for text in derived_texts)


def test_todo_status_bar_absent_cases(tmp_path):
    """build_todo_status 的不叠条件：无清单 / 全 completed → None。"""
    from my_coder.tools.todo import build_todo_status

    session = Session(id='status-absent')
    assert build_todo_status(session) is None          # 从未写过

    session.append('todo/write', {'todos': [
        {'content': 'a', 'status': 'pending'},
        {'content': 'b', 'status': 'in_progress'},
    ]})
    status = build_todo_status(session)
    assert status is not None
    assert '<todo_status>' in status and '1. [pending] a' in status

    # 全部 completed → 收尾，状态栏关闭
    session.append('todo/write', {'todos': [
        {'content': 'a', 'status': 'completed'},
        {'content': 'b', 'status': 'completed'},
    ]})
    assert build_todo_status(session) is None

    # 空清单 → 也不叠
    session.append('todo/write', {'todos': []})
    assert build_todo_status(session) is None


def test_todo_fold_persists_across_turns(tmp_path):
    """todo = 跨回合任务清单（2026-09 语义变更）：turn/start 不再清空。

    一旦 todo_write 建立清单就持续（后续回合继续更新同一份），直到模型
    把全部项标 completed（前端据此短暂展示后自动隐藏 dock）。
    """
    from my_coder.tools.todo import all_completed, fold_todos

    s = Session(id='t')

    # 回合 1：写清单
    s.append('turn/start', {'turn': 1})
    s.append('todo/write', {'todos': [
        {'content': 'a', 'status': 'completed'},
        {'content': 'b', 'status': 'pending'},
    ]})
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    folded = fold_todos(s)
    assert [t['content'] for t in folded] == ['a', 'b']
    assert all_completed(folded) is False

    # 回合 2 开始：**不再清空**——清单跨回合持续（b 还在 pending）
    s.append('turn/start', {'turn': 2})
    assert [t['content'] for t in fold_todos(s)] == ['a', 'b']

    # 回合 2 里继续更新同一份清单
    s.append('todo/write', {'todos': [
        {'content': 'a', 'status': 'completed'},
        {'content': 'b', 'status': 'completed'},
    ]})
    all_done = fold_todos(s)
    assert [t['content'] for t in all_done] == ['a', 'b']
    assert all_completed(all_done) is True      # 全 completed → 前端收尾隐藏

    # 回合 3：全 completed 清单仍折叠出来（dock 由前端判定全勾后隐藏，
    # 折叠本身保留最后快照——resume 重放一致）
    s.append('turn/start', {'turn': 3})
    assert all_completed(fold_todos(s)) is True

    # resume 重放语义一致：adopt 同样的序列得到同样的折叠
    from my_coder.state.session import Session as S2
    path = tmp_path / 'turn.jsonl'
    for e in s.events:
        save_event(path, e)
    restored = S2(id='t')
    for e in load_events(path):
        restored.adopt(e)
    assert all_completed(fold_todos(restored)) is True
