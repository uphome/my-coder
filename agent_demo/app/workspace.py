"""应用层：工作区选择策略——把"用户给的工作区字符串"变成可用的绝对路径。

为什么值得一个独立模块：`--workspace` 过去是**进程级唯一边界**，现在 Web 宿主允许
**每个对话各自选一个工作区**，于是"输入 → 校验 → 绝对路径"这条规则需要一个明确的归属。
写在路由里会散进 HTTP 细节、写在状态层会让状态层依赖文件系统语义；它只被 Web 宿主用到，
所以放应用层（`agent_demo/app/`）。

策略（2026-09 决策，对齐 DSH 的本地工具模型）：**信任选择者**——只校验"存在 + 是目录"，
不做白名单。理由与代价如实写在 README 的安全警告里：Web 宿主默认只绑 `127.0.0.1`，
能点这个界面的人本来就是这台机器的使用者；**给 agent 一个工作区 = 把该目录的读写授权给它**，
选择动作本身就是授权。想收紧的话，加白名单是在这里加一条判据，调用方不用改。
"""
from __future__ import annotations

from pathlib import Path


def resolve_workspace(raw: str | Path | None, *, default: Path) -> Path:
    """把用户输入解析成绝对工作区路径（空 → 宿主默认，不做存在性检查）。

    - 空（`None` / 空串 / 全空白）→ `default`：**不检查存在性**，与 CLI 的
      `--workspace` 语义保持一致（默认工作区缺失时工具调用自己会给出 is_error，
      不必让整个宿主起不来）。
    - 相对路径 → 相对**进程当前目录**解析（符合 shell 直觉；不是相对默认工作区——
      否则在 `--workspace /data/proj` 下输入 `../other` 会被解析到一个很意外的位置）。
    - `~` 展开（`expanduser`），最后 `resolve()` 归一化（折叠 `..`、解符号链接）。
    - 用户**显式**给的路径必须存在且是目录，否则抛 `ValueError`（调用方转 400）：
      "选了一个不存在的工作区"如果放过去，只会在第一次工具调用时变成一串 is_error，
      不如在选择的时刻就说清楚（宁炸勿静默）。
    """
    text = str(raw).strip() if raw is not None else ''
    if not text:
        return Path(default).expanduser().resolve()
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.resolve()
    if not resolved.exists():
        raise ValueError(f'workspace does not exist: {resolved}')
    if not resolved.is_dir():
        raise ValueError(f'workspace is not a directory: {resolved}')
    return resolved


def normalize_recorded_workspace(raw: str) -> Path:
    """**日志里记着**的工作区 → 绝对路径（相对路径按进程 cwd、`~` 展开）。

    为什么单独一条：写入时我们记的是绝对路径，但日志是可以手改的（也可能来自更早的版本）。
    **列表（`web/sessions.scan_sessions`）与 seat（`web/sessions._resolve_seat_workspace`）
    必须用同一条规则解释它**——否则会出现"列表显示 `rel-ws`、工具实际在 `<cwd>/rel-ws`"
    这种两处不一致，而这类不一致最难查（两边单看都合理）。

    不检查存在性：**是否存在由调用方按各自语义处理**——列表照原样显示（它只是信息），
    seat 发现不是目录就 409 报错（绝不静默回退）。
    """
    return Path(raw).expanduser().resolve()
