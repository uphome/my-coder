"""状态层：工具注册表——schema（给模型看）+ executor（自己跑）绑在一条注册里。

策略归注册表，循环只读声明、不写业务 if：

- **execution_mode 默认 'sequential'（fail-closed）**：并发许可是要声明出来的，
  不是白拿的默认值。对齐 harness `core/tools` 的 `executionMode()`——未声明、
  未注册、判定抛异常的调用一律按"独占"处理，只有显式声明才进并发池。
  理由：漏声明的代价是不确定的交错（写共享状态的工具并发读-改-写会丢更新），
  多声明一次的代价只是慢一点。方向反过来，代价就不对称了。
- **offload**：executor 体内全是同步阻塞调用（`path.read_text` 之类）时声明。
  这类协程体没有挂起点，会一口气跑完、不把控制权还给事件循环——后果是
  `asyncio.wait_for` 的定时器没机会触发（超时保护形同虚设），同进程的
  SSE/审批/别的会话也被这次同步读盘卡住。声明后丢工作线程跑，主循环立刻
  拿回控制权，超时与并发才真正成立。**只给纯 I/O、不碰 agent/session 状态的
  executor 声明**：换了线程，内存状态就没人在循环线程上守了。

坏参数在校验和解析两层兜底，模型给坏 JSON 只会得到一条 is_error 的工具结果，
不会炸掉循环。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from .constants import TOOL_RESULT_MAX_CHARS

# 合法的执行模式：parallel=可与其他调用并发；sequential=独占（也是默认值）。
EXECUTION_MODES = frozenset({'parallel', 'sequential'})


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
    - execution_mode：'parallel' 可并发 / 'sequential' 独占。**默认 sequential**——
      没声明就不许并发（fail-closed），判据是"它会不会写共享状态"
    - timeout_s：执行超时秒数，卡死自动返回超时结果，不拖垮循环
    - requires_approval：敏感工具（写文件/执行命令）执行前需人工确认——
      循环层执行前检查此声明并调确认钩子（决策走注册声明，不写死在循环里）
    - offload：executor 体内是同步阻塞 I/O（不含 await），丢工作线程执行。
      判据是"它阻不阻塞事件循环"，与 execution_mode 正交：只读工具既并发安全
      又阻塞（两样都声明），写工具不并发也不阻塞（两样都不声明）

    两处永不漂移：模型看到的 schema 和实际执行的函数来自同一条注册。
    """
    name: str
    description: str
    parameters: dict
    execute: Callable[..., Coroutine[Any, Any, ToolOutcome]]
    execution_mode: str = 'sequential'
    timeout_s: float = 60.0
    requires_approval: bool = False
    offload: bool = False


class ToolRegistry:
    """工具注册表：按名字索引 ToolSpec。

    职责边界：
    - register：登记一条工具（重复名字 / 非法执行模式抛错），返回注销函数
    - schemas：投影成 OpenAI tools 格式（纯翻译，无逻辑）
    - execute：校验参数 → 超时包裹执行 → 返回结果；任何失败都转成
      is_error 的 ToolOutcome，不让异常越过注册表边界（参数校验的
      ValueError 由循环层捕获降级，这里只管超时降级）。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec):
        """注册工具，返回注销函数（对称操作，和 prompt 的 section/variable 一致）。"""
        if spec.name in self._tools:
            raise ValueError(f'tool {spec.name!r} is already registered')
        if spec.execution_mode not in EXECUTION_MODES:
            # 执行模式是调度声明，写错了必须当场炸：默认值 fail-closed 兜底，
            # 拼错的值若被静默接受，等于把"独占"的工具放进并发池（宁炸勿静默）
            raise ValueError(
                f'tool {spec.name!r} has invalid execution_mode {spec.execution_mode!r}; '
                f'expected one of {sorted(EXECUTION_MODES)}')
        self._tools[spec.name] = spec
        return lambda: self._tools.pop(spec.name, None)

    def get(self, name: str) -> ToolSpec:
        """按名字取工具；未注册抛 KeyError（模型幻觉出不存在的工具时触发）。"""
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f'tool {name!r} is not registered') from None

    def mode(self, name: str) -> str:
        """查执行模式；**未注册的工具按 fail-closed 返回 'sequential'**。

        为什么这里不能抛 KeyError：分组发生在执行之前。模型幻觉出一个不存在的
        工具名时，若在这一步炸掉，`_run_one` 里那段"把 `get()` 的 KeyError 降级
        成 is_error 结果"的兜底就永远没机会执行——后果是整回合没有 `turn/end`、
        日志里留下"请求了工具却没有结果"的 assistant 消息（wire 格式非法）。
        对齐 harness `executionMode()`：未注册 / 未声明 / 判定抛异常一律
        `exclusive`，调用照旧往下走，失败由执行阶段降级成结果（不变式 5）。
        """
        spec = self._tools.get(name)
        return spec.execution_mode if spec is not None else 'sequential'

    def schemas(self) -> list[dict]:
        """投影成 OpenAI 的 tools 字段格式；模型看到的工具世界就是这份列表。"""
        return [
            {'name': spec.name, 'description': spec.description, 'parameters': spec.parameters}
            for spec in self._tools.values()
        ]

    async def execute(self, name: str, arguments: dict, agent, signal=None) -> ToolOutcome:
        """执行一条工具调用：校验参数 → 超时包裹执行 → 返回结果。

        唯一的兜底在这里：asyncio.TimeoutError 降级成 is_error 结果，
        以及执行结果统一过 `_truncate_outcome`（字符预算，最后的安全网）。
        参数校验抛出的 ValueError 不在这里捕获——由循环层统一捕获降级
        （见 loop._run_one），保证"任何工具失败都变成一条结果"。

        offload 的两条路径都套同一个 wait_for：区别只在"挂起点在哪"——
        不卸载时协程体没有 await，wait_for 的定时器根本没机会跑（超时失效）；
        卸载后 `to_thread` 本身就是挂起点，超时才真的能到点。
        超时后线程仍会跑完（Python 杀不掉线程），但它写不回任何东西：
        结果被丢弃，内存状态也没被它碰过（这才是 offload 只给纯 I/O 的原因）。
        """
        spec = self.get(name)
        _validate_arguments(name, spec.parameters, arguments)
        if spec.offload:
            pending = asyncio.to_thread(_run_blocking, spec.execute, arguments, agent, signal)
        else:
            pending = spec.execute(arguments, agent, signal)
        try:
            outcome = await asyncio.wait_for(pending, timeout=spec.timeout_s)
        except TimeoutError:
            return ToolOutcome(content=f'tool {name!r} timed out after {spec.timeout_s}s', is_error=True)
        return _truncate_outcome(outcome)


def _run_blocking(factory: Callable[..., Coroutine[Any, Any, ToolOutcome]], *args: Any) -> ToolOutcome:
    """在工作线程里把"写成 async、体内没有 await"的 executor 跑到底。

    工作线程里没有事件循环，所以这里自己起一个（`asyncio.run`）——executor
    体内既然是纯同步调用，就不依赖调用方循环上的任何东西。反过来，**任何
    依赖事件循环的 executor 都不能声明 offload**（比如 bash 的 create_subprocess
    要绑在运行中的循环上），这条约束由"只给纯 I/O 声明"的约定守住。
    """
    return asyncio.run(factory(*args))


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


def _truncate_outcome(outcome: ToolOutcome) -> ToolOutcome:
    """registry 层统一兜底：超长工具结果截断并附导航提示。

    提示文本也算进总预算，保证最终 content 长度不超过 TOOL_RESULT_MAX_CHARS；
    is_error 原样保留——截断只是内容预算，不改变成功/失败语义。
    """
    content = outcome.content or ''
    if len(content) <= TOOL_RESULT_MAX_CHARS:
        return outcome
    notice = (
        f'\n(output truncated at {TOOL_RESULT_MAX_CHARS} chars; '
        'narrow the request or page the result)'
    )
    keep = max(0, TOOL_RESULT_MAX_CHARS - len(notice))
    return ToolOutcome(content=content[:keep] + notice, is_error=outcome.is_error)
