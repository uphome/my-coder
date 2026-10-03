# Agent Note: 认领与落盘是原子的——取消不再静默吞掉用户消息

Status: implemented

## Problem

`run_turn` 每步开头的顺序曾经是：

```python
claimed = inbox.claim(target, turn)              # ① 把消息从队列摘掉（落 spliced 账）
messages = await _resolve_pre_step(...)          # ② await：取消点
session.append('user/message', message, ...)     # ③ 浮上水面
```

② 是个 `await`，于是 ①②③ 之间有一个窗口。实测（探针：让 `pre_step` 挂 5 秒再取消）：

```
事件序列: ['agent/inbox/spliced', 'turn/start', 'agent/inbox/spliced', 'turn/end']
  spliced: None removed= 1 target= next-turn     ← 认领把消息摘掉了
取消后 —— 队列: [] | surface 里的 user 消息: []
```

那条消息**三处都不在**：队列里没有、surface 里没有、日志里**也没有 `outcome=canceled`**。
而且 `Agent.cancel()` 会调 `inbox.clear()`——它也救不回来，消息压根不在队列里了。
用户原话静默消失，事后无法审计。这是不变式①（没有状态不进日志）的反例：
一次"摘除"发生了，却没有任何记录说明它去哪了。

同源还有第二个窗口：`user/message` 落盘之后、`request/header` 之前要
`await _resolve_request(...)`（`request` 钩子），在那里取消会留下"有用户消息、没有请求"
的半提交。

## Decision

**把两个 await 都提到 `claim` 之前，让"认领 → step/start → user/message"成为一段没有
await 的原子区**：

```python
pending = inbox.peek(target)                     # 只读候选，不改队列
messages = await _resolve_pre_step(...)          # await ①（队列未动）
...早退分支...
config = await _resolve_request(...)             # await ②（队列仍未动）
closing_step = agent.progress.closing
# ---- 以下到 user/message 落盘之间没有 await ----
inbox.claim(target, turn)                        # 提交
session.append('step/start', ...)
for message in messages: session.append('user/message', message, surface_op='append')
```

取消落在哪儿都有确定语义：

- **落在钩子里** → 队列一个字没动，`clear()` 走正常路径留下 `outcome='canceled'`（有记录）；
- **落在提交之后** → 那是"真的开始了这一步、随后被中断"，消息已在日志里（不是半提交）。

配套三处：

1. **`Inbox.peek(target)`**（新）：只读地看一眼这一步会认领到哪些消息，**批次规则与
   `claim` 逐字一致**（先整个 next-step、再从 next-turn 取一条）——否则"钩子看到的"
   与"真正认领的"会漂。
2. **`_run_step` 收一个已解析的 `config`**：由 `run_turn` 在认领前解析好传进来；
   `request_error` 返回 `retry` 时**函数内重新解析**（重试要按当时的钩子取路由，
   不能用失败前那份快照）。
3. **钩子拒绝（`pre_step` 返回 None）时也要消费批次**：拒绝的语义是"这条不发给模型"，
   不是"留着下次再问一次"。不消费的话消息还在队列里，`run_turn` 返回
   `has_pending=True`，被动状态机立刻拿同一条再开一轮——**无限空转**（实测：测试
   40 秒不返回）。

## Alternatives considered

- **认领后、取消时"放回队列"**：否决。放回要走两次 splice（移除 + 插入），
  中间又冒出取消点；而且"放回"会造出与用户原意相反的状态（他按了停止，
  系统却把它重新排队）。原子提交更简单也更诚实。
- **把 `pre_step`/`request` 钩子挪出取消作用域**（取消时等钩子跑完）**：否决**。
  那正是「停止」按钮最该立刻生效的地方——把用户卡在一个他不要的钩子里，
  比丢一条消息更糟。取消必须立刻生效。
- **只用 `try/finally` 包住 claim 之后回滚**：否决。回滚要重放"认领前的队列状态"，
  等于在内存里维护第二份状态；而且取消路径上再 await（回滚落账）又引入新窗口。
- **保留 `claim` 在前，把 `pre_step` 改成同步钩子**：否决。钩子要能查工作区/等外部
  条件，异步是它的能力；为了顺序把能力砍掉是本末倒置。
- **`peek` 直接返回 `claim` 的结果**（认领后再"等价回滚"）：否决。同第二条，
  回滚即第二份状态。

## Consequences

**换来的**：取消不再静默吞消息；两种取消（钩子内 / 提交后）各有确定且可审计的语义；
`clear()` 恢复它本来的作用（清掉尚未开始的步骤）。

**付出的 / 边界**：

- `peek` 与 `claim` 是**两处**同规则的代码，靠注释与测试约束一致（批次规则：
  next-step 整批 + next-turn 一条）。
- `pre_step` **改写**返回一条新 id 消息时，`claim` 摘掉的仍是队列里那条（新消息没经过
  队列，直接落成 `user/message`）——这是钩子契约里的既有语义，本次未改。
- `pre_step` 拒绝的步骤现在**消费掉**了那条消息（见上文决策 3）——语义变化，写在测试里。

## Testing

```sh
conda run -n agent-demo python -m pytest tests/test_loop.py -q   # 28 passed
conda run -n agent-demo python -m pytest -q                      # 215 passed, 3 skipped
```

两条新用例：

- `test_cancel_during_pre_step_leaves_the_message_in_the_queue`——`pre_step` 里挂住 →
  取消 → 断言日志里**有** `outcome='canceled'` 记录、消息没进 `derive_messages`、
  `turn/end` 记 `aborted`；
- `test_cancel_after_claim_still_lands_the_user_message`——模型已经开始流式 → 取消 →
  断言那条 `user/message` **在**（认领与落盘原子，提交后取消不是半提交）。
