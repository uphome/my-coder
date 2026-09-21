# Web 前端投影模型重构（设计注记）

> 目标：前端只保留**一份从日志重建的投影状态**，实时 SSE 与 /history 全量
> 加载都收敛到它；DOM 不再当状态。todo dock / context 面板保持独立订阅
> （不是对话消息），不进入本投影。

## 1. 投影节点类型

前端持有的投影 = 有序节点数组 `nodes`。每类节点是**不可变的快照数据**
（渲染器只读它画 DOM），实时事件通过"新建节点 / 追加内容"推进数组，
不改已渲染节点的历史——这样刷新重开会话 = 从 /history 重建同一数组。

```
nodes: [
  { kind:'user',       turn, text }                     // 真人发言（首问或插队）
  { kind:'checkpoint', body, chars }                    // 压缩摘要卡
  { kind:'assistant',  turn, step, text, tools }        // 一个助手应答 + 工具活动
  { kind:'tool-item',  callId, name, args, state, error, result }
]
```

说明：
- `assistant` 节点是一个"可生长"的活动节点：实时时 chunk 追加它的 text、
  tool_call/tool_result 追加/更新它的 tools 子项；历史时一次性填满。
- `tool-item` 是 assistant 的**子项**（放 assistant.tools 数组里），渲染成
  工具活动块里的一张卡。
- `turn` 归属在 user/assistant 节点上都有——回合分隔线由"出现新 turn 的
  user"推导，不再前端自数 turnNo。
- 为什么 checkpoint 是独立节点：它是 replace 顶替旧回合产生的 user/message，
  不是真人发言，独立成卡、不进回合计数（沿用现语义）。

## 2. 事件 → 投影的映射（实时增量）

SSE 帧（后端已带 turn/step 标记后）落到投影：

| SSE 帧 | 投影操作 |
|---|---|
| `turn_start`(新增) | 记住当前 turn（供后续节点归属）——画分隔线依据 |
| `chunk` {turn,step,text} | 找/建当前 (turn,step) 的 assistant 节点 → text += |
| `reasoning` {turn,step,text} | 同上节点 → reasoning +=（渲染成可折叠思考块） |
| `tool_call` {turn,step,call_id,...} | 同上节点 → tools 追加 tool-item(running) |
| `tool_result` {call_id,...} | 在**当前 assistant 的 tools** 里按 call_id 配对更新 |
| `turn_end` | 关闭当前 assistant（流式收尾） |
| `todo_update` | 独立订阅（不进投影） |
| `queue_update` | 独立订阅（不进投影）——待处理消息按 placement 分区渲染：`queued` 进输入框上方的队列区，`steering` 进消息流尾部的 pending 气泡 |
| `approval_request` | 独立 UI（不进投影） |

关键：chunk/reasoning/tool_call 都带 **turn+step** 后，投影能精确找到
"属于哪次模型应答"，不再需要 `cur` 指针猜。这是后端改动（D2-A）。

## 3. 历史（全量）→ 投影

`/history` 的消息数组与 SSE 事件**同构**地喂进同一构建器：

- 遇 `role:'user'`（带 turn）→ 若 turn 与上一条 user 不同，先推一个隐含的
  回合边界（画分隔线），再推 user 节点
- 遇 `role:'assistant'` → 推 assistant 节点（text + 已有 tool_calls 全量）
- 遇 `role:'tool_result'` → 因为历史里它紧跟所属 assistant 出现，把它配对到
  **最近一个 assistant 节点**的 tools（消息顺序即因果顺序，可靠）
- 遇 `role:'checkpoint'` → 推 checkpoint 节点

与实时共用 `assistant.tools` 的同一配对逻辑：call_id 命中即更新，未命中
（历史旧数据缺 call_id）退回"最早的 running 卡"。

## 4. 渲染：投影 → DOM（单一入口）

`renderProjection()` 把整个 nodes 数组画进 messagesEl。分层：

- **全量重渲**（刷新/切会话）：清空 → 画全部 → 无动画直接铺满
- **增量追加**（实时）：投影数组在尾部增长/最后节点内容更新——只更新受影响的
  DOM 尾部（新增节点 append；最后一个 assistant 的文本增量走 partialRender）

partialRender（段落固化）**降级为纯渲染优化**：它的 `_raw/_seg` 状态只活在
DOM 节点上、只服务"最后一个 assistant 的文本块如何平滑更新"，不再是会话
事实的一部分。任何时刻调用 renderProjection() 全量重画都能得到一致结果。

## 5. 删除的隐式状态

| 旧状态 | 去处 |
|---|---|
| `cur`（当前助手 DOM + 其 _raw/_toolById/_thinking...） | 删除；assistant 节点在投影里 |
| `turnNo` 前端自数 | 删除；turn 来自事件/载荷 |
| `streaming` 开关 | 仅控制"工具活动块自动展开"，保留为视图偏好 |

## 6. 后端配合

1. SSE 帧补结构标记：`chunk/reasoning/tool_call` 带 `turn`/`step`
   （`event.data` 里已有 turn/step——web_app 的 event_to_payload 现在没透传）
2. 新增 `turn_start` 帧（现在前端看不到 turn/start，靠 addUserMessage 猜新回合）
   ——或改为 user 消息帧：更干净的是在 SSE 里把"真人 user 消息"也发一帧
   `user_message {turn, text}`，替代前端本地 addUserMessage 渲染 + turnNo 自数。
3. /history 载荷已带 user.turn（上个修复）；assistant 消息也可带 turn 便于配对。

## 7. 范围外（保持现状）

- todo dock / context 圆环：独立订阅，不进投影
- 待处理消息：独立订阅，不进投影，**按 placement 分区且恒定贴尾**。
  **未 claim 的消息没有 seq 位置**，往流中间插只能靠 `(turn,step)` 锚点猜顺序
  （位置 bug 的根源）。分区：`queued`（next-turn）→ 输入框上方队列区；
  `steering`（next-step）→ **消息流尾部**的 pending 气泡。claim 落
  `user/message` 后 durable 节点落到真实 seq 位置，尾部那条消失。
  本地提交回显（`pendingSubmissions`）靠提交身份 `rpc_id` 与 durable 内容在
  同一次渲染里交接。勘误与 DSH 源码对照见 `agent.md` §5（DSH 的 steering
  也画在流尾，只有 queued 进 QueueDock）
- approval 卡片：即时 UI，不进投影（不落日志，刷新即消失——现状）
