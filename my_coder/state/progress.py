"""进度策略：数"自上次变更以来连续做了多少次只读调用"，到阈值给收敛压力。

**为什么用这个判据**（实测，见 `docs/notes/proposed/feature/2026-09-30-tool-convergence.md`）：
真实语料里"最长连续只读段" p50 = 1 / p90 = 8 / max = 39，而一个 61 步的**实现型**回合
最长只读段只有 4 —— 所以"连续读了多少次"比"做了多少步"更能分辨"原地打转"与"边改边验"。
按总步数设上限会砍掉后者（opencode 就是这么做的，我们没跟）。

判据来自**声明**（`ToolSpec.cacheable`），不是循环里对工具名的硬编码；本模块只持有逻辑，
**不持有任何文案**（提示语由 `app/factory.py` 传入，状态层不认识 prompt 内容）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 两种收敛压力的强度；调用方（循环层）据此决定"追加提示"还是"下一步收尾"
NUDGE = 'nudge'      # 软：把提示写进那次工具结果正文
CLOSE = 'close'      # 硬：下一次请求不带工具面（收尾步）


@dataclass
class ProgressPolicy:
    """一个回合内的无进展计数。

    - `nudge_at`：连续只读达到它时给软提示；之后每 4 次再给一次（不刷屏），`None` = 关
    - `close_at`：连续只读达到它时请求收尾步，`None` = 关
    - `enabled=False`：完全回到没有这个机制的行为（两个阈值都失效）
    - `nudge_text` / `closing_text`：**由应用层注入**（`app/constants.py` 的作者写文案，
      状态层只持有它）；空串 = 不追加/不叠指令，只走账不打扰

    回合是收敛的自然边界：`run_turn` 每回合开头调一次 `reset()`。
    """

    nudge_at: int | None = 8
    close_at: int | None = 16
    enabled: bool = True
    nudge_text: str = ''
    closing_text: str = ''
    readonly_run: int = 0            # 自上次"可能变更"以来的连续只读调用数
    closing: bool = False            # 已请求收尾（下一次请求不带工具面）
    _last_nudge_at: int = field(default=-1, repr=False)

    def reset(self) -> None:
        """新回合：计数清零、收尾标志复位。"""
        self.readonly_run = 0
        self.closing = False
        self._last_nudge_at = -1

    def note(self, cacheable: bool) -> str | None:
        """记一次工具调用，返回这次调用该附带哪种收敛压力。

        `cacheable=True` = 纯读、无产出 → 计数 +1；否则（会变更/未知）→ **清零**：
        "边改边验"的长任务因此永远不会触发收尾。
        """
        if not self.enabled:
            return None
        if not cacheable:
            self.readonly_run = 0
            self._last_nudge_at = -1
            return None
        self.readonly_run += 1
        if self.closing:
            # 已经请求过收尾步了：再叠提示没有意义（那一步本来就不带工具面）
            return None
        if (self.close_at is not None and not self.closing
                and self.readonly_run >= self.close_at):
            self.closing = True
            return CLOSE
        if (self.nudge_at is not None
                and self.readonly_run >= self.nudge_at
                and self.readonly_run != self._last_nudge_at
                and (self.readonly_run - self.nudge_at) % 4 == 0):
            self._last_nudge_at = self.readonly_run
            return NUDGE
        return None
