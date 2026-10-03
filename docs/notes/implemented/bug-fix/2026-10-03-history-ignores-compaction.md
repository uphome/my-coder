# Agent Note: 界面历史改走全量日志（压缩不再让人"失忆"）

Status: implemented

## Problem

压缩之后，**界面上的对话从 checkpoint 之后开始**，而且没有任何提示说明前面还有。
实测会话 `web-1788604766`（2026-10-03，冻结快照核对）：

| | 数值 |
|---|---|
| 事件总数 / surface（模型可见） | 425,985 / **138** |
| **不在 surface 上** | **425,847** |
| 界面 `/history` | **132 行**，最早可见回合 = **73**（共 78 个回合） |
| 压缩次数 | 2 次（`replace` 分别遮蔽 `(4, 10151)`、`(10644, 396429)`） |

内容一条没丢：`build_turns` 仍能列出全部 78 个回合、`read_turn` 能按行分页取回原文。
但**人看不到**——`/history` → `history_payloads()` → `surface_with_seq(session)`，
而 surface 的定义是"**模型此刻看得见的那一份**"。压缩本来只是省**模型**的额度，
却顺带抹掉了**人**的记忆，而且不留痕迹。

## Decision

**历史走全量日志**（新增 `log_messages_with_seq(session)`：按日志序遍历
`user/message` / `assistant/message` / `tool/result`，**不看 surface**）。

判据：**UI 是日志的投影**（"发生过什么"），而"模型此刻看得见什么"是一个**独立的边界**——
它由一个显式标记表达，不能靠"把前面的藏掉"来表达。

- 压缩点**在原位**变成一张卡片：`message_to_payload` 已按摘要标记（`SUMMARY_OPEN_TAG`）判出
  `role='checkpoint'`（前端 `addCheckpointCard` 认它），历史循环只补两件历史才需要的东西：
  `seq`（它在日志里的位置）与 `hidden`（**它遮蔽了哪一段 = 模型看不到什么**）。
- 顺手消掉一处字面量副本：`payload.py` 原来硬写 `'<compacted-summary>'`，现在用
  `app/compaction.py` 的 `SUMMARY_OPEN_TAG`（一个事实一个家）。

## Consequences

**换来的**：同一个会话的历史从 132 行变成 **1,378 行**（`user 93 / assistant 615 /
tool_result 668 / checkpoint 2`），最早可见回合从 73 变成 **1**，压缩点有卡片且带"模型看不到
哪一段"。用户不再需要靠 agent 回查才能知道自己说过什么。

**付出的与风险**：

- **界面与模型可见性不再重合**——这正是有意的：重合会让"压缩"变成隐形事故。代价是用户看到的
  比模型多，卡片必须承担"这里开始模型看不到了"的表达责任（字段已经给了：`hidden`）。
- **`/history` 载荷变大**（这个会话 132 → 1,378 行）。它天然有界：消息数等于回合的对话量，
  与流式帧无关（#51 之后帧根本不进日志）。真正巨大的会话仍可能让首屏变重，届时按回合分页。
- **旧契约的测试被改写**：`test_web_checkpoint_role_and_context_payload` 原来断言
  `['checkpoint', 'user']`（**被遮蔽的 Q1 不显示**）——那条断言就是不变量本身，已改成
  `['user', 'checkpoint', 'user']` 并补上 `hidden` 断言。

## Testing

证据类型：**单元测试（零 API）+ 真实会话的冻结快照核对**

- `tests/test_web.py::test_web_checkpoint_role_and_context_payload`：历史按全量日志出行
  （被遮蔽的早期消息**在**）、压缩点是 `role='checkpoint'`、带 `hidden`、摘要标记在正文里；
- 冻结快照核对（`.eval/snapshots/web-1788604766-frozen.jsonl`，111.7 MB，核对前复制、
  核对期间不再变化——**教训**：这份日志在上一轮测量时仍在被实时写入，跨快照比较曾给出
  自相矛盾的读数）：历史 1,378 行、回合 1–78、2 张卡片、`hidden` 与 2 条 `replace` 一致。
