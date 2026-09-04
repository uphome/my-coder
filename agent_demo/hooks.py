"""三个决策钩子：pre_step / request / request_error。

钩子语义：返回值为权威，核心循环负责兜底校验。所有改写都落日志——
pre_step 改过的消息由循环 append 成 user/message，request 结果进
request/header 事件，所以"模型看到什么"始终可重建。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .values import Message


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

    pre_step: Callable[[PreStepContext, object], Awaitable[object]] | None = None
    request: Callable[[RequestContext, dict], Awaitable[dict]] | None = None
    request_error: Callable[[RequestErrorContext], Awaitable[str]] | None = None
    # approval：敏感工具（requires_approval 声明）执行前的人工确认。
    # 签名 (name, arguments) -> bool；None 时循环用默认实现（CLI stdin 交互）。
    # 确认是"钩子"——怎么问用户是策略；"要不要问"是工具注册时的声明。
    approval: Callable[[str, dict], Awaitable[bool]] | None = None
