"""eval 用的 LLM 小工具：复用产品的 OpenAI 兼容客户端，不另造一份。

为什么复用：wire 格式、超时、错误分类都在 `my_coder/capability/llm.py` 里，
自己拿 httpx 再写一份，迟早和产品行为漂移（评测就失真了）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from my_coder.app.factory import load_env                            # noqa: E402
from my_coder.capability.llm import LlmRequest, OpenAiCompatibleLlm  # noqa: E402
from my_coder.values.messages import TextBlock, create_user_message  # noqa: E402

DEFAULT_MODEL = 'deepseek-v4-flash'
_engine: OpenAiCompatibleLlm | None = None
_model = ''


def _get_engine() -> tuple[OpenAiCompatibleLlm, str]:
    global _engine, _model
    if _engine is None:
        load_env(REPO / '.env')
        api_key = os.environ.get('DEEPSEEK_API_KEY')
        if not api_key:
            raise SystemExit('missing DEEPSEEK_API_KEY (put it in .env)')
        _model = os.environ.get('DEEPSEEK_MODEL') or DEFAULT_MODEL
        _engine = OpenAiCompatibleLlm(
            base_url=os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com'),
            api_key=api_key, model=_model, provider='deepseek',
        )
        print(f'[llm] model={_model}', flush=True)
    return _engine, _model


async def _stream(system: str, user: str, max_tokens: int, thinking: bool | None) -> str:
    engine, model = _get_engine()
    request = LlmRequest(
        provider='deepseek', model=model, system=system,
        messages=(create_user_message([TextBlock(text=user)]),),
        max_tokens=max_tokens,
        # 评测这类"短、结构化"的请求显式关掉思维链：v4 默认开，思维链会把
        # max_tokens 吃光 → content 为空（compaction.py 里记过同一个坑）
        thinking=thinking,
    )
    collected: list[str] = []
    async for chunk in engine.stream(request):
        if chunk.text:
            collected.append(chunk.text)
        if chunk.finish_reason:
            break
    return ''.join(collected).strip()


def ask(system: str, user: str, *, max_tokens: int = 600,
        thinking: bool | None = False) -> str:
    """一次同步提问（脚本用；每次独立 asyncio.run，够用且简单）。"""
    return asyncio.run(_stream(system, user, max_tokens, thinking))


def ask_json(system: str, user: str, *, max_tokens: int = 600, thinking: bool | None = False) -> dict | None:
    """要求模型输出 JSON 的场景：宽松解析（抠第一个 {...}）。"""
    raw = ask(system, user, max_tokens=max_tokens, thinking=thinking)
    match = re.search(r'\{.*\}', raw, re.S)
    if not match:
        print(f'    [ask_json] 无 JSON，原始输出前 200 字：{raw[:200]!r}')
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as error:
        print(f'    [ask_json] JSON 解析失败（{error}）：{match.group(0)[:200]!r}')
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------- 轻量词元化（中文 bigram + 英文词；评测校验/基线用） ----------

STOPWORDS = frozenset({
    'the', 'and', 'for', 'with', 'that', 'this', 'from', 'was', 'were', 'are', 'you',
    '我们', '你们', '这个', '那个', '什么', '怎么', '哪一', '一下', '现在', '可以',
    '就是', '还是', '因为', '所以', '但是', '如果', '然后', '已经', '一个',
})


def tokens(text: str) -> set[str]:
    out: set[str] = set()
    for word in re.findall(r'[A-Za-z_][A-Za-z0-9_.]{2,}', text.lower()):
        if word not in STOPWORDS:
            out.add(word)
    for run in re.findall(r'[\u4e00-\u9fff]{2,}', text):
        for i in range(len(run) - 1):
            bigram = run[i:i + 2]
            if bigram not in STOPWORDS:
                out.add(bigram)
        if len(run) <= 4:
            out.add(run)
    return out
