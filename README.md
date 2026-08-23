# agent-demo

deepseek-harness 核心架构的 Python 复刻（学习用）。约 1000 行代码，忠实实现
harness 的四个核心设计：**日志是唯一事实源、模型可见 ⟺ 可重建、被动状态机 +
Inbox、决策走钩子**。

## 快速开始

```sh
# 环境（Miniforge）
conda create -n agent-demo python=3.13 pytest pytest-asyncio httpx -y
conda activate agent-demo

# 离线演示（脚本化假模型，不联网、不需要 key，跑通工具循环）
# --workspace 必填：工具只能读写这个目录（安全边界由你声明）
# Windows 控制台是 GBK：conda run 加 --no-capture-output 避免中文乱码
conda run --no-capture-output -n agent-demo python main.py --fake --workspace . "read README.md and summarize"

# 恢复上次会话（JSONL 重放：队列、回合号、请求配置全部还原）
conda run --no-capture-output -n agent-demo python main.py --fake --workspace . --resume "continue"

# 真实模型（DeepSeek 官方 API，OpenAI 兼容格式；敏感工具执行前会弹 [approval] 确认）
# Windows PowerShell: $env:DEEPSEEK_API_KEY = "sk-..."
export DEEPSEEK_API_KEY=sk-...
conda run --no-capture-output -n agent-demo python main.py --workspace . "读一下 main.py 并用 todo_write 列出你的三步计划"

# 测试
conda run -n agent-demo python -m pytest -q
```

思维链（如 DeepSeek 的 `reasoning_content`）默认会以彩色 `[思考]` 实时显示，
但**不会回灌给模型**，只作为痕迹数据写入日志。不需要看思考过程时加 `--hide-reasoning`：

```sh
python main.py --fake --workspace . --hide-reasoning "read README.md and summarize"
```

运行后所有事件落在 `.sessions/<id>.jsonl`（每行一条事件），日志本身就是
调试器——模型每一步看到什么、工具干了什么，都按 seq 记录在案。

## 一句话架构

```
用户输入 ─▶ Inbox（持久化队列） ─▶ wake 唤醒被动状态机 ─▶ turn/step 两级循环
                 │                                          │
                 └──────────── Session 事件日志 ◀────────────┘
                               唯一事实源，全部状态都是它的投影
```

## 架构：四层单向依赖

```
入口层  agent.py      被动状态机：send → inbox → wake → driver → idle
                        │
循环层  loop.py       turn/step 两级循环 + 三个钩子
                        │
状态层  session.py    追加式事件日志（唯一事实源）+ derive_messages 投影
        inbox.py      双队列 pending 消息（spliced 事件的持久化投影）
        prompt.py     sections 按 order 拼接 + {{变量}} 严格插值
        tools.py      工具注册表：schema + executor + 执行模式
                        │
值 层  values.py      不可变 Message/SessionEvent + JSONL 编解码
```

依赖方向只有一条：上层依赖下层，下层不感知上层。

## 一次回合的数据流

```
用户输入 → create_user_message（值层：blocks + source + id，不可变）
  → send → inbox.append → 先落 agent/inbox/spliced 日志，再改内存（入队即记账）
  → wake → turn/start → claim（认领也落一条 spliced 删除事件）
  → pre_step 钩子（可改写消息或拒绝）→ user/message 落日志
  → step 内层 while：
      request/header 落日志（含 system 全文，resume 时恢复路由）
      → 流式：有内容的 chunk 落 assistant/chunk；有思维链时另落 assistant/reasoning/chunk
      → 结束时落完整 assistant/reasoning（痕迹数据，不进模型记忆）
      → assistant/message 落日志（usage、finish_reason）
      → 有工具调用：按 parallel/sequential 分组执行，每结果落 tool/result 表面日志
      → 回到 while 顶部，derive_messages 自动带上工具结果
  → turn/end{reason: completed|max-tokens|blocked|aborted|error} → 回 idle
```

所有事件追加写入 `.sessions/<id>.jsonl`；恢复 = 重放，零额外代码。

## 五条不变式

1. **日志是唯一事实源**：没有状态不进日志（工具结果、注入、配置变更都记）
2. **模型可见 ⟺ 可重建**：同一段日志 derive 出同一份消息序列（测试断言这一点）
3. **入队即记账**：inbox 改动先落 spliced 事件再改内存；队列是日志的投影
4. **决策走钩子**：pre_step / request / request_error 三个钩子，循环里没有业务 if
5. **取消单向传播**：asyncio.CancelledError 沿 await 链贯穿流式与工具执行

## 日志长什么样

```jsonl
{"seq":0,"type":"agent/inbox/spliced","data":{"target":"next-turn","start":0,"removed_count":0,"inserted":[...]}}
{"seq":1,"type":"turn/start","data":{"turn":1}}
{"seq":2,"type":"agent/inbox/spliced","data":{"target":"next-turn","start":0,"removed_count":1,"inserted":[]}}
{"seq":3,"type":"step/start","data":{"turn":1,"step":1}}
{"seq":4,"type":"user/message","data":{"$message":{...}},"surface_op":"append"}
{"seq":5,"type":"request/header","data":{"provider":"deepseek","model":"deepseek-chat","system":"...","tools":[...]}}
{"seq":6,"type":"assistant/chunk","data":{"chunk":{"text":"..."}}}
{"seq":7,"type":"assistant/reasoning/chunk","data":{"reasoning":"..."}}
{"seq":8,"type":"assistant/reasoning","data":{"reasoning":"..."}}
{"seq":9,"type":"assistant/message","data":{"message":{...}},"surface_op":"append"}
{"seq":10,"type":"tool/call","data":{"call_id":"call-1","name":"read_file","arguments":"{...}"}}
{"seq":11,"type":"tool/result","data":{"$message":{...}},"surface_op":"append"}
{"seq":12,"type":"step/end","data":{"turn":1,"step":1}}
{"seq":13,"type":"turn/end","data":{"turn":1,"reason":"completed"}}
```

关键点：`surface_op:"append"` 标记"这条事件会变成模型消息"；`derive_messages()`
只折叠这三种类型（user/message、assistant/message、tool/result），chunk、
思维链、边界、todo 等是痕迹数据，不进模型请求。

## 模块清单

| 文件 | 角色 |
|---|---|
| `values.py` | 值层：不可变 Message/SessionEvent + JSONL 编解码 |
| `session.py` | 日志 + surface 折叠投影（append / derive_messages / adopt / request_header） |
| `inbox.py` | 双队列（next-turn / next-step）+ claim 语义 + 持久化重放 |
| `prompt.py` | sections 按 order 拼接 + `{{var}}` 严格插值（未注册/无值抛错） |
| `tools.py` | 工具注册表（schema + executor + parallel/sequential + 超时） |
| `llm.py` | OpenAI 兼容 SSE 流式客户端 + 可脚本化 FakeLlm + wire 格式纯函数（含思维链字段解析） |
| `hooks.py` | pre_step / request / request_error 三个钩子的类型 |
| `loop.py` | turn/step 两级循环 + 流组装 + 工具分组执行 + 思维链痕迹落盘 |
| `agent.py` | 被动状态机：wake / kick / when_idle / cancel |
| `persistence.py` | JSONL 追加写 + 重放读 |
| `main.py` | CLI + 示例工具（read_file 行号分页 / list_files / grep / glob / edit / write_file / bash / todo_write）+ 日志驱动 UI |
| `tests/test_demo.py` | 31 个架构测试 |

## 与 harness 的保真度对照

| 学到并实现 | 简化/未实现（harness 的生产级增量） |
|---|---|
| surface 事件标记 + 纯函数折叠投影 | surface replace 区间遮蔽 + 溯源校验（compaction 用） |
| Inbox 双队列 + claim 语义 + 持久化重放 | 多宿主并发仲裁、steer 中断当前步 |
| sections + 严格 `{{var}}` 插值 | 作用域链 shadow（子 agent 换 persona）、complete 段 |
| 工具分组执行（parallel/sequential）+ 坏 JSON 兜底 | approval/权限桥、`[exit code: N]` 式跨调用准则 |
| request/header 落日志 + resume 恢复路由 | checkpoint 策略、持久化后端抽象 |
| 三个钩子（回调版） | 事件总线（emit/serial/waterfall + 作用域过滤） |
| CancelledError 贯穿 + when_idle 收敛 | 三源 abort 熔合（调用方/owner fiber/工厂销毁） |
| JSONL 追加 + adopt 重放 | 未知事件类型拒绝策略、ignorable 标记 |
| OpenAI function-call wire 格式（真模型可调工具） | max-tokens 粘性、compaction 触发 |

## 测试覆盖的架构行为

- 日志推导：只有 surface 事件投影成消息，顺序可重建
- Inbox：先记账后投影、重放恢复、claim 批次语义、重复 id 拒绝
- 插值：严格校验、字面量大括号、嵌套大括号拒绝
- 工具循环：假模型两步脚本（tool-call → 文本）跑通完整回合
- 钩子：pre_step 改写/拒绝、request_error 重试（RATE_LIMIT 后恢复）
- 取消：中途 cancel → turn/end 记 aborted → 状态机回 idle
- 持久化：JSONL 回写回放、resume 恢复队列与回合号
- wire 格式：OpenAI function 包装、tool_calls 回传、role:tool 结果
- 工具：read_file 行号分页（offset/limit/line_numbers、越界与坏参数降级为 is_error）
- 沙箱：workspace 边界（绝对路径越界、`..` 逃逸、越界写入不落盘、grep/glob 越界拒绝）
- 工具：grep/glob 搜索（分组/截断/include）、edit 字面量唯一匹配（零/多匹配拒绝）、bash 退出码/截断/超时 kill
- approval：敏感工具（bash/write_file/edit）执行前确认（拒绝 → tool/skipped + is_error 结果）

## 安全警告

所有文件工具被限制在 `--workspace` 指定的目录内（**必填**，越界读写返回
`path outside workspace` 错误结果）——这是**纯用户态的路径边界**（归一化 +
前缀匹配，对齐 harness 的 fs-sandbox 思路），**不是 OS 级沙箱**：工作区内
任意读写、TOCTOU 竞态（校验与访问之间的时间窗）、符号链接竞态都不设防。
`bash` 工具**没有命令级沙箱**（命令可以删除工作区外的文件），刹车只有
两道：`cwd` 限制 + approval 确认门（执行前人工确认，默认拒绝）。
只用于本地学习，不要暴露给不可信的输入。
