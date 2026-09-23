"""框架循环（层 4）：被动状态机 + turn/step 两级循环。

允许 import：`values` / `capability` / `state`。**唯一被允许"驱动"下层的层**：

- `agent.py` —— 被动状态机（idle ↔ running），消息进 inbox → wake 拉起 driver → 跑空回 idle；
- `loop.py`  —— 一个 turn = turn/start → [claim inbox + 一次模型请求 + 执行本步工具调用] 循环 → turn/end。
  step 粒度是**一次模型请求**（不是整段工具循环），插队消息才能在每个请求前被 claim。

这一层不知道 CLI/Web 的存在（宿主在上层，见 `agent_demo/cli.py` 与 `agent_demo/web/`）。
"""
