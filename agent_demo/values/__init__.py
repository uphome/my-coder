"""值层（层 1）：不可变的词汇表——消息、事件、工具结果，以及 JSONL 编解码。

**四层单向依赖的最底层**：这里只允许 import 标准库，不许 import 本包的任何其它层。
理由：值对象要能脱离框架被构造、比较、序列化（`derive_messages` 纯函数、JSONL 往返都
依赖这一点），一旦它能反向依赖状态或循环，值层就退化成了"另一份状态"。

目录内容：

- `messages.py`  —— 消息与事件的词汇表（content blocks、来源、Message、SessionEvent
  + tagged dict 编解码）。文件名说的是内容：这里装的就是"模型能看见的东西"。
- `persistence.py` —— JSONL 追加写 + 重放读（事件的落地形态）。
"""
