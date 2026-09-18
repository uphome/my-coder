"""导航数据集生成器：让 LLM 从"agent 侧的工作内容"写出导航题。

题目形状（用户定的）：**答案藏在某一回合里，模型看清单后给出回合号。**

```json
{"query": "当时我们是怎么确认 checkpoint 指针要带区间的？",
 "session": "web-1788604766", "answer_turn": 6,
 "difficulty": "T2", "answer_hint": "…", "overlap": []}
```

生成 + 三道自动校验：

1. **不与清单撞词**：query 的词元若出现在清单行里，导航退化成字符串匹配 →
   不丢弃，而是按实测重叠率打 **T1（撞词）/ T2（桥接）** 标签；
2. **回合唯一**：模型给的 `answer_hint` 必须只在该回合原文里出现（否则金标歧义 → 弃题）；
3. **干扰足够**：同会话至少还有 K 个其它回合。

用法：`python eval/recall/build_navigation.py [--per-session 12]`
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm_util import ask_json, tokens                                    # noqa: E402
from manifest import (SESSIONS, TurnInfo, build_turns, load_session,     # noqa: E402
                      render_manifest, render_turn)

OUT = Path(__file__).resolve().parent / 'navigation.jsonl'
MIN_OTHER_TURNS = 3

GEN_SYSTEM = """你在为一个编码 agent 的「历史召回」评测造题。

背景：agent 的上下文被压缩过，它只能看到一段摘要；但如果它需要某个细节，它可以先看
「会话的用户消息清单」（每行 = 一个回合的结论摘录 + 足迹 + 用户原话），再决定回哪一回合读原文。
**你要造的题，就是让它必须回某一回合才能回答的问题。**

我会给你：
- 目标回合的原文；
- **这一回合独有的短语**（其它回合都没有）——这是造题的锚点；
- 会话清单全文（模型导航时唯一能看到的线索）。

硬性要求：
1) 从"独有短语"里**挑一个**当主题，query 里必须**逐字包含**它；
2) query 是一句自然的中文提问（像一个 agent 或真人在问），≤40 字；
3) **不要**问"你能用哪些工具/你是谁/当前状态"这类不需要历史的问题；
4) **不要**用"上一轮 / 那次 / 刚才 / 这轮"这类指代；
5) 尽量不额外复用清单里的词（清单是唯一线索，复用等于送分）。

只输出 JSON：{"query": "...", "anchor": "你挑的那个独有短语"}"""


def phrase_candidates(text: str) -> list[str]:
    """可当主题的短语：英文标识符/路径、3 位以上数字、4~12 字的中文片段。"""
    found = re.findall(
        r'[A-Za-z_][A-Za-z0-9_.\-]{3,}|[\u4e00-\u9fff]{4,12}|\d{3,}', text)
    return [f for f in dict.fromkeys(found) if len(f) >= 3]


def rare_phrases(per_turn_text: dict[int, str], turn: int, limit: int = 12) -> list[str]:
    """只在该回合出现的短语（其它回合都没有）——造题锚点 + 良定义保证。"""
    mine = phrase_candidates(per_turn_text.get(turn, ''))
    others = '\n'.join(text for t, text in per_turn_text.items() if t != turn)
    return [p for p in mine if p not in others][:limit]


def turn_texts(session) -> dict[int, str]:
    """每个回合的原文全文（校验 answer_hint 唯一性用）。"""
    result: dict[int, str] = {}
    current = 0
    for event in session.events:
        data = event.data if isinstance(event.data, dict) else {}
        if event.type == 'turn/start':
            current = data.get('turn', 0)
        if event.type in ('user/message', 'assistant/message', 'tool/result'):
            from manifest import message_of, message_text
            text = message_text(message_of(event))
            if text:
                result[current] = result.get(current, '') + '\n' + text
    return result


def manifest_line(manifest: str, turn: int) -> str:
    for line in manifest.splitlines():
        if line.startswith(f'[{turn:>3}]'):
            return line
    return ''


def build_for_session(path: Path, per_session: int) -> list[dict]:
    session = load_session(path)
    turns = build_turns(session)
    manifest = render_manifest(turns)
    manifest_tokens = tokens(manifest)
    texts = turn_texts(session)
    candidates = [t for t in turns if t.user_texts and len(texts.get(t.turn, '')) > 400]
    items: list[dict] = []
    for info in candidates:
        if len(items) >= per_session:
            break
        anchors = rare_phrases(texts, info.turn)
        if not anchors:
            print(f'  turn {info.turn}: 没有独有短语（回合内容太杂/太短）→ 跳过')
            continue
        body = render_turn(session, info.turn, max_events=40, max_chars=3000)
        if len(body) < 200:
            continue
        prompt = (f'【目标回合原文】\n{body}\n\n'
                  f'【这一回合独有的短语（任选其一当主题）】\n'
                  + '\n'.join(f'- {a}' for a in anchors)
                  + f'\n\n【会话清单（尽量避免复用其中的词）】\n{manifest}')
        payload = ask_json(GEN_SYSTEM, prompt, max_tokens=500)
        if not payload:
            print(f'  turn {info.turn}: 生成失败（无 JSON）')
            continue
        query = str(payload.get('query', '')).strip()
        anchor = str(payload.get('anchor', '')).strip()
        if not (4 <= len(query) <= 80):
            print(f'  turn {info.turn}: query 长度不合格 {query!r}')
            continue
        # 校验①：query 必须逐字包含一个**该回合独有**的短语（良定义 + 锚定）
        used = [a for a in anchors if a in query]
        if not used:
            print(f'  turn {info.turn}: query 未包含任何独有短语 → 弃 :: {query[:40]}')
            continue
        # 校验②：不许用指代词（"上一轮/那次"这类无法导航）
        if any(word in query for word in ('上一轮', '上一回合', '那次', '刚才', '这轮')):
            print(f'  turn {info.turn}: 含指代词 → 弃 :: {query[:40]}')
            continue
        # 校验③：干扰回合足够
        if len([t for t in turns if t.user_texts]) - 1 < MIN_OTHER_TURNS:
            continue
        # 标签：query 是否复用了清单里的词（复用=简单档 T1，不复用=桥接档 T2）
        overlap = sorted(tokens(query) & manifest_tokens)
        ratio = len(overlap) / max(1, len(tokens(query)))
        items.append({
            'id': f'n-{path.stem}-{info.turn}',
            'query': query, 'session': path.stem, 'answer_turn': info.turn,
            'difficulty': 'T1' if overlap else 'T2',
            'overlap_ratio': round(ratio, 2), 'overlap': overlap[:8],
            'anchors': used[:3], 'anchor_declared': anchor,
            'manifest_chars': len(manifest),
            'manifest_turns': len([t for t in turns if t.user_texts]),
        })
        print(f'  turn {info.turn}: {items[-1]["difficulty"]} overlap={ratio:.0%} '
              f'anchor={used[0]!r} :: {query}')
    return items


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--per-session', type=int, default=12)
    args = parser.parse_args()

    all_items: list[dict] = []
    for path in sorted(SESSIONS.glob('*.jsonl')):
        session = load_session(path)
        usable = [t for t in build_turns(session) if t.user_texts]
        if len(usable) < MIN_OTHER_TURNS + 1:
            continue
        print(f'=== {path.name}（{len(usable)} 个有用户话的回合）===')
        all_items.extend(build_for_session(path, args.per_session))

    with OUT.open('w', encoding='utf-8') as handle:
        for item in all_items:
            handle.write(json.dumps(item, ensure_ascii=False) + '\n')

    t1 = sum(1 for i in all_items if i['difficulty'] == 'T1')
    t2 = sum(1 for i in all_items if i['difficulty'] == 'T2')
    print(f'\n共 {len(all_items)} 题（T1 撞词 {t1} / T2 桥接 {t2}）→ {OUT}')


if __name__ == '__main__':
    main()
