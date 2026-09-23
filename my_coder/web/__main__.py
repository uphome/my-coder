"""`python -m my_coder.web`：起 Web UI。

拆分前是 `python -m my_coder.web_app`（模块自带 `if __name__ == '__main__'`）；
包化之后入口挪到这里，命令短了一截。console script `my-coder-web` 走
`my_coder.web.app:main`，两者同一个函数。
"""
from __future__ import annotations

from .app import main

if __name__ == '__main__':
    main()
