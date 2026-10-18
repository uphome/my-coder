"""压缩事务的三条判据（review 后补）：顺序、失败方向、截断诚实性；外加并发互斥。

现场：
- `compaction/summary` 原来**先于** `user/message(replace)` 落盘——两步之间失败会留下
  "摘要存在、却没生效"的记录（审计/统计会把它算进"已压缩"）；
- 超长消息只取前 2000 字符进摘要请求，但**没告诉模型被截断了** ⇒ 摘要会缺中段/尾部；
- 两个标签页同时点 `/compact` 没有互斥 ⇒ 各自选段、各自 replace，可能 500 或顶替错段。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

from my_coder.app.compaction import (
    COMPACTION_END,
    COMPACTION_SUMMARY,
    run_compaction,
    select_compact_range,
)
from my_coder.state.session import Session
from my_coder.values.messages import (
    TextBlock,
    create_assistant_message,
    create_user_message,
)


class _FakeLlm:
    """最小的流式 llm：记录请求，吐固定摘要。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[object] = []

    async def stream(self, request):
        self.requests.append(request)
        yield SimpleNamespace(text=self.text, finish_reason='stop')


def _session(turns: int = 3, filler: str = 'x' * 120) -> Session:
    session = Session(id='c')
    for turn in range(1, turns + 1):
        session.append('turn/start', {'turn': turn})
        session.append('user/message',
                       create_user_message([TextBlock(text=f'第{turn}问 {filler}')]),
                       surface_op='append')
        session.append('assistant/message', {
            'turn': turn, 'step': 1,
            'message': create_assistant_message([TextBlock(text=f'第{turn}答 {filler}')]),
        }, surface_op='append')
        session.append('turn/end', {'turn': turn, 'reason': 'done'})
    return session


def test_summary_event_lands_after_the_replace():
    """顺序判据：生效的那一步（replace）必须先落，审计（summary）在后。"""
    session = _session()
    assert select_compact_range(session, keep_turns=1) is not None
    ok = asyncio.run(run_compaction(session, _FakeLlm('很短很短的摘要'), keep_turns=1))
    assert ok, '这次压缩应当成功'

    types = [event.type for event in session.events]
    replace_at = next(index for index, event in enumerate(session.events)
                      if event.type == 'user/message' and event.surface_op == 'replace')
    summary_at = types.index(COMPACTION_SUMMARY)
    assert summary_at > replace_at, ('summary 必须先于 replace 落盘吗？不——先 replace 再 summary，'
                                     '否则"摘要存在却没生效"会把审计带偏')


def test_failed_replace_leaves_no_summary_record():
    """失败方向良性：replace 失败时，日志里**不许有** compaction/summary。"""
    session = _session()
    real_append = session.append

    def boom(type_, data=None, **kwargs):
        if kwargs.get('surface_op') == 'replace':
            raise RuntimeError('disk full')
        return real_append(type_, data, **kwargs)

    session.append = boom            # type: ignore[method-assign]
    ok = asyncio.run(run_compaction(session, _FakeLlm('摘要'), keep_turns=1))
    assert ok is False

    types = [event.type for event in session.events]
    assert COMPACTION_SUMMARY not in types, 'replace 都失败了，却留下了摘要审计记录'
    assert COMPACTION_END in types, '失败必须落 compaction/end（配对完整性）'
    error_end = [event for event in session.events if event.type == COMPACTION_END][-1]
    assert 'disk full' in str(error_end.data.get('error', ''))


def test_long_message_is_marked_as_truncated_in_the_summary_request():
    """截断必须**说出来**：模型得知道它没看到全文。

    注意长消息要放进**被压的那段**（keep_turns=1 会保留最后一回合）——否则它根本不在
    摘要请求里，这条测试就测了个寂寞。
    """
    session = _session(turns=2, filler='长' * 2500)

    llm = _FakeLlm('短摘要')
    ok = asyncio.run(run_compaction(session, llm, keep_turns=1))
    assert ok
    sent = llm.requests[0].messages[0].content[0].text
    assert '此处截断' in sent, f'超长消息进摘要请求时没有截断标注：{sent[:120]!r}'
    assert '原文共' in sent


def test_compaction_mutex_is_exclusive_per_session():
    """并发互斥：同一会话同一时刻只允许一个手动压缩。"""
    app_module = importlib.import_module('my_coder.web.app')
    assert app_module._try_begin_compaction('sid-a') is True
    assert app_module._try_begin_compaction('sid-a') is False, '同一会话不许并发压缩'
    assert app_module._try_begin_compaction('sid-b') is True, '不同会话必须互不影响'
    app_module._end_compaction('sid-a')
    assert app_module._try_begin_compaction('sid-a') is True, '结束之后应能再开'
    app_module._end_compaction('sid-a')
    app_module._end_compaction('sid-b')
