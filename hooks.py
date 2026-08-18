"""三个决策钩子：pre_step / request / request_error。

钩子语义：返回值为权威，核心循环负责兜底校验。所有改写都落日志——
pre_step 改过的消息由循环 append 成 user/message，request 结果进
request/header 事件，所以"模型看到什么"始终可重建。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from values import Message


@dataclass(frozen=True)
class PreStepContext:
    turn: int
    step: int
    messages: tuple[Message, ...] = ()


@dataclass(frozen=True)
class RequestContext:
    turn: int
    step: int


@dataclass(frozen=True)
class RequestErrorContext:
    turn: int
    step: int
    code: str = ''
    message: str = ''


class Hooks:
    """可选回调；未挂载时循环走默认实现。"""

    pre_step: Optional[Callable[[PreStepContext, object], Awaitable[object]]] = None
    request: Optional[Callable[[RequestContext, dict], Awaitable[dict]]] = None
    request_error: Optional[Callable[[RequestErrorContext], Awaitable[str]]] = None
