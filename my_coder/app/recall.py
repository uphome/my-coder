"""应用层：上下文召回（issue #3 的 M1）——L0 会话目录 / L1 用户话清单 / L2 回合明细。

## 为什么需要它

压缩只改变"模型看得见什么"，不改变"存在什么"（`replace` 只动 `Session._surface`
索引，`_log` 一个字不动）。所以被折叠掉的原文随时可以捞回来——前提是模型**知道**
有哪些会话、每个会话发生过哪些回合、以及去哪儿读。三层就是这个"目录 → 清单 → 原文"：

- **L0 会话目录**：一行一个会话（用户话数 / 回合数 / 压缩次数 / 最近标题）。
  消费方是 **runtime status 贡献者**（`app/factory.py` 一行注册，每请求叠在 messages
  末尾，不进 system）；所以它必须**便宜**——见下面的 stat 键控缓存。
- **L1 用户话清单**：一个会话里全部**真人发言** + 每个回合的结构足迹
  （结论摘录 / step 数 / 工具 / 涉及文件 / 结局 / 是否已被压缩）。这是导航入口。
- **L2 回合明细**：某个回合的原文事件（按需、有界、渲染成文本而不是原始 JSONL）。

## 三条设计约束（都有实测依据，见 `docs/notes/implemented/feature/2026-09-19-context-recall.md`）

1. **不做相关性排序**：语料是自述的时间线，导航靠"顺序 + 回合号 + 足迹"；
   关键词检索只作兜底（v1 不做）。
2. **检索面 = 曾经进过模型上下文的事件**：`user/message` / `assistant/message` /
   `tool/result` 的**全集**（含被 `replace` 遮蔽的）。痕迹事件（chunk / reasoning /
   request_header）不进——它们从未进过上下文，谈不上损失，且占日志 99.6% 的行数。
3. **清单只收真人发言**：checkpoint 本身是一条 `user/message`（`surface_op='replace'`），
   不排除就会把 "This is an automatically generated checkpoint…" 混进目录。

## 成本与新鲜度

- **扫会话目录按 file stat 缓存**（`(mtime_ns, size)` 键控）：L0 每请求求值一次，
  命中缓存时只做 `scandir` + `stat`；未命中才逐行扫（行内匹配，**不整包解析**——
  实测 45 MB 语料整包 `json.loads` 要 842 ms，只解析命中行 140 ms）。
- **L1/L2 只解析目标会话**（当前会话直接用内存里的 `Session`，不重读盘）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..state.session import Session
from ..values.messages import Message, TextBlock, ToolCallBlock, ToolResultBlock
from ..values.persistence import load_events
from .workspace import normalize_recorded_workspace

log = logging.getLogger('recall')

SURFACE = ('user/message', 'assistant/message', 'tool/result')
CHECKPOINT_MARK = '<compacted-summary>'


# ---------- 会话目录（L0） ----------

@dataclass(frozen=True)
class SessionRow:
    """会话目录的一行。字段与 `web/sessions.py` 的列表载荷兼容（`as_dict`）。"""
    session_id: str
    events: int
    updated: float
    title: str
    title_source: str
    summary: str
    workspace: str
    workspace_ok: bool
    user_messages: int
    turns: int
    compactions: int

    def as_dict(self) -> dict:
        return {
            'id': self.session_id,
            'events': self.events,
            'updated': self.updated,
            'title': self.title or None,
            'title_source': self.title_source or None,
            'summary': self.summary,
            'workspace': self.workspace,
            'workspace_ok': self.workspace_ok,
        }


# stat 键控缓存：value = (键, 行)。键不变就复用——L0 每请求都要，不能每次重扫。
#
# **键必须含 `default_workspace`**：行里的 `workspace` 是"日志记录值 or 宿主默认值"，
# 而 Web 宿主**每个 seat 有自己的工作区**（`Seat.args` 只换 workspace）。少这一维时，
# 第二个 seat 会拿到第一个 seat 的归属判定——L0 会列错会话，更糟的是**跨工作区授权
# 会算错**（旧会话被算到别的 seat 的工作区上）。
_SCAN_CACHE: dict[tuple[str, str], tuple[tuple[int, int], SessionRow]] = {}


@dataclass
class _LiveCounter:
    """当前会话的**增量**目录账（事件只追加，所以游标只前进）。"""
    session_id: str = ''
    indexed: int = 0
    user_messages: int = 0
    turns: int = 0
    compactions: int = 0
    title: str = ''
    first_user: str = ''
    workspace: str = ''


# 按**会话对象身份**记增量账（同一个 agent 的会话对象是长命的；换了对象就重建）
_LIVE_CACHE: dict[int, _LiveCounter] = {}


def _scan_one(path: Path, default_workspace: str) -> SessionRow:
    """逐行扫一个日志：只做行内子串匹配 + 命中行才 `json.loads`（实测快 6 倍）。

    统计的三类事件都能靠 `"type": "..."` 干净地数出来（JSONL 一行一事件）。
    """
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    cache_key = (str(path), default_workspace)  # 见 _SCAN_CACHE 注释：缺第二维会串工作区
    cached = _SCAN_CACHE.get(cache_key)
    if cached is not None and cached[0] == key:
        return cached[1]

    events = user_messages = turns = compactions = 0
    summary = title = title_source = workspace = ''
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            events += 1
            if '"type": "user/message"' in line:
                # **checkpoint 也是一条 user/message**（`surface_op='replace'` 写的合成消息）：
                # 不许算进"用户话数"，否则目录里的条数和 L1 清单的行数对不上——
                # 而"清单只收真人发言"是我们自己定的规则（AGENTS.md 的召回归约）。
                if CHECKPOINT_MARK in line:
                    continue
                user_messages += 1
                if not summary:
                    try:
                        payload = (json.loads(line).get('data') or {}).get('$message') or {}
                        for block in payload.get('content') or []:
                            if '$text' in block:
                                summary = block['$text'][:60]
                                break
                    except (TypeError, ValueError, KeyError):
                        pass
            elif '"type": "turn/start"' in line:
                turns += 1
            elif '"type": "compaction/start"' in line:
                compactions += 1
            if '"type": "session/title"' in line:
                try:
                    payload = (json.loads(line).get('data') or {}).get('$dict') or {}
                    title = payload.get('title', '') or title
                    title_source = payload.get('source', '') or title_source
                except (TypeError, ValueError, KeyError):
                    pass
            if '"type": "session/workspace"' in line:
                # 与 seat 侧同一条归一化规则（否则"列表显示 A、工具在 B"最难查）；
                # 空白记录按"没记录过"处理——空白折成 cwd 就是一次静默换根
                try:
                    payload = (json.loads(line).get('data') or {}).get('$dict') or {}
                    raw = str(payload.get('workspace') or '').strip()
                    if raw:
                        try:
                            workspace = str(normalize_recorded_workspace(raw))
                        except OSError:
                            workspace = raw
                except (TypeError, ValueError, KeyError):
                    pass

    effective = workspace or default_workspace
    row = SessionRow(
        session_id=path.stem, events=events, updated=stat.st_mtime,
        title=title, title_source=title_source,
        summary=title or summary or '(empty)', workspace=effective,
        workspace_ok=Path(effective).is_dir() if effective else False,
        user_messages=user_messages, turns=turns, compactions=compactions,
    )
    _SCAN_CACHE[cache_key] = (key, row)
    return row


def scan_sessions(sessions_dir: Path, default_workspace: str = '',
                  exclude: str = '') -> list[SessionRow]:
    """扫会话目录（最近更新的在前）。**唯一实现**：web 的列表也走这里。

    `exclude` 是要跳过的会话 id——**当前会话必须排除**：它每追加一条事件就变，
    stat 键控缓存必然失效，于是"每请求求值一次"的 L0 会**每次重扫整个日志**
    （实测 54 MB 的会话逐行扫 278 ms/次）。当前会话的行由内存算（见
    `session_index_text`），不读盘——这也正是设计文档里写下的规则。
    """
    rows: list[SessionRow] = []
    for path in sorted(sessions_dir.glob('*.jsonl')):
        if exclude and path.stem == exclude:
            continue
        try:
            rows.append(_scan_one(path, default_workspace))
        except OSError as error:      # 单个文件读不了不该让整张列表消失
            log.warning('cannot scan session %s: %s', path, error)
    rows.sort(key=lambda row: row.updated, reverse=True)
    return rows


def render_session_index(rows: list[SessionRow], *, current_id: str = '',
                         workspace: str = '', limit: int = 5,
                         hint: bool = True) -> str | None:
    """L0 会话目录（运行时状态贡献者的原文）；没有可列的会话返回 None。

    **只列同一工作区的会话**：每个对话有自己的工作区（`docs/prior-art.md` §10），跨工作区列表
    等于把别的项目的会话念给模型（对齐 DSH 的 exact-cwd 授权）。旧会话没记录工作区时
    跟随宿主默认，与工作区选择策略同一判据。

    `hint=True` 且**当前会话被压缩过**时，追加一句"被折叠的内容仍可读回 + 怎么读"：
    这三条提示词供给面里唯一"出现在需求发生处"的一条（状态栏每请求都在），
    另外两条在 system（通用纪律 + `tool:recall` 段，见 `app/factory.py`）。
    只在确有压缩时出现——别的会话压缩过不关当前这一轮的事，那属于噪声。
    """
    same = [row for row in rows
            if not workspace or not row.workspace or row.workspace == workspace]
    if not same:
        return None
    ordered = sorted(same, key=lambda row: (row.session_id != current_id, -row.updated))
    keep = [row for row in ordered[:limit] if row.user_messages]
    if not keep:
        return None
    lines = ['<context_sessions>']
    for row in keep:
        mark = '  ← 当前' if row.session_id == current_id else ''
        compacted = f' · 已压缩×{row.compactions}' if row.compactions else ''
        lines.append(f'[{row.session_id}] {row.user_messages} 条用户话 · '
                     f'「{row.summary[:40]}」 · {row.turns} 回合{compacted}{mark}')
    hidden = len(same) - len(keep)
    if hidden > 0:
        lines.append(f'…（另有 {hidden} 个更早会话，用 session_manifest 查看）')
    current = next((row for row in keep if row.session_id == current_id), None)
    if hint and current is not None and current.compactions:
        lines.append(f'…（本会话压缩过 {current.compactions} 次：被折叠的原文没有被删掉——'
                     '缺细节时先 session_manifest 看清单，再 read_turn 读那一回合的原文；'
                     '摘要只留要点，精确值/路径/命令/报错串通常只在原文里）')
    lines.append('</context_sessions>')
    return '\n'.join(lines)


# ---------- 回合聚合（L1 的素材） ----------

@dataclass(frozen=True)
class TurnInfo:
    """一个回合的投影：真人发言 + 结构足迹 + 结局。"""
    turn: int
    seqs: tuple[int, ...]
    user_texts: tuple[str, ...]
    steps: int
    tools: tuple[str, ...]
    files: tuple[str, ...]
    commands: tuple[str, ...]
    outcome: str
    conclusion: str
    shadowed: bool

    def footprint(self) -> str:
        tools = ','.join(self.tools) or '—'
        files = ','.join(self.files[:3]) or '—'
        if len(self.files) > 3:
            files += f'(+{len(self.files) - 3})'
        return f'{self.steps} step · {tools} · {files} · {self.outcome}'


def message_of(event) -> Message:
    """事件 → 它承载的消息（`assistant/message` 的 data 是个 dict）。"""
    if event.type == 'assistant/message':
        return event.data['message']
    return event.data


def message_text(message: Message) -> str:
    """消息正文（只取 TextBlock；工具调用/结果另有渲染）。"""
    return '\n'.join(b.text for b in message.content if isinstance(b, TextBlock)).strip()


def is_checkpoint(event) -> bool:
    return event.type == 'user/message' and CHECKPOINT_MARK in message_text(message_of(event))


def shadowed_seqs(session: Session) -> tuple[int, ...]:
    """已被 `replace` 遮蔽的 surface 事件（= 曾经进过上下文、现在看不见的那些）。"""
    live = set(session.surface)
    return tuple(e.seq for e in session.events if e.type in SURFACE and e.seq not in live)


def _tool_arguments(event) -> dict:
    raw = event.data.get('arguments', '') if isinstance(event.data, dict) else ''
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_turns(session: Session) -> tuple[TurnInfo, ...]:
    """按回合聚合出 L1 需要的行；足迹全部从日志现算（纯函数）。"""
    live = set(session.surface)
    turns: list[TurnInfo] = []
    current: dict | None = None

    def flush() -> None:
        if current is None:
            return
        seqs = current['seqs']
        turns.append(TurnInfo(
            turn=current['turn'], seqs=tuple(seqs),
            user_texts=tuple(current['users']), steps=current['steps'],
            tools=tuple(dict.fromkeys(current['tools'])),
            files=tuple(dict.fromkeys(current['files'])),
            commands=tuple(dict.fromkeys(current['commands'])),
            outcome=current['outcome'], conclusion=current['conclusion'],
            shadowed=bool(seqs) and not any(seq in live for seq in seqs),
        ))

    for event in session.events:
        data = event.data if isinstance(event.data, dict) else {}
        if event.type == 'turn/start':
            flush()
            current = {'turn': data.get('turn', 0), 'seqs': [], 'users': [], 'steps': 0,
                       'tools': [], 'files': [], 'commands': [], 'outcome': 'running',
                       'conclusion': ''}
            continue
        if current is None:
            continue
        if event.type == 'turn/end':
            current['outcome'] = str(data.get('reason', '?'))
        elif event.type == 'step/start':
            current['steps'] += 1
        elif event.type in SURFACE:
            current['seqs'].append(event.seq)
            if event.type == 'user/message' and not is_checkpoint(event):
                current['users'].append(' '.join(message_text(message_of(event)).split()))
            elif event.type == 'assistant/message':
                text = ' '.join(message_text(message_of(event)).split())
                if text:
                    current['conclusion'] = text
        elif event.type == 'tool/call':
            current['tools'].append(str(data.get('name', '?')))
            arguments = _tool_arguments(event)
            for key in ('file_path', 'path', 'dir_path'):
                value = arguments.get(key)
                if isinstance(value, str) and value:
                    current['files'].append(value)
                    break
            command = arguments.get('command')
            if isinstance(command, str) and command:
                current['commands'].append(' '.join(command.split())[:60])
    flush()
    return tuple(turns)


# ---------- L1 用户话清单 ----------

def render_manifest(turns: tuple[TurnInfo, ...] | list[TurnInfo], *,
                    max_chars_per_line: int = 120, with_footprint: bool = True,
                    include_shadowed: bool = True, conclusion_chars: int = 60,
                    with_commands: bool = False) -> str:
    """L1：用户话清单（导航入口）。

    `with_commands` 把 shell 命令原文接进食足——实测 14/44 个回合只有 bash、
    没有文件足迹，那时足迹行几乎无信息量；命令原文是"跑 pytest 那次""git 操作那次"
    唯一能靠的信号。默认关（清单更短），由工具参数打开。

    三条渲染规则都是**实测**出来的（真会话 59 回合 / 4 道评测题，见
    `docs/notes/implemented/bug-fix/2026-09-29-silent-render-truncation.md`）：

    ① **截断必须明说，并给出读全文的坐标**。实测长用户话 1,269 字符 → 只留 120（**9%**），
       而被截掉的后半段恰好是"不新增第三方依赖""测试统一用 pytest"这类**约束**——
       设计里写着"用户话是约束来源，绝不能因为压缩就从目录里消失"，结果它没消失却只剩 9%，
       而且**只补一个 `…`**：这与 L2 的规则（超限要明说被截断）自相矛盾。所以现在补
       `（共 N 字，用 read_turn(turn=T) 看全文）`——**不做关键词筛选**（不搞相关性排序，
       让模型自己导航），只把"这里被砍了、去哪看"讲清楚。
    ② **重复的真人发言要标出来**（实测真会话 3 组/73 条）：导航时两条一模一样的行没法区分
       是哪一次；**不删行**（用户话一条都不能消失），只在首次标"另有 N 次同句"、后续标出处。
    ③ **没有文本结论的回合不能只留一个 `—`**（实测 2/59）：`—` 同时兼任"字段为空"和
       "确实没有结论"两种含义，模型没法判断是哪种，改为显式说明。
    """
    counts: dict[str, int] = {}
    first_turn: dict[str, int] = {}
    for info in turns:
        for text in info.user_texts:
            counts[text] = counts.get(text, 0) + 1
            first_turn.setdefault(text, info.turn)

    lines: list[str] = []
    for info in turns:
        if not info.user_texts:
            continue
        if info.shadowed and not include_shadowed:
            continue
        flags = ' [已压缩]' if info.shadowed else ''
        if with_footprint:
            summary = info.conclusion[:conclusion_chars]
            if len(info.conclusion) > conclusion_chars:
                summary += '…'
            foot = info.footprint()
            if with_commands and info.commands:
                foot += f' · cmd:{info.commands[0][:60]}'
            # 规则③：`—` 分不清"字段为空"与"确实没有结论"
            lines.append(f'[{info.turn:>3}] {summary or "（无文本结论）"} · {foot}{flags}')
        for index, text in enumerate(info.user_texts):
            body = text
            if len(text) > max_chars_per_line:
                # 规则①：明说被截断 + 给出坐标（回合号就是 L2 的入参）
                body = (f'{text[:max_chars_per_line]}…'
                        f'（共 {len(text)} 字，用 read_turn(turn={info.turn}) 看全文）')
            tags = '(插队) ' if index else ''
            if counts[text] > 1:
                # 规则②：首次标"还有几次"，后续标出处；两个方向都能对上
                tags += (f'（另有 {counts[text] - 1} 次同句）' if first_turn[text] == info.turn
                         else f'（与回合 {first_turn[text]} 同句）')
            lines.append(f'      用户：{tags}{body}')
    return '\n'.join(lines)


# ---------- L2 回合明细 ----------

def render_message(message: Message, *, limit: int | None = None) -> list[str]:
    """一条消息 → 可读行（工具调用/结果单独成行，**不带 call_id 噪音**）。

    `limit=None`（默认）**不截断**：截断只允许发生在**一个地方**——`render_turn` 的分页。
    为什么改（实测，见 `docs/notes/implemented/bug-fix/2026-09-29-silent-render-truncation.md`）：这里原本对文本/工具结果
    单块截 1500 字符、工具调用参数截 300 字符，而且**只补一个 `…`**——两条后果：
    ① 与"超限要明说被截断"的规则冲突；② 审计里 6 条"读不回来"的事实中 **5 条正是被这个
    单块上限挡住的**（内容明明在回合预算之内，却在块级别被静默砍掉，而模型**没有任何办法**
    把它读回来：`step` 过滤和 `offset` 分页都作用在回合层，块内的后半截根本不存在于输出里）。
    """
    out: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            text = ' '.join(block.text.split())
            if text:
                out.append(text if limit is None else text[:limit])
        elif isinstance(block, ToolCallBlock):
            args = ' '.join(block.arguments.split())
            out.append(f'tool_call {block.name}({args if limit is None else args[:300]})')
        elif isinstance(block, ToolResultBlock):
            body = ' '.join(block.content.split())
            tag = ' [error]' if block.is_error else ''
            out.append(f'tool_result{tag} '
                       + (body if limit is None else body[:limit]))
    return out or ['(empty)']


def render_turn(session: Session, turn: int, *, step: int | None = None,
                offset: int = 1, max_chars: int = 12000,
                scope: str = 'surface') -> str:
    """L2：某回合的原文（**按行分页**；总量与位置都明说）。

    实测回合尺寸：中位 1,340 字符 / p95 102,801 / 最大 219,323——所以不能无条件把整回合
    倒给模型（一次就是几万 token）。现在的做法与 `read_file` 同一套心智：

    - 第一行是回合头 `[turn N · K 步 · M 条事件]`；
    - `offset` = **从第几行开始**（1 起，默认 1）；`max_chars` = 本次最多回多少字符
      （**在行边界处停**，不切半行）；
    - 显示不全时**明说**：`…（本回合共 X 行 / Y 字符，本次显示第 A–B 行；继续用 offset=B+1，
      或用 step=S 精读某一步）`——继续读的坐标和可用的 step **都给**。

    为什么从"事件数 + 字符双上限"改成"行分页 + 字符预算"（2026-09 实测）：
    ① 旧的 `max_events=80` 让**第 81 条事件之后的内容无法读回**（审计里 1 条事实就是被它挡的）；
    ② 旧的单块 1500 字符上限**静默**砍掉块内后半段，而分页/step 都救不回来（5 条事实）；
    ③ 现在"截断"只有一处、且必须自报家门——与 L1 的规则①统一。

    `scope='trace'` 额外带上该回合的**推理痕迹**（`assistant/reasoning`）——它能解释
    "当时为什么这么决定"，是读回时的上下文；默认关（痕迹不是模型曾经见过的内容，
    体积也不小）。`request/header` 里的 system 快照**不**回灌：它很长、而且随时可重建。
    """
    trace = scope == 'trace'
    # `session.events` 每次访问都拷贝整个元组（见 `state/session.py`），而下面要扫两遍
    # ——所以先存进局部变量，别让它变成一遍一次拷贝
    events = session.events

    def keep(event, cur_turn: int, cur_step: int) -> bool:
        """这个事件属于本次渲染吗（回合 / step / 检索面三重过滤，两遍共用同一判据）。"""
        if cur_turn != turn:
            return False
        if step is not None and cur_step != step:
            return False
        if event.type in SURFACE:
            return not is_checkpoint(event)   # checkpoint 是合成 user/message，不是原文
        return trace and event.type == 'assistant/reasoning'

    # 第一遍：本回合存在吗、有哪些 step。**不是**为了"预览内容"——而是因为截断时我们
    # 叫模型"用 step 精读"，却从不告诉它有哪些 step 可选，那它只能猜（旧日志里
    # `step/start` 还可能整段缺失，`step` 一律是 0）。给不出目录的截断提示是空头支票。
    steps: list[int] = []
    matched = 0
    current_turn = 0
    current_step = 0
    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        if event.type == 'turn/start':
            current_turn = data.get('turn', 0)
        elif event.type == 'step/start':
            current_step = data.get('step', 0)
        if keep(event, current_turn, current_step):
            matched += 1
            if current_step not in steps:
                steps.append(current_step)
    if not matched:
        return ''   # 空串 = "没有这个回合"，调用方据此报 is_error（别用回合头冒充内容）

    header = (f'[turn {turn} · {len(steps)} 步 · {matched} 条事件'
              + (f' · 只看 step {step}' if step is not None else '') + ']')
    lines: list[str] = [header]
    current_turn = 0
    current_step = 0
    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        if event.type == 'turn/start':
            current_turn = data.get('turn', 0)
        elif event.type == 'step/start':
            current_step = data.get('step', 0)
        # 顺序要紧：**step 过滤必须在 trace 分支之前**。放后面时 `step=N` 会把该回合
        # 所有步的推理都吐出来（一步最多上万字符）。这里**不再**做字符/事件截断——
        # 有界性由末尾的分页负责（一处截断、且必须自报家门）。
        if not keep(event, current_turn, current_step):
            continue
        if event.type == 'assistant/reasoning':
            body = ' '.join(str(data.get('reasoning', '')).split())
            if body:
                lines.append(f'[turn {turn} · step {current_step} · reasoning] {body}')
            continue
        kind = {'user/message': 'user', 'assistant/message': 'assistant',
                'tool/result': 'tool_result'}[event.type]
        for body in render_message(message_of(event)):
            lines.append(f'[turn {turn} · step {current_step} · {kind}] {body}')

    total_chars = sum(len(line) + 1 for line in lines)
    start = max(1, offset) - 1                     # 入参是 1 起的行号
    if start >= len(lines):
        # 起点越界：**明说**总行数（模型据此修正参数），不要假装成功
        return (f'{header}\n…（offset={offset} 超出范围：本回合共 {len(lines)} 行 / '
                f'{total_chars} 字符；offset 从 1 起，用 offset=1 从头读）')
    shown: list[str] = []
    used = 0
    for line in lines[start:]:
        if shown and used + len(line) + 1 > max_chars:
            break
        shown.append(line)
        used += len(line) + 1
    end = start + len(shown)
    if end < len(lines):
        shown.append(f'…（本回合共 {len(lines)} 行 / {total_chars} 字符，本次显示第 '
                     f'{start + 1}–{end} 行；继续用 offset={end + 1}，或用 step 精读其中一步：'
                     + ', '.join(str(s) for s in steps) + '）')
    return '\n'.join(shown)


def load_session_log(path: Path, session_id: str = '') -> Session:
    """把另一个会话的日志重放成 `Session`（召回其他会话时用）。"""
    session = Session(session_id or path.stem)
    for event in load_events(path):
        session.adopt(event)
    return session


def row_from_session(session: Session, default_workspace: str = '',
                     updated: float = 0.0) -> SessionRow:
    """当前会话的目录行（**增量投影**，不读盘、不重扫内存）。

    为什么必须增量：L0 每个请求都要求值一次，而当前会话**每个请求都在长**——所以
    任何"整表记忆化"都等于每次失效。实测（54 MB / 20 万事件）整表遍历 ≈ 120 ms/请求，
    而增量只走新事件（稳态≈0）。
    做法：按会话对象记一个游标 `indexed`，只处理 `events[indexed:]`，把用户话数 /
    回合数 / 压缩数 / 标题 / 首个用户话 / 工作区累加进去。事件只追加，所以游标只前进。
    （`session.workspace()` 是**反向全扫**，在"没有这条事件的旧会话"上等于每请求一次
    全遍历——所以工作区也在增量游标里记，不调它。）
    """
    key = id(session)
    counter = _LIVE_CACHE.get(key)
    total = session.event_count
    # `id()` 会在对象被回收后**被复用**，所以光比对 id 会撞车：一个已死会话的游标
    # 被记在新会话名下 → 新会话的目录行凭空继承别人的用户话数/回合数。存下 `session.id`
    # 做身份核对，对不上就重建。
    # （`counter.indexed > total` 是另一条独立的防护：日志本该只追加，真变短了说明
    #  重放/换文件之类的意外——游标越界时必须重建，不能拿着它去切片。）
    if (counter is None or counter.session_id != session.id
            or counter.indexed > total):
        counter = _LiveCounter(session_id=session.id)
        _LIVE_CACHE[key] = counter
    # `events_since` 只拷贝新增段；**不能**用 `session.events`——那个 property 每次
    # 访问都拷贝整个元组，在 20 万事件的会话上就是 ~20 ms/请求（实测踩到过）
    for event in session.events_since(counter.indexed):
        if event.type == 'user/message':
            # checkpoint 是 `surface_op='replace'` 写的合成 user/message，不算"用户话"
            if not is_checkpoint(event):
                counter.user_messages += 1
                if not counter.first_user:
                    counter.first_user = ' '.join(
                        message_text(message_of(event)).split())[:60]
        elif event.type == 'turn/start':
            counter.turns += 1
        elif event.type == 'compaction/start':
            counter.compactions += 1
        elif event.type == 'session/title' and isinstance(event.data, dict):
            counter.title = str(event.data.get('title') or counter.title)
        elif event.type == 'session/workspace' and isinstance(event.data, dict):
            raw = str(event.data.get('workspace') or '').strip()
            if raw:
                try:
                    counter.workspace = str(normalize_recorded_workspace(raw))
                except OSError:
                    counter.workspace = raw
    counter.indexed = total

    workspace = counter.workspace or default_workspace
    return SessionRow(
        session_id=session.id, events=total, updated=updated,
        title=counter.title, title_source='',
        summary=counter.title or counter.first_user or '(empty)',
        workspace=workspace, workspace_ok=Path(workspace).is_dir() if workspace else False,
        user_messages=counter.user_messages, turns=counter.turns,
        compactions=counter.compactions,
    )


def session_index_text(session: Session, sessions_dir: Path, default_workspace: str = '',
                       limit: int = 5, hint: bool = True) -> str | None:
    """L0 的完整构造：扫**别的**会话 + 当前会话走内存 → 渲染。

    给 `app/factory.py` 的 runtime status 贡献者用（每请求求值一次）：所以
    ① 别的会话走 stat 键控缓存（文件不变就只做 stat）；
    ② **当前会话排除在磁盘扫描之外**、由**增量游标**投影（它每请求都在长）。
    实测（54 MB / 20 万事件会话）：求值 ≈ 0.3 ms；曾在这里踩到三处 O(n)
    ——整表记忆化、`session.events` 的整表拷贝、以及 `session.workspace()` 的反向全扫。

    `hint=False` 去掉"压缩过 → 可读回"那句（提示词供给面 A/B 实验的一个因子）。
    """
    current_row = row_from_session(session, default_workspace)
    rows = list(scan_sessions(sessions_dir, default_workspace, exclude=session.id))
    rows.append(current_row)
    # 用 `current_row.workspace`（增量游标里记着，O(1)）而不是 `session.workspace()`：
    # 后者是**反向全扫**，在没有那条事件的旧会话上等于每请求扫完整个日志（实测 ~10 ms）
    return render_session_index(
        rows, current_id=session.id, workspace=current_row.workspace, limit=limit,
        hint=hint,
    )
