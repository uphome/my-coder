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

`ATTEMPT_PARTIAL_MAX_CHARS`：失败/取消的模型尝试留档时，**半截流**最多记多少字符。
它住在这里而不是散在 `runtime/loop.py` 里，是因为"留档给人看"的渲染方（如将来
召回层展示失败尝试）要按同一个上限截——两处各写一个数字必然会漂。
"""

TOOL_RESULT_MAX_CHARS = 20000
# 失败尝试的 partial（已经流出来的半截文本）上限：留档是给人/模型判断"它到哪一步
# 才挂的"，记全文没有价值，反而把日志撑大。
ATTEMPT_PARTIAL_MAX_CHARS = 500
