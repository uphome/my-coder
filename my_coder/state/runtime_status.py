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

import logging
from collections.abc import Callable

from .session import Session

log = logging.getLogger('runtime_status')

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

        重名 / 空名 / `build` 不可调用**当场抛错**（宁炸勿静默）：重名意味着两个来源抢
        同一个审计键，到了 `request/header` 里只会剩一个，而且谁也说不清是哪个；不可调用
        则在求值时才会炸，被 `collect` 的兜底吞成一条日志、**永久静默跳过**——两种都该在
        装配时刻暴露，比在日志里发现"状态栏怎么不对"便宜得多。
        """
        if not name:
            raise ValueError('runtime status name must not be empty')
        if not callable(build):
            raise TypeError(
                f'runtime status {name!r}: build must be callable, got {type(build).__name__}')
        if name in self._builders:
            raise ValueError(f'runtime status {name!r} is already registered')
        self._builders[name] = build

        def unregister() -> None:
            self._builders.pop(name, None)

        return unregister

    def collect(self, session: Session) -> tuple[tuple[str, str], ...]:
        """按注册顺序求值，只收**非空字符串**的 `(name, text)`。

        每次请求现算（状态是"此刻的事实"）：同一个会话在同一 step 里多次组请求
        （重试路径）会拿到最新的状态；`build` 必须是纯函数或至少是"读日志算"的纯投影，
        这样"模型可见 ⟺ 可重建"仍成立。

        **契约：`build` 返回 `str | None`**——`None` 表示"这一轮没有这份状态"（不出现，
        也不进审计）；返回**非字符串**视为写坏了，记一条 ERROR 日志并跳过（见下）。

        **单个贡献者坏掉不炸对话**：求值抛异常、返回非字符串，都记 ERROR 日志、跳过它——
        这一轮就是"没有这份状态"。判据是这条通道的定位：它是**可选的状态展示**，坏了不该
        让整个回合作废（对照：工具失败必须降级成 is_error 结果，因为那是模型输入可能不
        合法的通道；这里连"结果"都不给，审计映射只列**真的被告知模型**的项，失败只进日志）。
        与"注册时刻的错误（空名/重名/不可调用）当场抛"并不矛盾：那是宿主写错了代码，
        早在装配时就该响（宁炸勿静默）。

        注意两件容易写错的事：
        - 迭代的是**快照**（`tuple(...)`）：贡献者在求值期间注册/注销会把字典改大改小，
          直接迭代 `items()` 会由**迭代器**抛 `RuntimeError`——它逃得过下面的 try，于是
          "坏一个不炸对话"就不成立了。快照同时给出一句明确语义：**求值期间的增删本轮
          不生效，下一请求生效**。
        - `except Exception` **不捕 `BaseException`**：`CancelledError` / `KeyboardInterrupt`
          照常穿透，所以"取消单向传播"这条不变式不被这条兜底破坏（有用例钉住）。
        """
        out: list[tuple[str, str]] = []
        for name, build in tuple(self._builders.items()):
            try:
                text = build(session)
            except Exception:  # noqa: BLE001 - 见 docstring：可选状态坏了不该炸回合
                log.exception('runtime status %r failed; skipped for this request', name)
                continue
            if text is None:
                continue
            if not isinstance(text, str):
                # 非字符串一旦混进 messages / 审计，`bytes` 之类要到落盘序列化时才炸（离现场
                # 很远）；这里当面报出来并跳过——仍然不炸回合
                log.error('runtime status %r returned %s, expected str; skipped',
                          name, type(text).__name__)
                continue
            if text:
                out.append((name, text))
        return tuple(out)

    @property
    def names(self) -> tuple[str, ...]:
        """已注册的名字（注册顺序）——审计/诊断用。"""
        return tuple(self._builders)
