<div align="center">

# MyCoder

**deepseek-harness 的 Python 复刻** —— 一个能读代码、改文件、跑命令、联网搜索的本地编码 agent。

*把 agent 框架本身讲清楚的实现：四层架构 + 真实工具集 + 零构建 Web UI。*

`日志是唯一事实源` · `模型可见 ⟺ 可重建` · `被动状态机 + Inbox` · `决策走钩子`

![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![tests](https://img.shields.io/badge/tests-154%20passing-3fb950)
![license](https://img.shields.io/badge/license-MIT-4d6bfe)
![web](https://img.shields.io/badge/Web%20UI-%E9%9B%B6%E6%9E%84%E5%BB%BA%20%C2%B7%20%E5%8D%95%E6%96%87%E4%BB%B6-39c5cf)

</div>

---

## 这是什么

把 [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)（`dsh`）的核心架构，用
**6700 行 Python + 2400 行零构建前端**重写一遍（另有 5200 行测试）——不是又一个 agent 应用，
而是**把 agent 框架本身讲清楚**：每一处设计都对着它的母本，每一条规则都有测试盯着。

它不是玩具：模型真能在这个仓库里读文件、搜代码、改代码、跑 `pytest`、联网查资料，
并且**每次对话只被允许读写你指定的那个目录**。

```console
$ my-coder --fake --workspace . "read README.md and summarize"

════ turn 1 ════
── step 1.1 ──
[req 1 fake-model]
[思考] 用户让我总结 README，先读取文件内容再回答。

[tool 1] read_file({"file_path": "README.md"})
[result]      1: <div align="center">
     2:
     3: # MyCoder
  (…共 N 字符 / M 行)
── step 1.2 ──
[req 2 fake-model]
[思考] README 已经读完，核心是四层架构，现在整理成简短总结。
README 讲的是这个项目的四层架构。任务完成。
```

`--fake` 是脚本化的假模型：**不联网、不需要 API key**，用来把整条链路（请求 → 工具调用 →
结果回灌 → 收尾）跑给你看。换成真模型只是去掉这个参数。

### 四个核心设计

| 设计 | 一句话 | 换来了什么 |
|---|---|---|
| **日志是唯一事实源** | 所有状态都落 `.sessions/<id>.jsonl`，没有第二份 | 日志本身就是调试器 |
| **模型可见 ⟺ 可重建** | 模型记忆是日志的**纯函数投影**，不是被存下来的 | 崩溃/OOM 后重放日志即可原样续跑 |
| **被动状态机 + Inbox** | agent 从不主动干活，谁跟它说话谁拍醒它 | 排队、插队、并发会话互不干扰 |
| **决策走钩子** | 循环里没有业务 `if`，策略由注册声明与钩子注入 | 加工具/改策略不碰框架代码 |

---

## 30 秒上手

```sh
# ① 环境与安装（Python ≥ 3.11）
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                          # 装上依赖，并得到 my-coder / my-coder-web 命令

# ② 离线跑一遍（脚本化假模型，不需要 key）
my-coder --fake --workspace . "read README.md and summarize"

# ③ 接真实模型（DeepSeek 官方 API，OpenAI 兼容）
#    Windows PowerShell: $env:DEEPSEEK_API_KEY = "sk-..."
export DEEPSEEK_API_KEY=sk-...
my-coder --workspace . "给这个仓库补一个测试并跑通"

# ④ 浏览器版（默认 http://127.0.0.1:8000；--workspace 是新对话的默认工作区）
my-coder-web --workspace . --fake
```

> `--workspace` **必填**：它是工具唯一的读写边界，由你显式声明。
> 不带任务参数启动 CLI 会进入 REPL（多轮对话，`/exit` 退出，`--resume` 续上上次）。
> Windows 控制台是 GBK，前面加 `--no-capture-output`（用 conda 时）可避免中文乱码；
> 本仓库的开发环境是 conda，具体环境名与等价命令见 `AGENTS.md`。

---

## 能做什么

### 十个工具

| 工具 | 说明 |
|---|---|
| `read_file` / `list_files` | 读文件（行号分页，大文件不会一次读爆上下文）、列目录 |
| `grep` / `glob` | 按内容搜 / 按路径找；跳过 `.git`、`.env` 等隐藏条目 |
| `edit` / `write_file` | 精确字符串替换（零匹配或多匹配都拒绝改）/ 整文件写入 |
| `bash` | 跑命令；非零退出码以 `[exit code: N]` 回给模型，输出上限 8000 字符 |
| `todo_write` | 跨回合的任务清单——模型每次重发整张表，完成一项标一项 |
| `web_search` | 联网搜索：走 **DeepSeek 官方原生搜索**（服务端工具），不自己抓网页 |
| `skill` | 按名字取技能正文（包内 `bundled_skills/` + 工作区 `skills/`），技能是"指令文件"不是新能力 |

`edit` / `write_file` / `bash` 执行前会**停下来等你批准**（CLI 输 `y`，Web 点按钮）；
拒绝不是失败：模型会收到一条"没执行"的结果，自己换方案。工具失败（坏参数、异常、超时）
一律降级成一条 `is_error` 结果，**不会炸掉对话**。

### 两个入口，同一份日志

**CLI** 渲染成终端，**Web** 渲染成 DOM——UI 只是日志的投影，框架代码一行不改。

Web 端：流式输出 + 可折叠思维链 + 工具卡片 · 多会话管理（新建/切换/删除/双击改名，
首条消息后自动起名）· 批准按钮 · **每对话一个工作区**（创建时定下、写进日志、不可变）·
常驻 todo dock · **运行中插队**（`steer`）· 待处理消息分区（排队进队列区，插队画在消息流尾部）·
输入框旁的**上下文占用圆环**（占用 %、全会话 token、缓存命中率、手动压缩按钮）·
两会话并行互不干扰（按 seat 隔离）。

### 长会话不会撑爆

`compaction` 把旧回合折叠成结构化 checkpoint（四步事务 + `<compacted-summary>` 标签）：
**溢出恢复**（模型报上下文过长 → 压缩后重试）+ **阈值自动压缩**（默认过半窗口即压）+
**手动压缩**（圆环面板按钮）。token 成本与缓存命中率是日志的投影，不需要第二份记账。

### 崩溃自愈

进程被 kill / 断电时，日志可能停在"工具已调用、结果未落"之间——这会让之后的请求**永久 400**。
恢复入口会自动补一条 `is_error` 合成结果并留下修复痕迹（`my_coder/state/recovery.py`），
**重放 + 自愈 = 零额外状态代码**地续跑。

---

## 架构一眼

```
用户输入 ─▶ Inbox（持久化队列） ─▶ wake 唤醒被动状态机 ─▶ turn/step 两级循环
                 │                                          │
                 └──────────── Session 事件日志 ◀────────────┘
                               唯一事实源，全部状态都是它的投影
```

包结构按**分层**组织，依赖方向只有一条（上层依赖下层，下层不感知上层），
并且由 `tests/test_architecture.py` **机械检查**——下层 `import` 上层当场红：

```
入口   my_coder/cli.py · my_coder/web/                CLI 与 Web 两个宿主
框架   my_coder/runtime/{agent,loop}.py               被动状态机 · turn/step 循环（step = 一次模型请求）
状态   my_coder/state/{session,inbox,prompt,registry,recovery,runtime_status}.py
能力   my_coder/capability/{llm,hooks}.py
值     my_coder/values/{messages,persistence,limits}.py
应用   my_coder/app/{factory,constants,sandbox,workspace,ui,skills,instructions,compaction}.py
       my_coder/tools/（十个工具）· my_coder/bundled_skills/（随包发布的技能）
```

## 五条不变式

改动任何代码时不能破的规则（细节见 `AGENTS.md`）：

1. **没有状态不进日志**——工具结果、注入、配置变更都要落事件
2. **模型可见 ⟺ 可重建**——只有三类 surface 事件能进模型消息，`derive_messages()` 是纯函数
3. **入队即记账**——inbox 先落 `spliced` 事件再改内存，磁盘日志永远 ≥ 内存状态
4. **决策走钩子/注册声明**——`pre_step` / `request` / `request_error` + 工具注册声明，循环里不写业务 `if`
5. **失败降级为结果**——工具失败变 `is_error` 结果不炸循环；`CancelledError` 沿 await 链单向传播

---

## 文档地图

| 文档 | 回答什么问题 |
|---|---|
| **`USAGE_zh.md`** | 只想把 agent 跑起来 —— 参数、会话恢复、审批、FAQ |
| **`ARCHITECTURE.md`** | 为什么这样设计 —— 21 个核心机制、数据流、日志样例、模块职责、路线图 |
| **`agent.md`** | 别人怎么做的 —— DSH / PI / opencode 三家机制对照（参考手册，非规范） |
| **`NEXT_STEPS.md`** | 做到哪一步了 —— 实施进度、已定设计决策、待办 |
| **`AGENTS.md`** | 给（AI）协作者看的规则 —— 命令、不变式、约定 |
| **`web/PROJECTION_DESIGN.md`** | 前端为什么这么写 —— nodes 投影模型与 SSE 帧协议 |
| `show_memory.py` | 亲手验证"记忆 = 日志投影" —— 重放日志，打印每次请求时的消息序列 |

---

## 安全警告

文件工具被限制在**当前对话的工作区**内（越界返回 `path outside workspace` 错误结果）。
这是**纯用户态的路径边界**（归一化 + 前缀匹配），**不是 OS 级沙箱**：工作区内任意读写、
TOCTOU 竞态、符号链接竞态都不设防。`bash` **没有命令级沙箱**（命令可以删除工作区外的文件），
刹车只有两道：`cwd` 限制 + 审批确认门。

Web 端默认只绑 `127.0.0.1` 且不校验来源——**不要**把它暴露到网络上。仅供本地学习与个人使用。

## 开发

```sh
python -m ruff check my_coder tests   # 风格
python -m mypy my_coder               # 类型
python -m pytest                      # 154 个测试（3 条平台相关会 skip）
```

三绿才提交。测试按关注点分 15 个文件 + `conftest.py` + `test_architecture.py`（依赖方向 = 包结构）。

## License

MIT —— 声明在 `pyproject.toml` 的 `license` 字段（仓库暂未放 `LICENSE` 文件）。
