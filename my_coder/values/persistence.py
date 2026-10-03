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
import re
from array import array
from bisect import bisect_left
from dataclasses import dataclass
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


@dataclass(frozen=True)
class EventIndex:
    """事件索引（issue #46 ③ 阶段 1）：只记"每条事件在哪、是什么"，**不建 payload**。

    为什么需要：整份重放要造 42.5 万个 `SessionEvent`（带 payload），实测常驻 ≈ 文本量的
    **3.4×**（217 MB / 峰值 590 MB）。而多数消费者只要"类型 + 位置"：判回合边界、数消息、
    按 seq 定点取一条原文。索引让"打开一个大会话"从"整份对象图"变成"几条紧凑数组"。

    **刻意用 `array` 而不是 `tuple`**：这是**视图**不是值对象（不放进消息/事件），
    42.5 万条下 tuple 光指针与整数对象就要 ~60 MB，array 只要 ~7 MB——紧凑正是它的全部意义。
    不可变语义由"日志变了就重建，绝不原地改"保证。

    `types` 用 tuple 存**驻留过的短字符串**（几十种类型共享同一批对象），所以它只花指针钱。
    """

    path: Path
    seqs: array            # 每条事件在日志里记的 seq（`ignorable` 被跳过的那些不占位）
    types: tuple[str, ...]
    offsets: array         # 该行在文件里的**字节**起点（二进制逐行统计，不受编码影响）
    lengths: array         # 该行的字节长度
    # `surface_op` / `shadowed` 是**顶层字段**（不在 payload 里），所以索引足以重建
    # **surface**（= 模型可见顺序），不必为每条事件造 payload——这正是冷热分层能省内存的机制：
    # 投影（surface）与"哪些区间被压缩遮蔽"都能只靠索引算出来，payload 只按需取。
    surface_ops: tuple[str | None, ...] = ()
    shadowed: tuple[tuple[int, int] | None, ...] = ()

    def __len__(self) -> int:
        return len(self.types)


# 行首**信封**的类型字段：只为"这条要不要跳过"做一次**廉价**判断（不解析 JSON）。
#
# **必须锚定**：写入端 `event_to_json` 的字段次序固定为 `seq → time → type → data`，
# 而 `data`（payload）就在同一行里——若只搜 `"type": "…"`，payload 里出现同样文本就会
# 被误判（比如某条 `tool/result` 的正文里贴了会话日志片段），于是一条**真事件**被当成
# "要跳过的帧"整行丢掉 ⇒ **静默少读**。锚定 `^{"seq":…,"time":…,"type":…` 之后，
# 匹配只可能落在信封上，payload 怎么长都影响不到它。
# 超长字段（>240 字节才出现 type）会让快路失效 → 退化为正常解析，**只慢不错**。
_ENVELOPE_TYPE_RE = re.compile(
    r'^\{"seq":\s*\d+,\s*"time":\s*[0-9.eE+-]+,\s*"type":\s*"([^"]+)"')


def scan_index(path: Path, known_types: frozenset[str] | None = KNOWN_SESSION_EVENT_TYPES,
               supported: int = SESSION_FORMAT_VERSION,
               skip_types: tuple[str, ...] = ()) -> EventIndex:
    """扫一遍日志，只建索引（不解析成 `SessionEvent`）。

    与 `load_events` **同一套守卫**（方向感知拒绝 + 未知事件守卫），差别只有一处：
    它把每条事件的 payload 丢掉，只留 `(seq, type, offset, length, surface_op, shadowed)`。
    未知且不可忽略的事件仍然当场抛错——**不因为"反正不建对象"就放松守卫**。

    `skip_types` 里的事件**连 JSON 都不解析**：先用正则从行首取 `type`，命中就直接跳过。
    实测意义：老日志里流式帧占 98.9% 的行，而逐行 `json.loads` 是打开会话的全部耗时来源
    （42.5 万行约 9 s）——跳过它们之后，解析量降到 1.1%。守卫不受影响：**只有已知类型**
    才走这条快路，未知类型仍然解析出来判 `ignorable`（该拒就拒）。
    """
    seqs = array('q')
    offsets = array('q')
    lengths = array('i')
    types: list[str] = []
    surface_ops: list[str | None] = []
    shadowed: list[tuple[int, int] | None] = []
    if not path.exists():
        return EventIndex(path=path, seqs=seqs, types=(), offsets=offsets, lengths=lengths,
                          surface_ops=(), shadowed=())
    check_compatible(path, supported=supported)
    skipped = set(skip_types)
    with path.open('rb') as handle:
        offset = 0
        for raw in handle:
            length = len(raw)
            # 只解前 240 **字节**；在字节边界切断可能切坏一个多字节字符，所以用
            # `errors='ignore'` 解码——它只影响判断用的那截片段，不影响真正的解析
            head = raw[:240].decode('utf-8', errors='ignore')
            match = _ENVELOPE_TYPE_RE.match(head)
            fast_type = match.group(1) if match else None
            # 快路：已知类型 + 要跳过 ⇒ 只花一次正则，不解析 JSON
            if fast_type is not None and fast_type in skipped:
                offset += length
                continue
            line = raw.decode('utf-8').strip()
            if line:
                data = json.loads(line)
                if is_session_header_line(data):
                    offset += length
                    continue
                type_ = data.get('type')
                if known_types is not None and type_ not in known_types:
                    if data.get('ignorable') is True:
                        offset += length
                        continue
                    raise UnknownSessionEventError(path, str(type_), int(data.get('seq', -1)))
                seqs.append(int(data.get('seq', -1)))
                types.append(str(type_))
                offsets.append(offset)
                lengths.append(length)
                surface_ops.append(data.get('surface_op'))
                shadowed_pair = data.get('shadowed')
                shadowed.append(tuple(shadowed_pair) if shadowed_pair else None)
            offset += length
    return EventIndex(path=path, seqs=seqs, types=tuple(types), offsets=offsets, lengths=lengths,
                      surface_ops=tuple(surface_ops), shadowed=tuple(shadowed))


def read_event_at(index: EventIndex, seq: int) -> SessionEvent:
    """按 seq 从索引**定点**读回一条事件（只解析那一行）。

    越界 / 索引与文件不一致 → **抛错**（fail-closed）。绝不静默返回空：
    那会把"读不到"伪装成"不存在"，而召回与压缩都靠"能读到任意 seq 的原文"成立。
    """
    position = bisect_left(index.seqs, seq)
    if position >= len(index.seqs) or index.seqs[position] != seq:
        raise KeyError(f'seq {seq} not in index of {index.path}')
    offset = index.offsets[position]
    length = index.lengths[position]
    with index.path.open('rb') as handle:
        handle.seek(offset)
        raw = handle.read(length)
    if len(raw) != length:
        raise ValueError(f'{index.path}: short read at offset {offset} (want {length}, got {len(raw)})')
    data = json.loads(raw.decode('utf-8'))
    if str(data.get('type')) != index.types[position]:
        raise ValueError(f'{index.path}: index/file mismatch at seq {seq} '
                         f'(index says {index.types[position]!r}, file says {data.get("type")!r})')
    return event_from_json(data)


def iter_events(index: EventIndex, skip_types: tuple[str, ...] = ()):
    """按索引**惰性**产出事件（要全部 payload、但可以流式处理时用它）。

    `skip_types` 里的类型**连 payload 都不解析**——用于"确定没人读"的整类事件：
    流式帧（`assistant/chunk` / `assistant/reasoning/chunk`）是纯痕迹，内容已完整落在
    `assistant/message`（正文 + 工具参数）与 `assistant/reasoning`（思维链全文）里，
    而老日志里它们占 **98.9%** 的事件。索引的 `offsets` 是顺序的，所以这里
    **只开一次文件、顺序向前 seek**——不重复 open（42.5 万条逐个 open 会慢一个量级）。
    """
    skipped = set(skip_types)
    with index.path.open('rb') as handle:
        for position, type_ in enumerate(index.types):
            if type_ in skipped:
                continue
            offset = index.offsets[position]
            length = index.lengths[position]
            handle.seek(offset)
            raw = handle.read(length)
            if len(raw) != length:
                raise ValueError(f'{index.path}: short read at offset {offset}')
            data = json.loads(raw.decode('utf-8'))
            if str(data.get('type')) != type_:
                raise ValueError(f'{index.path}: index/file mismatch at offset {offset} '
                                 f'(index says {type_!r}, file says {data.get("type")!r})')
            yield event_from_json(data)
