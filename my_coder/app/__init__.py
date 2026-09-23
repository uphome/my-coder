"""应用内容：组装（factory）+ 具体能力（指令文件 / 技能 / 压缩 / 渲染 / 沙箱 / 常量）。

允许 import：下面四层全部，以及 `my_coder.tools`。这一层是"这个 agent 具体会做什么"，
不是框架本身——换一个应用可以整层替换掉：

- `factory.py`     —— 组装一次完整 agent（system 段、工具、hooks、压缩接线）
- `constants.py`   —— 常量（`TOOL_RESULT_MAX_CHARS` 这类框架级上限 + 演示/工具预算）
- `sandbox.py`     —— 路径边界（工具入参沙箱 + 宿主直读文件的越界判据）
- `instructions.py` / `skills.py` —— 宿主直读工作区文件的两条路（指令文件、技能）
- `compaction.py`  —— 上下文压缩引擎
- `ui.py`          —— CLI 渲染（UI 是日志的投影）
"""
