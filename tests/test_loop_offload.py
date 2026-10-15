"""#54 ③ 的判据：异步路由里的同步重活必须在**事件循环之外**执行。

为什么这是一条真判据而不是形式：打开一个大会话要 `scan_index` 扫完整份日志（实测
**0.5–0.6 s** / 425,985 行）。若它在 `async def` 路由里同步跑，这 0.6 s 内**整个事件循环停摆**
——另一个会话的 SSE、审批、心跳全部排队，于是"并行"退化成"排队"。

判据两层：
1. **机制**：`_offload(...)` 让被卸载的函数跑在**线程池**里（不是事件循环线程）；
2. **接线**：`/sessions/new` 与 `/chat` 打开席位时**确实经 `_offload`**——探针替换 `_offload`
   本身（它是我们自己的接缝），并记录被卸载的**是哪个函数**；这样断言既精确又不依赖
   `open_session` 的导入方式。
"""
from __future__ import annotations

import asyncio
import importlib
import threading

from fastapi.testclient import TestClient

from my_coder import web
from my_coder.web.app import _offload

# **必须用 importlib**：`from my_coder.web import app` / `import my_coder.web.app as app`
# 拿到的都是包 `__init__` 里重绑过的 **FastAPI 实例**（FastAPI 的常见 re-export 写法），
# 不是模块——那样 `app_module._offload` 就是 AttributeError。
app_module = importlib.import_module('my_coder.web.app')


def test_offload_runs_the_function_off_the_loop_thread():
    """机制判据：被卸载的函数必须在**另一个线程**上跑（且是线程池的 worker）。"""
    seen: dict[str, object] = {}

    def probe():
        seen['thread'] = threading.get_ident()
        seen['name'] = threading.current_thread().name
        return 'done'

    async def scenario():
        seen['loop_thread'] = threading.get_ident()
        return await _offload(probe)

    assert asyncio.run(scenario()) == 'done'
    assert seen['thread'] != seen['loop_thread'], (
        f'重活仍在事件循环线程上跑（{seen["name"]}）——那段时间循环停摆')
    assert 'worker' in str(seen['name']).lower(), (
        f'应当落在 starlette 的线程池里，实际线程名 {seen["name"]!r}')


class _OffloadSpy:
    """替换 `_offload`：记下**被卸载的函数名**，再委托给真正的实现。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, function, /, *args, **kwargs):
        self.calls.append(getattr(function, '__name__', repr(function)))
        return await _offload(function, *args, **kwargs)


def test_new_session_offloads_the_replay(tmp_path):
    """接线判据：`/sessions/new` 打开席位（= 重放整份日志）必须经 `_offload`。"""
    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)

    spy = _OffloadSpy()
    original = app_module._offload
    app_module._offload = spy
    try:
        response = client.post('/sessions/new')
    finally:
        app_module._offload = original

    assert response.status_code == 200, response.text
    assert spy.calls, '/sessions/new 没有把重活交给 _offload（循环会被卡住）'
    assert 'open_session' in spy.calls, f'卸载的是 {spy.calls}，预期包含 open_session'


def test_chat_offloads_the_seat_replay(tmp_path):
    """接线判据：`/chat` 首次给某会话发消息时要打开席位，同样必须经 `_offload`。"""
    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    client = TestClient(web.app)
    client.post('/sessions/new')

    spy = _OffloadSpy()
    original = app_module._offload
    app_module._offload = spy
    try:
        with client.stream('POST', '/chat',
                           json={'sid': 'fresh-sid', 'message': '你好'}) as response:
            assert response.status_code == 200, response.read()
            for _ in response.iter_lines():
                pass
    finally:
        app_module._offload = original

    assert spy.calls, '/chat 没有把打开席位的重放交给 _offload（首条消息会卡住循环）'
    assert 'open_session_seat' in spy.calls, f'卸载的是 {spy.calls}，预期包含 open_session_seat'
