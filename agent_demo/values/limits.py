"""值层：**跨层共享的常量**（预算/上限这类"数字"）。

为什么放值层而不是 `app/constants.py`：这些数字被**多个层**需要，而值层是最底层——
放这里谁 import 都不违反依赖方向。反例就是这次修掉的那条：
`TOOL_RESULT_MAX_CHARS` 原先住在应用层的 `app/constants.py`，而要用它的是**状态层**的
`state/registry.py`——状态层 import 应用层，方向就反了（`tests/test_architecture.py`
把它记在白名单里，这次搬完就删掉了）。

放这里的判据（写清楚免得下次又要讨论）：**一个常量如果只有应用/工具层用，就留在
`app/constants.py`（或工具自己的模块里）；一旦有更低层要用它，就必须下沉到这一层。**

`TOOL_RESULT_MAX_CHARS`：registry 层统一的结果上限——任何工具返回内容超过它就截断。
这是最后的安全网；具体工具（read_file / bash 等）各自有更早、更精确的预算。
"""

TOOL_RESULT_MAX_CHARS = 20000
