"""web_search：结构化块解析、摘要合并、后端请求形状与失败降级。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**断言与用例体一字未改**；
唯一差异是 26 处函数内冗余 import 被 ruff 的 F401/F811 删掉——那是拆分暴露出来的旧问题。）
"""
from __future__ import annotations

import json

import httpx
import pytest

from agent_demo.registry import ToolRegistry
from agent_demo.session import Session

# ============================================================
# web_search —— DeepSeek 官方原生搜索（Anthropic 兼容 Messages 端点）
#
# 搜索由服务端 `web_search_20250305` 工具执行，我们只解析结构化块
# （web_search_tool_result → web_search_result），绝不抓网页、绝不去
# text 正文里抠 URL。夹具剪裁自真实响应（page_age 实测为 null）。
# ============================================================

# 夹具：含重复 URL（验证去重）、空 title（验证 label 退化到 hostname）、
# 一条非 web_search_result 项（验证被跳过）。
_WEB_SEARCH_FIXTURE = {
    'type': 'message',
    'model': 'deepseek-v4-flash',
    'stop_reason': 'end_turn',
    'content': [
        {'type': 'thinking', 'thinking': '先搜一下', 'signature': 'sig'},
        {'type': 'server_tool_use', 'id': 'call_1', 'name': 'web_search',
         'input': {'query': 'deepseek-harness 架构'}},
        {'type': 'web_search_tool_result', 'tool_use_id': 'call_1', 'content': [
            {'type': 'web_search_result', 'title': 'Harness 架构（中文）',
             'url': 'https://example.com/a', 'page_age': None, 'encrypted_content': 'xxx'},
            {'type': 'web_search_result', 'title': '',
             'url': 'https://example.com/b', 'page_age': None, 'encrypted_content': 'yyy'},
            {'type': 'web_search_result', 'title': '重复 URL 应被丢弃',
             'url': 'https://example.com/a', 'page_age': None},
            {'type': 'web_search_result', 'title': '空 url 应被丢弃',
             'url': '', 'page_age': None},
        ]},
        {'type': 'text', 'text': '以下是整理后的答复，参考 [架构文档](https://example.com/a)。'},
    ],
    'usage': {'server_tool_use': {'web_search_requests': 1}},
}


def _web_search_backend(monkey_env='test-key'):
    """造一个 httpx.MockTransport 后端（喂夹具，记录请求体），不碰网络。"""
    from agent_demo.tools import web_search as ws

    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append({
            'url': str(request.url),
            'headers': dict(request.headers),
            'body': json.loads(request.content),
        })
        return httpx.Response(200, json=_WEB_SEARCH_FIXTURE)

    async def backend(query: str, max_results: int, config):
        return await ws.deepseek_search_backend(
            query, max_results, config,
            transport=httpx.MockTransport(handler), api_key=monkey_env,
        )

    return backend, requests


def test_web_search_parses_structured_blocks_only():
    """只认结构化块：去重 / 空 url 丢弃 / title 缺失 / page_age=null 都不炸。"""
    from agent_demo.tools import web_search as ws

    outcome = ws.parse_search_response(_WEB_SEARCH_FIXTURE)
    results = list(outcome.results)
    assert [r.url for r in results] == ['https://example.com/a', 'https://example.com/b']
    assert results[0].title == 'Harness 架构（中文）'
    assert results[1].title == ''            # 缺 title 不是错误
    assert results[0].published_at == ''     # page_age 实测为 null → 空
    assert results[0].snippet == ''          # 实测 text 块没有 citations → snippet 空
    # 摘要来自 text 块（那个辅助模型的转述）
    assert outcome.summary == '以下是整理后的答复，参考 [架构文档](https://example.com/a)。'

    # 摘要必须带"转述、无结构化引用"的标注，且排在 Sources 之前
    output = ws.format_search_output(results, summary=outcome.summary)
    assert output.startswith(ws.EXTERNAL_WEB_CONTENT_NOTICE)
    assert ws.SUMMARY_NOTICE in output
    assert output.index(ws.SUMMARY_NOTICE) < output.index('Sources:')
    assert '- [Harness 架构（中文）](https://example.com/a)' in output
    assert '- [example.com](https://example.com/b)' in output
    assert output.endswith(ws.CITE_INSTRUCTION)
    assert 'No results found.' not in output

    # 没来源但有摘要：不打 "No results found."（否则自相矛盾）
    only_summary = ws.format_search_output([], summary='一段转述')
    assert 'No results found.' not in only_summary and ws.SUMMARY_NOTICE in only_summary

    # 两者都没有：才说"没找到"
    empty = ws.format_search_output([])
    assert 'No results found.' in empty and 'Sources:' not in empty
    assert ws.SUMMARY_NOTICE not in empty


def test_web_search_summary_merge_and_truncation():
    """多 query 的摘要分段拼接（>1 条才加 ### 标题）、超长截断、可整体关掉。"""
    from agent_demo.tools import web_search as ws

    def outcome(summary: str, url: str) -> ws.SearchOutcome:
        return ws.SearchOutcome(
            summary=summary,
            results=(ws.SearchResult(title='T', url=url),),
        )

    # 单 query：不加多余的 ### 标题
    summary, results, truncated = ws.merge_outcomes(
        ['q1'], [outcome('答话一', 'https://a.test')], 5)
    assert summary == '答话一'
    assert [r.url for r in results] == ['https://a.test'] and truncated is False

    # 多 query：按 DSH 的分段形状，各自带 query 标题
    summary, results, _ = ws.merge_outcomes(
        ['q1', 'q2'],
        [outcome('答话一', 'https://a.test'),
         outcome('答话二', 'https://a.test'),    # 重复 URL → 只留一条
         ],
        5)
    assert summary == '### q1\n\n答话一\n\n### q2\n\n答话二'
    assert [r.url for r in results] == ['https://a.test']

    # 超长截断 + 标记
    long_summary, _, _ = ws.merge_outcomes(['q'], [outcome('x' * 100, 'https://a.test')], 5,
                                           max_summary_chars=40)
    assert long_summary.startswith('x' * 40)
    assert 'Summary truncated at 40 chars.' in long_summary

    # 某条 query 没写答话（text 块缺失）→ 那一段跳过，不产生空标题
    summary, _, _ = ws.merge_outcomes(
        ['q1', 'q2'], [outcome('', 'https://a.test'), outcome('只有二', 'https://b.test')], 5)
    assert summary == '### q2\n\n只有二'


def test_web_search_citation_snippet_when_present():
    """text 块的 citations 提供 snippet（DSH 的 citationSnippets 语义；首次出现者胜）。

    注意：这是**防御性**覆盖——DeepSeek 实测从不返回 citations（探针两次确认，
    连 system 里明确要求标注来源也没有），但协议支持，所以解析层照 DSH 实现。
    """
    from agent_demo.tools import web_search as ws

    payload = {
        'content': [
            {'type': 'web_search_tool_result', 'content': [
                {'type': 'web_search_result', 'title': 'T', 'url': 'https://x.test/1', 'page_age': '2026-08-13'},
            ]},
            {'type': 'text', 'text': '正文', 'citations': [
                {'url': 'https://x.test/1', 'cited_text': '第一次的摘录'},
                {'url': 'https://x.test/1', 'cited_text': '应被忽略'},
            ]},
        ],
    }
    outcome = ws.parse_search_response(payload)
    assert outcome.results[0].snippet == '第一次的摘录'
    assert outcome.results[0].published_at == '2026-08-13'
    assert outcome.summary == '正文'
    assert '(2026-08-13)' in ws.format_search_output(list(outcome.results))


def test_web_search_no_result_block_is_error_not_empty():
    """没触发原生搜索 → 响亮失败（WEB_PROVIDER_ERROR），不退化成"没找到"。"""
    from agent_demo.tools import web_search as ws

    for payload in (
        {'content': [{'type': 'text', 'text': '我直接回答了，没搜索'}]},
        {'content': []},
        {},
    ):
        with pytest.raises(ws.WebSearchError) as excinfo:
            ws.parse_search_response(payload)
        assert excinfo.value.code == 'WEB_PROVIDER_ERROR'
        assert 'web_search_tool_result' in excinfo.value.message


@pytest.mark.asyncio
async def test_web_search_backend_request_shape_and_failures(monkeypatch):
    """默认后端：请求体/头照 DSH；缺 key / HTTP 非 200 / 响应不可解析都结构化失败。"""
    from agent_demo.tools import web_search as ws

    backend, requests = _web_search_backend()
    outcome = await backend('deepseek-harness 架构', 3, ws.default_config())
    # 夹具里只有 a / b 是唯一且非空的 URL（重复项与空 url 被丢弃）
    assert [r.url for r in outcome.results] == ['https://example.com/a', 'https://example.com/b']
    assert outcome.summary.startswith('以下是整理后的答复')

    sent = requests[0]
    assert sent['url'] == 'https://api.deepseek.com/anthropic/v1/messages'
    assert sent['headers']['x-api-key'] == 'test-key'
    assert sent['headers']['anthropic-version'] == '2023-06-01'
    assert sent['body']['model'] == 'deepseek-v4-flash'
    assert sent['body']['max_tokens'] == 4096
    assert sent['body']['tools'] == [
        {'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 5}]
    assert sent['body']['messages'][0]['content'][0]['text'] == (
        'Perform a web search for the query: deepseek-harness 架构')

    # 缺 key → WEB_PROVIDER_CREDENTIAL_MISSING（不抛穿，交给包装层降级）
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    with pytest.raises(ws.WebSearchError) as excinfo:
        await ws.deepseek_search_backend('q', 3, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert excinfo.value.code == 'WEB_PROVIDER_CREDENTIAL_MISSING'

    # HTTP 非 200 → 带上状态码与 detail
    def http_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={'error': {'message': 'rate limited'}})

    with pytest.raises(ws.WebSearchError) as excinfo:
        await ws.deepseek_search_backend('q', 3, transport=httpx.MockTransport(http_error), api_key='k')
    assert excinfo.value.code == 'WEB_PROVIDER_HTTP_ERROR'
    assert '429' in excinfo.value.message and 'rate limited' in excinfo.value.message

    # 响应体不是 JSON → WEB_PROVIDER_ERROR（不炸）
    with pytest.raises(ws.WebSearchError) as excinfo:
        await ws.deepseek_search_backend(
            'q', 3, transport=httpx.MockTransport(lambda r: httpx.Response(200, text='not json')), api_key='k')
    assert excinfo.value.code == 'WEB_PROVIDER_ERROR'


def test_web_search_query_validation():
    """queries 校验：空数组 / 超限 / 空白项 → is_error；重复项折叠。"""
    from agent_demo.tools import web_search as ws

    assert ws.parse_query_args(['a', ' b ', 'a'], 4) == ['a', 'b']   # 折叠 + strip
    for bad in ([], ['  '], ['a'] * 5, 'a', [1]):
        with pytest.raises(ws.WebSearchError) as excinfo:
            ws.parse_query_args(bad, 4)
        assert excinfo.value.code == 'INVALID_QUERIES'


@pytest.mark.asyncio
async def test_web_search_tool_multi_query_merge_and_trace(tmp_path):
    """工具层：多 query 逐条搜索、按 url 去重合并、派发前落 web/search 痕迹（无 key）。

    DSH 的 mergeSearchResults 是 round-robin；这里按 query 顺序拼接（先到先得），
    超上限截断。痕迹事件对齐 DSH 的 web/deepseek-search-llm-request：
    记 query/endpoint/model/max_uses，**绝不含 key**。
    """
    from agent_demo.tools import web_search as ws

    backend, requests = _web_search_backend()
    registry = ToolRegistry()
    ws.register(registry, backend=backend, max_results=3)
    spec = registry._tools['web_search']

    session = Session(id='web-search-test')
    agent = type('A', (), {'session': session})()

    first = await spec.execute({'queries': ['q1', 'q2']}, agent, None)
    assert first.is_error is False
    # 两条 query 各发一次请求；结果按 url 去重（夹具里 a 重复）→ 只剩 a/b，未到上限
    assert len(requests) == 2
    assert first.content.count('- [') == 2
    assert 'https://example.com/a' in first.content and 'https://example.com/b' in first.content

    # 痕迹事件：派发前落、不含 key
    traces = [e for e in session.events if e.type == 'web/search']
    assert [t.data['query'] for t in traces] == ['q1', 'q2']
    assert traces[0].data['endpoint'] == 'https://api.deepseek.com/anthropic/v1/messages'
    assert traces[0].data['model'] == 'deepseek-v4-flash'
    assert traces[0].data['max_uses'] == 5
    assert 'key' not in json.dumps(traces[0].data, ensure_ascii=False).lower()
    # 痕迹不是 surface：不进模型记忆（不变式②）
    assert session.derive_messages() == []
    # 请求顺序：痕迹先落，再发请求（派发前记账）
    assert session.events.index(traces[0]) < session.events.index(traces[1])


@pytest.mark.asyncio
async def test_web_search_trace_matches_the_real_request(tmp_path):
    """痕迹事件记的必须是**那次真实请求**用的配置（回归：两份默认值会脱节）。

    旧实现的 endpoint/model/max_uses 来自 register() 的闭包参数，而真正发请求的
    后端用自己的默认值——于是 ① 日志可能记一个端点、请求打到另一个；
    ② register(endpoint=...) 这类覆盖对真实请求**完全无效**（静默失效）。
    现在配置是一个值对象，由工具层解析一次、trace 与请求共用。
    """
    from agent_demo.tools import web_search as ws

    backend, requests = _web_search_backend()
    custom = ws.SearchConfig(endpoint='http://mock.local/v1/messages',
                             model='custom-search-model', max_uses=2)
    registry = ToolRegistry()
    ws.register(registry, backend=backend, max_results=3, config=custom)

    session = Session(id='web-search-config')
    agent = type('A', (), {'session': session})()
    outcome = await registry.execute('web_search', {'queries': ['q']}, agent)
    assert outcome.is_error is False

    trace = next(e for e in session.events if e.type == 'web/search')
    assert trace.data == {
        'query': 'q',
        'endpoint': 'http://mock.local/v1/messages',
        'model': 'custom-search-model',
        'max_uses': 2,
    }
    # 真实请求与痕迹逐字段一致（这才是"日志 = 请求"）
    sent = requests[0]
    assert sent['url'] == trace.data['endpoint']
    assert sent['body']['model'] == trace.data['model']
    assert sent['body']['tools'][0]['max_uses'] == trace.data['max_uses']

    # include_summary=False：模型可见文本里不留那段转述（将来有 web_fetch 时退回
    # DSH 的 deepseek 策略），但结构化来源照旧
    no_summary = ws.SearchConfig(endpoint=custom.endpoint, model=custom.model,
                                 max_uses=custom.max_uses, include_summary=False)
    registry2 = ToolRegistry()
    ws.register(registry2, backend=_web_search_backend()[0], max_results=3, config=no_summary)
    plain = await registry2.execute('web_search', {'queries': ['q']}, agent)
    assert plain.is_error is False
    assert ws.SUMMARY_NOTICE not in plain.content
    assert 'Sources:' in plain.content


@pytest.mark.asyncio
async def test_web_search_tool_degrades_to_is_error(tmp_path):
    """工具包装层：坏入参 / 后端失败都返回 is_error 的 ToolOutcome，绝不抛异常。"""
    from agent_demo.tools import web_search as ws

    # 坏参数：空 queries（schema 外的话直接拒绝）
    registry = ToolRegistry()
    ws.register(registry, backend=_web_search_backend()[0])
    spec = registry._tools['web_search']
    session = Session(id='degrade')
    agent = type('A', (), {'session': session})()

    out = await spec.execute({'queries': []}, agent, None)
    assert out.is_error is True and 'at least one query' in out.content

    out = await spec.execute({'queries': ['a', 'b', 'c', 'd', 'e']}, agent, None)
    assert out.is_error is True and 'at most 4' in out.content

    out = await spec.execute({'queries': ['ok', '   ']}, agent, None)
    assert out.is_error is True

    # 后端结构化失败（缺 key）→ is_error，且带上 code 供模型/诊断识别
    async def no_key_backend(query, max_results, config):
        raise ws.WebSearchError('WEB_PROVIDER_CREDENTIAL_MISSING', 'no key')

    registry2 = ToolRegistry()
    ws.register(registry2, backend=no_key_backend)
    out = await registry2._tools['web_search'].execute({'queries': ['q']}, None, None)
    assert out.is_error is True and 'WEB_PROVIDER_CREDENTIAL_MISSING' in out.content

    # 后端抛别的异常也不穿：一律降级为 is_error 结果（不变式⑤）
    async def boom(query, max_results, config):
        raise RuntimeError('boom')

    registry3 = ToolRegistry()
    ws.register(registry3, backend=boom)
    out = await registry3._tools['web_search'].execute({'queries': ['q']}, None, None)
    assert out.is_error is True and 'boom' in out.content
