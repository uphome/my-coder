"""issue #46 ①：冷却席位释放——只在空闲时释放、受保护的绝不释放、重开靠重放恢复。

为什么值得单独测：释放 = 丢掉整个 Seat 的内存状态（Session / Agent）。判据错一条就会
**把进行中的回合连根拔掉**（日志里只剩一半），所以三条边界都要钉住：
① 超过上限才释放；② 忙的（在跑 / 有 SSE 流 / 有待审批）与受保护的（焦点 / 当前）不释放；
③ 释放不丢数据——重开时 adopt 重放，记忆照旧。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import my_coder.web.sessions as web_sessions
from my_coder import web
from my_coder.web import state as web_state
from my_coder.web.sessions import MAX_OPEN_SEATS, open_session_seat
from my_coder.web.state import Seat


def _init(tmp_path):
    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')


def test_seat_is_idle_requires_no_running_turn():
    """空闲 = 不在跑 **且** 没有活跃 SSE 流 **且** 没有待审批（三个条件缺一不可）。"""
    seat = Seat('s', SimpleNamespace(id='s'), SimpleNamespace())
    seat.agent = SimpleNamespace(status='idle')
    assert seat.is_idle()

    seat.agent = SimpleNamespace(status='running')
    assert not seat.is_idle(), '正在跑的回合绝不能被释放'

    seat.agent = SimpleNamespace(status='idle')
    seat.queue = asyncio.Queue()
    assert not seat.is_idle(), '有活跃 SSE 流（前端正连着）不能释放'

    seat.queue = None
    seat.approvals['a1'] = SimpleNamespace()
    assert not seat.is_idle(), '有待审批不能释放（否则用户点了批准会找不到那个 Future）'


def test_oldest_idle_seat_is_released_over_the_limit(tmp_path):
    _init(tmp_path)
    sids = [f's{index}' for index in range(MAX_OPEN_SEATS + 1)]
    for sid in sids:
        open_session_seat(sid, allow_missing=True)

    seats = web_state.seats
    assert len(seats) <= MAX_OPEN_SEATS, f'常驻席位应收敛到上限内，实际 {len(seats)}'
    assert sids[0] not in seats, '最久没用过的先被释放'
    assert sids[-1] in seats, '刚打开的那个必须留着'


def test_reopening_a_released_seat_replays_instead_of_failing(tmp_path):
    _init(tmp_path)
    sids = [f's{index}' for index in range(MAX_OPEN_SEATS + 1)]
    for sid in sids:
        open_session_seat(sid, allow_missing=True)
    released = sids[0]
    assert released not in web_state.seats

    # 重开：释放不丢数据——日志是唯一事实源，adopt 重放即恢复
    seat = open_session_seat(released, allow_missing=False)
    assert seat.session.id == released
    assert seat.agent is not None


def test_busy_seats_are_never_released_even_over_the_limit(tmp_path):
    """全忙时宁可超编，也不拔进行中的回合（fail-closed）。"""
    _init(tmp_path)
    for index in range(MAX_OPEN_SEATS):
        # **建好一个立刻标忙**：否则它会在这段循环里就因"空闲"被释放掉，
        # 那测的就不是"忙的不释放"，而是"空闲的先释放"（这个坑我踩过一次）
        open_session_seat(f'b{index}', allow_missing=True).queue = asyncio.Queue()

    open_session_seat('extra', allow_missing=True)
    # 全忙时**允许超编**（宁可超出上限，也不拔进行中的回合）；`init_web` 自带的默认席位
    # （current_sid）同样受保护，所以这里不写死总数，只断言"忙的一个都没少"
    assert len(web_state.seats) > MAX_OPEN_SEATS, '忙的时段允许超编'
    for index in range(MAX_OPEN_SEATS):
        assert web_state.seats[f'b{index}'].queue is not None, '忙的席位必须原样留着'


def test_eviction_is_reported_on_the_new_seat(tmp_path):
    """#54 ② 回归：回收**不能是静默的**——被释放的 sid 必须记在刚建的那个席位上。

    事故现场：`evict_idle_seats` 一直返回被释放的 sid，但调用方把返回值丢了，
    于是用户还挂着的标签页只会在**下一次请求**撞上 `not open — switch to it first`，
    而不知道发生了什么。（那个 404 就是 #52/#44 现象的真实根因。）
    """
    _init(tmp_path)
    sids = [f'v{index}' for index in range(MAX_OPEN_SEATS + 1)]
    seats = [open_session_seat(sid, allow_missing=True) for sid in sids]

    reported = [sid for seat in seats for sid in seat.evicted]
    assert reported, '超过上限时必须有席位被释放，且必须**被报告出来**'
    released = set(sids) - set(web_state.seats)
    assert set(reported) == released, (
        f'报告的必须与实际释放的一致：报告 {sorted(set(reported))} vs 实际 {sorted(released)}')


def test_max_open_seats_is_configurable(tmp_path):
    """上限可配（`--max-open-seats` 落到 `state.args`）：不再写死 3。"""
    _init(tmp_path)
    web_state.args.max_open_seats = 2
    seats = [open_session_seat(f'c{index}', allow_missing=True) for index in range(3)]

    assert len(web_state.seats) <= 2, f'应收敛到配置的上限内，实际 {len(web_state.seats)}'
    assert seats[-1].evicted, '最后一次打开应当报告它释放了谁'


def test_negative_max_open_seats_does_not_evict_everything(tmp_path):
    """自查发现的边界 bug：`--max-open-seats` 传负数时，不许把空闲席位全踢掉。

    现场：`evict_idle_seats` 的条件是 `while len(state.seats) > max_open`。负数会让它恒真，
    于是"LRU 收敛"退化成"逐个踢光"（只剩 `keep` 与焦点会话）——用户的多个标签页会一起失效。
    上限夹到 >= 1 之后：负数 = 用缺省 3。
    """
    _init(tmp_path)
    web_state.args.max_open_seats = -5
    seats = [open_session_seat(f'n{index}', allow_missing=True) for index in range(4)]

    assert len(web_state.seats) >= 2, (
        f'负数上限把席位踢光了（剩 {len(web_state.seats)} 个）——应收敛到缺省值而不是 0 或 1')
    assert all(seat is not None for seat in seats)


def test_concurrent_open_same_sid_creates_one_seat(tmp_path):
    """并发两条"首消息"打开同一会话：**只能有一个 Seat/Agent**（否则两个 agent 各写同一日志）。

    现场：`/chat` 里 `state.seats.get(sid) or await _offload(open_session_seat, …)`——`await`
    会让出循环，于是两条并发请求都可能看到"还没建"，各自在线程里建一个 Seat。文件层的
    `touch(exist_ok=False)` 只防跨进程，防不了进程内这把竞态。
    """
    import threading
    import time as _time

    _init(tmp_path)
    real = web_sessions.repair_dangling_tool_calls

    def slow_repair(session):
        _time.sleep(0.05)          # 撑开创建窗口，让四条线程真的重叠
        return real(session)

    web_sessions.repair_dangling_tool_calls = slow_repair
    try:
        results: list[object] = []
        lock = threading.Lock()

        def opener():
            seat = open_session_seat('race', allow_missing=True)
            with lock:
                results.append(seat)

        threads = [threading.Thread(target=opener) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        web_sessions.repair_dangling_tool_calls = real

    assert len(results) == 4
    assert len({id(seat) for seat in results}) == 1, '并发打开产生了多个 Seat（各带一个 Agent）'
    assert web_state.seats['race'] is results[0], '登记的必须就是大家拿到的那个'
    assert results[0].agent is not None


def test_seat_is_registered_only_after_agent_is_ready(tmp_path):
    """登记必须在 `build_agent` **之后**：否则循环里别的请求会查到 agent 还是 None 的席位。"""
    _init(tmp_path)
    seen: dict[str, object] = {}
    real = web_sessions.build_agent

    def spy(session, args, counters, *, hooks):
        seen['registered_during_build'] = 'probe' in web_state.seats
        return real(session, args, counters, hooks=hooks)

    web_sessions.build_agent = spy
    try:
        open_session_seat('probe', allow_missing=True)
    finally:
        web_sessions.build_agent = real

    assert seen['registered_during_build'] is False, (
        'agent 还没建好就把席位登记了——并发下 /chat 的 assert seat.agent is not None 会 500')
    assert 'probe' in web_state.seats


def test_reusing_a_seat_clears_stale_evicted(tmp_path):
    """复用席位时清掉上次创建留下的"已释放"记录（否则前端会弹过时的提示）。"""
    _init(tmp_path)
    web_state.args.max_open_seats = 1
    open_session_seat('old', allow_missing=True)
    fresh = open_session_seat('new', allow_missing=True)
    assert fresh.evicted, '超过上限时应当报告释放了谁'

    again = open_session_seat('new', allow_missing=True)     # 复用
    assert again is fresh
    assert again.evicted == [], f'复用后仍带着过时的 evicted：{again.evicted}'
