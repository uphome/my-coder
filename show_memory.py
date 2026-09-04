"""教学脚本：展示日志如何折叠出 agent 的记忆。

从 JSONL 重放会话，在每次 request/header（模型请求）时刻，
打印当时 derive_messages() 折叠出的完整消息序列——证明
"模型看到什么"完全由日志投影而来，没有任何独立的对话状态。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent_demo.persistence import load_events
from agent_demo.session import Session


def preview(message, limit: int = 80) -> str:
    parts = []
    for block in message.content:
        if block.type == 'text':
            parts.append(block.text)
        elif block.type == 'tool-call':
            parts.append(f'<tool-call {block.name}({block.arguments[:40]})>')
        elif block.type == 'tool-result':
            parts.append(f'<tool-result {block.content[:40]}>')
    text = ' | '.join(parts).replace('\n', ' ')
    return text[:limit] + ('…' if len(text) > limit else '')


def main(path: Path) -> None:
    events = load_events(path)
    session = Session(id=path.stem)
    request_no = 0

    print(f'=== 重放 {path}（{len(events)} 条事件）===\n')
    for event in events:
        if event.type == 'request/header':
            request_no += 1
            print(f'\n──────── 第 {request_no} 次模型请求：此刻日志折叠出的记忆 ────────')
            for index, message in enumerate(session.derive_messages(), start=1):
                print(f'  [{index}] role={message.role:<9} source={message.source.kind:<6} {preview(message)}')
            header = event.data
            print(f'  （system 提示词 {len(header["system"])} 字符、工具 {header["tools"]}）')
            print('──────────────────────────────────────────────────────────')
        session.adopt(event)
    print('\n最终 derive_messages() 共', len(session.derive_messages()), '条消息')


if __name__ == '__main__':
    main(Path(sys.argv[1]))
