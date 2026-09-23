"""Web 宿主：SSE 流、会话管理/标题、队列动作、审批、并发隔离与断开取消。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**断言与用例体一字未改**；
唯一差异是 26 处函数内冗余 import 被 ruff 的 F401/F811 删掉——那是拆分暴露出来的旧问题。）
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_demo.app.constants import MODEL_CONTEXT_WINDOW
from agent_demo.capability.llm import StreamChunk
from agent_demo.state.session import Session
from agent_demo.values.messages import (
    TextBlock,
    ToolCallBlock,
    create_assistant_message,
    create_tool_result_message,
    create_user_message,
)
from agent_demo.values.persistence import load_events, save_event


def test_web_chat_streams_events(tmp_path):
    from fastapi.testclient import TestClient

    from agent_demo import web

    # SSE 流全链路（--fake 离线验证；sessions_dir 隔离，不污染真实会话）
    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)

    assert client.get('/').status_code == 200          # 页面可访问
    h = client.get('/history').json()
    assert h['history'] == [] and h['todos'] == [] and 'context' in h  # 空会话

    resp = client.post('/chat', json={'message': 'hi'})
    assert resp.status_code == 200
    # SSE 帧：流式文本 + 工具调用 + 回合结束标记
    assert resp.text.startswith('data: ')
    assert '"type": "chunk"' in resp.text
    assert '"type": "tool_call"' in resp.text
    assert '"type": "turn_end"' in resp.text
    # 统一投影模型的协议：回合开始帧、真人发言帧（带 turn+message_id）、
    # 请求边界帧（issue #4 ③）、内容帧带 turn/step
    assert '"type": "turn_start"' in resp.text
    assert '"type": "user_message"' in resp.text
    assert '"type": "chunk", "turn": 1' in resp.text      # chunk 带 turn（前端按节点分块）
    assert '"type": "request_start", "turn": 1' in resp.text  # 请求边界（按请求分块）
    user_frame = [f for f in resp.text.split('data: ') if '"user_message"' in f]
    assert user_frame, 'user_message frame missing'
    frame = json.loads(user_frame[0].split('\n\n')[0])
    assert frame['text'] == 'hi'
    assert frame['turn'] == 1
    assert frame['message_id'], 'user_message frame must carry message_id (乐观气泡认领用)'

    # 对话后历史可查（记忆 = 日志投影，Web 视角同样成立）
    payload = client.get('/history').json()
    assert payload['todos'] == []
    history = payload['history']
    assert history[0]['role'] == 'user'
    assert any(m['role'] == 'assistant' and m['text'] for m in history)


def test_history_projects_reasoning_per_request(tmp_path):
    """issue #4：/history 把思维链（痕迹）投影到对应 assistant 消息上。

    UI 是日志的投影——思维链虽不回灌模型，但历史/刷新后深度思考块必须能
    重建。配对规则：assistant/reasoning 紧跟在它的 assistant/message 之前，
    按 seq 顺序 buffer 配对；同一步工具循环的多次请求各配一份（不合并）。
    """
    from fastapi.testclient import TestClient

    from agent_demo import web

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)
    session = web.state.session

    # 手工落一轮：两次请求（同 turn/step，模拟工具循环），各带思维链
    session.append('turn/start', {'turn': 1})
    session.append('user/message', create_user_message([TextBlock(text='调查一下')]),
                   surface_op='append')
    session.append('step/start', {'turn': 1, 'step': 1})
    # 第一次请求：reasoning + assistant/message（带 tool_call）
    session.append('assistant/reasoning', {'turn': 1, 'step': 1, 'reasoning': '先读文件'})
    session.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message(
            [ToolCallBlock(id='c1', name='read_file', arguments='{}')],
            provider='fake', model='m'),
    }, surface_op='append')
    session.append('tool/call', {'turn': 1, 'step': 1, 'call_id': 'c1',
                                 'name': 'read_file', 'arguments': '{}'})
    session.append('tool/result', create_tool_result_message('c1', '内容', False), surface_op='append')
    # 第二次请求：另一份 reasoning + 纯文本回答
    session.append('assistant/reasoning', {'turn': 1, 'step': 1, 'reasoning': '看完了，可以总结'})
    session.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message([TextBlock(text='总结如下')],
                                            provider='fake', model='m'),
    }, surface_op='append')
    session.append('step/end', {'turn': 1, 'step': 1})
    session.append('turn/end', {'turn': 1, 'reason': 'completed'})

    history = client.get('/history').json()['history']
    assistants = [m for m in history if m['role'] == 'assistant']
    assert len(assistants) == 2
    # 每条 assistant 各自带自己那次的思维链（同一步两次请求不合并）
    assert assistants[0]['reasoning'] == '先读文件'
    assert assistants[1]['reasoning'] == '看完了，可以总结'
    # 无思维链的消息不出现该字段
    user_msgs = [m for m in history if m['role'] == 'user']
    assert all('reasoning' not in m for m in user_msgs)


def test_web_session_management(tmp_path):
    from fastapi.testclient import TestClient

    from agent_demo import web

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)

    # 隔离目录初始化即含空的 web 会话（"会话存在 = 有文件"）；
    # 聊天后摘要更新为首条用户消息
    initial = client.get('/sessions').json()
    assert [s['id'] for s in initial] == ['web']
    assert initial[0]['summary'] == '(empty)'
    client.post('/chat', json={'message': 'hello web'})
    items = client.get('/sessions').json()
    assert items[0]['summary'].startswith('hello web')
    assert items[0]['events'] > 0

    # 新建会话并切换（空历史）
    fresh = client.post('/sessions/new').json()
    assert fresh['id'] != 'web' and fresh['history'] == []
    h = client.get('/history').json()
    assert h['history'] == [] and h['todos'] == [] and 'context' in h

    # 切回 web 会话：历史还原（恢复 = 重放）
    back = client.post(f"/sessions/{fresh['id']}/switch").json()
    assert back['id'] == fresh['id']
    switched = client.post('/sessions/web/switch').json()
    assert switched['id'] == 'web'
    assert switched['history'][0]['role'] == 'user'
    assert switched['history'][0]['text'] == 'hello web'

    # 非法/不存在会话 id：拒绝而不是穿路径
    assert client.post('/sessions/%2e%2e%2fswitch').status_code in (400, 404)
    assert client.post('/sessions/no-such-session/switch').status_code == 404

    # 删除：非当前会话可删；当前会话拒绝（先切换走再删）
    fresh2 = client.post('/sessions/new').json()
    assert fresh2['id'] != 'web'
    client.post('/sessions/web/switch')   # new 已切到 fresh2，先切回 web
    assert client.post(f"/sessions/{fresh2['id']}/delete").status_code == 200
    assert all(s['id'] != fresh2['id'] for s in client.get('/sessions').json())
    assert client.post('/sessions/web/delete').status_code == 400  # 当前会话
    assert client.post('/sessions/%2e%2e%2fdelete').status_code in (400, 404)


def test_web_session_title_endpoint(tmp_path):
    """手动改名：append session/title（user）→ 列表 summary 以标题优先，重放可恢复。"""
    from fastapi.testclient import TestClient

    from agent_demo import web

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)

    # 聊天产生内容 → fallback 摘要为首条消息
    client.post('/chat', json={'message': 'hello rename me'})
    items = client.get('/sessions').json()
    assert items[0]['summary'].startswith('hello rename me')
    assert items[0]['title'] is None

    # 改名（非当前会话也可以：先新建一个并切走，再改回 web 的名字）
    fresh = client.post('/sessions/new').json()
    resp = client.post(f"/sessions/{fresh['id']}/title", json={'title': '我的自定义名字'})
    assert resp.status_code == 200
    assert resp.json()['source'] == 'user'
    by_id = {s['id']: s for s in client.get('/sessions').json()}
    assert by_id[fresh['id']]['summary'] == '我的自定义名字'
    assert by_id[fresh['id']]['title_source'] == 'user'

    # 标题 = 日志投影：切回该会话后，日志里存在 session/title 事件（可重放）
    switched = client.post(f"/sessions/{fresh['id']}/switch").json()
    assert switched['id'] == fresh['id']
    log = (tmp_path / 'sess' / f"{fresh['id']}.jsonl").read_text(encoding='utf-8')
    assert '"type": "session/title"' in log
    assert '"source": "user"' in log

    # 空标题拒绝
    assert client.post(f"/sessions/{fresh['id']}/title", json={'title': '   '}).status_code == 400


def test_auto_title_trigger_conditions(tmp_path):
    """自动起名只在 真模型 + 无标题 + 首条消息 时触发（fake 一律跳过）。"""
    from agent_demo import web

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    s = Session(id='x')
    # fake 模式：不自动起名（没有真模型可调）
    assert web.titles.should_auto_title(s) is False

    # 已有标题：不重复起名
    from argparse import Namespace
    web.state.args = Namespace(
        fake=False, model='m', workspace=tmp_path, hide_reasoning=False,
        session='x', sessions=str(tmp_path / 'sess'), prompt='', resume=False, verbose=False,
    )
    s.append('session/title', {'title': 't', 'source': 'user'})
    assert web.titles.should_auto_title(s) is False

    # 已有用户消息（resume 继续对话）：不再自动起名（标题应基于第一条）
    s2 = Session(id='x')
    s2.append('user/message', create_user_message([TextBlock(text='hi')]), surface_op='append')
    assert web.titles.should_auto_title(s2) is False

    # 干净会话 + 真模型：触发
    s3 = Session(id='x')
    assert web.titles.should_auto_title(s3) is True


def test_session_title_event_is_trace_not_surface(tmp_path):
    """session/title 是痕迹事件：不进模型记忆（derive_messages），但重放保留。"""
    from agent_demo import web
    from agent_demo.state.session import Session

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    s = Session(id='t')
    s.append('session/title', {'title': '我的标题', 'source': 'user'})
    assert [e.type for e in s.events] == ['session/title']
    assert s.derive_messages() == []  # 不污染模型可见消息

    # 持久化 → 新会话重放（adopt）仍能看到标题事件
    path = tmp_path / 'sess' / 't.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    for e in s.events:
        save_event(path, e)
    restored = Session(id='t')
    for e in load_events(path):
        restored.adopt(e)
    titles = [e for e in restored.events if e.type == 'session/title']
    assert titles and titles[-1].data['title'] == '我的标题'


def test_auto_title_rejects_verbatim_copy():
    """自动起名逐字复读首条消息 → 判定为失败（不落 auto 事件，退回 fallback）。"""
    from agent_demo import web
    assert web.titles.is_verbatim_copy('你好', '你好') is True
    assert web.titles.is_verbatim_copy('总结README', '请帮我总结README') is True   # 子串
    assert web.titles.is_verbatim_copy('代码审查', '请帮我审查这段代码') is False  # 概括 ≠ 复读
    assert web.titles.is_verbatim_copy('问候', '你好') is False


def test_web_todo_dock_payloads(tmp_path):
    """todo dock 的数据通道：SSE 帧 todo_update + /history 附带 todos + 会话切换恢复。"""
    from fastapi.testclient import TestClient

    from agent_demo import web

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)

    # fake 会话默认脚本不含 todo_write；先直接往日志写 todo，模拟已有清单
    s = web.state.session
    s.append('todo/write', {'todos': [
        {'content': '读代码', 'status': 'completed'},
        {'content': '写修复', 'status': 'in_progress'},
        {'content': '跑测试', 'status': 'pending'},
    ]})

    # /history 附带当前 todos 投影
    payload = client.get('/history').json()
    assert [t['content'] for t in payload['todos']] == ['读代码', '写修复', '跑测试']
    assert payload['todos'][1]['status'] == 'in_progress'

    # 会话切换也带 todos（dock 在换会话时恢复）
    fresh = client.post('/sessions/new').json()
    assert fresh['todos'] == []
    switched = client.post('/sessions/web/switch').json()
    assert [t['content'] for t in switched['todos']] == ['读代码', '写修复', '跑测试']


def test_web_checkpoint_role_and_context_payload(tmp_path):
    """checkpoint 消息标记 role=checkpoint；history/会话响应带 context。"""
    from fastapi.testclient import TestClient

    from agent_demo import web
    from agent_demo.values.messages import TextBlock, create_user_message

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)

    # 直接往当前会话写 checkpoint 形态的 user/message（带 compacted-summary 标签）
    s = web.state.session
    s.append('turn/start', {'turn': 1})
    # 用**事件自己的 seq**做遮蔽区间，而不是写死 (1, 1)：会话日志开头可能已经有别的痕迹
    # 事件（例如创建时落的 `session/workspace`），seq 不是从 0 数起的固定值——写死会在
    # 那种会话上指向错误的事件（实测：`1 is not in list`）。
    q1 = s.append('user/message', create_user_message([TextBlock(text='Q1')]), surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    s.append('turn/start', {'turn': 2})
    cp_text = ('This is an automatically generated checkpoint…\n\n'
               '<compacted-summary>\n## 主要请求\n- 重构\n## 下一步\n1. 测试\n</compacted-summary>')
    s.append('user/message', create_user_message([TextBlock(text=cp_text)]),
             surface_op='replace', shadowed=(q1.seq, q1.seq))
    s.append('user/message', create_user_message([TextBlock(text='继续')]), surface_op='append')

    hist = client.get('/history').json()
    roles = [m['role'] for m in hist['history']]
    assert roles == ['checkpoint', 'user']          # checkpoint 被标记，后续 user 正常
    assert hist['context'] is not None              # 上下文 payload 存在
    assert 'window' in hist['context'] and 'percent' in hist['context']
    # fake 模式无真实 usage → 不带 session 累计账（前端隐藏命中/消耗标签）
    assert 'session' not in hist['context']

    # 切换也带 context
    fresh = client.post('/sessions/new').json()
    assert 'context' in fresh


def test_web_context_session_totals_accumulate(tmp_path):
    """真实 usage 多条 → 会话级累计账（消耗 token 求和、缓存命中率 token 加权）。"""
    from fastapi.testclient import TestClient

    from agent_demo import web
    from agent_demo.values.messages import TextBlock, create_assistant_message, create_user_message

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)
    s = web.state.session
    s.append('turn/start', {'turn': 1})
    s.append('user/message', create_user_message([TextBlock(text='Q1')]), surface_op='append')
    # 两次真实请求：usage 必须各自落账，命中率是 Σ 比值而非单次、也非简单平均
    s.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message([TextBlock(text='A1')]),
        'usage': {'prompt_tokens': 2000, 'completion_tokens': 300,
                  'prompt_cache_hit_tokens': 1500, 'prompt_cache_miss_tokens': 500},
    }, surface_op='append')
    s.append('assistant/message', {
        'turn': 1, 'step': 2,
        'message': create_assistant_message([TextBlock(text='A2')]),
        'usage': {'prompt_tokens': 1000, 'completion_tokens': 100,
                  'prompt_cache_hit_tokens': 200, 'prompt_cache_miss_tokens': 800},
    }, surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})

    hist = client.get('/history').json()
    ctx = hist['context']
    # 快照仍取最后一条真实 usage（圆环语义不变）
    assert ctx['used'] == 1000
    assert ctx['percent'] == round(1000 * 100 / MODEL_CONTEXT_WINDOW)
    # 会话级累计账：消耗 = Σ input + Σ output；命中率 = Σhit / Σ(hit+miss)
    session = ctx['session']
    assert session['input_tokens'] == 3000            # 2000 + 1000
    assert session['output_tokens'] == 400            # 300 + 100
    assert session['total_tokens'] == 3400            # 3000 + 400
    assert session['cache_hit_pct'] == 57             # (1500+200)/(2000+1000) = 56.67 → 57%
    # 简单平均会得 (75% + 20%)/2 = 47.5 —— 断言拒绝该口径
    assert session['cache_hit_pct'] != 48

    # 会话内某些请求没报缓存拆分 → 该请求不计入命中统计（但消耗照记）
    s2 = client.post('/sessions/new').json()
    sid = s2['id']
    client.post(f'/sessions/{sid}/switch')
    s = web.state.session
    s.append('turn/start', {'turn': 1})
    s.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message([TextBlock(text='no-cache-field')]),
        'usage': {'prompt_tokens': 500, 'completion_tokens': 50},
    }, surface_op='append')
    s.append('assistant/message', {
        'turn': 1, 'step': 2,
        'message': create_assistant_message([TextBlock(text='with-cache')]),
        'usage': {'prompt_tokens': 300, 'completion_tokens': 30,
                  'prompt_cache_hit_tokens': 90, 'prompt_cache_miss_tokens': 210},
    }, surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    ctx2 = client.get('/history').json()['context']
    assert ctx2['session']['total_tokens'] == 880      # 消耗照记全部：500+300+50+30
    assert ctx2['session']['cache_hit_pct'] == 30      # 命中只统计报了拆分的：90/300


def test_web_manual_compact_endpoint(tmp_path):
    """手动压缩 POST /compact：fake 拒绝；真实模式压缩旧回合落 checkpoint。"""
    import os
    os.environ['DEEPSEEK_API_KEY'] = 'sk-placeholder'
    from fastapi.testclient import TestClient

    from agent_demo import web
    from agent_demo.values.messages import TextBlock, create_assistant_message, create_user_message

    # fake 模式：脚本模型不能生成摘要 → 400 拒绝
    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)
    assert client.post('/compact').status_code == 400

    # 真实模式 + stub llm：跑一轮完整旧回合，手动压缩应落 checkpoint
    web.init_web(tmp_path, fake=False, sessions_dir=tmp_path / 'sess2',
                     model='deepseek-v4-flash')
    s = web.state.session
    agent = web.state.agent

    class CompactLlm:
        async def stream(self, request, signal=None):
            if 'compaction engine' in (request.system or ''):
                yield StreamChunk(text='## 主要请求\n- 压历史\n## 下一步\n1. 继续', finish_reason='stop')
                return
            yield StreamChunk(text='回答', finish_reason='stop')
    agent.llm = CompactLlm()

    def user(t): return create_user_message([TextBlock(text=t)])
    s.append('turn/start', {'turn': 1})
    s.append('user/message', user('历史任务' * 50), surface_op='append')
    s.append('assistant/message', {'turn': 1, 'step': 1,
             'message': create_assistant_message([TextBlock(text='旧回答' * 50)])},
             surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    s.append('turn/start', {'turn': 2})
    s.append('user/message', user('新任务'), surface_op='append')

    resp = client.post('/compact')
    body = resp.json()
    assert resp.status_code == 200
    assert body['compacted'] is True
    assert any(e.type == 'compaction/summary' for e in s.events)
    assert any(e.type == 'compaction/end' for e in s.events)
    # checkpoint 进了模型可见历史，且新回合消息保留
    texts = [m.content[0].text for m in s.derive_messages()
             if m.content and getattr(m.content[0], 'type', '') == 'text']
    assert any('<compacted-summary>' in t for t in texts)
    assert '新任务' in texts[-1]

    # 无可压旧回合 → compacted False + reason（前面已全压完，只剩新回合）
    body2 = client.post('/compact').json()
    assert body2['compacted'] is False
    assert body2['reason']


def test_web_sessions_run_in_parallel_isolated(tmp_path):
    """Web 并发隔离：两会话各自跑一轮 chat，事件/审批/焦点互不踩。

    旧实现是全局单例（_session/_agent/_active_queue）：两会话同时跑时
    B 会覆盖 A 的指针与 SSE 队列。seat 化后每个 sid 一个
    {session, agent, queue}——验证：/chat 带 sid 路由到各自 seat、
    消息只进自己的日志、焦点别名随切换走、agent 实例彼此不同。
    """
    from fastapi.testclient import TestClient

    from agent_demo import web

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)
    assert _id_of(client) == 'web'

    # 建第二个会话（new 返回描述并切为焦点）
    b = client.post('/sessions/new').json()
    assert b['id'] != 'web'

    # 两个会话各自发一轮消息（fake llm 离线脚本，能跑完一个回合）
    ra = client.post('/chat', json={'message': '任务甲：先读 A', 'sid': 'web'})
    rb = client.post('/chat', json={'message': '任务乙：先读 B', 'sid': b['id']})
    assert ra.status_code == 200 and rb.status_code == 200
    assert '"type": "turn_end"' in ra.text and '"type": "turn_end"' in rb.text

    # 焦点 = 最后切换/操作的会话（兼容旧路由无 sid 语义）
    assert web.state.current_sid == b['id']
    assert web.state.session.id == b['id']

    # 两会话的 agent 是不同实例，各自日志只有自己的消息
    seat_a = web.state.seats['web']
    seat_b = web.state.seats[b['id']]
    assert seat_a.agent is not seat_b.agent          # 独立 agent
    assert seat_a.session is not seat_b.session      # 独立事件日志

    def user_texts(seat):
        return [m.content[0].text for m in seat.session.derive_messages()
                if m.content and getattr(m.content[0], 'type', '') == 'text'
                and m.role == 'user']

    assert user_texts(seat_a) == ['任务甲：先读 A']   # A 的日志只有甲
    assert user_texts(seat_b) == ['任务乙：先读 B']   # B 的日志只有乙

    # /history 带 sid 各取各的（不带 = 焦点 b）
    ha = client.get('/history', params={'sid': 'web'}).json()
    hb = client.get('/history').json()
    assert any('任务甲' in (m.get('text') or '') for m in ha['history'])
    assert any('任务乙' in (m.get('text') or '') for m in hb['history'])

    # 切回 A：seat 复用（同实例，agent 事件日志延续）——不重建销毁
    switched = client.post('/sessions/web/switch').json()
    assert web.state.session.id == 'web'
    assert web.state.seats['web'] is seat_a             # 复用而非重建
    assert any('任务甲' in (m.get('text') or '') for m in switched['history'])


def _id_of(client) -> str:
    """当前焦点会话 id（init 后默认 'web'）。"""
    from agent_demo import web
    return web.state.current_sid


def test_web_queue_actions_endpoint(tmp_path):
    """POST /queue/update：队列项操作（对齐 DSH 的 updateQueue(itemId, action)）。

    三种动作与两个语义要守住：
    - remove：队列区快照为空，且日志里没有产生 user/message surface——
      这条消息从未成为模型可见的输入（"没有状态不进日志"的逆向：撤回
      只留 spliced 痕迹）
    - edit：就地改写还没 claim 的消息（同 id 换文案），同样不产生 surface
    - steer：next-turn → next-step 的搬家（空闲时拒绝：没有"下一步"）
    - 已 claim（已进消息流）的操作返回 ok=False + queue-item-not-found，
      这是并发下的**正常收敛**而不是错误（HTTP 仍 200）
    """
    from fastapi.testclient import TestClient

    from agent_demo import web

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)
    seat = web.state.seats['web']

    def update(item_id, action):
        resp = client.post('/queue/update',
                           json={'item_id': item_id, 'action': action, 'sid': 'web'})
        assert resp.status_code == 200, resp.text
        return resp.json()

    # 直接入队一条（wakeup=False：不真跑回合）：模拟"已发出、还没轮到"
    message = create_user_message([TextBlock(text='这条还没轮到')])
    seat.agent.send(message, 'next-step', wakeup=False)
    queued_id = message.id
    assert [r['id'] for r in web.payload.queue_rows(seat.agent)] == [queued_id]

    # /history 与 /steer 一样带 queue（刷新页面也能恢复队列区）
    hist = client.get('/history').json()
    assert [(r['id'], r['placement']) for r in hist['queue']] == [(queued_id, 'steering')]
    # 会话切换也带 queue
    client.post('/sessions/new')
    switched = client.post('/sessions/web/switch').json()
    assert [r['id'] for r in switched['queue']] == [queued_id]

    resp = client.post('/queue/update',
                       json={'item_id': queued_id, 'action': {'kind': 'remove'}, 'sid': 'web'})
    assert resp.json() == {'ok': True, 'code': 'ok', 'sid': 'web', 'queue': []}
    assert not seat.agent.inbox.has_pending
    assert not [e for e in seat.session.events if e.type == 'user/message']

    # 已认领/已消失的项：ok=False + 并发收敛码（HTTP 仍 200）
    gone = update(queued_id, {'kind': 'remove'})
    assert gone['ok'] is False and gone['code'] == 'queue-item-not-found'
    # 校验：缺 item_id / 动作词表外 / 空编辑 / 会话不存在
    assert client.post('/queue/update', json={'action': {'kind': 'remove'}}).status_code == 400
    assert client.post('/queue/update',
                       json={'item_id': 'x', 'action': {'kind': 'nope'}}).status_code == 400
    assert client.post('/queue/update',
                       json={'item_id': 'x', 'action': {'kind': 'edit', 'text': ' '}}).status_code == 400
    assert client.post('/queue/update',
                       json={'item_id': 'x', 'action': {'kind': 'remove'},
                             'sid': 'nope'}).status_code == 404


def test_web_queue_rows_serialize_state_projection(tmp_path):
    """web 层只做序列化：_queue_rows 把状态层的投影摊平成前端 JSON。

    注意入队走 wakeup=False：这里只测投影，不真跑回合（sync 测试里
    没有事件循环，_wake 会拉不起 driver）。
    """
    from agent_demo import web

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    session, agent = web.state.session, web.state.agent
    assert session is not None and agent is not None

    first = create_user_message([TextBlock(text='插队一')])
    agent.send(first, 'next-step', wakeup=False)
    second = create_user_message([TextBlock(text='排队二')])
    agent.send(second, 'next-turn', wakeup=False)      # next-turn = 'queued'
    rows = web.payload.queue_rows(agent)
    assert [(r['text'], r['placement']) for r in rows] == [
        ('排队二', 'queued'), ('插队一', 'steering')]

    agent.unqueue(first.id)
    assert [(r['text'], r['placement']) for r in web.payload.queue_rows(agent)] == [
        ('排队二', 'queued')]
    # 序列化 = 投影的镜像（id 集合一致，一一对应）
    assert [r['id'] for r in web.payload.queue_rows(agent)] == [
        item.id for item in agent.inbox.queued_items()]


def test_web_steer_requires_active_stream(tmp_path):
    """POST /steer：无活跃对话流（idle）时 409 拒绝；提示用 /chat 开回合。"""
    from fastapi.testclient import TestClient

    from agent_demo import web

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)

    # 会话刚初始化，无活跃 SSE 流 → 409
    resp = client.post('/steer', json={'message': 'hi'})
    assert resp.status_code == 409
    assert '活跃对话流' in resp.json()['detail']

    # fake llm 回合很快结束，chat 结束后流已关 → 再 steer 仍 409
    client.post('/chat', json={'message': 'hi'})
    resp2 = client.post('/steer', json={'message': 'again'})
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_web_steer_interrupts_open_sse_stream(tmp_path):
    """Web 端到端打断：/chat 的 SSE 流还开着时 POST /steer → 插队回答沿原流推回。

    这是"运行中可打断对话"的关键链路，同步 TestClient 测不了（post 阻塞到
    回合结束，无法中途发 /steer），故用 httpx.AsyncClient + ASGITransport
    并发两个请求：一个开着 SSE（agent 卡在可控挂起 LLM 上），一个 /steer。
    断言：插队消息在同一回合内被消费（无第二个 turn/start），回答沿原流推送。
    """

    from agent_demo import web
    from agent_demo.capability.llm import StreamChunk

    class HoldLlm:
        """第一次 stream 挂起（started 置位等 release）；放行后给第一轮回答。

        steer 在挂起期间入队 → 第一轮 step 结束后，第二步立即轮到插队消息，
        第二次 stream 返回插队后的回答。
        """
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def stream(self, request, signal=None):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await self.release.wait()
                yield StreamChunk(text='首轮回答', finish_reason='stop')
            else:
                yield StreamChunk(text='插队后回答', finish_reason='stop')

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    seat = web.state.seats['web']
    llm = HoldLlm()
    seat.agent.llm = llm

    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        async def read_sse() -> str:
            parts = []
            async with client.stream('POST', '/chat',
                                     json={'message': '首问', 'sid': 'web',
                                           'request_id': 'rpc-first'}) as resp:
                assert resp.status_code == 200
                async for chunk in resp.aiter_text():
                    parts.append(chunk)
            return '\n'.join(parts)

        reader = asyncio.create_task(read_sse())
        await llm.started.wait()                      # 回合真的挂起在 LLM 里
        steer_resp = await client.post(
            '/steer', json={'message': '停一下改方向', 'sid': 'web',
                            'request_id': 'rpc-steer'})
        assert steer_resp.status_code == 200
        assert steer_resp.json()['queued'] == 'next-step'
        # 返回 message_id + 队列快照：前端据此在消息流尾部画"待处理插队"气泡
        steer_id = steer_resp.json().get('message_id')
        assert steer_id, 'steer must return the queued message id'
        queued = steer_resp.json()['queue']
        # rpc_id 随消息 source 一起投影出来：前端靠它把本地回显原子换成真身
        assert [(r['id'], r['text'], r['placement'], r['rpc_id']) for r in queued] == [
            (steer_id, '停一下改方向', 'steering', 'rpc-steer')]
        llm.release.set()                             # 放行：第一步完成，下一步轮到插队
        sse = await asyncio.wait_for(reader, timeout=10)

    # 插队回答沿原 SSE 流推回来（同回合，无第二个回合）
    assert '首轮回答' in sse
    assert '插队后回答' in sse
    assert sse.count('"type": "turn_end"') == 1       # 一个回合结束 = 插队没开新回合
    # 认领链路：插队消息的 user_message 帧带 /steer 返回的同一个 message_id
    # 与提交身份 rpc_id（前端据此在**同一次渲染**里把本地回显换成真身）
    steer_frames = [f for f in sse.split('data: ')
                    if '"user_message"' in f and '停一下改方向' in f]
    assert steer_frames, 'steer message must be pushed as a user_message frame'
    steer_frame = json.loads(steer_frames[0].split('\n\n')[0])
    assert steer_frame['message_id'] == steer_id
    assert steer_frame['rpc_id'] == 'rpc-steer'
    # 首问那条也带自己的提交身份（idle 发送的回显同样要能交接）
    first_frames = [f for f in sse.split('data: ')
                    if '"user_message"' in f and '首问' in f]
    assert json.loads(first_frames[0].split('\n\n')[0])['rpc_id'] == 'rpc-first'
    # durable 消息 source 上落了提交身份（重放/历史都能认出来）
    sources = {e.data.content[0].text: e.data.source
               for e in seat.session.events if e.type == 'user/message'}
    assert sources['首问'].rpc_id == 'rpc-first'
    assert sources['停一下改方向'].rpc_id == 'rpc-steer'
    # 队列区通道：入队（spliced）推一条含插队消息的快照，claim 后再推一条空快照
    queue_frames = [json.loads(f.split('\n\n')[0]) for f in sse.split('data: ')
                    if '"queue_update"' in f]
    assert queue_frames, 'inbox splice must be pushed as queue_update frames'
    assert any(f['queue'] and f['queue'][0]['id'] == steer_id for f in queue_frames)
    assert queue_frames[-1]['queue'] == []            # 认领后队列区清空
    # 事件日志：全程只有一次 turn/start，两条 user 消息都在
    turns = [e for e in seat.session.events if e.type == 'turn/start']
    assert len(turns) == 1
    users = [e.data for e in seat.session.events if e.type == 'user/message']
    texts = []
    for u in users:
        for block in getattr(u, 'content', ()):
            if getattr(block, 'type', '') == 'text':
                texts.append(block.text)
    assert texts == ['首问', '停一下改方向']


@pytest.mark.asyncio
async def test_web_sse_disconnect_cancels_agent(tmp_path):
    """SSE 客户端断开（停止/关页）必须取消 agent，否则回合永不收敛。

    回归：sse_stream 的 finally 曾只 task.cancel()（run_agent），而
    when_idle 用 asyncio.shield 保护 driver——run_agent 被取消只是让
    when_idle 返回，正在跑的 driver（回合）继续执行、永不结束：
    前端 busy 复位后新消息走 /chat 全堵在 next-turn 排队，新回合永远
    开不了。修复：断开时 agent.cancel() 直达 driver（记 turn/end aborted）。

    可控挂起 LLM：流开着、agent 卡在等待 → 关流（asyncio 取消读取）→
    等 agent 收敛 → 断言回合被 abort、agent 回 idle、inbox 被清。
    """

    from agent_demo import web
    from agent_demo.capability.llm import StreamChunk

    class HoldLlm:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def stream(self, request, signal=None):
            self.started.set()
            await self.release.wait()          # 一直挂到测试放行/取消
            yield StreamChunk(text='never', finish_reason='stop')

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    seat = web.state.seats['web']
    seat.agent.llm = HoldLlm()

    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        async def read_sse() -> None:
            async with client.stream('POST', '/chat',
                                     json={'message': '首问', 'sid': 'web'}) as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_text():
                    pass

        reader = asyncio.create_task(read_sse())
        await seat.agent.llm.started.wait()    # 回合确实挂起（agent running）
        assert seat.agent.status == 'running'
        reader.cancel()                        # 模拟客户端断开（点停止/关页）
        try:
            await reader
        except asyncio.CancelledError:
            pass
        # 断开后允许 run_agent 收尾（cancel 传播 + DONE 落队列）
        await asyncio.sleep(0.3)
        # agent 必须回 idle（driver 被 agent.cancel() 打断，不是被 shield 留着）
        assert seat.agent.status == 'idle', 'SSE 断开后 agent 必须被取消回 idle'
        # 回合记 aborted（不是 completed——被打断，不是自然结束）
        ends = [e.data['reason'] for e in seat.session.events
                if e.type == 'turn/end']
        assert ends == ['aborted'], f'expected aborted turn, got {ends}'
        # inbox 被清空（cancel 默认清队列）：无幽灵消息等下次执行
        assert not seat.agent.inbox.has_pending


def test_web_history_marks_same_turn_steer(tmp_path):
    """历史渲染的回合归属：同回合插队（steer）的 user 消息 turn 相同。

    重开会话时前端靠 user 消息的 turn 决定画不画回合分隔线——若两条
    user 同 turn（首问 + 插队），不画；若跨回合（followup 开新回合），
    画。回归：之前 steer 消息在刷新后被渲染成独立"回合 N"（缺 turn 信息）。
    """
    from fastapi.testclient import TestClient

    from agent_demo import web
    from agent_demo.values.messages import TextBlock, create_assistant_message, create_user_message

    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)
    s = web.state.session

    # turn 1：首问 → 模型答 → 插队（同回合第二条 user）→ 模型答
    def user(t): return create_user_message([TextBlock(text=t)])
    s.append('turn/start', {'turn': 1})
    s.append('user/message', user('首问：读 README'), surface_op='append')
    s.append('assistant/message', {'turn': 1, 'step': 1,
             'message': create_assistant_message([TextBlock(text='A1')])},
             surface_op='append')
    s.append('user/message', user('插队：先别读，列文件'), surface_op='append')  # steer 同回合
    s.append('assistant/message', {'turn': 1, 'step': 2,
             'message': create_assistant_message([TextBlock(text='A2')])},
             surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    # turn 2：新回合
    s.append('turn/start', {'turn': 2})
    s.append('user/message', user('第二轮问题'), surface_op='append')
    s.append('turn/end', {'turn': 2, 'reason': 'completed'})

    hist = client.get('/history').json()['history']
    users = [(m['text'], m.get('turn')) for m in hist if m['role'] == 'user']
    assert [t for _, t in users] == [1, 1, 2]
    assert users[0][0].startswith('首问') and users[1][0].startswith('插队')
    # 同回合两条 user 的 turn 相同（前端据此不画分隔线）；新回合不同
    turns = [t for _, t in users]
    assert turns[0] == turns[1] and turns[1] != turns[2]


# ---------------------------------------------------------------------------
# 每对话工作区：创建时选定 → 写进日志 → 按 seat 隔离沙箱
# ---------------------------------------------------------------------------

def _tool_texts(client) -> list[str]:
    """当前会话历史里所有工具结果的正文（断言"读到的是哪个目录的文件"用）。"""
    out: list[str] = []
    for message in client.get('/history').json()['history']:
        for result in message.get('tool_results') or []:
            out.append(result['content'])
    return out


def test_web_per_session_workspace_isolates_the_sandbox(tmp_path):
    """每个对话的工作区就是它的沙箱根：同名文件在两个对话里读到各自的内容。

    这是本功能的核心承诺（对齐 DSH 的 `SessionHeader.cwd`）。用 fake 脚本固定读
    `README.md`，于是**同一句用户消息**在两个会话里读到不同正文——一份日志一个根，
    `build_tools` 的绑定是 build 时注入的，但每个 seat 各自 build 一次。
    """
    from pathlib import Path

    from fastapi.testclient import TestClient

    from agent_demo import web

    root_a = tmp_path / 'a'
    root_b = tmp_path / 'b'
    for root, text in ((root_a, '# A\n'), (root_b, '# B\n')):
        root.mkdir()
        (root / 'README.md').write_text(text, encoding='utf-8')
    web.init_web(root_a, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)

    # /meta 给的是**宿主默认**工作区 + 进程 cwd（前端预填与相对路径提示用）
    meta = client.get('/meta').json()
    assert Path(meta['workspace']) == root_a.resolve()
    assert Path(meta['cwd']) == Path.cwd().resolve()

    # 宿主默认会话（'web'）：没选工作区 → 宿主默认 A
    client.post('/chat', json={'message': 'read README.md and summarize'})
    assert any('# A' in text for text in _tool_texts(client))

    # 新对话选 B：同一句消息读到 B 的文件，且看不到 A 的内容
    fresh = client.post('/sessions/new', json={'workspace': str(root_b)}).json()
    assert Path(fresh['workspace']) == root_b.resolve()
    client.post('/chat', json={'message': 'read README.md and summarize'})
    assert any('# B' in text for text in _tool_texts(client))
    assert not any('# A' in text for text in _tool_texts(client))

    # seat 级证据：每会话一份 args，**只有 workspace 不同**（fake/model 仍是宿主级）
    assert Path(web.state.seats['web'].args.workspace) == root_a.resolve()
    assert Path(web.state.seats[fresh['id']].args.workspace) == root_b.resolve()
    assert web.state.seats['web'].args.fake is True
    assert web.state.seats[fresh['id']].args.model == web.state.seats['web'].args.model

    # 列表里每个对话都带自己的工作区（没记录过的显示宿主默认）
    listing = {item['id']: Path(item['workspace']) for item in client.get('/sessions').json()}
    assert listing['web'] == root_a.resolve()
    assert listing[fresh['id']] == root_b.resolve()


def test_web_workspace_is_a_trace_event_and_survives_replay(tmp_path):
    """工作区写进日志（痕迹事件）：不进模型记忆，但换个宿主打开还回到同一个目录。

    与 `session/title` 同构：折叠出来的 `derive_messages()` 里没有它，重放却有它——
    这正是"日志是唯一事实源"要的形状（配置事实从日志读回来，不另存一份状态）。
    """
    from pathlib import Path

    from fastapi.testclient import TestClient

    from agent_demo import web
    from agent_demo.web.sessions import open_session_seat

    root_a = tmp_path / 'a'
    root_b = tmp_path / 'b'
    root_a.mkdir()
    root_b.mkdir()
    sessions_dir = tmp_path / 'sess'
    web.init_web(root_a, fake=True, sessions_dir=sessions_dir)
    client = TestClient(web.app)
    sid = client.post('/sessions/new', json={'workspace': str(root_b)}).json()['id']

    events = load_events(sessions_dir / f'{sid}.jsonl')
    workspace_events = [e for e in events if e.type == 'session/workspace']
    assert len(workspace_events) == 1
    assert workspace_events[0].data == {'workspace': str(root_b.resolve()), 'source': 'user'}
    assert workspace_events[0].surface_op is None           # 痕迹事件：不带 surface_op
    seat = web.state.seats[sid]
    assert workspace_events[0].seq not in seat.session.surface  # 不在模型可见投影里
    assert seat.session.workspace() == str(root_b.resolve())

    # 换个宿主（默认工作区仍是 A）重开这个会话：工作区从日志读回来，不是宿主默认
    web.init_web(root_a, fake=True, sessions_dir=sessions_dir)
    reopened = open_session_seat(sid, allow_missing=False)
    assert Path(reopened.args.workspace) == root_b.resolve()

    # 没选工作区的新对话则记录 source='default'（方便日后分辨"用户选的"与"跟随默认"）
    default_sid = client.post('/sessions/new').json()['id']
    default_event = [e for e in load_events(sessions_dir / f'{default_sid}.jsonl')
                     if e.type == 'session/workspace'][0]
    assert default_event.data == {'workspace': str(root_a.resolve()), 'source': 'default'}


def test_web_legacy_session_follows_host_default_without_rewriting_log(tmp_path):
    """旧会话（日志里没有 `session/workspace`）跟随宿主默认，且**不回头改写它的日志**。"""
    from pathlib import Path

    from fastapi.testclient import TestClient

    from agent_demo import web

    root_a = tmp_path / 'a'
    root_a.mkdir()
    sessions_dir = tmp_path / 'sess'
    sessions_dir.mkdir()
    legacy = Session(id='legacy')
    legacy.bind_store(sessions_dir / 'legacy.jsonl')
    legacy.append('user/message', create_user_message([TextBlock(text='旧会话')]),
                  surface_op='append')

    web.init_web(root_a, fake=True, sessions_dir=sessions_dir)
    client = TestClient(web.app)
    switched = client.post('/sessions/legacy/switch').json()
    assert switched['id'] == 'legacy'
    assert Path(switched['workspace']) == root_a.resolve()
    assert switched['history'][0]['text'] == '旧会话'
    assert [e.type for e in load_events(sessions_dir / 'legacy.jsonl')] == ['user/message']


def test_web_workspace_validation_and_immutability(tmp_path):
    """选择期的校验与"创建后固定"：不存在 / 不是目录 → 400 说清原因；换工作区要新开对话。"""
    import argparse
    from pathlib import Path

    import pytest
    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    from agent_demo import web
    from agent_demo.web.sessions import WORKSPACE_FIXED_MESSAGE
    from agent_demo.web.sessions import open_session_seat as _open_seat

    root_a = tmp_path / 'a'
    root_b = tmp_path / 'b'
    root_a.mkdir()
    root_b.mkdir()
    web.init_web(root_a, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)

    missing = client.post('/sessions/new', json={'workspace': str(tmp_path / 'nope')})
    assert missing.status_code == 400
    assert 'does not exist' in missing.json()['detail']

    a_file = tmp_path / 'plain.txt'
    a_file.write_text('x', encoding='utf-8')
    not_dir = client.post('/sessions/new', json={'workspace': str(a_file)})
    assert not_dir.status_code == 400
    assert 'not a directory' in not_dir.json()['detail']

    # 空串等价于"不选"→ 宿主默认（前端清空输入框也走这条）
    blank = client.post('/sessions/new', json={'workspace': '   '})
    assert blank.status_code == 200
    assert Path(blank.json()['workspace']) == root_a.resolve()

    # 已有会话的工作区固定：想换就新建对话（直接调 seat API 验这条守卫）
    sid = client.post('/sessions/new').json()['id']
    seat = _open_seat(sid, allow_missing=False)
    with pytest.raises(HTTPException) as error:
        _open_seat(sid, allow_missing=False, workspace=str(root_b))
    assert error.value.status_code == 400
    assert 'fixed' in error.value.detail
    assert Path(seat.args.workspace) == root_a.resolve()
    assert isinstance(seat.args, argparse.Namespace)

    # 另一条入口也要拦：**磁盘上有日志、内存里还没 seat**（服务刚重启 / 重新 init）——
    # 这条若"静默忽略"，调用方会以为换成功了，实际工具还在旧根上
    web.init_web(root_a, fake=True, sessions_dir=tmp_path / 'sess')   # 清空 seat 注册表
    with pytest.raises(HTTPException) as error:
        _open_seat(sid, allow_missing=False, workspace=str(root_b))
    assert error.value.status_code == 400
    assert error.value.detail == WORKSPACE_FIXED_MESSAGE


def test_web_reopening_a_session_whose_workspace_is_gone_is_loud(tmp_path):
    """日志里的工作区不存在了 → 409 说清；**不许**静默换成宿主默认。

    静默回退是最坏情况：工具指向另一个项目，而模型以为自己还在原来的目录里。
    """
    from pathlib import Path

    from fastapi.testclient import TestClient

    from agent_demo import web

    root_a = tmp_path / 'a'
    root_b = tmp_path / 'b'
    root_a.mkdir()
    root_b.mkdir()
    sessions_dir = tmp_path / 'sess'
    web.init_web(root_a, fake=True, sessions_dir=sessions_dir)
    client = TestClient(web.app)
    sid = client.post('/sessions/new', json={'workspace': str(root_b)}).json()['id']

    web.init_web(root_a, fake=True, sessions_dir=sessions_dir)   # 新宿主：seat 注册表清空
    root_b.rename(tmp_path / 'b-moved')                          # 目录没了（改名/删除都一样）
    gone = client.post(f'/sessions/{sid}/switch')
    assert gone.status_code == 409
    assert 'workspace is gone' in gone.json()['detail']
    assert Path(web.state.seats['web'].args.workspace) == root_a.resolve()  # 其他会话不受影响


def test_resolve_workspace_policy(tmp_path):
    """`app/workspace.py` 的策略：空 → 默认（不查存在性）；显式路径必须存在 + 是目录。"""
    from pathlib import Path

    import pytest

    from agent_demo.app.workspace import resolve_workspace

    default = tmp_path / 'default'
    default.mkdir()
    assert resolve_workspace(None, default=default) == default.resolve()
    assert resolve_workspace('  ', default=default) == default.resolve()
    assert resolve_workspace(str(default), default=tmp_path / 'elsewhere') == default.resolve()

    with pytest.raises(ValueError, match='does not exist'):
        resolve_workspace(str(tmp_path / 'ghost'), default=default)
    a_file = tmp_path / 'plain.txt'
    a_file.write_text('x', encoding='utf-8')
    with pytest.raises(ValueError, match='not a directory'):
        resolve_workspace(str(a_file), default=default)

    # 相对路径按**进程当前目录**解析（与 shell 直觉一致，不是相对默认工作区）
    assert resolve_workspace('.', default=default) == Path.cwd().resolve()
    # 默认工作区不检查存在性：与 CLI 的 --workspace 行为一致（缺失时工具调用自己报 is_error）
    assert resolve_workspace(None, default=tmp_path / 'ghost-default') \
        == (tmp_path / 'ghost-default').resolve()


def test_web_list_and_seat_agree_on_a_hand_written_workspace(tmp_path, monkeypatch):
    """列表与 seat 用**同一条规则**解释日志里的工作区（相对路径按进程 cwd）。

    日志是我们自己写的（绝对路径），但手改过的日志可能是相对路径。这时"列表显示 `rel-ws`、
    工具实际在 `<cwd>/rel-ws`"是最难查的一类不一致——两边单看都合理。所以解析规则只能有
    一处实现（`app/workspace.py` 的 `normalize_recorded_workspace`），这条测试盯住它。
    """
    from pathlib import Path

    from fastapi.testclient import TestClient

    from agent_demo import web
    from agent_demo.web.sessions import open_session_seat as _open_seat

    sessions_dir = tmp_path / 'sess'
    sessions_dir.mkdir()
    (tmp_path / 'rel-ws').mkdir()
    (sessions_dir / 'hand.jsonl').write_text(
        json.dumps({'seq': 0, 'time': 0.0, 'type': 'session/workspace',
                    'data': {'$dict': {'workspace': 'rel-ws', 'source': 'user'}},
                    'surface_op': None, 'shadowed': None, 'ignorable': False}) + '\n',
        encoding='utf-8')

    host = tmp_path / 'host'
    host.mkdir()
    web.init_web(host, fake=True, sessions_dir=sessions_dir)
    client = TestClient(web.app)
    monkeypatch.chdir(tmp_path)          # 相对路径的解析基准 = 进程 cwd

    items = {item['id']: item['workspace'] for item in client.get('/sessions').json()}
    assert Path(items['hand']) == (tmp_path / 'rel-ws').resolve()
    seat = _open_seat('hand', allow_missing=False)
    assert Path(seat.args.workspace) == Path(items['hand'])   # 两处一致才是重点


def test_web_new_session_ids_do_not_collide_within_a_second(tmp_path, monkeypatch):
    """同一秒内连开两个对话不能撞 id：撞了会**静默复用**上一个会话（工作区还被拒改）。

    把时钟钉死，让"基础 id 已被占用"这一态**确定性地**出现——否则只有恰好跨秒才走到
    顺延分支，测试看起来绿其实没覆盖。（补丁打在标准库 `time.time` 上：路由就是通过
    `time.time()` 取时间戳的，pytest 的 monkeypatch 会在用例结束还原。）
    """
    from fastapi.testclient import TestClient

    from agent_demo import web

    monkeypatch.setattr('time.time', lambda: 1_700_000_000.0)
    sessions_dir = tmp_path / 'sess'
    web.init_web(tmp_path, fake=True, sessions_dir=sessions_dir)
    client = TestClient(web.app)

    first = client.post('/sessions/new').json()['id']
    assert first == 'web-1700000000'
    second = client.post('/sessions/new').json()['id']
    assert second == 'web-1700000000-2'          # 顺延分支
    (sessions_dir / f'{second}.jsonl').touch()
    assert client.post('/sessions/new').json()['id'] == 'web-1700000000-3'
    assert len(client.get('/sessions').json()) == 4  # 'web' + 三个新会话，都在列表里

