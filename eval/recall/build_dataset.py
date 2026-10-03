"""召回数据集生成器 v0（零 LLM）。

目标：从真实日志里自动产出"只能靠召回才能答对"的题目 + 标准答案坐标，
用来量化检索策略好不好用（`run_retriever.py` 打分，不需要模型）。

流程
----
1. 读 `.sessions/*.jsonl`，重放 surface 投影 → 拿到 live / 已被压缩遮蔽的事件；
2. 定位每次压缩的遮蔽区间与它的摘要；
3. 从**遮蔽区间**抽候选事实（路径 / 数字 / 标识符 / 大写常量 / URL）；
4. **三道过滤**（决定题目有没有意义）：
     ① 不在该次摘要里   —— 否则 R2（只有摘要）也能答对
     ② 不在当前工作区里 —— 否则读文件即可，测的不是召回
     ③ 不在 live 事件里 —— 否则模型现在就看得到，不算损失
5. 生成 query（模板：名字→值 / 功能→标识符 / 职责→路径），并做**泄露校验**
   （答案串不得出现在 query 里）；
6. 按 df（稀有度）排序取最锋利的一批，落 `questions.jsonl` + 统计。

v0.1 只出 `detail`（事实细节）与 `negative`（负样本）两类；
`manifest`（用户话清单层）与 LLM 改写（多措辞）留给 v0.2，见
`docs/notes/implemented/testing/2026-09-19-context-recall-evaluation.md`。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SESSIONS = REPO / '.sessions'
OUT = Path(__file__).resolve().parent / 'questions.jsonl'

SURFACE = ('user/message', 'assistant/message', 'tool/result')
# 元数据字段不算正文（计数/建索引时排除）
META_KEYS = frozenset({
    'id', 'role', 'type', 'name', 'call_id', 'provider', 'model',
    'finish_reason', 'tool_call_id', 'source', '$user', 'is_error',
})
# 工作区索引要跳过的目录（.sessions 是语料本身；eval 是评测工件——不跳过会**自我污染**：
# 上一轮写出的 questions.jsonl 里含答案，下一轮就被"工作区里有"这条过滤掉）
# `.eval` 同理，而且更隐蔽：它是**本轮迭代的产物目录**（审计报告、runner 的 run 日志、
# 导出的会话拷贝），跑第二遍时"上一遍的输出"就会把答案喂给"工作区可重算"这条标签。
SKIP_DIRS = frozenset({
    '.git', '.sessions', '.codegraph', '__pycache__', '.pytest_cache',
    '.mypy_cache', '.ruff_cache', '.venv', 'node_modules', '.idea', 'eval', '.eval',
})

PATTERNS: dict[str, re.Pattern] = {
    'constant': re.compile(r'\b[A-Z][A-Z0-9_]{4,}\b'),
    'identifier': re.compile(r'\btest_[a-z0-9_]{6,}\b|\b[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}\b'),
    'path': re.compile(r'\b[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|md|json|jsonl|html|ts|tsx|rs|yml|yaml|toml|css)\b'),
    'number': re.compile(r'\b\d[\d,_]{2,}(?:\.\d+)?\b'),
    'url': re.compile(r'https?://[^\s")\]}，。；]+'),
}
CJK = re.compile(r'[\u4e00-\u9fff]{3,12}')


@dataclass(frozen=True)
class Item:
    qid: str
    cls: str
    query: str
    answer: str
    answer_kind: str
    session: str
    seq: int
    turn: int
    shadowed: bool
    df: int
    source: str
    # 以下三个是**标签不是过滤**：它们衡量"端到端是否需要召回"，
    # 而检索器层只需要"金标坐标在遮蔽区间里"。是否真有区分度要靠 R2 实测，
    # 不能靠字符串包含猜（摘要保留要点 ≠ 保留细节——记忆漂移就出在这里）。
    in_summary: bool = False
    in_workspace: bool = False
    in_live: bool = False
    gold_all: tuple[int, ...] = ()      # 所有含该答案的坐标（含 live 副本）

    def as_dict(self) -> dict:
        return {
            'id': self.qid, 'class': self.cls, 'query': self.query,
            'answer': self.answer, 'answer_kind': self.answer_kind,
            'gold': {'session': self.session, 'seq': self.seq, 'turn': self.turn},
            'gold_all': list(self.gold_all),
            'shadowed': self.shadowed, 'df': self.df, 'source': self.source,
            'labels': {
                'in_summary': self.in_summary,
                'in_workspace': self.in_workspace,
                'in_live': self.in_live,
            },
        }


# ---------- 日志与投影 ----------

def load_events(path: Path) -> list[dict]:
    events = []
    with path.open('r', encoding='utf-8') as handle:
        for line in handle:
            if not line.strip():
                continue
            data = json.loads(line)
            # 第一行是会话头（文件级元数据，不是事件）：跳过，否则 events[seq]
            # 的下标会整体错位一格（本模块按下标取事件）。
            if data.get('session') is True:
                continue
            events.append(data)
    return events


def collect_text(node, out: list[str]) -> None:
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key not in META_KEYS:
                collect_text(value, out)
    elif isinstance(node, list):
        for item in node:
            collect_text(item, out)


def text_of(event: dict) -> str:
    parts: list[str] = []
    collect_text(event['data'], parts)
    return '\n'.join(parts)


def replay(events: list[dict]) -> tuple[list[int], list[dict]]:
    """重放 surface 投影：返回 (live seq 列表, 每次压缩的遮蔽跨度)。

    注意时序：`compaction/summary` 在 `replace` **之前**落日志（四步事务是
    start → summary → replace → end），所以摘要要先用 pending 暂存，
    等 replace 建跨度时再挂上去——否则每次跨度都拿不到摘要。
    """
    surface: list[int] = []
    spans: list[dict] = []
    pending_summary = ''
    for event in events:
        op = event.get('surface_op')
        if op == 'append':
            surface.append(event['seq'])
        elif op == 'replace':
            start = surface.index(event['shadowed'][0])
            end = surface.index(event['shadowed'][1])
            spans.append({
                'seqs': surface[start:end + 1],
                'checkpoint_seq': event['seq'],
                'summary': pending_summary,
            })
            pending_summary = ''
            del surface[start:end + 1]
            surface.insert(start, event['seq'])
        elif event['type'] == 'compaction/summary':
            pending_summary = text_of(event)
    return surface, spans


def turn_of_seq(events: list[dict]) -> dict[int, int]:
    """每个 seq 属于第几回合（按 turn/start 划界）。"""
    result: dict[int, int] = {}
    turn = 0
    for event in events:
        if event['type'] == 'turn/start':
            turn = event['data'].get('$dict', event['data']).get('turn', turn + 1)
        result[event['seq']] = turn
    return result


# ---------- 工作区索引（过滤 ②） ----------

def workspace_text() -> str:
    chunks: list[str] = []
    for path in REPO.rglob('*'):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in {'.png', '.jpg', '.ico', '.woff', '.woff2', '.db'}:
            continue
        try:
            chunks.append(path.read_text(encoding='utf-8', errors='ignore'))
        except OSError:
            continue
    return '\n'.join(chunks).lower()


# ---------- 候选事实 ----------

def candidates(text: str) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            value = match.group(0).strip('`\'"()[]，。；')
            if len(value) < 3:
                continue
            found.append((kind, value, match.start()))
    return found


def topic_for(text: str, offset: int) -> str | None:
    """取事实前 80 字符内最近的一段中文短语当"主体"（用于语义型 query）。

    窗口故意开小（80 字符）：开大容易抓到与事实无关的上一句话。
    过短（<4 字）或全是虚词的片段直接放弃——宁可不出题，也不出烂题。
    """
    window = text[max(0, offset - 80):offset]
    runs = [run for run in CJK.findall(window) if len(run) >= 4]
    if not runs:
        return None
    topic = runs[-1].strip('的了是和与在对把为')
    if len(topic) < 4:
        return None
    stopwords = ('关于', '这个', '那个', '我们', '你们', '他们', '因为', '所以', '但是')
    return None if topic in stopwords else topic


def looks_like_line_number(text: str, value: str) -> bool:
    """行号/行数（`963: def foo`、`Line 974:`、`file has 1761 lines`）不是事实——
    它们属于"文件内容的副产品"，不是"对话里发生的事"。实测这三类占了候选的 73%。"""
    if re.search(rf'\n\s*{re.escape(value)}\s*:', text):
        return True
    if re.search(rf'\bLine\s+{re.escape(value)}\b', text):
        return True
    return re.search(rf'\bfile has\s+{re.escape(value)}\s+lines\b', text) is not None


def make_query(kind: str, value: str, topic: str | None) -> tuple[str, str] | None:
    """生成 (query, 来源标签)。**绝不把 value 放进 query**（泄露校验兜底）。"""
    if kind == 'number':
        if topic is None:
            return None
        return f'关于「{topic}」，我们当时把数字定成了多少？', 'number+topic'
    if kind == 'constant':
        # 常量名进 query 是安全的（要问的是它的值）；但常量名本身若就是答案则不出题
        return f'当时 `{value}` 的值是多少？', 'constant->value'
    if kind == 'identifier':
        if value.startswith('test_') and topic is not None:
            return f'哪个测试覆盖了「{topic}」这件事？', 'topic->testname'
        return None
    if kind == 'path':
        if topic is None:
            return None
        return f'当时处理「{topic}」的是哪个文件？', 'topic->path'
    if kind == 'url':
        if topic is None:
            return None
        return f'当时关于「{topic}」引用的是哪个链接？', 'topic->url'
    return None


def cjk_phrases(text: str, min_len: int = 4) -> list[str]:
    return [p for p in re.findall(rf'[\u4e00-\u9fff]{{{min_len},16}}', text)]


def manifest_items(session: str, events: list[dict], span: dict, turns: dict,
                   summary: str, live_text: str, workspace: str, corpus_text: str,
                   surfaces: list[tuple[int, str]], seen: set[str], start: int) -> list[Item]:
    """清单层题目：被压缩遮蔽的**真人发言**——它的要求在原文里是怎么说的。

    答案取该发言里最长的一段独特中文短语（逐字可判分）；**不因为"摘要里也出现过"
    就丢弃**（摘要可能只保住了要点，具体约束恰恰丢了——那才是要测的）。
    """
    made: list[Item] = []
    for seq in span['seqs']:
        event = events[seq]
        if event['type'] != 'user/message':
            continue
        text = text_of(event)
        if 'compacted-summary' in text:      # checkpoint 合成消息不是用户语言
            continue
        phrases = cjk_phrases(text, min_len=5)
        answer = next((p for p in sorted(phrases, key=len, reverse=True)
                       if p.lower() not in seen), None)
        if answer is None:
            continue
        topic = next((p for p in phrases if p != answer and len(p) >= 4), None)
        hint = f'（提示：与「{topic}」有关）' if topic else ''
        low = answer.lower()
        gold_all = tuple(s for s, t in surfaces if low in t)
        made.append(Item(
            qid=f'm-{start + len(made):04d}', cls='manifest',
            query=f'我在之前某一轮提过什么要求？{hint}',
            answer=answer, answer_kind='user_requirement',
            session=session, seq=seq, turn=turns.get(seq, 0), shadowed=True,
            df=corpus_text.count(low), source='manifest',
            in_summary=low in summary, in_workspace=low in workspace,
            in_live=low in live_text, gold_all=gold_all,
        ))
        seen.add(low)
    return made


def is_fixture_artifact(value: str) -> bool:
    """临时夹具路径（`a.py`、`tests/test_tmp/x.py`）不是"对话里的事实"——它们是测试
    运行时造出来的临时文件，问"当时处理 X 的是哪个文件"答出来也没有信息量。"""
    lower = value.lower()
    if any(marker in lower for marker in ('tmp_path', 'test_tmp', '/tmp/', '\\tmp\\')):
        return True
    basename = lower.replace('\\', '/').rsplit('/', 1)[-1]
    return len(basename.split('.')[0]) <= 2      # a.py / b.md 这类单字母名


def normalize(text: str) -> str:
    return ' '.join(text.lower().split())


# ---------- 主流程 ----------

def main() -> None:
    workspace = workspace_text()
    print(f'工作区索引：{len(workspace):,} 字符')
    items: list[Item] = []
    counter = 0
    seen_answers: set[str] = set()
    stats = {'candidates': 0, 'in_summary': 0, 'in_workspace': 0, 'in_live': 0,
             'no_query': 0, 'leak': 0, 'kept': 0, 'line_number': 0, 'duplicate': 0,
             'manifest': 0, 'fixture': 0}

    for session_path in sorted(SESSIONS.glob('*.jsonl')):
        events = load_events(session_path)
        if not any(e['type'] == 'compaction/summary' for e in events):
            continue
        live, spans = replay(events)
        live_text = normalize('\n'.join(text_of(events[seq]) for seq in live
                                        if events[seq]['type'] in SURFACE))
        turns = turn_of_seq(events)
        corpus_text = normalize('\n'.join(
            text_of(e) for e in events if e['type'] in SURFACE))

        for span in spans:
            summary = normalize(span['summary'])
            if not summary:
                continue    # 失败的压缩（无摘要）
            # 该会话所有 surface 事件的规范化正文：用来算"答案还在哪些坐标里出现"
            surfaces = [(e['seq'], normalize(text_of(e)))
                        for e in events if e['type'] in SURFACE]
            # 清单层：被遮蔽的真人发言（今天就能出题）
            manifest = manifest_items(
                session_path.name, events, span, turns, summary, live_text,
                workspace, corpus_text, surfaces, seen_answers, counter + 1,
            )
            items.extend(manifest)
            counter += len(manifest)
            stats['manifest'] += len(manifest)
            for seq in span['seqs']:
                event = events[seq]
                if event['type'] not in SURFACE:
                    continue
                if event['type'] == 'user/message' and 'compacted-summary' in text_of(event):
                    continue    # checkpoint 合成消息本身不算事实来源
                text = text_of(event)
                # 同一事件最多贡献 3 条：防止一个巨大的工具输出（文件 dump）淹没整个数据集
                per_event = 0
                for kind, value, offset in candidates(text):
                    stats['candidates'] += 1
                    low = value.lower()
                    if kind == 'number' and looks_like_line_number(text, value):
                        stats['line_number'] += 1      # 行号是噪音：硬过滤
                        continue
                    if is_fixture_artifact(value):
                        stats['fixture'] += 1          # 临时夹具路径：硬过滤
                        continue
                    if low in seen_answers:
                        stats['duplicate'] += 1
                        continue
                    made = make_query(kind, value, topic_for(text, offset))
                    if made is None:
                        stats['no_query'] += 1
                        continue
                    query, source = made
                    if normalize(value) in normalize(query):
                        stats['leak'] += 1             # 答案混进题干 → 弃
                        continue
                    counter += 1
                    seen_answers.add(low)
                    per_event += 1
                    in_summary = low in summary
                    in_workspace = low in workspace
                    in_live = low in live_text
                    stats['in_summary'] += in_summary
                    stats['in_workspace'] += in_workspace
                    stats['in_live'] += in_live
                    items.append(Item(
                        qid=f'q-{counter:04d}', cls='detail', query=query,
                        answer=value, answer_kind=kind, session=session_path.name,
                        seq=seq, turn=turns.get(seq, 0), shadowed=True,
                        df=corpus_text.count(low), source=source,
                        in_summary=in_summary, in_workspace=in_workspace,
                        in_live=in_live,
                        gold_all=tuple(s for s, t in surfaces if low in t),
                    ))
                    stats['kept'] += 1
                    if per_event >= 3:
                        break

    # 负样本：确认全库不存在的串（考"干净返回空"）
    negatives = [
        ('我们有没有配置过 `plan_compaction` 这个函数？', 'plan_compaction'),
        ('仓库里有没有用过 `new-context-already-used` 这个错误码？', 'new-context-already-used'),
        ('我们讨论过把检索结果做成向量（embedding）索引吗？', 'embedding 向量索引'),
    ]
    for index, (query, answer) in enumerate(negatives, start=1):
        items.append(Item(
            qid=f'n-{index:04d}', cls='negative', query=query, answer=answer,
            answer_kind='absent', session='', seq=0, turn=0, shadowed=False,
            df=0, source='handwritten',
        ))

    # 最锋利的先排：df 小（稀有）→ 只能靠召回
    items.sort(key=lambda item: (item.cls != 'detail', item.df, item.qid))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open('w', encoding='utf-8') as handle:
        for item in items:
            handle.write(json.dumps(item.as_dict(), ensure_ascii=False) + '\n')

    detail = [i for i in items if i.cls == 'detail']
    print(f'硬过滤（真噪音）：行号 {stats["line_number"]} · 临时夹具 {stats["fixture"]} · '
          f'重复 {stats["duplicate"]} · 无 query 模板 {stats["no_query"]} · 泄露 {stats["leak"]}')
    print(f'标签（不是过滤，供端到端筛选）：detail 里 摘要也提到 {stats["in_summary"]} · '
          f'工作区可读 {stats["in_workspace"]} · live 还可见 {stats["in_live"]}')
    print(f'题目：detail {len(detail)} 条 + '
          f'manifest {sum(1 for i in items if i.cls == "manifest")} 条 + '
          f'negative {sum(1 for i in items if i.cls == "negative")} 条 → {OUT}')
    print(f'其中"摘要保要点、细节只在原文"的（in_summary=True）: '
          f'{sum(1 for i in items if i.in_summary)} 条 ← 最想要的一类')
    print('\n--- 样例（df 最小的 8 条）---')
    for item in items[:8]:
        print(f'[{item.qid}] df={item.df:<3} {item.answer_kind:<10} '
              f'{item.session}:{item.seq} turn={item.turn}')
        print(f'    Q: {item.query}')
        print(f'    A: {item.answer}')


if __name__ == '__main__':
    main()
