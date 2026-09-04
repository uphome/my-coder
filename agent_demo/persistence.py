"""JSONL 持久化：追加写、每行一条事件；加载即重放。

恢复 = 重放，零额外代码：adopt 重建 surface 投影，Inbox 构造时
重放 spliced 事件恢复队列，request_header 从日志恢复上次模型配置。
"""
from __future__ import annotations

import json
from pathlib import Path

from .values import SessionEvent, event_from_json, event_to_json


def save_event(path: Path, event: SessionEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(event_to_json(event), ensure_ascii=False) + '\n')


def load_events(path: Path) -> list[SessionEvent]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        events.append(event_from_json(json.loads(line)))
    return events
