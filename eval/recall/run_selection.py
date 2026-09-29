"""选择评测：把 L1 清单 + 一道导航题交给模型，要求它给出回合号。

主指标 = **top-1 / top-3 选择准确率**（不是检索器 hit@3）。
同时跑一条**词元重叠基线**做对照：如果基线就有 90%，说明题太送分、测不出模型的价值。

成本：每题 1 次调用 + 一份小清单（≈2~4k token）。

用法：`python eval/recall/run_selection.py`
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm_util import ask, tokens  # noqa: E402
from manifest import SESSIONS, build_turns, load_session, render_manifest  # noqa: E402

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / 'navigation.jsonl'

SELECT_SYSTEM = """你在扮演一个编码 agent。

下面是你会话的「用户消息清单」：每一行是一个回合的**结论摘录 + 足迹**
（步数 / 工具 / 涉及文件 / 结局），下面缩进的一行是该回合的**用户原话**。
清单之后是你的一个问题——回答它需要回到历史里读某一回合的原文。

请只输出**回合号**：最多 3 个候选，按可能性从高到低，每行一个纯数字。
不要解释、不要输出别的字符。如果拿不准，也要给出你认为最可能的候选。"""


def parse_turns(raw: str) -> list[int]:
    return [int(n) for n in re.findall(r'\b(\d{1,4})\b', raw)][:3]


def lexical_baseline(query: str, turns) -> list[int]:
    """词元重叠基线：按 query 与该回合清单文本的重叠数排序。"""
    query_tokens = tokens(query)
    scored = []
    for info in turns:
        text = ' '.join(info.user_texts) + ' ' + info.footprint() + ' ' + info.conclusion
        scored.append((len(query_tokens & tokens(text)), info.turn))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [turn for score, turn in scored if score > 0][:3]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0, help='只跑前 N 题（0=全部）')
    args = parser.parse_args()

    items = [json.loads(line) for line in QUESTIONS.read_text(encoding='utf-8').splitlines() if line.strip()]
    if args.limit:
        items = items[:args.limit]

    cache: dict[str, tuple[str, list]] = {}
    rows = []
    for index, item in enumerate(items, start=1):
        name = item['session']
        if name not in cache:
            path = next(p for p in SESSIONS.glob('*.jsonl') if p.stem == name)
            session = load_session(path)
            turns = build_turns(session)
            cache[name] = (render_manifest(turns), turns)
        manifest, turns = cache[name]

        raw = ask(SELECT_SYSTEM, f'{manifest}\n\n问题：{item["query"]}', max_tokens=60)
        picked = parse_turns(raw)
        base = lexical_baseline(item['query'], turns)
        gold = item['answer_turn']
        rows.append({
            'id': item['id'], 'difficulty': item['difficulty'], 'gold': gold,
            'picked': picked, 'baseline': base,
            'hit1': picked[:1] == [gold], 'hit3': gold in picked,
            'near': bool(picked) and abs(picked[0] - gold) <= 1,
            'base_hit1': base[:1] == [gold], 'base_hit3': gold in base,
            'base_near': bool(base) and abs(base[0] - gold) <= 1,
            'query': item['query'],
        })
        mark = '✓' if rows[-1]['hit1'] else ('~' if rows[-1]['near'] else '✗')
        print(f'[{index}/{len(items)}] {mark} {item["difficulty"]} gold={gold} '
              f'picked={picked} base={base} :: {item["query"][:40]}', flush=True)

    total = len(rows)
    if not total:
        print('没有题目：先跑 build_navigation.py')
        return

    def rate(key: str, subset: list[dict]) -> str:
        if not subset:
            return '—'
        return f'{sum(1 for r in subset if r[key]) / len(subset):.0%}'

    print('\n=== 结果（top-1 精确 / ±1 回合 / top-3 命中）===')
    for label, subset in [
        ('全部', rows),
        ('T1 撞词', [r for r in rows if r['difficulty'] == 'T1']),
        ('T2 桥接', [r for r in rows if r['difficulty'] == 'T2']),
    ]:
        print(f'{label:8} n={len(subset):<3} '
              f'LLM {rate("hit1", subset):>4} / {rate("near", subset):>4} / {rate("hit3", subset):>4}'
              f'   |   基线 {rate("base_hit1", subset):>4} / {rate("base_near", subset):>4} / '
              f'{rate("base_hit3", subset):>4}')
    sizes = {i['session']: i['manifest_chars'] for i in items}
    print('\n清单大小（每会话）：' + ' · '.join(f'{k} {v:,} 字符' for k, v in sizes.items()))


if __name__ == '__main__':
    main()
