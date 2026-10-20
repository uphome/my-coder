# 可复用小队的角色卡

> 这是**工作流**，不是面试内容：把本轮用过的成员固化成"可重建的角色"。
> 同一会话里它们一直保留（`inactive` 只是没在跑，`send_message` 会唤醒、上下文还在）；
> **新会话**里按本表用 `spawn_teammate(name, description, prompt)` 一键重建。
>
> 分工原则：**一个写范围 = 一个人**；Lead 只做派活、整合、全量验收。

## 成员表

| 成员 | 角色 | 写范围 | 什么时候再叫它 |
|---|---|---|---|
| `fact-checker` | 只读事实核查员 | **无**（只报告） | 文档或代码改完后，核查断言/行号/数字；它的价值是**对抗**，所以永远不给写权限 |
| `fix-tools-flags` | 工具声明与宿主装配 | `tools/skill.py`、`web/app.py`、`tests/test_skills.py`、`tests/test_web.py`、`tests/test_seats.py` | 工具声明、宿主参数接线、对应测试 |
| `fix-resume-render` | CLI 渲染 / 重放 | `app/ui.py`、`tests/test_cli.py` | UI 投影、`--resume`、渲染去重 |
| `fix-compaction-atomic` | 压缩事务 / 文件 I/O | `app/compaction.py`、`tools/file_io.py`、`tests/test_tools.py` | 压缩四步、原子写、文件工具 |
| `fix-approval-race` | Web 并发 / 审批 | `web/app.py`、`tests/test_web.py` | 竞态、SSE、审批、断线取消 |
| `fix-scan-dedupe` | 日志扫描 / 持久化口径 | `values/persistence.py`、`app/recall.py`、`web/sessions.py`、`tests/test_recall.py` | 行扫描、计数口径、L0 目录 |
| `docs-writer` | 文档作者 | `docs/interview/04-story-arc.md`、`05-drills.md` | 新增派生文档（只写，不碰代码） |

> 早期那 4 名只读调查员（状态层 / 应用层 / 入口层 / 测试与文档）是 `subagent`，
> 一次性、**没有保留**——它们的产出已经并进 `01-tour`、`02-decisions`、`03-qa`、
> `04-story-arc`。需要再挖一层时，按同样的方式临时派 `subagent` 即可。

## 复用协议

1. **先建任务再叫人**：`team_task_create(subject, description, write_scopes)` →
   `send_message(target, "读 task-N → claim → 做 → complete")`。
2. **写范围重叠就不许并行**：同一文件同一时刻只给一个人；任务板上的
   `writeScopeWarnings` 是在提醒这条。
3. **验收固定两条**：目标测试文件 + `ruff check my_coder tests`；成员自己跑完再报。
   Lead 在整合后跑 `ruff` / `mypy` / `pytest` 全量三绿，并同步文档数字。
4. **成员报告 ≤15–20 行**：改了什么、测试输出行、遗留/越界项。
   "需要改范围外文件"必须先报告，不许擅自越界。
5. **不做的事**：不许 `git add` / `git commit`（提交由 Lead/用户决定）；
   不许顺手格式化；不许改任务描述之外的语义。

## 何时重建（跨会话）

新会话没有这些成员，但角色定义在上表里，一条消息就能重建：

```text
spawn_teammate(
  name="fact-checker",
  description="只读核查 docs/interview 的事实与行号",
  prompt="你是只读核查员，工作目录 E:\\codeall\\agent-demo；"
         "只报证据不改文件；输出【错误】【不精确】【遗漏】，每条 文件:行号 + 建议改法；"
         "不许 git add/commit。任务：<本轮的 delta 清单>",
)
```

## 这套分工为什么能复用

- **按文件切，不按任务切**：一次修复通常"实现 + 测试"要一起改，按文件切才不会互相踩。
- **只读核查员单列**：它必须能说"你写错了"，所以不能有写权限。
- **Lead 不写实现**：只改文档数字与做整合，于是"谁改的"永远可追，最终 diff 也只有一个审阅点。

## 已知小限制

- `_atomic_write_text` 用 `NamedTemporaryFile` + `os.replace`：POSIX 下临时文件是 `0600`，
  rename 后目标文件权限也变成 `0600`（Windows 无影响）。要修的话交给
  `fix-compaction-atomic`：rename 后 `os.chmod` 保留原文件 mode（新文件按 umask）。
