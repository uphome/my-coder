"""应用层工具：web_search——联网搜索（唯一"读工作区之外"的工具）。

设计取舍：
- 网络是"外部"，但工具哲学不变——**失败降级为结果**：网络错误/超时/解析不到
  结果都变成一条 is_error 或提示性 ToolOutcome，绝不炸循环（不变式 5）。
- 它是只读工具（不需要 approval，对齐 grep/read_file），parallel 可并发。
- 后端可注入：`register(registry, backend=...)` 的 backend 是
  `(query, max_results) -> list[SearchResult]` 的异步函数。默认走
  `http_search_backend`（DuckDuckGo HTML 端点，无需 API key）；测试注入假后端
  即可完全离线，不碰网络。
"""
from __future__ import annotations

import html
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from ..constants import WEB_SEARCH_ENDPOINT, WEB_SEARCH_MAX_RESULTS, WEB_SEARCH_TIMEOUT_S
from ..registry import ToolOutcome, ToolSpec


@dataclass(frozen=True)
class SearchResult:
    """一条搜索结果（值对象：frozen + 纯文本字段，便于格式化与测试）。"""
    title: str
    url: str
    snippet: str = ''


# 后端签名：query + 上限 → 结果列表。可注入，也是单元测试的接缝。
SearchBackend = Callable[[str, int], Awaitable[list[SearchResult]]]


# DuckDuckGo HTML 页面里的结果锚点（class 与 href 的先后不固定，统一用宽松匹配）
_ANCHOR_RE = re.compile(r'<a\b([^>]*)>(.*?)</a>', re.S | re.I)
_HREF_RE = re.compile(r'href="([^"]*)"', re.I)
_TAG_RE = re.compile(r'<[^>]+>')


def _clean_text(raw: str) -> str:
    """去标签 + 反转义 + 压空白：把 DDG 的 `<b>关键词</b>` 还原成纯文本。"""
    return re.sub(r'\s+', ' ', unescape(_TAG_RE.sub('', raw))).strip()


def _clean_url(url: str) -> str:
    """还原 DDG 的跳转链接：//duckduckgo.com/l/?uddg=<真实地址> → 真实地址。"""
    if not url:
        return ''
    if url.startswith('//'):
        url = 'https:' + url
    if 'duckduckgo.com/l/' in url or 'uddg=' in url:
        target = parse_qs(urlparse(url).query).get('uddg')
        if target:
            return unquote(target[0])
    return html.unescape(url)


def parse_results(body: str, max_results: int) -> list[SearchResult]:
    """纯函数：把 DDG HTML 页面解析成结果列表（可单测，不碰网络）。

    页面结构里"标题锚点"与"摘要锚点"各自按出现顺序成对——第 n 个标题
    对应第 n 个摘要，据此配对；只有标题没有摘要也能出一条结果。
    """
    titles: list[tuple[str, str]] = []
    snippets: list[str] = []
    for match in _ANCHOR_RE.finditer(body):
        attrs, inner = match.group(1), match.group(2)
        href_match = _HREF_RE.search(attrs)
        href = _clean_url(href_match.group(1)) if href_match else ''
        if 'result__a' in attrs:
            title = _clean_text(inner)
            if title and href:
                titles.append((title, href))
        elif 'result__snippet' in attrs:
            snippets.append(_clean_text(inner))
    results = []
    for index, (title, url) in enumerate(titles[:max_results]):
        snippet = snippets[index] if index < len(snippets) else ''
        results.append(SearchResult(title=title, url=url, snippet=snippet))
    return results


async def http_search_backend(query: str, max_results: int) -> list[SearchResult]:
    """默认后端：POST DuckDuckGo HTML 端点，无需 API key。

    抛出 httpx.HTTPError 交给上层降级为结果（工具层不吞异常，语义留给 executor）。
    """
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(WEB_SEARCH_TIMEOUT_S),
        headers={'User-Agent': 'Mozilla/5.0 (compatible; agent-demo)'},
        follow_redirects=True,
    ) as client:
        response = await client.post(WEB_SEARCH_ENDPOINT, data={'q': query})
        response.raise_for_status()
        return parse_results(response.text, max_results)


def format_results(query: str, results: list[SearchResult]) -> str:
    """结果 → 模型友好的纯文本（编号 + 标题 + URL + 摘要）。无结果返回提示句。"""
    if not results:
        return f'(no results for "{query}")'
    blocks = []
    for index, item in enumerate(results, 1):
        lines = [f'{index}. {item.title}', f'   {item.url}']
        if item.snippet:
            lines.append(f'   {item.snippet}')
        blocks.append('\n'.join(lines))
    header = f'Found {len(results)} results for "{query}":'
    return header + '\n\n' + '\n\n'.join(blocks)


def register(
    registry,
    backend: SearchBackend | None = None,
    max_results: int = WEB_SEARCH_MAX_RESULTS,
) -> None:
    """注册 web_search——backend 可注入（缺省走真实网络）。"""
    search_backend = backend or http_search_backend

    async def web_search(args, agent, signal):
        query = str(args['query']).strip()
        if not query:
            return ToolOutcome(content='query must not be empty', is_error=True)
        # 上限钳制：模型可以要少一点，但不能要到爆上下文（预算不进 schema）
        try:
            wanted = int(args.get('max_results', max_results))
        except (TypeError, ValueError):
            return ToolOutcome(content='max_results must be an integer', is_error=True)
        wanted = max(1, min(wanted, max_results))
        try:
            results = await search_backend(query, wanted)
        except httpx.HTTPError as error:
            # 网络故障是"结果"不是崩溃：模型知道原因后可换关键词/改走 bash
            return ToolOutcome(content=f'web_search failed: {error}', is_error=True)
        return ToolOutcome(content=format_results(query, results))

    registry.register(ToolSpec(
        name='web_search',
        description=(
            'Search the public web and return ranked results (title, URL, snippet). '
            'Use it for up-to-date facts, docs, or anything outside the workspace.'
        ),
        parameters={
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'Search query.'},
                'max_results': {'type': 'integer', 'description': f'Max results to return (1-{max_results}), default {max_results}.'},
            },
            'required': ['query'],
        },
        execute=web_search,
        timeout_s=WEB_SEARCH_TIMEOUT_S + 5.0,
    ))
