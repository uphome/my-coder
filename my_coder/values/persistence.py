"""JSONL 持久化：追加写、每行一条事件；加载即重放。

恢复 = 重放，零额外代码：adopt 重建 surface 投影，Inbox 构造时
重放 spliced 事件恢复队列，request_header 从日志恢复上次模型配置。

**第一行是会话头**（`SessionHeader`，不是事件）：它带 `format_version`，
给"日志格式"一个锚点。读的时候按方向判定（见 `check_compatible`）：
日志比读取者新 → 拒绝；旧 → 走迁移链；相等 → 直接读。没有头的老文件
按 `LEGACY_SESSION_FORMAT_VERSION` 处理（本机制落地前的文件与 v1 事件形状相同）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .messages import (
    KNOWN_SESSION_EVENT_TYPES,
    SESSION_FORMAT_VERSION,
    SessionEvent,
    SessionHeader,
    event_from_json,
    event_to_json,
    is_session_header_line,
    session_header_from_json,
    session_header_to_json,
)


class SessionFormatUnsupportedError(Exception):
    """日志格式比当前读取者**新**——拒绝重建。

    刻意与"损坏"分开（用独立异常类型）：文件什么都没坏，只是写它的运行时比
    读它的新。消息里给"升级"方向与原始文件路径，让用户至少能拿文本编辑器看。
    """

    def __init__(self, path: Path, found: int, supported: int) -> None:
        self.path = path
        self.found = found
        self.supported = supported
        super().__init__(
            f'session log {path} was written by a newer runtime '
            f'(format_version {found} > supported {supported}) — '
            'upgrade this runtime to read it; the file itself is intact',
        )


class UnknownSessionEventError(Exception):
    """日志里有本运行时**不认识**、且没标 `ignorable` 的事件——拒绝重建。

    未知事件默认"必读"：忘标记 → 过度拒绝（麻烦）远好于默认忽略 →
    静默恢复出一份被掏空的会话（安全事故）。
    """

    def __init__(self, path: Path, event_type: str, seq: int) -> None:
        self.path = path
        self.event_type = event_type
        self.seq = seq
        super().__init__(
            f'session log {path} contains unknown event type {event_type!r} at seq {seq}: '
            'this runtime would silently drop it — upgrade, or mark it ignorable',
        )


def save_event(path: Path, event: SessionEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(event_to_json(event), ensure_ascii=False) + '\n')


def save_header(path: Path, header: SessionHeader) -> None:
    """写会话头（新建会话的第一行）。**只在文件不存在时写**——头是文件级元数据，
    追加第二条头没有意义，反而会让读取端分不清哪条有效。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(session_header_to_json(header), ensure_ascii=False) + '\n')


def read_header(path: Path) -> SessionHeader | None:
    """读第一行；不是会话头（老文件）返回 None。

    只读一行——会话列表要做行内快扫，不能为了拿版本号把整个日志读进来。
    """
    if not path.exists():
        return None
    with path.open(encoding='utf-8') as handle:
        first = handle.readline()
    if not first.strip():
        return None
    try:
        data = json.loads(first)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not is_session_header_line(data):
        return None
    return session_header_from_json(data)


def declared_version(path: Path) -> int:
    """日志声称的格式版本：有头用头，没头按 v0（本机制落地前的老文件）。"""
    header = read_header(path)
    return header.version if header is not None else 0


def check_compatible(path: Path, supported: int = SESSION_FORMAT_VERSION) -> int:
    """方向感知判定；返回该文件的版本。拒绝时抛（见两个异常类型）。

    - 相等 → 返回该版本
    - 日志更新 → `SessionFormatUnsupportedError`
    - 日志更旧 → 返回旧版本号（调用方决定是否走迁移；当前 v0 与 v1 事件形状相同，
      所以无需迁移，直接读）
    """
    version = declared_version(path)
    if version > supported:
        raise SessionFormatUnsupportedError(path, version, supported)
    return version


def load_events(path: Path, known_types: frozenset[str] | None = KNOWN_SESSION_EVENT_TYPES,
                supported: int = SESSION_FORMAT_VERSION) -> list[SessionEvent]:
    """读回全部事件（跳过会话头）。

    两道守卫（都可关闭，`known_types=None` 时不查未知事件）：
    1. **方向感知拒绝**：日志比读取者新 → 抛 `SessionFormatUnsupportedError`；
    2. **未知事件守卫**：类型不在 `known_types` 且 `ignorable != True` →
       抛 `UnknownSessionEventError`；标了 `ignorable` 的未知事件跳过、其余照常重建。

    守卫**默认开着**：未知事件默认"必读"——忘标记 → 过度拒绝（麻烦）远好于
    默认忽略 → 静默恢复出一份被掏空的会话（安全事故）。
    """
    if not path.exists():
        return []
    check_compatible(path, supported=supported)
    events: list[SessionEvent] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if is_session_header_line(data):
            continue
        if known_types is not None and data.get('type') not in known_types:
            if data.get('ignorable') is True:
                continue
            raise UnknownSessionEventError(path, str(data.get('type')), int(data.get('seq', -1)))
        events.append(event_from_json(data))
    return events
