"""状态层：工具注册表——schema（给模型看）+ executor（自己跑）绑在一条注册里。

策略归注册表：工具可声明 execution_mode='sequential'（交互式工具），
循环按模式分组执行，不硬编码"全部并行"。坏参数在校验和解析两层兜底，
模型给坏 JSON 只会得到一条 is_error 的工具结果，不会炸掉循环。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass(frozen=True)
class ToolOutcome:
    """工具执行结果：一段文本 + 是否出错。

    is_error=True 只是"这条结果告诉模型：调用失败了"，
    不会抛给循环——失败降级成结果，是工具层最重要的约定。
    """
    content: str = ''
    is_error: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """一条工具注册：给模型看的 schema + 给自己跑的 executor，绑定在同一个对象里。

    - name/description/parameters：进请求的 tools 字段，模型靠它决定怎么调用
    - execute：真实函数签名 execute(arguments, agent, signal) -> ToolOutcome
    - execution_mode：'parallel' 可并发 / 'sequential' 必须逐个（交互式工具）
    - timeout_s：执行超时秒数，卡死自动返回超时结果，不拖垮循环

    两处永不漂移：模型看到的 schema 和实际执行的函数来自同一条注册。
    """
    name: str
    description: str
    parameters: dict
    execute: Callable[..., Awaitable[ToolOutcome]]
    execution_mode: str = 'parallel'
    timeout_s: float = 60.0


class ToolRegistry:
    """工具注册表：按名字索引 ToolSpec。

    职责边界：
    - register：登记一条工具（重复名字抛错），返回注销函数
    - schemas：投影成 OpenAI tools 格式（纯翻译，无逻辑）
    - execute：校验参数 → wait_for 超时包裹 → 执行；任何失败都转成
      is_error 的 ToolOutcome，不让异常越过注册表边界（参数校验的
      ValueError 由循环层捕获降级，这里只管超时降级）。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec):
        """注册工具，返回注销函数（对称操作，和 prompt 的 section/variable 一致）。"""
        if spec.name in self._tools:
            raise ValueError(f'tool {spec.name!r} is already registered')
        self._tools[spec.name] = spec
        return lambda: self._tools.pop(spec.name, None)

    def get(self, name: str) -> ToolSpec:
        """按名字取工具；未注册抛 KeyError（模型幻觉出不存在的工具时触发）。"""
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f'tool {name!r} is not registered') from None

    def mode(self, name: str) -> str:
        """查执行模式——循环层按它把工具调用分组（parallel/sequential）。"""
        return self.get(name).execution_mode

    def schemas(self) -> list[dict]:
        """投影成 OpenAI 的 tools 字段格式；模型看到的工具世界就是这份列表。"""
        return [
            {'name': spec.name, 'description': spec.description, 'parameters': spec.parameters}
            for spec in self._tools.values()
        ]

    async def execute(self, name: str, arguments: dict, agent, signal=None) -> ToolOutcome:
        """执行一条工具调用：校验参数 → 超时包裹执行 → 返回结果。

        唯一的兜底在这里：asyncio.TimeoutError 降级成 is_error 结果。
        参数校验抛出的 ValueError 不在这里捕获——由循环层统一捕获降级
        （见 loop._run_group），保证"任何工具失败都变成一条结果"。
        """
        spec = self.get(name)
        _validate_arguments(name, spec.parameters, arguments)
        try:
            return await asyncio.wait_for(spec.execute(arguments, agent, signal), timeout=spec.timeout_s)
        except asyncio.TimeoutError:
            return ToolOutcome(content=f'tool {name!r} timed out after {spec.timeout_s}s', is_error=True)


def _validate_arguments(name: str, parameters: dict, arguments: dict) -> None:
    """轻量参数校验：required 必须齐全，多余的参数直接拒绝。

    只做这两件事，不做类型/格式校验（demo 的克制）：
    - 缺 required → 模型漏了参数，报错让它补
    - 多传未知参数 → 模型幻觉出 schema 里没有的字段，报错让它改
    """
    properties = parameters.get('properties', {})
    for key in parameters.get('required', []):
        if key not in arguments:
            raise ValueError(f'tool {name!r} missing required argument {key!r}')
    for key in arguments:
        if properties and key not in properties:
            raise ValueError(f'tool {name!r} got unexpected argument {key!r}')
