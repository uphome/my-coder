# agent-demo 使用指南

> 面向**想立刻把 agent 跑起来**的你。这份文档只讲「怎么用」，不讲内部架构
> （那个看 `README.md` / `ARCHITECTURE.md`）。

agent-demo 有两种跑法，其余操作完全一样：

- **离线演示**：`--fake` 用脚本化的假 LLM —— 不联网、不需要 API key，走一遍完整
  循环（读文件 → 调工具 → 回答），适合初次体验。
- **真实模式**：接 DeepSeek 官方 API（OpenAI 兼容），agent 真的读代码、搜索、
  跑命令，然后给你有依据的答复。

### 两条必读规矩

1. `--workspace` **必填**：声明 agent 只能在哪个目录里读写 —这是安全边界。
2. `edit` / `write_file` / `bash` 这类敏感动作**不会自己执行**：执行前会先停下来
   问你 `y` / `n`（approval）。agent 是交互式的，别指望它后台无人值守干完。

### 30 秒上手（TL;DR）

你只需要记住三条「一键复制」命令，细节后面各章都有：

```sh
# ① 环境（跑一次后补 pip install -r requirements.txt，见第 1 章）
conda create -n agent-demo python=3.13 pytest pytest-asyncio httpx -y

# ② 离线体验（不联网，30 秒跑通工具循环）
conda run --no-capture-output -n agent-demo python main.py --fake --workspace . "read README.md and summarize"

# ③ 真实任务（先 export DEEPSEEK_API_KEY=sk-...，见第 3 章）
conda run --no-capture-output -n agent-demo python main.py --workspace . "调查这个仓库是干什么的"
```

- 想用浏览器：把上面 `main.py` 换成 `web_app.py`（第 7 章）。
- 感觉 agent 动作危险 → 因为它要你先批 `edit`/`bash`（第 6 章）。

---

## 目录

1. [准备环境](#1-准备环境)
2. [最快跑通（离线）](#2-最快跑通离线-30-秒)
3. [接真实模型（DeepSeek）](#3-接真实模型deepseek)
4. [参数速查](#4-参数速查-mainpy)
5. [会话与恢复](#5-会话与恢复)
6. [敏感工具与审批](#6-敏感工具与审批approval)
7. [Web UI](#7-web-ui浏览器里对话)
8. [跑测试](#8-跑测试)
9. [常见问题 FAQ](#9-常见问题faq)

---

## 1. 准备环境

```sh
# 建环境并装依赖（一次就够）
conda create -n agent-demo python=3.13 pytest pytest-asyncio httpx -y
conda activate agent-demo
pip install -r requirements.txt      # httpx / fastapi / uvicorn（真实 Web UI 需要）
```

后续所有命令都在**仓库根目录**执行。

## 2. 最快跑通（离线，30 秒）

```sh
conda run --no-capture-output -n agent-demo \
  python main.py --fake --workspace . "read README.md and summarize"
```

fake 的回答是预置的两句话 —— 重点看**过程**：读取文件 → 返回结果 → 在几步内结束。
终端上你会依次看到：

| 输出 | 含义 |
|---|---|
| `════ turn 1 ════` | 回合开始（一次用户任务 = 一个 turn） |
| `[req 1 fake-model]` | 发起一次模型请求 |
| `[思考] …` | 思维链（痕迹，"假装在想"；**不进**模型上下文） |
| `[tool 1] read_file(…)` | 调用工具 |
| `[result] … (…共 N 字符 / M 行)` | 工具结果摘要（完整内容都进 JSONL，屏幕只给摘要） |
| 白色正文 | 正式回答 |

换一句话换一项任务：

```sh
conda run --no-capture-output -n agent-demo \
  python main.py --fake --workspace . "read main.py and list the tools it registers"
```

## 3. 接真实模型（DeepSeek）

需要 `sk-...` 的 key（二选一提供）：

```sh
# 方式 A：环境变量
export DEEPSEEK_API_KEY=sk-...              # macOS / Linux
# $env:DEEPSEEK_API_KEY = "sk-..."          # PowerShell

# 方式 B：仓库根目录放 .env（不入库、不提交）
# DEEPSEEK_API_KEY=sk-...
# DEEPSEEK_BASE_URL=https://api.deepseek.com     # 可选
```

**去掉 `--fake`** 再跑：

```sh
conda run --no-capture-output -n agent-demo \
  python main.py --workspace . "调查这个仓库：它是干什么的？测试怎么跑？"
```

它会真实地搜索、读文件再总结。默认模型 `deepseek-chat`；换模型：

```sh
python main.py --workspace . --model deepseek-reasoner "你的任务"
```

> 思考过程在真实模式下也有（模型支持时），默认以 `[思考]` 显示；
> 想折叠可用 `--hide-reasoning`（照常写进日志，只是屏幕不显示）。

## 4. 参数速查（main.py）

| 参数 | 说明 | 必填 |
|---|---|---|
| `… "任务"`（位置） | 让 agent 干什么 | ✅ |
| `--workspace <目录>` | agent 可读写的目录（安全边界） | ✅ |
| `--session <id>` | 会话 id，默认 `main`（对应 `.sessions/<id>.jsonl`） | |
| `--sessions <目录>` | 会话日志目录，默认 `.sessions` | |
| `--resume` | 重放日志并从上次进度继续 | |
| `--fake` | 离线假模型（无需 key） | |
| `--model <id>` | 默认 `deepseek-chat` | |
| `--hide-reasoning` | 折叠思考链（仍写日志） | |
| `--verbose` | debug 日志 | |

## 5. 会话与恢复

agent 的“记忆”就是一份日志：每次对话都**追加**写进 `.sessions/<id>.jsonl`，
从头重放即可完整还原上下文、工具结果、回合号。

```sh
# 延续刚才的会话（默认 id main）——命令带 --resume：
conda run --no-capture-output -n agent-demo \
  python main.py --workspace . --resume "上一步看到哪些文件？"

# 另开互不干扰的新会话：
conda run --no-capture-output -n agent-demo \
  python main.py --workspace . --session mytask "读 main.py 再总结"
```

启动时终端会把历史**重放一遍**再接着跑——你打开的就是「日志的可视化」。

> 同一个 session id 已存在且没加 `--resume` 时，程序会拒绝：带 `--resume` 继续 /
> 换新 `--session` / 删旧日志，三选一。

## 6. 敏感工具与审批（approval）

以下动作执行前会弹审批：

- `edit` —精确替换文件某段
- `write_file` —新建/覆盖文件
- `bash` —跑任意 shell 命令

询问格式：

```
[approval] bash({"command": "conda run -n agent-demo python -m pytest -q"})? [y/N]
```

输入 `y` 才执行；回车或别的任何输入 = 拒绝（agent 收到 is_error 结果并自行纠正）。
`bash` 尤其注意：它**没有命令级沙箱**，审批是它唯一的安全闸 ——需要深思。

> 想了解机制上限：`README.md`「安全警告」那节讲清了路径边界为何**不是** OS 沙箱。

## 7. Web UI（浏览器里对话）

同样是那台引擎，换成浏览器渲染同一份日志：

```sh
conda run --no-capture-output -n agent-demo \
  python web_app.py --workspace . --fake          # 离线演示
# python web_app.py --workspace . --model deepseek-chat     # 真实模型
```

打开 <http://127.0.0.1:8000>：

- 左侧列出 `.sessions/*.jsonl` 全部会话，可新建/切换/删除；
- 流式输出、**可折叠的「已深度思考」**、工具调用卡片、Markdown 渲染；
- 刷新页面对话不丢（默认会话 `web`）。

参数同 CLI 再加 `--host`（默认 127.0.0.1）/`--port`（默认 8000）。

> **注意**：浏览器里没有交互式 stdin，approval 会走默认实现 → **fail-safe 拒绝**
> （点击批准/拒绝按钮是后续迭代项）。

## 8. 跑测试

库顶层的验证手段（改动代码 / 给真实模型一条可执行的检查令）：

```sh
conda run -n agent-demo python -m pytest -q
```

覆盖：日志投影、inbox 记账/恢复、工具循环、钩子与错误重试、取消语义、持久化重放、
OpenAI wire 格式、路径沙箱越界、approval 拒绝等。

## 9. 常见问题（FAQ）

**Q：不带 `--fake` 会怎样？**
若环境里没有 `DEEPSEEK_API_KEY` 直接用真实模式会报 `missing DEEPSEEK_API_KEY`
退出。设置 key 或加 `--fake`，二选一。

**Q：没看到 `[思考]`？**
可能 `--hide-reasoning` 折叠了；或当前模型并不输出 reasoning（`deepseek-chat`
一般不输出）。

**Q：换会话就「失忆」了？**
换新 `--session` = 一份空白日志，从零开始。要延续请用同一 id 并加 `--resume`。

**Q：agent 读到路径外被拒？**
会得到 `path outside workspace: …`。所有文件工具都锁在 `--workspace` 内；
这是纯用户态路径校验，防越界读写，但**不是你想象的系统级沙箱**——别拿去处理
不可信的输入。

**Q：它能跑通吗，为什么任务盯着我的 approval？**
它是交互式编码助手：能读/搜/改/跑测试，但 `edit`/`write_file`/`bash` 都要你
点头。适合**目标明确、几步就完**的任务；不适合无人值守的长任务。

---

想更深入？`README.md`（五个设计、日志样例、模块清单、与 harness 保真度对照）、
`ARCHITECTURE.md`（四层依赖与数据流）。
