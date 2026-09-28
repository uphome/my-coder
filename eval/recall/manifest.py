"""eval 的接入层：**只用产品实现**（`my_coder/app/recall.py`），不在这里放副本。

这个文件曾经是 M1 的原型（自带一份 `build_turns` / `render_manifest` / `render_turn`）。
产品版落地后它就变成了**第二份实现**——两份逻辑碰巧一样时看不出问题，一旦产品改了
（比如清单格式、遮蔽标记、`with_commands`）评测数字就会**静默偏离产品行为**，
而这正是本仓库"一条规则一处实现"要防的事。所以现在它只是一层薄壳：

- 三个投影全部 re-export 产品的函数（评测测的必须是**产品**跑的东西）；
- 只补两处 eval 专用的小东西：`SESSIONS`（产品没有这个常量——会话目录是注入的）
  与 `load_session`（产品叫 `load_session_log`，这里给个短别名，省得改各脚本）。

切换时做过字节比对（54 MB / 59 回合的真实会话）：`build_turns` 字段 0 差异，
`render_manifest` 与 `render_turn` **逐字节一致**——所以已入库的数据集与题目仍然有效。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:          # 让 eval 脚本能 import my_coder
    sys.path.insert(0, str(REPO))

from my_coder.app.recall import (TurnInfo, build_turns,  # noqa: E402,F401
                                 load_session_log, render_manifest,
                                 render_turn, shadowed_seqs)

# eval 专用：会话日志目录（产品里由宿主注入 `sessions_dir`，这里就是本仓库的 .sessions）
SESSIONS = REPO / '.sessions'


def load_session(path: Path, session_id: str = ''):
    """产品函数 `load_session_log` 的短别名（各脚本按这个名字用）。"""
    return load_session_log(path, session_id)
