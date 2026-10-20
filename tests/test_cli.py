"""CLI 入口：REPL 多轮交互。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**断言与用例体一字未改**；
唯一差异是 26 处函数内冗余 import 被 ruff 的 F401/F811 删掉——那是拆分暴露出来的旧问题。）
"""
from __future__ import annotations

from pathlib import Path

from my_coder.app.ui import render_event
from my_coder.values.messages import (
    StreamFrame,
    TextBlock,
    ToolCallBlock,
    create_assistant_message,
    new_event,
)
from my_coder.values.persistence import load_events


def test_cli_repl_runs_multiple_turns(tmp_path):
    """CLI REPL：无 prompt 启动 → 多轮输入各自开回合，/exit 退出。

    用 subprocess 真跑 CLI（管道喂 stdin），最接近真实交互；fake llm
    离线跑通。断言：退出后会话日志里有两个回合、两条 user 消息。
    """
    import subprocess
    import sys

    sessions_dir = tmp_path / 'sess'
    sessions_dir.mkdir(exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, '-m', 'my_coder.cli', '--fake',
         '--workspace', str(tmp_path), '--session', 'repl-test',
         '--sessions', str(sessions_dir)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=Path(__file__).resolve().parent.parent,  # 仓库根（my_coder 可导入）
    )
    out, err = proc.communicate(
        input='第一轮：读 README\n第二轮：列文件\n/exit\n'.encode(),
        timeout=60,
    )
    assert proc.returncode == 0, f'REPL exit {proc.returncode}\n{err.decode(errors="replace")}'
    # 提示符出现多次 = 多轮
    assert out.decode(errors='replace').count('agent> ') >= 3   # 两轮 + 首提示 + 结尾

    # 会话日志落盘：两个回合、两条 user 消息
    log = (sessions_dir / 'repl-test.jsonl')
    assert log.exists()
    events = list(load_events(log))
    turns = [e for e in events if e.type == 'turn/start']
    assert len(turns) >= 2
    texts = []
    for e in events:
        if e.type != 'user/message':
            continue
        msg = e.data
        for block in getattr(msg, 'content', ()):
            if getattr(block, 'type', '') == 'text':
                texts.append(block.text)
    assert '第一轮：读 README' in texts and '第二轮：列文件' in texts


# ---- 渲染器的确定性单测（issue：--resume 重放看不到助手正文）----
# 不起 subprocess、不跑模型：直接构造事件/流帧序列喂给同一个 render_event，
# 用 capsys 断言正文的出现次数。两条路径（重放 / 实时）在这里是行级可比的。

def _render_state() -> dict:
    """与 cli.py 的 ui_state 初值一致（streamed 标记由渲染时按需补上）。"""
    return {'reasoning_started': False, 'request_no': 0, 'tool_no': 0}


def _render(events, capsys) -> str:
    """按顺序把事件/流帧喂给同一个 render_event（共用一份 state），返回终端文本。"""
    state = _render_state()
    for event in events:
        render_event(event, False, state)
    return capsys.readouterr().out


def _turn_prefix() -> list:
    """一次请求的开头（两条路径共有）：回合/步骤边界 + 请求头。"""
    return [
        new_event(1, 'turn/start', {'turn': 1}),
        new_event(2, 'step/start', {'turn': 1, 'step': 1}),
        new_event(3, 'request/header', {'turn': 1, 'step': 1, 'model': 'fake'}),
    ]


def _assistant_message(*blocks):
    """构造一条 assistant/message 事件（data 形状与 loop.py 落日志的一致）。"""
    return new_event(
        8, 'assistant/message',
        {'turn': 1, 'step': 1, 'message': create_assistant_message(blocks)},
        surface_op='append',
    )


def test_resume_replay_prints_assistant_text_once(capsys):
    """重放路径：issue #51 之后新日志没有 assistant/chunk 帧，正文必须由
    assistant/message 补打——否则 --resume 完全看不到历史助手正文。"""
    events = [*_turn_prefix(), _assistant_message(TextBlock(text='历史助手正文'))]
    out = _render(events, capsys)
    assert out.count('历史助手正文') == 1


def test_live_stream_does_not_duplicate_assistant_text(capsys):
    """实时路径：正文已由 assistant/chunk 逐字打印，随后到达的 assistant/message
    必须跳过（同一 (turn, step) 的 streamed 标记），否则正文出现两次。"""
    events = [
        *_turn_prefix(),
        StreamFrame(type='assistant/chunk', data={
            'turn': 1, 'step': 1,
            'chunk': {'text': '实时助手正文', 'tool_calls': [],
                      'finish_reason': None, 'usage': None},
        }),
        _assistant_message(TextBlock(text='实时助手正文')),
    ]
    out = _render(events, capsys)
    assert out.count('实时助手正文') == 1


def test_assistant_message_prints_only_text_blocks(capsys):
    """正文从 block tuple 里按类型挑：不假设 content[0] 是文本块。
    工具调用块不进正文（它由随后的 tool/call 事件渲染）。"""
    events = [
        *_turn_prefix(),
        _assistant_message(
            ToolCallBlock(id='call-1', name='read_file', arguments='{"path": "README.md"}'),
            TextBlock(text='跟在工具调用后面的正文'),
        ),
    ]
    out = _render(events, capsys)
    assert out.count('跟在工具调用后面的正文') == 1
    assert 'read_file' not in out

