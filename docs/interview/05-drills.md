# 闭卷练习册：12 概念 · 3 白板推导 · 2 压力题

> 本文件是**派生视图**，事实以 [`../architecture.md`](../architecture.md) 与
> [`../notes/`](../notes/) 为准。用法：先不看文件口述答案，再按"答案所在文件"回代码核对；
> 答不上来的题就是下一站。三组分别练 **复述 / 推导 / 辩护**。

## A. 概念题（12 道）

### A1. 六层怎么分？依赖方向靠什么保证？

- **答案所在文件**：`tests/test_architecture.py`（`LAYERS`）、`docs/architecture.md`
- **合格答案**：① 0 `values` → 1 `capability` → 2 `state` → 3 `runtime` → 4 `app`/`tools` → 5 `web`/`cli`，一个模块只能 import 同层或更低层；② AST 扫全部 `import`（`import_edges`），反向边（`layer_of(src) < layer_of(dst)`）当场红；③ 白名单 `KNOWN_VIOLATIONS` 当前为空，`test_known_violations_are_still_real` 防僵尸条目；④ "四层"只是框架本体（values–runtime）的历史叫法。
- **常见错误**：把"四层"当成全部；说依赖方向靠文档与 code review 保证。
- **追问**：白名单为什么必须"修好即删"，留着会怎样？

### A2. `surface` 是什么？`replace` 为什么是位置语义？

- **答案所在文件**：`my_coder/state/session.py`（`SURFACE_EVENT_TYPES`、`append`、`_apply_surface`、`derive_messages`）
- **合格答案**：① `_surface` 只存 seq 的列表、不存事件本体；`derive_messages` 按它逐个 `by_seq` 折叠，是纯函数；② 只有 `user/message`、`assistant/message`、`tool/result` 三类能带 `surface_op`，写入时校验，其它事件带了就抛错；③ `replace` 的 `shadowed=(start_seq,end_seq)` 只是两个端点，遮蔽范围用两端在 `_surface` 里的**下标**定位连续段；④ checkpoint 的 seq 大于被顶替的旧 seq，替换后 surface 不再按 seq 单调。
- **常见错误**：按 `start <= seq <= end` 数值过滤；说 `_surface` 存的是消息内容。
- **追问**：为什么按数值区间过滤会在第二次压缩时吞掉无关节点？

### A3. inbox 为什么"先记账再改内存"？

- **答案所在文件**：`my_coder/state/inbox.py`（`_splice`）
- **合格答案**：① `_splice` 是队列改动的唯一入口，append/prepend/clear/claim 全归到它；② 顺序严格：先 `session.append('agent/inbox/spliced', …)`，再改内存；③ 崩溃窗口里"已经发生的摘除/插入"在磁盘上有记录，内存可重放，磁盘日志永远 ≥ 内存状态；④ `discard=False`（claim、steer 搬家）不触发丢弃通知，`outcome='canceled'` 只在真丢弃时写。
- **常见错误**：先改内存再落盘；把 `claim` 当成"丢弃"。
- **追问**：如果反过来（先内存后落盘），崩溃时用户消息会从哪里消失？

### A4. `peek` 与 `claim` 为什么分开？

- **答案所在文件**：`my_coder/runtime/loop.py:run_turn`、`my_coder/state/inbox.py:peek`/`claim`
- **合格答案**：① `peek` 只读候选、不改队列不记账；② `pre_step` 与 `request` 两个 await 都提到 `claim` 之前，`claim → step/start → user/message` 之间**没有 await**；③ 取消落在钩子里 → 队列一个字没动，`clear()` 能记 `canceled`；落在提交后 → 消息已在日志，不是半提交；④ `peek` 的批次规则必须与 `claim` 逐字一致（先整个 next-step，再从 next-turn 取一条）；⑤ `pre_step` 拒绝也要消费批次，否则同一条消息会被无限重问。
- **常见错误**：先 `claim` 再跑钩子；钩子拒绝时把消息留在队列里"下次再问"。
- **追问**：三个位置（队列 / surface / 日志）各在什么时刻变化？

### A5. 为什么 step 必须等于"一次模型请求"？

- **答案所在文件**：`my_coder/runtime/loop.py:run_turn`/`_run_step`
- **合格答案**：① 一个 step = 一次模型请求 + 它发起的工具调用，工具循环由外层 `run_turn` 驱动；② 每轮开头都 `peek`/`claim`，插队消息在**下一次请求前**就能浮上水面；③ 旧实现把整段工具循环算一步：实测一条插队消息躺了 **2 分 40 秒**、期间 50+ 次工具调用后被 cancel 清掉；④ `step/start` 与 `request/header` 因此 1:1；`_run_step` 的内层 while 只服务 `request_error` 的 retry。
- **常见错误**：step = 一次工具调用；step = 整段工具循环。
- **追问**：工具结果只落日志，为什么没有"把结果发回模型"的代码？

### A6. 取消为什么不能每层都吞？

- **答案所在文件**：`my_coder/runtime/loop.py`（`run_turn`、`_run_group`、`_execute_tool_calls`）、`my_coder/runtime/agent.py:_kick`、`my_coder/state/recovery.py`
- **合格答案**：① `CancelledError` 沿 await 链**单向向上**，`_run_one` 不捕获；② 每层只记账：`_run_group` drain 已起跑调用、给缺结果的调用补 `is_error` 并 `commit_ready` 后 re-raise；`_execute_tool_calls` 给未起跑的补记账；③ `run_turn` 记 `turn/end{reason:'aborted'}` 后 re-raise；④ 只有 driver 边界 `_kick` 吞掉，回落 idle；⑤ 不补记账会留下"assistant 带 tool_calls 却没有 result"的 wire，下一轮请求直接 400。
- **常见错误**：每层 `except CancelledError: pass`；把取消当成一条普通的 `is_error` 工具结果。
- **追问**：为什么 `_kick` 可以吞，而 `_run_group` 不能？

### A7. `execution_mode` 与 `offload` 各回答什么？

- **答案所在文件**：`my_coder/state/registry.py`（`ToolSpec`、`mode`、`execute`）、`my_coder/tools/*.py` 的注册声明
- **合格答案**：① 两根正交轴：`execution_mode` 回答"能不能与其它调用并发"，`offload` 回答"会不会阻塞事件循环"；② 并发许可是声明出来的，默认 `'sequential'`，非法值注册时抛错；未注册工具名 `mode()` 返回 `'sequential'`（fail-closed，不能抛 `KeyError`）；③ `offload` 只给体内是同步 I/O、没有 await 的 executor，实现是 `asyncio.to_thread` + `asyncio.run`；④ 不卸载时协程体没有挂起点，`asyncio.wait_for` 的超时永远不触发；⑤ `read_file` 两者都是、`todo_write` 两者都不是、`bash` 不 offload（要绑运行中的事件循环）。
- **常见错误**：用"安全/不安全"一个标志同时表达；给 `bash` 声明 `offload=True`；说并发许可是按工具名在循环里判断的。
- **追问**：卸载之后超时线程还会跑完，为什么可以接受？

### A8. 悬空工具调用为什么在恢复时修？

- **答案所在文件**：`my_coder/state/recovery.py:repair_dangling_tool_calls`/`dangling_tool_calls`、`my_coder/cli.py`、`my_coder/web/sessions.py`
- **合格答案**：① 判据只看**投影**（`derive_messages`）：被压缩遮蔽、模型不会再看到的老调用不该算；② 先落 `session/repaired` 痕迹，再逐条补 `is_error` 的 `tool/result`，顺序即因果；③ 幂等（修完再扫为空）；两个恢复入口（重放后、`bind_store` 后）都调用；④ 刻意**不在请求构造时补占位消息**——那会让"这里断过"从日志消失。
- **常见错误**：在 `_to_wire_messages` 里补占位消息；只改内存不写日志。
- **追问**：不修的话，模型下一次请求会收到什么错误？为什么不会自愈？

### A9. 压缩的四步事务是什么顺序？

- **答案所在文件**：`my_coder/app/compaction.py:run_compaction`（顶部注释 `:78-80` 与函数 docstring 写的就是实际顺序）
- **合格答案**：① 实现顺序：`compaction/start` → **replace**（checkpoint 以 `user/message` + `surface_op='replace'` 原位顶替）→ `compaction/summary`（审计：摘要全文、遮蔽范围、文件操作）→ `compaction/end`；② 先 replace 后 summary：替换失败不会留下一份"看起来压过了"的假审计；③ 摘要必须比被压 conversation 小，否则拒绝；④ 摘要请求只取 `text` 块，工具结果对摘要器不可见；⑤ 失败落 `compaction/end{error}`，日志完好可重试。
- **常见错误**：把四步背成 `start→summary→replace→end`（旧实现/旧注释的顺序）；说压缩把原文删了。
- **追问**：为什么"审计晚一条"比"审计早一条"安全？

### A10. 召回为什么分三层、为什么不做排序？

- **答案所在文件**：`my_coder/app/recall.py`（`shadowed_seqs`、`build_turns`、`render_manifest`、`render_turn`、`session_index_text`）、`my_coder/app/factory.py:_runtime_status`、`my_coder/tools/recall.py`
- **合格答案**：① L0 会话目录（每请求状态贡献者）→ L1 `session_manifest`（用户话清单 + 回合足迹）→ L2 `read_turn`（回合明细）；② 检索面 = 曾经进过模型上下文的事件**全集**，含被 `replace` 遮蔽的（`shadowed_seqs`），痕迹事件不进；③ 不做相关性排序/embedding：语料是自述的时间线，靠顺序 + 回合号 + 足迹导航；④ L1 截断必须明说并给 `read_turn` 坐标；L2 按行分页（`offset` 1 起），块级不许静默截断；⑤ 跨工作区 fail-closed；两个工具 `parallel` + `offload` + `cacheable`。
- **常见错误**：说召回会做相关性排序；把 `request/header` 算进检索面；说压缩删掉了原文。
- **追问**：为什么"让 LLM 先挑回合再读"不能当评测骨架？

### A11. 工作区指令文件为什么是三态探测？

- **答案所在文件**：`my_coder/app/instructions.py`（`_read`、`load`、`render`）
- **合格答案**：① 三态：`(正文,'')` 确认存在 / `(None,'')` 确认不存在 / `(None,原因)` 读不到；② 读不到**绝不**降级成"没有约定"，否则模型会在假前提上提议创建一份其实已存在的文件；③ `lstat()` 再 `stat()`：条目在、目标不在 = 断链符号链接，是第三态；④ 越界符号链接不注入但报出来；⑤ 读完再取一次 `(mtime_ns,size)`，不一致不写缓存（撕裂读不服务）；⑥ 不向上发现，子目录同名文件只列路径；正文 live 段每请求重算、清单按回合刷新。
- **常见错误**：两态（读到 / 当成没有）；只调一次 `stat()` 判存在。
- **追问**：缓存键在"读之前"和"读之后"各取一次，各防什么？

### A12. 三条门禁各钉什么？

- **答案所在文件**：`tests/test_architecture.py`、`tests/test_docs.py`、`tests/test_doc_numbers.py`
- **合格答案**：① 依赖方向：AST 断言 + `scanned >= 30` 先钉扫描面（root 指错时两条断言会静默全绿）；② 文档格式：路径↔`Status` 一致、头三行与骨架、相对链接可解析、`AGENTS.md` 渲染后不含截断提示；③ 文档数字：现场测量，`约 N` 允许 ±10%、裸 `N` 必须精确；扫描面限定 L0/L1/L1.5/L3，`HISTORICAL` 只收历史叙述且豁免必须仍真实，`len(claims)+exempted >= 5` 防扫描面塌掉。
- **常见错误**：说"规则写进 `AGENTS.md` 就生效"；说靠 CI 跑（GitHub Actions 已移除，门禁在 `pytest` 里）。
- **追问**：为什么数字门禁的扫描面故意不覆盖 `docs/notes/`？

## B. 白板推导题（3 道）

### B1. 加一个 `summarize` 工具，要改哪几处？

- **答案所在文件**：`my_coder/tools/file_io.py`（ToolSpec 形状）、`my_coder/tools/__init__.py:build_tools`、`my_coder/app/factory.py`、`my_coder/state/registry.py`、`tests/test_tools.py`
- **要改的清单**（"实现 + 供给面"）：
  1. **实现**：新建 `my_coder/tools/summarize.py`，`register(registry, workspace)` + executor（路径先过 `sandbox.resolve_in_workspace`，参考 `file_io`）。
  2. **注册**：在 `build_tools()` 里调用 `summarize.register(...)`；工具属于第 4 层，只能 import 更低层。
  3. **声明**：纯读且同步读盘 → `execution_mode='parallel'`、`offload=True`、`cacheable=True`；若会写文件则反过来（默认 `sequential`、不 `cacheable`）。声明由 `ToolSpec` 校验，写错注册时抛错。
  4. **"什么时候用"（通用规则）**：`app/factory.py` 的 `discipline` 段加一条判据（或并入 Economy/Evidence）。
  5. **"怎么用"（工具专属规则）**：`prompt.section('tool:summarize', <order>, …)`，与通用规则不互串。
  6. **"在需求发生处提醒"（可选）**：若需要每回合状态，`_runtime_status()` 里 `status.register('summarize', build)` 一行，循环不用改。
  7. **测试与文档**：`tests/test_tools.py`（越界、失败降级、声明）；`docs/architecture.md` 工具表；有设计决定才写 note。
- **常见错误**：只写 executor 就交卷；把工具规则写进 `discipline` 与 `tool:*` 两处（互串）；漏掉 `cacheable` 导致收敛计数被绕过。
- **追问**：为什么"工具存在 ≠ 模型会用"要三段供给面一起补？

### B2. "两个工具并发执行后日志顺序错乱"，怎么定位与修？

- **答案所在文件**：`my_coder/runtime/loop.py`（`_run_group` 的 `slots` / `commit_ready`、`_execute_tool_calls`）、`my_coder/state/registry.py`
- **定位**：症状是日志里 `tool/result` 顺序 ≠ 模型请求里 `tool_calls` 的顺序。依次查三处：① 分组是否按**连续段**——`_execute_tool_calls` 每次用队首重新分类，并行段撞上 `sequential` 调用就停手；② 结果是否先进 `slots[index]`，再由 `commit_ready` 从队首**连续**提交（旧实现按完成顺序 append 就是病根）；③ 取消路径是否也走 `commit_ready`（drain 后补 slots 再提交，而不是直接写）。
- **修法**：保持"槽位 + 连续推进"；`_run_one` 只**返回**结果、不落盘；落盘统一由 `commit_ready` 按模型顺序做。
- **不修的后果**：同一段对话的重放前缀不稳定 → 前缀缓存失效、测试 flaky、前端按 `call_id` 配对要额外处理"结果早于调用"的畸形序列。
- **验证**：`tests/test_loop.py` 里并发批次与有序提交的用例。
- **追问**：为什么并发只该改变"谁先跑完"，不该改变日志顺序？

### B3. "压缩后模型开始编造文件内容"，先诊断什么、再给什么方案？

- **答案所在文件**：`my_coder/app/compaction.py`（摘要输入只取 `text` 块；`run_compaction`）、`my_coder/app/recall.py`、`my_coder/app/factory.py`（`RECALL_DISCIPLINE` / `RECALL_TOOL_SECTION`）、`my_coder/state/session.py`（`replace` 不改 `_log`）
- **先诊断（不要先改提示词）**：
  1. **对照**：压缩前原文（R1）能答对、压缩后只有摘要（R2）答错 → 是可见性损失；两者都错 → 不是压缩的问题。
  2. **值在哪里**：在工具输出里（端口/路径/报错串）→ 摘要器只喂 `text` 块，**确定性地**看不到，是机制缺陷；在工作区文件里 → 应该重读文件，编造说明 Evidence 纪律没生效；在历史用户话/助手话里 → 本可经 L1/L2 读回。
  3. **通道是否被知道**：查三段供给面（`discipline` 的 Continuity、`tool:recall` 段、L0 压缩提示）是否在。
- **方案（分层，不破坏"压缩不删数据"）**：① 机制层：摘要输入纳入工具结果正文，或压缩时由代码拼入精确值/文件操作清单（`extract_file_ops` 是先例）；`replace` 仍只改 `surface`。② 供给面：压缩过时的 L0 提示"原文没删，先看清单再读原文"；`tool:recall` 段；Continuity 写明"看不到的既有决定不是没有决定，绝不用看似合理的值补空缺"。③ 验证：受控会话 + 必要性预检 + 三臂，判据看产物与行为，不看自称。
- **常见错误**：只加一句"不要编造"到 system；在请求构造时把被压缩原文塞回去（绕开压缩）；直接改摘要提示词而不检查工具输出根本进不去。
- **追问**：为什么"能读回原文"不等于"模型会去读"？

## C. 压力题评分 rubric（2 道）

### C1. "这项目有什么不足？"

- **答案所在文件**：`docs/interview/03-qa.md` 的 D1（取舍）与本题列出的未修项、`README.md` 安全警告、`NEXT_STEPS.md`。注意 `03-qa` 的 D2 现在是**已修清单**（可用来讲"修的过程"），别再当未修缺口。
- **1 分**：说"没有不足/都挺好"，或只有抽象评价（"代码还能更优雅"），给不出任何位置与影响。
- **3 分**：能说出 1–2 个具体取舍（如 OS 级沙箱、多 worker 并发、可观测性），但停在名词，不给前提、不区分"设计取舍"与"未修缺口"。
- **5 分**：① 先分两类——**设计取舍**（有前提，如"OS 沙箱要 bash 后端可替换 + 平台 API"）与**未修缺口**（可修的真缺陷）；② 每项给位置与影响，例如压缩互斥是进程内模块级 set（`web/app.py` 的 `_compacting`）、席位锁是进程内 `threading.Lock`（`web/sessions.py` 的 `_open_locks`）——多 worker 都不互斥；`app/recall.py` 的 `_SCAN_CACHE/_LIVE_CACHE` 只增不淘汰；没有指标与告警；③ 主动说明"哪些是取舍、哪些已经修掉、哪些仍未修"而不是装作没有。
- **追问**：哪一条是"取舍"，哪一条是"缺陷"？判据是什么？

### C2. "这能上生产吗？"

- **答案所在文件**：`README.md` 安全警告、`docs/interview/03-qa.md` C3、`my_coder/app/sandbox.py`、`my_coder/web/app.py`
- **1 分**：回答"能，加个 Docker 就行"或"不能，因为它是 demo"，没有具体差距。
- **3 分**：能说三块差距（OS 级沙箱、多 worker 并发、可观测性），但说不清每块的前置条件与替代方案，或把"安全边界"讲成"已经安全"。
- **5 分**：① 明确"不能"并给三块差距及各自前提：平台沙箱（Windows token / Linux bwrap / macOS seatbelt，且 `bash` 后端要可替换）、锁与幂等外置到进程/存储层（现在 `_compacting`/`_open_locks` 只在进程内）、指标与告警（目前只有日志与教学脚本）；② 能背出 README 的诚实边界：路径校验是纯用户态归一化 + 前缀匹配，**不是** OS 级沙箱，`bash` 没有命令级沙箱、TOCTOU 不设防，真正的刹车是 approval；③ 给出"最小可上线路径"而不是一句"重构"。
- **追问**：如果只允许先做一块，选哪块？为什么？

> 复盘用法：答不出的题回 [`01-tour.md`](01-tour.md) 找站、回 [`02-decisions.md`](02-decisions.md)
> 找取舍；被真实面试问倒的问题补进 [`03-qa.md`](03-qa.md) 的追问链。
