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

## 三条设计约束（都有实测依据，见 `CONTEXT_BUDGET_DESIGN.md`）

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
_SCAN_CACHE: dict[str, tuple[tuple[int, int], SessionRow]] = {}


def _scan_one(path: Path, default_workspace: str) -> SessionRow:
    """逐行扫一个日志：只做行内子串匹配 + 命中行才 `json.loads`（实测快 6 倍）。

    统计的三类事件都能靠 `"type": "..."` 干净地数出来（JSONL 一行一事件）。
    """
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _SCAN_CACHE.get(str(path))
    if cached is not None and cached[0] == key:
        return cached[1]

    events = user_messages = turns = compactions = 0
    summary = title = title_source = workspace = ''
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            events += 1
            if not summary and '"type": "user/message"' in line:
                try:
                    payload = (json.loads(line).get('data') or {}).get('$message') or {}
                    for block in payload.get('content') or []:
                        if '$text' in block:
                            summary = block['$text'][:60]
                            break
                except (TypeError, ValueError, KeyError):
                    pass
            if '"type": "user/message"' in line:
                user_messages += 1
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
    _SCAN_CACHE[str(path)] = (key, row)
    return row


def scan_sessions(sessions_dir: Path, default_workspace: str = '') -> list[SessionRow]:
    """扫会话目录（最近更新的在前）。**唯一实现**：web 的列表也走这里。"""
    rows: list[SessionRow] = []
    for path in sorted(sessions_dir.glob('*.jsonl')):
        try:
            rows.append(_scan_one(path, default_workspace))
        except OSError as error:      # 单个文件读不了不该让整张列表消失
            log.warning('cannot scan session %s: %s', path, error)
    rows.sort(key=lambda row: row.updated, reverse=True)
    return rows


def render_session_index(rows: list[SessionRow], *, current_id: str = '',
                         workspace: str = '', limit: int = 5) -> str | None:
    """L0 会话目录（运行时状态贡献者的原文）；没有可列的会话返回 None。

    **只列同一工作区的会话**：每个对话有自己的工作区（`agent.md` §10），跨工作区列表
    等于把别的项目的会话念给模型（对齐 DSH 的 exact-cwd 授权）。旧会话没记录工作区时
    跟随宿主默认，与工作区选择策略同一判据。
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
    """
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
            lines.append(f'[{info.turn:>3}] {summary or "—"} · {foot}{flags}')
        for index, text in enumerate(info.user_texts):
            body = text[:max_chars_per_line] + ('…' if len(text) > max_chars_per_line else '')
            tag = '(插队) ' if index else ''
            lines.append(f'      用户：{tag}{body}')
    return '\n'.join(lines)


# ---------- L2 回合明细 ----------

def render_message(message: Message, *, limit: int = 1500) -> list[str]:
    """一条消息 → 可读行（工具调用/结果单独成行，**不带 call_id 噪音**）。"""
    out: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            text = ' '.join(block.text.split())
            if text:
                out.append(text[:limit] + ('…' if len(text) > limit else ''))
        elif isinstance(block, ToolCallBlock):
            args = ' '.join(block.arguments.split())
            out.append(f'tool_call {block.name}({args[:300]})')
        elif isinstance(block, ToolResultBlock):
            body = ' '.join(block.content.split())
            tag = ' [error]' if block.is_error else ''
            out.append(f'tool_result{tag} {body[:limit]}' + ('…' if len(body) > limit else ''))
    return out or ['(empty)']


def render_turn(session: Session, turn: int, *, step: int | None = None,
                max_events: int = 80, max_chars: int = 12000,
                scope: str = 'surface') -> str:
    """L2：某回合的原文（有界；超限明确告知被截断）。

    实测回合尺寸：中位 1,340 字符 / p95 102,801 / 最大 219,323——所以**不能**无条件
    把整回合倒给模型（一次就是几万 token）。`step` 为空时给"回合头 + 步骤目录 + 装得下
    的步"，超限就明说被截断、让模型按 `step` 精读。
    """
    lines: list[str] = []
    total = 0
    truncated = False
    current_turn = 0
    current_step = 0
    for event in session.events:
        data = event.data if isinstance(event.data, dict) else {}
        if event.type == 'turn/start':
            current_turn = data.get('turn', 0)
        elif event.type == 'step/start':
            current_step = data.get('step', 0)
        if current_turn != turn or event.type not in SURFACE:
            continue
        if step is not None and current_step != step:
            continue
        if is_checkpoint(event):
            continue
        if len(lines) >= max_events or total >= max_chars:
            truncated = True
            break
        kind = {'user/message': 'user', 'assistant/message': 'assistant',
                'tool/result': 'tool_result'}[event.type]
        for body in render_message(message_of(event)):
            line = f'[turn {turn} · step {current_step} · {kind}] {body}'
            lines.append(line)
            total += len(line)
    if truncated:
        lines.append('…（本回合内容超过上限被截断；用 step 参数精读某一步）')
    return '\n'.join(lines)


def load_session_log(path: Path, session_id: str = '') -> Session:
    """把另一个会话的日志重放成 `Session`（召回其他会话时用）。"""
    session = Session(session_id or path.stem)
    for event in load_events(path):
        session.adopt(event)
    return session


def row_from_session(session: Session, default_workspace: str = '',
                     updated: float = 0.0) -> SessionRow:
    """当前会话的目录行（**从内存算**，不读盘）。

    只在"当前会话还没落盘"（测试、或宿主没 `bind_store`）时兜底用；正常路径下磁盘
    扫描已经把当前会话数进去了，不必额外遍历一遍内存事件（那可是十几万条）。
    """
    user_messages = turns = compactions = 0
    for event in session.events:
        if event.type == 'user/message':
            user_messages += 1
        elif event.type == 'turn/start':
            turns += 1
        elif event.type == 'compaction/start':
            compactions += 1
    title = ''
    for event in session.events:
        if event.type == 'session/title' and isinstance(event.data, dict):
            title = str(event.data.get('title') or title)
    first = ''
    for turn in build_turns(session):
        if turn.user_texts:
            first = turn.user_texts[0][:60]
            break
    workspace = session.workspace() or default_workspace
    return SessionRow(
        session_id=session.id, events=len(session.events), updated=updated,
        title=title, title_source='', summary=title or first or '(empty)',
        workspace=workspace, workspace_ok=Path(workspace).is_dir() if workspace else False,
        user_messages=user_messages, turns=turns, compactions=compactions,
    )


def session_index_text(session: Session, sessions_dir: Path, default_workspace: str = '',
                       limit: int = 5) -> str | None:
    """L0 的完整构造：扫目录 + 兜底当前会话 → 渲染。

    给 `app/factory.py` 的 runtime status 贡献者用（每请求求值一次，所以扫描必须
    命中 stat 缓存；未落盘的当前会话才走内存兜底）。
    """
    rows = list(scan_sessions(sessions_dir, default_workspace))
    if not any(row.session_id == session.id for row in rows):
        rows.append(row_from_session(session, default_workspace))
    return render_session_index(
        rows, current_id=session.id,
        workspace=session.workspace() or default_workspace, limit=limit,
    )
