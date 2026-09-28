"""L0/L1/L2 三层投影（评测版；落地时搬进 `my_coder/state/recall.py`）。

**复用产品自己的投影代码**：`persistence.load_events` + `Session.adopt` 重建
surface 投影与类型化块（`TextBlock` / `ToolCallBlock` / `ToolResultBlock`），
不在这里手写第二套 JSON 解析（否则值层一改，评测就悄悄失真）。

- **L0 会话层**：会话目录（用户话数/回合数/压缩次数）—— 产品里放状态栏
- **L1 用户话清单**：全部**真人发言** + 每个回合的结构足迹 —— 导航入口
- **L2 完整层**：某个回合的原文事件（按需、有界）

约束（见 `CONTEXT_BUDGET_DESIGN.md` §4）：
- 检索面 = 曾经进过模型上下文的事件（surface 全集，**含被 replace 遮蔽的**）；
- 清单只收真人发言：排除 checkpoint 合成消息（它们也是 `user/message`）；
- 足迹（step 数 / 工具 / 文件 / 结局 / 结论摘录）是**导航必需项**：导航题从 agent
  侧内容提问，模型只能靠足迹把"用户说了什么"和"我现在要找什么"桥起来。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:          # 让 eval 脚本能 import my_coder
    sys.path.insert(0, str(REPO))

from my_coder.state.session import Session                       # noqa: E402
from my_coder.values.messages import (Message, TextBlock,        # noqa: E402
                                      ToolCallBlock, ToolResultBlock)
from my_coder.values.persistence import load_events              # noqa: E402

SESSIONS = REPO / '.sessions'
SURFACE = ('user/message', 'assistant/message', 'tool/result')
CHECKPOINT_MARK = '<compacted-summary>'


# ---------- 载入与投影 ----------

def load_session(path: Path) -> Session:
    """重放日志成 Session：`surface` 就是"模型现在看得见什么"。"""
    session = Session(path.stem)
    for event in load_events(path):
        session.adopt(event)
    return session


def message_of(event) -> Message:
    data = event.data
    if event.type == 'assistant/message':
        return data['message']
    return data


def message_text(message: Message) -> str:
    """消息的正文（只取 TextBlock；工具调用/结果另有渲染）。"""
    return '\n'.join(b.text for b in message.content if isinstance(b, TextBlock)).strip()


def is_checkpoint(event) -> bool:
    return event.type == 'user/message' and CHECKPOINT_MARK in message_text(message_of(event))


def shadowed_seqs(session: Session) -> tuple[int, ...]:
    live = set(session.surface)
    return tuple(e.seq for e in session.events if e.type in SURFACE and e.seq not in live)


# ---------- 回合聚合 ----------

@dataclass(frozen=True)
class TurnInfo:
    turn: int
    seqs: tuple[int, ...]
    user_texts: tuple[str, ...]        # 该回合的**真人**发言（含插队）
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


def _tool_args(event) -> dict:
    import json
    raw = event.data.get('arguments', '') if isinstance(event.data, dict) else ''
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_turns(session: Session) -> list[TurnInfo]:
    """按回合聚合出 L1 需要的行（足迹全部从日志现算）。"""
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
            shadowed=bool(seqs) and not any(s in live for s in seqs),
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
            name = str(data.get('name', '?'))
            current['tools'].append(name)
            arguments = _tool_args(event)
            for key in ('file_path', 'path', 'dir_path'):
                value = arguments.get(key)
                if isinstance(value, str) and value:
                    current['files'].append(value)
                    break
            command = arguments.get('command')
            if isinstance(command, str) and command:
                current['commands'].append(' '.join(command.split())[:60])
    flush()
    return turns


# ---------- L0 ----------

def session_row(path: Path, *, current: str = '') -> dict:
    session = load_session(path)
    turns = build_turns(session)
    title = ''
    for event in session.events:
        if event.type == 'session/title':
            title = str(event.data.get('title', ''))
    return {
        'session': path.stem, 'title': title, 'turns': len(turns),
        'user_count': sum(len(t.user_texts) for t in turns),
        'compactions': sum(1 for e in session.events if e.type == 'compaction/start'),
        'mtime': path.stat().st_mtime,
        'first_user': next((t.user_texts[0] for t in turns if t.user_texts), '')[:40],
        'is_current': path.stem == current,
    }


def render_session_list(rows: list[dict], limit: int = 5) -> str:
    """L0：会话目录（产品里放状态栏）。"""
    ordered = sorted(rows, key=lambda r: r['mtime'], reverse=True)
    keep = [r for r in ordered[:limit] if r['user_count']]
    lines = ['<context_sessions>']
    for row in keep:
        mark = '  ← 当前' if row['is_current'] else ''
        compacted = f' · 已压缩×{row["compactions"]}' if row['compactions'] else ''
        lines.append(f'[{row["session"]}] {row["user_count"]} 条用户话 · '
                     f'「{row["first_user"]}」 · {row["turns"]} 回合{compacted}{mark}')
    hidden = sum(1 for r in ordered if r not in keep)
    if hidden:
        lines.append(f'…（另有 {hidden} 个更早会话，用 session_manifest 查看）')
    lines.append('</context_sessions>')
    return '\n'.join(lines)


# ---------- L1 ----------

def render_manifest(turns: list[TurnInfo], *, max_chars_per_line: int = 120,
                    with_footprint: bool = True, include_shadowed: bool = True,
                    conclusion_chars: int = 60) -> str:
    """L1：用户话清单（导航入口）。"""
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
            lines.append(f'[{info.turn:>3}] {summary or "—"} · {info.footprint()}{flags}')
        for index, text in enumerate(info.user_texts):
            body = text[:max_chars_per_line] + ('…' if len(text) > max_chars_per_line else '')
            tag = '(插队) ' if index else ''
            lines.append(f'      用户：{tag}{body}')
    return '\n'.join(lines)


# ---------- L2 ----------

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
                max_events: int = 80, max_chars: int = 12000) -> str:
    """L2：某回合的原文（有界；超限明确告知被截断）。"""
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
