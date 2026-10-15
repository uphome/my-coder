"""issue #46 ①：冷却席位释放——只在空闲时释放、受保护的绝不释放、重开靠重放恢复。

为什么值得单独测：释放 = 丢掉整个 Seat 的内存状态（Session / Agent）。判据错一条就会
**把进行中的回合连根拔掉**（日志里只剩一半），所以三条边界都要钉住：
① 超过上限才释放；② 忙的（在跑 / 有 SSE 流 / 有待审批）与受保护的（焦点 / 当前）不释放；
③ 释放不丢数据——重开时 adopt 重放，记忆照旧。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

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
