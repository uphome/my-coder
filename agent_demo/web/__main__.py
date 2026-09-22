"""`python -m agent_demo.web`：起 Web UI。

拆分前是 `python -m agent_demo.web_app`（模块自带 `if __name__ == '__main__'`）；
包化之后入口挪到这里，命令短了一截。console script `agent-demo-web` 走
`agent_demo.web.app:main`，两者同一个函数。
"""
from __future__ import annotations

from .app import main

if __name__ == '__main__':
    main()
