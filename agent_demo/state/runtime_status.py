"""状态层：**每轮叠给模型的运行时状态**——注册制贡献者（issue #19）。

## 为什么有这么一层

模型每一轮请求里，除了日志推导出的对话历史（`derive_messages`），还要看到一些**当下
才算得出来的状态**：最典型的是 todo 清单（"你现在进行到第几步"）。这类状态的特点是
**不进历史**——它每步都变，写进日志会污染对话、也会打碎前缀缓存；它又不该进 system
（system 是"每轮都生效的规则"，而它是"此刻的事实"）。所以它作为一条**合成 user 消息**
贴在 messages 末尾。

原先这条通道是**硬编码在循环里**的：`runtime/loop.py` 里 `from ..tools.todo import
build_todo_status`，加一个状态源（会话目录、预算水位…）就得改循环。那是"框架层反向依赖
应用层"的典型，也让 `request/header` 的审计字段只能一个来源一个平铺字段。
现在改成**注册制**：

- 一个状态源 = 一个名字 + 一个 `build(session) -> str | None`（**无内容返回 None**，
  表示"这一轮没什么可说的"）；
- `Agent` 与 `prompt` / `tools` 并列持有一个 `RuntimeStatusRegistry`；
- 循环只做三件事：`collect()` → 非空的各贴一条合成消息 → 把 `{名字: 原文}` 记进
  `request/header.runtime_status`（审计用，痕迹数据）；
- **具体贡献者由应用层注册**（`app/factory.py` 一行：`runtime_status.register('todo', …)`），
  循环不认识任何具体状态源。

## 为什么类型放状态层

用它的是 `runtime/`（层 3），被注册的实现可能在 `app/`/`tools/`（层 4）——放状态层
（层 2）两边都能 import 而不违反"只能 import 同层或更低层"。

## 与 DSH / opencode 的对照

DSH 把这类内容做成 runtime-context contributor（每 step 渲染、落成 user/message 以保住
system 前缀缓存）；opencode 的 SystemContext 则提供"不可用"的第三态。我们的形状取自
前者的"贡献者"与后者的"宁缺勿猜"：**`build` 返回 None 就不出现**，绝不用空字符串占位。
"""
from __future__ import annotations

from collections.abc import Callable

from .session import Session

# 贡献者签名：拿到会话（日志投影），返回要叠给模型的原文；无内容返回 None
StatusBuilder = Callable[[Session], 'str | None']


class RuntimeStatusRegistry:
    """运行时状态贡献者的注册表（每会话一个 agent 一份，宿主可注入）。

    设计对齐 `state/prompt.py` 的 `PromptRegistry`：按**名字**注册、注册时刻严格校验、
    `register` 返回注销函数（测试与宿主热插拔用）。
    """

    def __init__(self) -> None:
        self._builders: dict[str, StatusBuilder] = {}

    def register(self, name: str, build: StatusBuilder) -> Callable[[], None]:
        """注册一个状态源，返回注销函数。

        重名 / 空名**当场抛错**（宁炸勿静默）：重名意味着两个来源抢同一个审计键，
        到了 `request/header` 里只会剩一个，而且谁也说不清是哪个——这种错误在注册时刻
        就暴露，比在日志里发现"状态栏怎么不对"便宜得多。
        """
        if not name:
            raise ValueError('runtime status name must not be empty')
        if name in self._builders:
            raise ValueError(f'runtime status {name!r} is already registered')
        self._builders[name] = build

        def unregister() -> None:
            self._builders.pop(name, None)

        return unregister

    def collect(self, session: Session) -> tuple[tuple[str, str], ...]:
        """按注册顺序求值，只收**非空**的 `(name, text)`。

        每次请求现算（状态是"此刻的事实"）：同一个会话在同一 step 里多次组请求
        （重试路径）会拿到最新的状态；`build` 必须是纯函数或至少是"读日志算"的纯投影，
        这样"模型可见 ⟺ 可重建"仍成立。
        """
        out: list[tuple[str, str]] = []
        for name, build in self._builders.items():
            text = build(session)
            if text:
                out.append((name, text))
        return tuple(out)

    @property
    def names(self) -> tuple[str, ...]:
        """已注册的名字（注册顺序）——审计/诊断用。"""
        return tuple(self._builders)
