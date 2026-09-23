"""inbox 双队列：claim 语义、splice 先落日志、队列项操作与队列投影。

（2026-09 从单文件 tests/test_demo.py 按关注点拆出：**断言与用例体一字未改**；
唯一差异是 26 处函数内冗余 import 被 ruff 的 F401/F811 删掉——那是拆分暴露出来的旧问题。）
"""
from __future__ import annotations

import pytest

from agent_demo.state.inbox import Inbox
from agent_demo.state.session import Session
from agent_demo.values.messages import TextBlock, create_user_message


def test_inbox_durable_first_then_replay():
    session = Session(id='s')
    inbox = Inbox(session)
    message = create_user_message([TextBlock(text='x')])
    inbox.append('next-turn', message)
    assert session.events[-1].type == 'agent/inbox/spliced'
    assert session.events[-1].data['inserted'][0].id == message.id
    replayed = Inbox(session)
    assert [m.id for m in replayed.next_turn] == [message.id]


def test_inbox_claim_takes_next_step_batch_then_one_turn():
    session = Session(id='s')
    inbox = Inbox(session)
    turn_msg = create_user_message([TextBlock(text='turn')])
    step_a = create_user_message([TextBlock(text='a')])
    step_b = create_user_message([TextBlock(text='b')])
    inbox.append('next-turn', turn_msg)
    inbox.append('next-step', step_a)
    inbox.append('next-step', step_b)
    claimed = inbox.claim('next-turn', 1)
    assert [m.id for m in claimed] == [step_a.id, step_b.id, turn_msg.id]
    assert not inbox.has_pending


def test_inbox_rejects_duplicate_identity():
    session = Session(id='s')
    inbox = Inbox(session)
    message = create_user_message([TextBlock(text='x')])
    inbox.append('next-turn', message)
    with pytest.raises(ValueError, match='already pending'):
        inbox.append('next-step', message)


def test_inbox_remove_is_durable_and_replays():
    """撤回（队列区 × 按钮）：撤回同样先落 spliced 事件——重放不会复活它。

    未 claim 的消息没有 surface，撤回后日志里只剩 spliced（outcome=canceled）
    这一条痕迹；它不进模型记忆（surface 才进），所以撤回是干净的。
    """
    session = Session(id='s')
    inbox = Inbox(session)
    keep = create_user_message([TextBlock(text='keep')])
    drop = create_user_message([TextBlock(text='drop')])
    inbox.append('next-turn', keep)
    inbox.append('next-step', drop)

    assert inbox.remove(drop.id) is True
    assert [m.id for m in inbox.next_step] == []
    assert [m.id for m in inbox.next_turn] == [keep.id]
    last = session.events[-1]
    assert last.type == 'agent/inbox/spliced'
    assert last.data['removed_count'] == 1 and last.data['outcome'] == 'canceled'
    # 认领过的（不存在于任何队列）撤不回来：返回 False，不改日志
    assert inbox.remove(drop.id) is False
    splices = [e for e in session.events if e.type == 'agent/inbox/spliced']
    assert len(splices) == 3      # 2 次入队 + 1 次撤回；失败的撤回不落事件

    # 重放：队列原地复活成撤回后的样子（撤回不是"内存里删掉"）
    replayed = Inbox(session)
    assert [m.id for m in replayed.next_turn] == [keep.id]
    assert [m.id for m in replayed.next_step] == []


def test_inbox_queue_actions_edit_and_promote():
    """状态层的三个队列动作：edit（同 id 换文案）/ promote（搬家）/ remove。

    edit 的安全性来自"还没 claim 就没有 surface"：改的只是将要成为模型输入的
    内容，日志里只有 spliced 痕迹。promote 是 next-turn → next-step 的搬家，
    两步各自落账、可重放。
    """
    session = Session(id='s')
    inbox = Inbox(session)
    queued = create_user_message([TextBlock(text='先写个草稿')])
    inbox.append('next-turn', queued)

    assert inbox.edit(queued.id, '改成：直接给结论') is True
    items = inbox.queued_items()
    assert items[0].id == queued.id                      # 身份不变（前端那行不闪）
    assert items[0].message.content[0].text == '改成：直接给结论'
    # 一次原子改动（replace 而不是"删+插"两条事件）
    splices = [e for e in session.events if e.type == 'agent/inbox/spliced']
    assert len(splices) == 2 and splices[-1].data['removed_count'] == 1
    assert splices[-1].data['inserted'][0].id == queued.id

    assert inbox.promote(queued.id) is True              # queued → steering
    assert [i.placement for i in inbox.queued_items()] == ['steering']
    assert inbox.promote(queued.id) is False             # 已经在 next-step：幂等无变化
    # 搬家 = 两次 splice（先摘后插）；摘除那步 discard=False：这不是丢弃，
    # 不该标 outcome='canceled'（那个标记专给撤回）
    moves = [e for e in session.events if e.type == 'agent/inbox/spliced'][2:]
    assert len(moves) == 2
    assert moves[0].data['target'] == 'next-turn' and moves[0].data['removed_count'] == 1
    assert moves[0].data.get('outcome') is None
    assert moves[1].data['target'] == 'next-step' and moves[1].data['inserted'][0].id == queued.id

    replayed = Inbox(session).queued_items()             # 重放：编辑与搬家都在
    assert replayed == inbox.queued_items()
    assert replayed[0].message.content[0].text == '改成：直接给结论'

    assert inbox.edit('nope', 'x') is False
    assert inbox.promote('nope') is False


def test_inbox_queued_items_is_a_session_projection():
    """队列投影在**状态层**：Inbox.queued_items() 与 derive_messages() 并列。

    为什么这条要单独测（架构回归）：投影一度被放在 Web 宿主（当时的
    单文件 `web_app.py`）里自己重放 agent/inbox/spliced——同一事件类型两份折叠
    （Inbox._apply 一份、web 一份）必然分叉，而且投影绑死在 Web 宿主上
    （CLI/测试拿不到）。现在只有一份：_state 本身就是重放结果，queued_items()
    只是给它贴上 placement 语义。

    断言三件事：
    - placement 映射：next-turn→queued、next-step→steering，顺序固定
    - 撤回/拼接后投影跟着变（走的是同一份折叠）
    - **换个 Inbox 重放同一段日志，投影逐字段相同**（可重建 ⟺ 模型可见的
      同款保证；resume 后队列区不会变形）
    """
    session = Session(id='s')
    inbox = Inbox(session)
    steer_one = create_user_message([TextBlock(text='插队一')])
    queued_two = create_user_message([TextBlock(text='排队二')])
    steer_three = create_user_message([TextBlock(text='插队三')])
    inbox.append('next-step', steer_one)
    inbox.append('next-turn', queued_two)
    inbox.append('next-step', steer_three)

    items = inbox.queued_items()
    assert [(i.placement, i.id) for i in items] == [
        ('queued', queued_two.id), ('steering', steer_one.id), ('steering', steer_three.id)]
    assert items[0].message is queued_two          # 值对象持有本体，不是副本
    assert items[1].id == steer_one.id             # id 直接取自 message

    inbox.remove(steer_one.id)
    assert [i.id for i in inbox.queued_items()] == [queued_two.id, steer_three.id]

    replayed = Inbox(session).queued_items()       # 日志重放 → 同一投影
    assert replayed == inbox.queued_items()        # frozen dataclass：逐字段相等
