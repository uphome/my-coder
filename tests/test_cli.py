"""CLI 入口：REPL 多轮交互。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**断言与用例体一字未改**；
唯一差异是 26 处函数内冗余 import 被 ruff 的 F401/F811 删掉——那是拆分暴露出来的旧问题。）
"""
from __future__ import annotations

from pathlib import Path

from agent_demo.persistence import load_events


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
        [sys.executable, '-m', 'agent_demo.cli', '--fake',
         '--workspace', str(tmp_path), '--session', 'repl-test',
         '--sessions', str(sessions_dir)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=Path(__file__).resolve().parent.parent,  # 仓库根（agent_demo 可导入）
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
