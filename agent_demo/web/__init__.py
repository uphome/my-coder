"""Web 宿主（入口层）：把日志投影成 DOM 的那个渲染器——CLI 是终端投影，Web 是 DOM 投影。

拆分前的 `agent_demo/web_app.py` 有 993 行 / 13 路由 / 41 个顶层定义，混了 8 项职责；
现在按"状态 / 生命周期 / 投影 / 路由"分开（执行文档：`REFACTOR_PLAN.md` §4 PR 1）：

| 模块 | 职责 | 依赖 |
|---|---|---|
| `state.py` | `Seat`（每会话资源）+ `WebState`（宿主级单例） | `..session` |
| `sessions.py` | seat 生命周期、会话文件、标题落盘、审批钩子、后台任务 | state + payload + 框架层 |
| `titles.py` | 自动会话标题（三级来源 + 逐字复读判定） | state + `..llm` / `..values` |
| `payload.py` | **纯函数投影**：事件/消息 → 前端载荷（不依赖 FastAPI） | `..compaction` / `..constants` |
| `app.py` | FastAPI 路由 + `init_web` + `main` | 上面全部 |

**这是唯一允许 re-export 的包**（`AGENTS.md` 的规矩）：`app` / `init_web` / `main` 既被
uvicorn 与 console script 按路径使用，也被测试与宿主按名字使用；其余模块一律走显式路径
（`from .payload import queue_rows`），这样依赖方向才 grep 得到、也才能被 ast 测试钉住。
"""
from __future__ import annotations

from .app import app, init_web, main
from .payload import event_to_payload, history_payloads, message_to_payload, queue_rows
from .state import state

__all__ = [
    'app',
    'event_to_payload',
    'history_payloads',
    'init_web',
    'main',
    'message_to_payload',
    'queue_rows',
    'state',
]
