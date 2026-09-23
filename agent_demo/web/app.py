"""Web 宿主：FastAPI 路由声明 + 装配 + 命令行入口。

架构：UI 是日志投影的第二个渲染器——复用现有框架（Session/Agent/build_tools/loop
一行不改），`session.on_event` 订阅事件，经 `asyncio.Queue` 桥接成 SSE 流推给浏览器。
CLI 是终端投影，Web 是 DOM 投影，同一份日志。

功能：多会话（左侧栏列出 `.sessions/*.jsonl`，可新建/切换/删除/改名）+ **每个对话
自己的工作区**（新建时选，写进日志，见 `app/workspace.py` 与 `/sessions/new`）+ 自动会话标题
（对齐 harness session-title 的三级来源）+ 流式输出 + 思考折叠 + 工具卡片（变体图标/
状态点/摘要，仿 harness ui-tool）+ Markdown 渲染 + approval 按钮（钩子推送
approval_request 到 SSE，浏览器批准/拒绝）。

**这个文件只管路由与装配**：状态在 `state.py`、会话/seat 生命周期在 `sessions.py`、
自动起名在 `titles.py`、投影在 `payload.py`——993 行的单文件里这四件事互相纠缠，
改一处要在千行里找上下文（拆分清单见 `web/__init__.py` 的模块表）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..app.compaction import run_compaction, select_compact_range
from ..app.factory import load_env
from ..tools.todo import fold_todos
from .payload import context_payload, event_to_payload, first_text_of, history_payloads, queue_rows
from .sessions import append_title, open_session, open_session_seat, scan_sessions, spawn, validate_sid
from .state import state
from .titles import auto_title, clean_title, first_user_message_just_landed

# 仓库根 = 包上两级（`agent_demo/web/app.py` → `agent_demo/web` → `agent_demo` → 根）：
# 静态资源（web/）与 .env 都在仓库根。**拆包时这里最容易算错**，手测清单第一条就是它。
_ROOT = Path(__file__).resolve().parents[2]

app = FastAPI(title='agent-demo web')
# 前端依赖（marked / DOMPurify 本地 vendor，免 CDN）
app.mount('/vendor', StaticFiles(directory=_ROOT / 'web' / 'vendor'), name='vendor')

DONE_MARKER = object()


def _check_init() -> None:
    if state.session is None or state.agent is None:
        raise HTTPException(503, 'web session not initialized')


def init_web(workspace: Path, fake: bool = False, model: str = 'deepseek-v4-flash',
             sessions_dir: Path | None = None, sid: str = 'web',
             compact_at: int | None = None) -> None:
    """初始化宿主状态（测试可注入 workspace / fake / sessions_dir / compact_at）。

    `workspace` 是**默认**工作区：新建对话没指定工作区时用它。每个对话的工作区存在
    自己的 Seat 上（`Seat.args.workspace`），并在创建时落一条 `session/workspace` 事件。

    Seat 注册表是宿主级缓存：每次 init_web 都清空重来（`state.reset`）——测试每个用例
    用独立 sessions_dir，若不清空，旧目录的 seat（同 sid，如 'web'）会被复用，把上个
    会话的内存日志带进新初始化。
    """
    sessions_dir = Path(sessions_dir or '.sessions')
    state.reset(sessions_dir, argparse.Namespace(
        fake=fake, model=model, workspace=workspace, hide_reasoning=False,
        session=sid, sessions=str(sessions_dir), prompt='', resume=False, verbose=False,
        compact_at=compact_at,
    ))
    open_session(sid, allow_missing=True)


@app.get('/')
def index() -> FileResponse:
    return FileResponse(_ROOT / 'web' / 'index.html')


@app.get('/meta')
def meta() -> dict:
    """前端元信息：默认工作区 + 相对路径解析基准 + 模型名 + fake 标记。

    `workspace` 是**宿主默认**（`--workspace`）：新建对话不指定工作区时用它，也是
    前端输入框的预填值；每个会话**实际**用哪个工作区由 `/sessions` 与
    `/sessions/*/switch|new` 的响应带回来（工作区可以按对话指定）。
    `cwd` 是服务进程的当前目录：用户输入相对路径时按它解析（与 shell 直觉一致），
    前端拿它给输入框做提示。
    """
    _check_init()
    args = state.args
    assert args is not None  # init_web 已初始化（_check_init 保证）
    return {
        'workspace': str(Path(args.workspace).resolve()),  # 绝对路径，前端剥前缀显示相对路径
        'cwd': str(Path.cwd()),
        'model': args.model,
        'fake': bool(args.fake),
    }


@app.get('/sessions')
def sessions() -> list[dict]:
    _check_init()
    return scan_sessions()


@app.post('/sessions/new')
async def new_session(request: Request) -> dict:
    """新建会话并切换；可选 `{"workspace": "<目录>"}` 指定这个对话的工作区。

    - 不带 body（旧前端/测试）→ 用宿主默认工作区；
    - 带 `workspace`：必须是**字符串**（别的类型直接 400，不做隐式 `str()`），
      只校验"存在 + 是目录"（策略见 `app/workspace.py`），校验失败 400 并说明原因；
    - id 在同秒内保证唯一（`web-<ts>`，撞了就加 `-2`/`-3`…）：两个对话被塞进同一个
      sid 会**静默复用**上一个会话，而工作区在创建时固定——撞车时第二次选择还会被
      "workspace is fixed" 拒掉，看起来像功能坏了。
    """
    _check_init()
    raw = await request.body()
    workspace = None
    if raw:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as error:
            raise HTTPException(400, 'invalid JSON body') from error
        if body is not None and not isinstance(body, dict):
            raise HTTPException(400, 'body must be a JSON object')
        workspace = (body or {}).get('workspace')
        if workspace is not None and not isinstance(workspace, str):
            # 严格校验：`{"workspace": 123}` 若被 `str()` 化会变成"路径 123 不存在"这种
            # 误导性报错，还可能被非字符串假值（`0`/`[]`）绕过"已固定"的守卫
            raise HTTPException(400, 'workspace must be a string')
    return open_session(_fresh_sid(), allow_missing=True, workspace=workspace)


def _fresh_sid() -> str:
    """`web-<秒级时间戳>`，同秒撞车就顺延 `-2`/`-3`…（会话 id = 日志文件名）。

    判据同时看**磁盘文件**与**内存里的 seat**：只看文件时，"文件刚被删掉但 seat 还在"
    或"另一个 worker 已经建了 seat 但文件还没落盘"都可能选出一个已在用的 id，
    而 `open_session_seat` 命中已有 seat 会**静默复用**它（响应与真实座位的工作区对不上）。
    真正防并发的是 `open_session_seat` 里的 `exist_ok=False` 原子占坑，这里只是把
    两条已知来源都躲开。
    """
    directory = state.sessions_dir or Path('.sessions')
    base = f'web-{int(time.time())}'
    index = 1
    while True:
        candidate = base if index == 1 else f'{base}-{index}'
        if candidate not in state.seats and not (directory / f'{candidate}.jsonl').exists():
            return candidate
        index += 1


@app.post('/sessions/{sid}/switch')
def switch_session(sid: str) -> dict:
    """切换到已有会话（重放日志 = 恢复该会话的记忆）。"""
    _check_init()
    validate_sid(sid)
    return open_session(sid, allow_missing=False)


@app.post('/sessions/{sid}/delete')
def delete_session(sid: str) -> dict:
    """删除会话文件。当前会话不可删（删了状态会混乱）——先切换走再删。"""
    _check_init()
    validate_sid(sid)
    if sid == state.current_sid:
        raise HTTPException(400, 'cannot delete the active session — switch away first')
    path = (state.sessions_dir or Path('.sessions')) / f'{sid}.jsonl'
    if not path.exists():
        raise HTTPException(404, f'session {sid!r} not found')
    path.unlink()
    return {'deleted': sid}


@app.post('/sessions/{sid}/title')
async def session_title(sid: str, request: Request) -> dict:
    """手动改名：校验后 append session/title（source='user'），覆盖自动标题。

    目标会话不要求是当前会话（列表里改任意会话名）；标题即日志投影。
    """
    _check_init()
    validate_sid(sid)
    body = await request.json()
    title = clean_title(str(body.get('title') or ''))
    if not title:
        raise HTTPException(400, 'title must not be empty')
    append_title(sid, title, source='user')
    return {'id': sid, 'title': title, 'source': 'user'}


@app.get('/history')
def history(sid: str | None = None) -> dict:
    """指定会话的历史消息 + 当前 todo 投影（页面加载/刷新时恢复 UI）。

    query ?sid= 可选：缺省 = 全局焦点会话（旧前端/测试不带 sid 也能跑）；
    多标签页并行时前端带自己看的 sid，各取各的 Seat。
    """
    _check_init()
    target_sid = sid or state.current_sid
    seat = state.seats.get(target_sid)
    if seat is None:
        raise HTTPException(404, f'session {target_sid!r} not open — switch to it first')
    return {
        'id': target_sid,
        'workspace': str(seat.args.workspace),     # 本会话的工作区（顶栏/相对路径显示用）
        'history': history_payloads(seat.session),
        'todos': fold_todos(seat.session) or [],
        'queue': queue_rows(seat.agent),
        'context': context_payload(seat.session),
    }


@app.post('/chat')
async def chat(request: Request) -> StreamingResponse:
    """发起一轮对话并以 SSE 流返回事件；客户端断开即取消该会话 agent。

    body.sid 可选：缺省 = 全局焦点会话（旧前端/测试不带 sid 也能跑）。
    显式带 sid 时定位到对应 seat——两会话各开一条 SSE 流，互不干扰。
    """
    _check_init()
    assert state.session is not None and state.agent is not None  # 初始化后必有焦点 seat
    body = await request.json()
    message = (body.get('message') or '').strip()
    if not message:
        raise HTTPException(400, 'message must not be empty')
    sid = body.get('sid') or state.current_sid
    seat = state.seats.get(sid) or open_session_seat(sid, allow_missing=True)
    # seat 一旦建成就带 agent：`Seat.agent` 只在 `open_session_seat` 的构造窗口里是
    # None（先登记 seat 好让审批钩子闭包引用它，再 build_agent），路由侧看不到那个窗口
    assert seat.agent is not None
    session, agent = seat.session, seat.agent
    # 提交身份（对齐 dsh 的 prompt requestId）：前端铸的 uuid，落到 durable
    # 消息 source 上；前端据此把"本地回显"原子换成真身。
    request_id = (body.get('request_id') or '').strip()

    queue: asyncio.Queue = asyncio.Queue()
    unsubscribe = session.on_event(lambda event: queue.put_nowait(event))

    # 自动起名：首条用户消息一旦落日志立即触发（不等回合结束——回合可能因
    # approval / 长任务迟迟不结束；标题只依赖第一条消息，尽早起名体验最好）。
    # 对齐 harness：监听 user/message，title 事件是独立小请求。
    title_spawned = {'done': False}

    def _watch_first_user_message(event) -> None:
        if title_spawned['done']:
            return
        if event.type == 'user/message' and first_user_message_just_landed(session):
            title_spawned['done'] = True
            spawn(auto_title(seat, first_text_of(event.data)))

    watch = session.on_event(_watch_first_user_message)

    async def run_agent() -> None:
        try:
            agent.followup(message, rpc_id=request_id)
            await agent.when_idle()
        finally:
            await queue.put(DONE_MARKER)

    task = asyncio.create_task(run_agent())

    async def sse_stream():
        # 本会话的活跃队列挂到 seat：per-seat approval 钩子经它推送请求。
        # （旧实现写全局 _active_queue——两会话并行时会被互相覆盖）
        seat.queue = queue
        cur_turn = 0   # 会话事件流的当前回合（turn_start 推进；user_message 帧附上）
        try:
            while True:
                item = await queue.get()
                if item is DONE_MARKER:
                    break
                if isinstance(item, dict):
                    # Web approval 请求（钩子直接放的自定义载荷，非 session 事件）
                    yield f'data: {json.dumps(item, ensure_ascii=False)}\n\n'
                    continue
                payload = event_to_payload(item, session, agent)
                if payload is None:
                    continue
                if payload['type'] == 'turn_start':
                    cur_turn = payload['turn']
                elif payload['type'] == 'user_message':
                    # user/message 事件本身不带 turn（Message 无此字段），
                    # 订阅端按事件顺序知道最近一次 turn_start——补上，
                    # 前端据此画/不画回合分隔线（同回合 steer 不画）
                    payload['turn'] = cur_turn
                yield f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'
        finally:
            # 客户端断开（停止按钮 / 关页面）：取消该会话 agent。
            # 不能只 cancel run_agent 任务：when_idle 用 asyncio.shield(driver)
            # 保护 driver 不被外部取消殃及——run_agent 被 cancel 只会让
            # when_idle 返回，driver（正在跑的回合）会继续执行、永不收敛，
            # 前端也就永远开不了新回合（消息全堵在 next-turn 排队）。
            # 必须 agent.cancel() 直达 driver：inbox.clear + driver.cancel()，
            # CancelledError 沿 await 链传播，run_turn 记 turn/end aborted。
            if seat.agent.status == 'running':
                seat.agent.cancel()
            task.cancel()
            unsubscribe()
            watch()   # 退订首条消息监听（会话切换后不留悬挂监听）
            if seat.queue is queue:   # 只有自己挂的才清（并发：别清掉别的流的）
                seat.queue = None

    return StreamingResponse(sse_stream(), media_type='text/event-stream')


@app.post('/steer')
async def steer(request: Request) -> dict:
    """运行中插队：把消息塞进当前回合的 next-step 队列（steer，即时生效）。

    与 /chat 的分工：idle 时新开回合走 /chat（followup）；回合进行中
    改方向/加指令走 /steer——消息作为当前回合的下一步处理，事件继续
    沿已打开的 SSE 流推送（回合不结束）。
    前置：该会话的 agent 必须在跑（有活跃对话流）；idle 时用 /chat。
    """
    _check_init()
    assert state.session is not None and state.agent is not None
    body = await request.json()
    message = (body.get('message') or '').strip()
    if not message:
        raise HTTPException(400, 'message must not be empty')
    sid = body.get('sid') or state.current_sid
    seat = state.seats.get(sid)
    if seat is None:
        raise HTTPException(404, f'session {sid!r} not open — switch to it first')
    assert seat.agent is not None
    if seat.queue is None:
        raise HTTPException(409, '会话没有活跃对话流——用 /chat 开新回合')
    if seat.agent.status != 'running':
        # 竞态窗口：回合刚 turn_end、SSE 尚未收尾（DONE 未发）时 queue 还在，
        # 但 agent 已 idle——插队入队后事件会没人读（流即将关闭）。拒绝，
        # 前端会把输入放回，等回合真正结束后走 /chat。
        raise HTTPException(409, 'agent 已空闲——回合即将结束，请稍后用普通消息')
    message_id = seat.agent.steer(message, rpc_id=(body.get('request_id') or '').strip())
    # 返回消息 id + 队列投影：前端把消息画在**消息流尾部**（pending 气泡 +
    # 待处理标记，对齐 DSH 的 pending-steering）——未 claim 的消息还没有 seq
    # 位置，所以恒定贴尾、绝不往流中间插锚点；claim 后 user_message 帧带同
    # 一个 id/rpc_id，气泡就地转正、本地回显在同一次渲染里消失。
    return {
        'ok': True,
        'sid': sid,
        'queued': 'next-step',
        'message_id': message_id,
        'queue': queue_rows(seat.agent),
    }


@app.post('/queue/update')
async def queue_update(request: Request) -> dict:
    """队列项操作（对齐 DSH 的 `session.updateQueue(itemId, QueueAction)`）。

    body: {sid?, item_id, action: {'kind': 'edit'|'remove'|'steer', 'text'?}}
    DSH 的 edit 带 `content: ContentBlock[]`；我们只有文本框，所以收 `text`
    （差异记在 AGENTS.md，语义一致：改的是还没进模型记忆的那条消息）。

    返回 HTTP 200 + `ok`：并发下"那条已经不在了"是**正常收敛**而不是错误
    （它可能刚好被 claim 掉了），与 dsh 的 `session/queue-item-not-found`
    静默收敛一致——前端据此不弹错、只按最新快照重画。
    """
    _check_init()
    body = await request.json()
    item_id = (body.get('item_id') or '').strip()
    action = body.get('action') or {}
    kind = action.get('kind') if isinstance(action, dict) else None
    if not item_id:
        raise HTTPException(400, 'item_id must not be empty')
    if kind not in ('edit', 'remove', 'steer'):
        raise HTTPException(400, f'unknown queue action: {kind!r}')
    text = (action.get('text') or '') if kind == 'edit' else ''
    if kind == 'edit' and not text.strip():
        raise HTTPException(400, 'edit requires non-empty text')
    sid = body.get('sid') or state.current_sid
    seat = state.seats.get(sid)
    if seat is None:
        raise HTTPException(404, f'session {sid!r} not open — switch to it first')
    assert seat.agent is not None
    code = seat.agent.update_queue(item_id, kind, text)
    return {'ok': code == 'ok', 'code': code, 'sid': sid,
            'queue': queue_rows(seat.agent)}


@app.post('/compact')
async def compact(request: Request) -> dict:
    """手动压缩会话：把旧回合折叠成 checkpoint（复用 run_compaction）。

    body.sid 可选（缺省 = 全局焦点会话；无 body 也允许——旧前端/测试
    直接 POST 空体压缩当前会话）。前置校验（对齐 dsh /compact 命令的
    串行语义）：
    - fake 模式拒绝：脚本模型不能生成摘要（自动压缩本来也不挂）
    - agent 运行中拒绝：压缩事务会动 surface，与进行中的回合冲突
    - 无可压段（旧回合不足）→ compacted=False + reason，由前端提示
    """
    _check_init()
    assert state.session is not None and state.agent is not None
    args = state.args
    if args is not None and args.fake:
        raise HTTPException(400, 'fake 模式不支持手动压缩（脚本模型不能生成摘要）')
    # body 可选：空体也允许（压缩焦点会话）
    raw = await request.body()
    sid = state.current_sid
    if raw:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as error:
            raise HTTPException(400, 'invalid JSON body') from error
        sid = (body or {}).get('sid') or state.current_sid
    seat = state.seats.get(sid)
    if seat is None:
        raise HTTPException(404, f'session {sid!r} not open — switch to it first')
    assert seat.agent is not None
    agent = seat.agent
    if agent.status != 'idle':
        raise HTTPException(409, 'agent 正在运行——回合结束后再压缩')
    if select_compact_range(seat.session, keep_turns=1) is None:
        return {'compacted': False, 'reason': '没有可压缩的旧回合'}
    ok = await run_compaction(
        seat.session, agent.llm, keep_turns=1,
        model=agent.options.get('model', ''),
    )
    return {'compacted': ok,
            'reason': '压缩完成' if ok else '压缩未完成（摘要生成失败或摘要未通过校验）'}


@app.post('/approval/respond')
async def approval_respond(request: Request) -> dict:
    """浏览器对 approval 请求的响应：批准（true）或拒绝（false），唤醒钩子。

    aid 在各 seat 的 approvals 里查（并发：每个会话的审批表独立，按 aid 唯一）。
    """
    body = await request.json()
    aid = body.get('id')
    for seat in state.seats.values():
        fut = seat.approvals.get(aid)
        if fut is not None:
            if not fut.done():
                fut.set_result(bool(body.get('approved')))
            return {'ok': True, 'sid': seat.sid}
    raise HTTPException(404, f'unknown approval id {aid!r}')


def main() -> None:
    parser = argparse.ArgumentParser(description='agent-demo Web UI (DeepSeek-style chat)')
    parser.add_argument('--workspace', type=Path, required=True,
                        help='default workspace root — new conversations use it unless they '
                             'pick another one (tools may only read/write inside the '
                             'conversation\'s workspace)')
    parser.add_argument('--fake', action='store_true', help='offline scripted model (architecture demo)')
    parser.add_argument('--model', default='deepseek-v4-flash', help='model id for the OpenAI-compatible API')
    parser.add_argument('--host', default='127.0.0.1', help='bind host (default 127.0.0.1)')
    parser.add_argument('--port', default=8000, type=int, help='bind port (default 8000)')
    parser.add_argument('--compact-at', type=int, default=None, metavar='TOKENS',
                        help='auto-compact when the routed context exceeds TOKENS '
                             '(deepseek-v4 window is 1M; default off)')
    args = parser.parse_args()
    load_env(_ROOT / '.env')  # 与 CLI 一致：注入 .env 的 API key
    init_web(args.workspace, fake=args.fake, model=args.model, compact_at=args.compact_at)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)
