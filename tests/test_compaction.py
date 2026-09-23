"""上下文压缩：选区、四步事务、失败降级、阈值触发与溢出恢复、token 账。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**断言与用例体一字未改**；
唯一差异是 26 处函数内冗余 import 被 ruff 的 F401/F811 删掉——那是拆分暴露出来的旧问题。）
"""
from __future__ import annotations

import asyncio

import pytest

from my_coder.capability.llm import StreamChunk
from my_coder.state.session import Session
from my_coder.values.messages import (
    TextBlock,
    ToolCallBlock,
    create_assistant_message,
    create_tool_result_message,
    create_user_message,
)


@pytest.mark.asyncio
async def test_compaction_selection_and_file_ops():
    """选区：保留最近 N 回合；文件提取：read/write/edit 归类。"""
    from my_coder.app.compaction import extract_file_ops, select_compact_range

    def user(text):
        return create_user_message([TextBlock(text=text)])

    def asst(text='', calls=()):
        return {'message': create_assistant_message([TextBlock(text=text)] + list(calls))}

    s = Session(id='c')
    s.append('turn/start', {'turn': 1})
    s.append('user/message', user('Q1'), surface_op='append')
    s.append('assistant/message', asst('A1'), surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    s.append('turn/start', {'turn': 2})
    s.append('user/message', user('Q2'), surface_op='append')
    s.append('assistant/message', asst('', [ToolCallBlock(id='c1', name='read_file', arguments='{"file_path": "a.py"}')]),
             surface_op='append')
    s.append('tool/call', {'call_id': 'c1', 'name': 'read_file', 'arguments': '{"file_path": "a.py"}'})
    s.append('assistant/message', asst('', [ToolCallBlock(id='c2', name='edit', arguments='{"file_path": "b.py"}')]),
             surface_op='append')
    s.append('tool/call', {'call_id': 'c2', 'name': 'edit', 'arguments': '{"file_path": "b.py"}'})
    s.append('tool/result', create_tool_result_message('c1', 'ok', False), surface_op='append')
    s.append('turn/end', {'turn': 2, 'reason': 'completed'})
    s.append('turn/start', {'turn': 3})
    s.append('user/message', user('Q3'), surface_op='append')

    # keep=2 → 压掉 turn1（Q1/A1 在最前）
    rng = select_compact_range(s, keep_turns=2)
    assert rng is not None
    start, end = rng
    assert (start, end) == (s.surface[0], s.surface[1])  # Q1, A1
    texts = []
    for m in s.derive_messages():
        for b in m.content:
            if getattr(b, 'type', '') == 'text':
                texts.append(b.text)
    assert texts[:2] == ['Q1', 'A1']

    # keep=3 → 无可压
    assert select_compact_range(s, keep_turns=3) is None

    # 文件提取：turn2 区间（Q2..tool result）含 read a.py + edit b.py
    rng2 = select_compact_range(s, keep_turns=1)
    assert rng2 is not None
    ops = extract_file_ops(s, *rng2)
    assert ops.read == frozenset({'a.py'})
    assert ops.edited == frozenset({'b.py'})
    assert ops.written == frozenset()


@pytest.mark.asyncio
async def test_compaction_transaction_fake_llm(tmp_path):
    """整链路：start → summary → replace(checkpoint) → end，摘要替换旧回合。"""
    from my_coder.app.compaction import run_compaction

    class FakeCompactorLlm:
        def __init__(self, summary):
            self._summary = summary
            self.seen_requests: list = []

        async def stream(self, request, signal=None):
            self.seen_requests.append(request)
            yield StreamChunk(text=self._summary, finish_reason='stop')

    def user(text):
        return create_user_message([TextBlock(text=text)])

    def asst(text):
        return {'message': create_assistant_message([TextBlock(text=text)])}

    s = Session(id='tc')
    s.append('turn/start', {'turn': 1})
    s.append('user/message', user('请做一个非常非常长的任务说明，内容足以超过摘要长度以便触发压缩检查'), surface_op='append')
    s.append('assistant/message', asst('完成了第一步的详细描述，这里还有很多后续内容要继续展开说明以便填充足够长度'), surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    s.append('turn/start', {'turn': 2})
    s.append('user/message', user('继续'), surface_op='append')

    llm = FakeCompactorLlm('[checkpoint] 早期任务的简短摘要')
    ok = await run_compaction(s, llm, keep_turns=1)
    assert ok is True

    # 四事件齐全 + checkpoint replace
    types = [e.type for e in s.events]
    assert types.count('compaction/start') == 1
    assert types.count('compaction/summary') == 1
    assert types.count('compaction/end') == 1
    replace_events = [e for e in s.events if e.surface_op == 'replace']
    assert len(replace_events) == 1
    cp = replace_events[0]
    # 遮蔽的是原始 surface 里 turn1 的两条消息（Q1/A1，seq 1 和 2）
    assert cp.shadowed == (1, 2)

    # derive 只剩 checkpoint + turn2 的 Q2
    texts = [m.content[0].text for m in s.derive_messages()]
    assert 'automatically generated checkpoint' in texts[0]      # preamble
    assert '<compacted-summary>' in texts[0]                      # 标签包裹
    assert '[checkpoint] 早期任务的简短摘要' in texts[0]           # 模型摘要本体
    assert texts[-1] == '继续'

    # summary 审计事件存模型产出本体（不含 preamble/标签包装）
    summary_evt = [e for e in s.events if e.type == 'compaction/summary'][0]
    assert summary_evt.data['summary'].startswith('[checkpoint]')

    # summary 事件含审计字段
    summary_evt = [e for e in s.events if e.type == 'compaction/summary'][0]
    assert 'range' in summary_evt.data
    assert 'file_ops' in summary_evt.data

    # 摘要请求保留 thinking（默认开，v4）——压缩要提炼取舍，思维链有助质量；
    # max_tokens 给足（8k），让"思考 + 正文"都放得下（曾设 600 被思维链吃光 →
    # content 空 → empty summary：真 bug，曾让手动压缩多次失败）
    req = llm.seen_requests[0]
    assert req.thinking is not False
    assert req.max_tokens is not None and req.max_tokens >= 8192


@pytest.mark.asyncio
async def test_compaction_failure_degrades():
    """失败降级：空摘要 / 摘要不够小 → 落 end{error}，日志完好、无 replace。"""
    from my_coder.app.compaction import run_compaction

    def user(text):
        return create_user_message([TextBlock(text=text)])

    def asst(text):
        return {'message': create_assistant_message([TextBlock(text=text)])}

    def fresh():
        s = Session(id='f')
        s.append('turn/start', {'turn': 1})
        s.append('user/message', user('很长的原始内容' * 30), surface_op='append')
        s.append('assistant/message', asst('回复内容' * 30), surface_op='append')
        s.append('turn/end', {'turn': 1, 'reason': 'completed'})
        s.append('turn/start', {'turn': 2})
        s.append('user/message', user('继续'), surface_op='append')
        return s

    class StubLlm:
        def __init__(self, text): self._text = text
        async def stream(self, request, signal=None):
            yield StreamChunk(text=self._text, finish_reason='stop')

    # 空摘要
    s = fresh()
    assert await run_compaction(s, StubLlm(''), keep_turns=1) is False
    ends = [e for e in s.events if e.type == 'compaction/end']
    assert ends and 'error' in ends[-1].data
    assert not any(e.surface_op == 'replace' for e in s.events)  # 没提交
    assert [m.content[0].text for m in s.derive_messages()][0].startswith('很长的原始内容')  # 原样

    # 摘要不小于原文（模型偷懒抄回去）→ 拒绝提交
    s2 = fresh()
    assert await run_compaction(s2, StubLlm('模型偷懒复读的摘要内容' * 100), keep_turns=1) is False
    assert not any(e.surface_op == 'replace' for e in s2.events)


@pytest.mark.asyncio
async def test_auto_compaction_fires_on_threshold(tmp_path):
    """自动压缩：turn/end 后上下文超阈值 → 触发压缩；低于阈值不触发。"""
    from my_coder.app.compaction import wire_auto_compaction

    def user(text):
        return create_user_message([TextBlock(text=text)])

    def asst(text):
        return {'message': create_assistant_message([TextBlock(text=text)])}

    class StubLlm:
        async def stream(self, request, signal=None):
            yield StreamChunk(text='## 主要请求\n- 总结\n## 下一步\n1. 无', finish_reason='stop')

    def build():
        s = Session(id='a')
        long_txt = '任务：全面重构工具并补充边界测试覆盖空文件越界非法输入等场景。' * 20
        s.append('turn/start', {'turn': 1})
        s.append('user/message', user(long_txt), surface_op='append')
        s.append('assistant/message', asst('收到，先读实现再分步重构。' * 8), surface_op='append')
        s.append('turn/end', {'turn': 1, 'reason': 'completed'})
        s.append('turn/start', {'turn': 2})
        s.append('user/message', user('继续'), surface_op='append')
        return s

    # 超阈值（估算 ~几百）→ turn2 结束触发压缩
    s = build()
    agent = type('A', (), {'session': s, 'llm': StubLlm(), 'options': {'model': 'm'}})()
    unsub = wire_auto_compaction(agent, max_tokens=100, keep_turns=1)
    s.append('assistant/message', asst('继续推进。' * 3), surface_op='append')
    s.append('turn/end', {'turn': 2, 'reason': 'completed'})
    await asyncio.sleep(0.3)
    unsub()
    assert any(e.type == 'compaction/summary' for e in s.events)
    assert any(e.type == 'compaction/end' for e in s.events)
    assert any(e.type == 'turn/end' for e in s.events)

    # 低于阈值 → 不触发
    s2 = Session(id='a2')
    s2.append('turn/start', {'turn': 1})
    s2.append('user/message', user('hi'), surface_op='append')
    s2.append('assistant/message', asst('hello'), surface_op='append')
    agent2 = type('A', (), {'session': s2, 'llm': StubLlm(), 'options': {'model': 'm'}})()
    unsub2 = wire_auto_compaction(agent2, max_tokens=100000, keep_turns=1)
    s2.append('turn/end', {'turn': 1, 'reason': 'completed'})
    await asyncio.sleep(0.2)
    unsub2()
    assert not any(e.type == 'compaction/start' for e in s2.events)


def test_build_agent_wires_auto_compaction_by_default(tmp_path):
    """真实模式默认挂阈值压缩（0.5M）+ 溢出恢复；fake 模式不挂；0 关闭阈值压缩。"""
    import os
    from argparse import Namespace

    from my_coder.app.constants import DEFAULT_COMPACT_TOKENS
    from my_coder.app.factory import build_agent
    os.environ['DEEPSEEK_API_KEY'] = 'sk-placeholder'  # build_agent 只构造 llm 不连接
    import my_coder.app.compaction as comp  # factory 函数体内 import 会实时取这里，mock 生效
    wired = []
    orig_wire = comp.wire_auto_compaction
    orig_overflow = comp.wire_overflow_recovery
    # 观察接线调用（不真正挂，避免副作用）
    comp.wire_auto_compaction = lambda agent, **kw: wired.append(('auto', kw)) or object()
    comp.wire_overflow_recovery = lambda agent, **kw: wired.append(('overflow', kw))
    try:
        args = Namespace(fake=False, model='m', workspace=tmp_path, hide_reasoning=False,
                         session='x', sessions=str(tmp_path), prompt='', resume=False, verbose=False)
        session = Session(id='x')
        build_agent(session, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
        kinds = [k for k, _ in wired]
        assert kinds == ['overflow', 'auto']  # 溢出恢复恒挂 + 阈值默认挂
        auto_kw = dict(wired[1][1])
        assert auto_kw['max_tokens'] == DEFAULT_COMPACT_TOKENS

        # 显式 0 → 阈值压缩关，但溢出恢复仍在（错误兜底不依赖阈值开关）
        wired.clear()
        args2 = Namespace(fake=False, model='m', workspace=tmp_path, hide_reasoning=False,
                          session='x', sessions=str(tmp_path), prompt='', resume=False, verbose=False,
                          compact_at=0)
        build_agent(Session(id='x2'), args2, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
        assert [k for k, _ in wired] == ['overflow']

        # fake 模式 → 都不挂（脚本 llm 不能真摘要）
        wired.clear()
        args3 = Namespace(fake=True, model='m', workspace=tmp_path, hide_reasoning=False,
                          session='x', sessions=str(tmp_path), prompt='', resume=False, verbose=False)
        build_agent(Session(id='x3'), args3, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
        assert wired == []
    finally:
        comp.wire_auto_compaction = orig_wire
        comp.wire_overflow_recovery = orig_overflow


def test_cache_hit_rate_from_real_usage():
    """真实 usage 拆分缓存命中率：hit/(hit+miss)；无 usage → None。"""
    from my_coder.app.compaction import cache_hit_rate, estimate_context_tokens, last_prompt_usage

    s = Session(id='t')
    s.append('turn/start', {'turn': 1})
    # 模拟真实 provider 的 usage：prompt_tokens = hit + miss（DeepSeek 加性拆分）
    usage = {
        'prompt_tokens': 1000,
        'prompt_cache_hit_tokens': 700,
        'prompt_cache_miss_tokens': 300,
    }
    s.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message([TextBlock(text='ok')]),
        'usage': usage,
    }, surface_op='append')
    assert last_prompt_usage(s) == usage
    assert estimate_context_tokens(s) == 1000          # 真实 usage 优先
    assert cache_hit_rate(s) == 0.7                    # 700 / (700+300)
    assert cache_hit_rate(s) is not None and 0 <= cache_hit_rate(s) <= 1

    # 无 usage（fake 模式 / 最新 assistant 无 usage）→ 无真实测量
    s2 = Session(id='t2')
    s2.append('turn/start', {'turn': 1})
    s2.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message([TextBlock(text='x' * 200)]),
    }, surface_op='append')
    assert last_prompt_usage(s2) is None
    assert cache_hit_rate(s2) is None
    assert estimate_context_tokens(s2) == 100           # 兜底字符估算 200//2

    # usage 缺 cache 拆分字段 → 命中率 None（不是 0——别假装测量过）
    s3 = Session(id='t3')
    s3.append('turn/start', {'turn': 1})
    s3.append('assistant/message', {
        'turn': 1, 'step': 1,
        'message': create_assistant_message([TextBlock(text='ok')]),
        'usage': {'prompt_tokens': 50, 'completion_tokens': 5, 'total_tokens': 55},
    }, surface_op='append')
    assert cache_hit_rate(s3) is None


def test_context_overflow_detection():
    """溢出错误识别：HTTP_ERROR + 特征串为真，其他为假。"""
    from my_coder.app.compaction import _is_context_overflow
    assert _is_context_overflow('HTTP_ERROR', '400: maximum context length exceeded') is True
    assert _is_context_overflow('HTTP_ERROR', 'input is too long for the model') is True
    assert _is_context_overflow('HTTP_ERROR', '上下文长度超过限制') is True
    assert _is_context_overflow('HTTP_ERROR', 'rate limit reached') is False
    assert _is_context_overflow('RATE_LIMIT', 'context length') is False  # 非 HTTP_ERROR code
    assert _is_context_overflow('HTTP_ERROR', '') is False


@pytest.mark.asyncio
async def test_overflow_recovery_compacts_and_retries(tmp_path):
    """溢出恢复：模型报上下文过长 → 压缩旧回合 → retry 成功。"""
    import os
    os.environ['DEEPSEEK_API_KEY'] = 'sk-placeholder'
    from argparse import Namespace

    from my_coder.app.factory import build_agent

    class OverflowLlm:
        def __init__(self):
            self.main_steps = [
                {'error': {'code': 'HTTP_ERROR', 'message': 'maximum context length exceeded (1M)'}},
                {'text': '压缩后重试成功', 'finish_reason': 'stop'},
            ]

        async def stream(self, request, signal=None):
            from my_coder.capability.llm import LlmError
            if 'compaction engine' in (request.system or ''):
                yield StreamChunk(text='## 主要请求\n- 压缩历史\n## 下一步\n1. 继续', finish_reason='stop')
                return
            step = self.main_steps.pop(0)
            if 'error' in step:
                raise LlmError(step['error']['code'], step['error']['message'])
            yield StreamChunk(text=step['text'], finish_reason='stop')

    s = Session(id='ov')
    def user(t): return create_user_message([TextBlock(text=t)])
    def asst(t): return {'message': create_assistant_message([TextBlock(text=t)])}
    s.append('turn/start', {'turn': 1})
    s.append('user/message', user('历史任务内容' * 30), surface_op='append')
    s.append('assistant/message', asst('历史回答' * 30), surface_op='append')
    s.append('turn/end', {'turn': 1, 'reason': 'completed'})
    s.append('turn/start', {'turn': 2})
    s.append('user/message', user('继续'), surface_op='append')

    args = Namespace(fake=False, model='m', workspace=tmp_path, hide_reasoning=False,
                     session='ov', sessions=str(tmp_path), prompt='', resume=False, verbose=False)
    agent = build_agent(s, args, {'reasoning_started': False, 'request_no': 0, 'tool_no': 0})
    agent.llm = OverflowLlm()
    agent.followup('继续')
    await agent.when_idle()

    # 溢出触发压缩（四事件）+ retry 成功
    assert any(e.type == 'compaction/summary' for e in s.events)
    assert any(e.type == 'compaction/end' for e in s.events)
    assert s.events[-1].data['reason'] == 'completed'
    texts = [m.content[0].text for m in s.derive_messages() if m.content and m.content[0].type == 'text']
    assert any('<compacted-summary>' in t for t in texts)
    assert texts[-1] == '压缩后重试成功'
