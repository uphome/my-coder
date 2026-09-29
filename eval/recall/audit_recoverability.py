"""可恢复性审计（**零 LLM、零 token**）：压缩把事实丢到哪儿去了？

它量化的是**赌注**，不是工具的作用（作用只能定性，见 `CONTEXT_BUDGET_DESIGN.md` §5.1/§5.3）。
为什么这条能"硬"：给定一条事实 F，下列判断全是**程序化**的，与模型行为无关——

    无损失        F 还在 live surface 里（没被遮蔽）
    摘要保住了    F 出现在该次压缩的摘要正文里
    工作区可重算  F 在工作区（当前文件）里读得到 → 不是损失，是恢复成本
    只能靠 L2     F 被遮蔽、摘要里没有、工作区里没有，但 `read_turn` 能读回来
                   （分两档：**第一页**就有 / **要翻页**才拿到——后者不是损失，只要渲染器
                    明说总量与 `offset`，模型就能继续读）
    真损失        F 连整回合的分页渲染都不产出（渲染器的空洞）

★ 这一格是 2026-09 审计新增的：**"能回到原文"不等于"原文读得到"**。当时量出来的结论是
"6 条读不回来，5 条被单块 1500 字符上限挡住、1 条被 80 事件上限挡住"——**那两条上限现在
已经删掉**（改成按行分页 + 明说被截断，见 `my_coder/app/recall.py` 的 `render_turn`）。
所以这份审计现在的用途有两个：① 持续盯着"渲染器有没有新的空洞"；② 用**改动前后**的数字
当证据（改前：6 条不可达；改后应当全部可达）。

用法：
    python eval/recall/audit_recoverability.py                    # 默认扫 .sessions 最大的会话
    python eval/recall/audit_recoverability.py --session web-1788604766
    python eval/recall/audit_recoverability.py --limit 400        # 每段最多审多少条事实

产物：`.eval/recall/AUDIT.md` + `.eval/recall/audit.json`（都是工件，不入库）。

**样本偏差要如实说**：候选事实来自正则（路径/数字/标识符/常量/URL）+ 用户话里的中文短语，
所以偏"机器长相"的字符串；决策理由、被否决方案这类散文事实**抽不出来**（§5.6 陷阱 3）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _path in (str(REPO), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import build_dataset as mining  # noqa: E402  复用它的投影/候选/工作区索引

from my_coder.app.recall import (  # noqa: E402
    build_turns,
    render_manifest,
    render_turn,
    shadowed_seqs,
)
from my_coder.state.session import Session  # noqa: E402

OUT_DIR = REPO / '.eval' / 'recall'
CJK_RUN = re.compile(r'[\u4e00-\u9fff]{6,40}')
UNBOUNDED = 10 ** 6
# §5.4 定的**硬过滤**噪音（真噪音，与召回无关）：临时夹具路径、第三方/文档路径。
# 不过滤的话"工作区可重算"会被 `big.py` / `test_a.py` / `site-packages` 这类撑起来
# ——它们按定义就能在工作区里找到，测它们等于测"能不能读文件"。
FIXTURE_PATH = re.compile(r'^[a-z]{1,4}\.(?:py|json|jsonl)$|^test_[a-z]\.py$')
THIRD_PARTY = re.compile(r'site-packages|node_modules|pytest\.org|testclient\.py|'
                         r'\.venv|dist-packages')
# 泛化常量名（`STDOUT` / `REPLACES` 这种到处都有的词不是"针"）：
# 常量必须带下划线或数字才算有辨识度（`FS_AMBIGUOUS_EDIT` 留下，`COMPLETE` 丢掉）
GENERIC_CONSTANT = re.compile(r'^[A-Z]{2,12}$')


def is_noise(kind: str, value: str) -> bool:
    if kind == 'path' and (FIXTURE_PATH.match(value) or THIRD_PARTY.search(value)):
        return True
    if kind == 'constant' and GENERIC_CONSTANT.match(value):
        return True
    return bool(kind == 'url' and THIRD_PARTY.search(value))


@dataclass(frozen=True)
class Fact:
    """一条被遮蔽区间里的事实候选。"""
    kind: str          # path / number / identifier / constant / url / 用户话
    value: str
    seq: int
    turn: int
    event_type: str


def _shadowed_facts(events: list[dict], spans: list[dict], limit: int) -> list[Fact]:
    """从每次压缩的遮蔽区间抽事实（复用 mining 的正则 + 行号噪音过滤）。"""
    turn_map = mining.turn_of_seq(events)
    facts: list[Fact] = []
    for span in spans:
        seen: set[tuple[str, str]] = set()
        per_span = 0
        for seq in span['seqs']:
            event = events[seq]
            text = mining.text_of(event)
            for kind, value, _offset in mining.candidates(text):
                if mining.looks_like_line_number(text, value):
                    continue
                key = (kind, value)
                if key in seen:
                    continue
                seen.add(key)
                facts.append(Fact(kind, value, seq, turn_map.get(seq, 0), event['type']))
                per_span += 1
                if per_span >= limit:
                    break
            if event['type'] == 'user/message' and per_span < limit:
                # 用户话里的中文短语：这一类是"约束/偏好"的载体，正则抽不到，
                # 但它恰恰是设计里最怕丢的东西（清单只收真人发言就是为了它）
                for run in CJK_RUN.findall(text):
                    key = ('用户话', run)
                    if key in seen:
                        continue
                    seen.add(key)
                    facts.append(Fact('用户话', run, seq, turn_map.get(seq, 0), event['type']))
                    per_span += 1
                    if per_span >= limit:
                        break
    return facts


def audit(session_id: str, limit: int, max_df: int) -> dict:
    path = mining.SESSIONS / f'{session_id}.jsonl'
    if not path.exists():
        raise SystemExit(f'没有这个会话：{path}')
    events = mining.load_events(path)
    live, spans = mining.replay(events)
    live_seqs = set(live)

    # 产品侧投影：L2 的两种渲染（默认有界 / 放开上限）+ L1 清单
    session = Session(id=session_id)
    from my_coder.values.persistence import load_events as load_product_events
    for event in load_product_events(path):
        session.adopt(event)
    turns = build_turns(session)
    manifest = render_manifest(turns)
    shadowed = set(shadowed_seqs(session))
    bounded: dict[int, str] = {}
    unbounded: dict[int, str] = {}

    def render(turn: int, *, wide: bool) -> str:
        """`wide` = 整回合（分页放开）；否则 = **第一页**（产品默认预算 12000 字符）。

        2026-09 产品侧把 L2 从"事件数 + 字符双上限 + 单块静默截断"改成**按行分页**之后，
        这个函数问的两个问题也变了：① 事实在不在**第一页**里（不用翻页就能看到）；
        ② 事实在不在**整回合**里（翻页能拿到）。"翻页才拿到"不是损失——只要渲染器
        **明说总量与 offset**，模型就能继续读（这正是修掉的那条缺陷）。
        """
        cache = unbounded if wide else bounded
        if turn not in cache:
            cache[turn] = render_turn(session, turn,
                                      max_chars=UNBOUNDED if wide else 12000)
        return cache[turn]

    # **稀有度筛选（df）**：只留真正的"针"。这一步不做的话，抽样结果会被
    # "agent.py / README.md" 这类到处都有的串淹没——实测把每段上限从 60 提到 300，
    # 结论就从"约一半是损失"翻成"93% 无损失"，因为后者把大量 trivially 可见的串算了进来。
    # 判据与挖矿生成器一致（§5.4）：一条事实只在 ≤ `max_df` 个事件里出现，才值得测。
    log_text = '\n'.join(mining.text_of(event) for event in events)
    extracted = _shadowed_facts(events, spans, limit=4000)
    facts = [fact for fact in extracted
             if not is_noise(fact.kind, fact.value)
             and 0 < log_text.count(fact.value) <= max_df]
    facts.sort(key=lambda fact: (fact.turn, fact.seq, fact.kind, fact.value))
    facts = facts[:limit]

    workspace = mining.workspace_text()
    # live 可见文本**只算一次**：`in_live` 问的是"同一个值在没被遮蔽的地方也有吗"
    # （有 → 这条事实没丢），不是"它出现的那条事件还活着"（那样恒为 False，等于没测）
    live_text = '\n'.join(mining.text_of(events[seq]) for seq in sorted(live_seqs))
    summary_of_seq: dict[int, str] = {}
    for span in spans:
        for seq in span['seqs']:
            summary_of_seq[seq] = span['summary']

    rows = []
    for fact in facts:
        wide = fact.value in render(fact.turn, wide=True)
        narrow = fact.value in render(fact.turn, wide=False)
        offset = mining.text_of(events[fact.seq]).find(fact.value)
        rows.append({
            'kind': fact.kind, 'value': fact.value, 'seq': fact.seq, 'turn': fact.turn,
            'event': fact.event_type,
            'in_live': fact.value in live_text,
            'in_summary': fact.value in summary_of_seq.get(fact.seq, ''),
            # path 类事实的"可重算"判**文件真的在不在**，而不是"这个串在某个文件里被提到过"
            # ——后者会把 `hidden.py` 这种**夹具名**（写在测试代码里的字符串）算成"可重算"，
            # 而它根本不是工作区里的文件（§5.4 把夹具路径列为硬过滤的噪音）
            'in_workspace': ((REPO / fact.value).is_file() if fact.kind == 'path'
                             else fact.value.lower() in workspace),
            'in_manifest': fact.value in manifest,
            'l2_full': wide,
            'l2_bounded': narrow,
            'offset': offset,
        })

    # 去向分类（互斥、按"最容易拿到"优先）
    def destination(row: dict) -> str:
        if row['in_live']:
            return '无损失（还在 live）'
        if row['in_summary']:
            return '摘要保住了'
        if row['in_workspace']:
            return '工作区可重算'
        if row['l2_bounded']:
            return '只能靠 L2 读回（第一页）'
        if row['l2_full']:
            return '只能靠 L2 读回（**要翻页**：分页会明说 offset）'
        return '★ 真损失（渲染器不产出）'

    for row in rows:
        row['destination'] = destination(row)

    by_kind: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        by_kind[row['kind']][row['destination']] += 1

    return {
        'session': session_id,
        'events': len(events),
        'compactions': len(spans),
        # 事务的**尝试数与失败原因**也要报：真实语料里"压缩"经常是失败的
        # （实测那次会话：尝试 4 次、成功 1 次，前三次都是 `empty summary`——
        # 正是仓库后来把摘要 max_tokens 提到 8192 修掉的坑）。
        # 只报"成功几次"会让人以为损失面只被算了一次，而失败的那几次**什么都没压**。
        'compaction_attempts': sum(1 for e in events if e['type'] == 'compaction/start'),
        'compaction_errors': [
            (e['data'].get('$dict', e['data']) or {}).get('error', '')
            for e in events if e['type'] == 'compaction/end'
            and (e['data'].get('$dict', e['data']) or {}).get('error')
        ],
        'compaction_summary_chars': [len(span['summary']) for span in spans],
        'shadowed_events': len(shadowed),
        'facts': len(rows),
        'extracted': len(extracted),
        'max_df': max_df,
        'totals': dict(Counter(row['destination'] for row in rows)),
        'by_kind': {kind: dict(counter) for kind, counter in by_kind.items()},
        'rows': rows,
    }


def corpus_summary() -> list[tuple[str, int, int]]:
    """整个语料里有几次成功压缩（逐会话数事务成败）——**赌注规模的前提**。

    只报被审计那个会话会让读者以为"语料里压缩很多"；实际本仓 `.sessions` 里
    成功压缩极少（实测 4 个会话里只有 1 个压成功，且只成功 1 次）——
    这正是 §5.4 说"历史里可挖的太少、所以主路径是受控构造"的现场证据。
    """
    rows = []
    for path in sorted(mining.SESSIONS.glob('*.jsonl')):
        events = mining.load_events(path)
        attempts = sum(1 for e in events if e['type'] == 'compaction/start')
        ok = sum(1 for e in events if e['type'] == 'compaction/end'
                 and not (e['data'].get('$dict', e['data']) or {}).get('error'))
        if attempts:
            rows.append((path.stem, attempts, ok))
    return rows


def render_report(result: dict, corpus: list[tuple[str, int, int]]) -> str:
    lines: list[str] = []
    add = lines.append
    total = max(1, result['facts'])
    add(f'# 可恢复性审计：`{result["session"]}`（零 LLM）')
    add('')
    add('> 语料规模：' + '；'.join(f'`{name}` 尝试 {a} 次成功 {ok} 次' for name, a, ok in corpus)
        + f' —— **整个 `.sessions` 里只有 {sum(ok for _, _, ok in corpus)} 次成功压缩**，'
          '这就是"赌注规模"的现场：真实语料几乎没压过，所以主路径只能是受控构造（§5.4）。')
    add('')
    add(f'- 事件 {result["events"]:,} 条；**压缩事务：尝试 {result["compaction_attempts"]} 次 / '
        f'成功 {result["compactions"]} 次**；被遮蔽 surface 事件 {result["shadowed_events"]} 个')
    if result['compaction_errors']:
        reasons = Counter(result['compaction_errors'])
        add(f'- 失败的压缩：{", ".join(f"{reason} ×{count}" for reason, count in reasons.items())}'
            '（失败的**什么都没压**，所以损失面只由成功那几次构成）')
    add(f'- 摘要大小：{" / ".join(f"{n:,}" for n in result["compaction_summary_chars"])} 字符')
    add(f'- 抽样事实 **{result["facts"]}** 条'
        f'（从 {result["extracted"]} 条候选里按稀有度筛出：只留出现 ≤{result["max_df"]} 次事件的"针"）')
    add('')
    add('## 去向分布（这就是"赌注规模"）')
    add('')
    add('| 去向 | 条数 | 占比 | 含义 |')
    add('|---|---:|---:|---|')
    meaning = {
        '无损失（还在 live）': '没被遮蔽，模型现在就看得到',
        '摘要保住了': 'R2（只有摘要）也拿得到',
        '工作区可重算': '文件还在（path 类）/ 工作区里查得到 → 不是损失，是恢复成本',
        '只能靠 L2 读回（第一页）': '**必须走召回**：L0/L1 定位 + `read_turn` 精读，默认那一页就有',
        '只能靠 L2 读回（**要翻页**：分页会明说 offset）':
            '**必须走召回**，而且要把 `read_turn` 翻到第 N 页——不是损失（渲染器明说总量与 offset），'
            '但要靠模型**照着提示继续读**（这是"工具查了但没读全"的观察点）',
        '★ 真损失（渲染器不产出）': '整回合按行分页都渲染不出来 → 渲染器的空洞，要看具体块类型',
    }
    order = list(meaning)
    for name in order:
        count = result['totals'].get(name, 0)
        if not count:
            continue
        add(f'| {name} | {count:,} | {count / total:.0%} | {meaning[name]} |')
    if total < 20:
        add('')
        add(f'> ⚠️ **样本只有 {total} 条**，占比没有统计意义——**只看计数**。'
            '"针"少本身就是结论：这次成功压缩里真正"只能靠召回"的东西非常少（§5.4）。')
    add('')
    add('## 按事实类型')
    add('')
    kinds = sorted(result['by_kind'], key=lambda k: -sum(result['by_kind'][k].values()))
    header = '| 类型 | 条数 | ' + ' | '.join(order) + ' |'
    add(header)
    add('|---' * (len(order) + 2) + '|')
    for kind in kinds:
        counter = result['by_kind'][kind]
        count = sum(counter.values())
        cells = ' | '.join(str(counter.get(name, 0)) for name in order)
        add(f'| {kind} | {count} | {cells} |')
    add('')
    add('## 怎么读这张表')
    add('')
    add('- **"摘要保住了" 不是好事也不是坏事**：它说明这条事实不需要召回就能用（§5.4 的修正口径）。')
    add('- **"只能靠 L2 读回" 才是召回的目标人群**；它的**可达率**决定工具的天花板。')
    add('- **"★ L2 也读不到" 是工具自己的缺陷**（渲染上限），与压缩无关——'
        '修它只看 `render_message` / `render_turn` 的上限，不需要动压缩。')
    add('- 候选来自正则 → **偏机器长相的字符串**；决策理由、被否决方案这类散文事实抽不出来，'
        '所以真实"真损失"占比只会比这里更高（§5.6 陷阱 3）。')
    add('')
    add('## 附录：这次审计用到的全部"针"（原始证据，可逐条核对）')
    add('')
    add('| 去向 | 类型 | 值 | 回合 | seq | 在摘要 | 在工作区 | L2(第一页) | L2(整回合) |')
    add('|---|---|---|---:|---:|---|---|---|---|')
    mark = {'无损失（还在 live）': 'live', '摘要保住了': '摘要', '工作区可重算': '工作区',
            '只能靠 L2 读回（第一页）': '**L2**',
            '只能靠 L2 读回（**要翻页**：分页会明说 offset）': '**L2 翻页**',
            '★ 真损失（渲染器不产出）': '**不产出**'}
    for row in sorted(result['rows'], key=lambda r: (order.index(r['destination']), r['turn'])):
        value = row['value'].replace('|', '\\|')[:60]
        add(f'| {mark[row["destination"]]} | {row["kind"]} | `{value}` | {row["turn"]} | '
            f'{row["seq"]} | {"✓" if row["in_summary"] else ""} | '
            f'{"✓" if row["in_workspace"] else ""} | {"✓" if row["l2_bounded"] else ""} | '
            f'{"✓" if row["l2_full"] else ""} |')
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', default='', help='会话 id（默认取 .sessions 里最大的）')
    parser.add_argument('--limit', type=int, default=300, help='抽样上限（稀有度筛过之后）')
    parser.add_argument('--max-df', type=int, default=2,
                        help='稀有度阈值：一个值最多在几个事件里出现才算"针"（默认 2）')
    parser.add_argument('--out', type=Path, default=OUT_DIR)
    args = parser.parse_args()

    session_id = args.session
    if not session_id:
        biggest = max(mining.SESSIONS.glob('*.jsonl'), key=lambda p: p.stat().st_size)
        session_id = biggest.stem
    result = audit(session_id, args.limit, args.max_df)
    corpus = corpus_summary()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'audit.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    report = render_report(result, corpus)
    (args.out / 'AUDIT.md').write_text(report + '\n', encoding='utf-8')
    print(report)


if __name__ == '__main__':
    main()
