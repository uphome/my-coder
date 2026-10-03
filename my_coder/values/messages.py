"""值层：消息、事件与工具返回值的不可变词汇表 + JSONL 编解码。

Python 的 frozen dataclass 只冻结字段赋值，不深冻结嵌套容器——
约定：所有内容一律用 frozen dataclass 与 tuple，禁止把可变容器放进
消息/事件。这就是"不可变值对象"在 Python 里的落地方式（harness 用
deepFreeze，这里用类型 + 约定）。

`ToolOutcome`（工具执行的返回值）也住在这里：它和 `ToolResultBlock` 是同一件事的
两个阶段（执行返回值 → 补上 call_id 落进日志的 wire 形态），而且**只描述结果、不含
执行逻辑**——`ToolSpec`（schema + executor）留在 `state/registry.py`。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from typing import Literal

# 三种内容块：一条消息的内容是这些不可变块的 tuple。
# type 字段是判别标签（discriminated union 的 Python 落地），
# 编解码和 wire 转换都靠 isinstance + type 字段分派。


@dataclass(frozen=True)
class TextBlock:
    """文本块：模型或用户说的一段话。type='text' 是判别标签。"""
    type: Literal['text'] = 'text'
    text: str = ''


@dataclass(frozen=True)
class ToolCallBlock:
    """工具调用块：模型请求执行一个工具。arguments 还是原始字符串，
    执行前才由循环层 json.loads（坏 JSON 降级成 is_error 结果）。"""
    type: Literal['tool-call'] = 'tool-call'
    id: str = ''
    name: str = ''
    arguments: str = ''


@dataclass(frozen=True)
class ToolResultBlock:
    """工具结果块：一个工具调用的产物。tool_call_id 回指 ToolCallBlock.id，
    让模型能把结果和调用对上；is_error 标记执行失败（不炸循环）。"""
    type: Literal['tool-result'] = 'tool-result'
    tool_call_id: str = ''
    content: str = ''
    is_error: bool = False


@dataclass(frozen=True)
class ToolOutcome:
    """工具**执行**的返回值：一段文本 + 是否出错（`ToolResultBlock` 的前身）。

    两个对象是同一件事的两个阶段：executor 返回 `ToolOutcome` → 宿主把它包成
    `ToolResultBlock`（补上 call_id）落进日志。放在值层是因为它**只描述结果**，
    不含任何执行逻辑：`ToolSpec`（schema + executor）留在 `state/registry.py`，
    而"一个工具返回了什么"是词汇表的一部分。

    is_error=True 只是"这条结果告诉模型：调用失败了"，不会抛给循环——
    失败降级成结果，是工具层最重要的约定。
    """
    content: str = ''
    is_error: bool = False


ContentBlock = TextBlock | ToolCallBlock | ToolResultBlock


# 消息来源：回答"这条消息是谁产生的"。
# user=用户输入；model=某个模型（记录 provider/model，audit 用）；
# tool=工具结果（记录 call_id，回指工具调用）；plugin=插件注入。


@dataclass(frozen=True)
class UserSource:
    """用户输入的消息。

    rpc_id 是**提交身份**（对齐 dsh 的 `user source.rpcId`）：前端提交时铸一个
    uuid，随请求一起上来，落到 durable 消息的 source 上。它让"本地回显"和
    "服务端落地"是同一件事的两个阶段——前端看到带同一 rpc_id 的 durable
    消息，就能在**同一次渲染**里把回显换成真身（原子交接，不重复也不留空档）。
    空串 = 没有提交身份（CLI 输入、历史数据、直接构造的消息）。
    """
    kind: Literal['user'] = 'user'
    rpc_id: str = ''


@dataclass(frozen=True)
class ModelSource:
    """某个模型产生的消息。provider/model 记录下来供审计和 resume 恢复路由。"""
    kind: Literal['model'] = 'model'
    provider: str = ''
    model: str = ''


@dataclass(frozen=True)
class ToolSource:
    """工具结果消息。call_id 回指发起它的工具调用。"""
    kind: Literal['tool'] = 'tool'
    call_id: str = ''


@dataclass(frozen=True)
class PluginSource:
    """插件注入的消息（demo 未用到，保留词汇表完整性）。"""
    kind: Literal['plugin'] = 'plugin'
    plugin: str = ''


MessageSource = UserSource | ModelSource | ToolSource | PluginSource


@dataclass(frozen=True)
class Message:
    """一条不可变消息：role + content blocks + source + id。"""
    id: str
    role: Literal['system', 'user', 'assistant']
    content: tuple[ContentBlock, ...]
    source: MessageSource


def create_message(
    role: Literal['system', 'user', 'assistant'], content, source: MessageSource,
) -> Message:
    """底层工厂：生成新 id、把 content 转 tuple。上层用三个便捷工厂。"""
    return Message(id=str(uuid.uuid4()), role=role, content=tuple(content), source=source)


def create_user_message(blocks, source: MessageSource | None = None) -> Message:
    """用户角色消息：不传 source 默认 UserSource。"""
    return create_message('user', blocks, source if source is not None else UserSource())


def create_assistant_message(blocks, provider: str = '', model: str = '') -> Message:
    """助手角色消息：source 记录产生它的模型。"""
    return create_message('assistant', blocks, ModelSource(provider=provider, model=model))


def create_tool_result_message(call_id: str, content: str, is_error: bool) -> Message:
    """工具结果消息：角色是 user（工具替你说话），wire 层再转 role:"tool"。"""
    return create_user_message(
        [ToolResultBlock(tool_call_id=call_id, content=content, is_error=is_error)],
        ToolSource(call_id=call_id),
    )


# 队列占位：待处理消息"为什么在队列里"。placement 决定它何时被认领、
# 以及前端队列区怎么标它（对齐 dsh 的 SessionQueuedItem.placement）。
QueuedPlacement = Literal['queued', 'steering']


@dataclass(frozen=True)
class QueuedItem:
    """inbox 里的一条待处理消息 = 队列投影的元素。

    - placement='queued'：普通排队（next-turn）——等当前回合干完，开新回合处理
    - placement='steering'：插队（next-step）——当前回合的下一步就处理
    - message：消息本体；id / rpc_id / 文本都从它取，不另存副本
      （值对象不重复状态——rpc_id 的权威来源是 message.source）

    dsh 还有第三种 placement='context'（运行时上下文快照：进队列区但不等处理）。
    本仓库刻意不走那条路——动态上下文靠每轮现叠 <todo_status> 合成消息
    （方案 A，见 agent.md §4），所以队列只有两种 placement。
    """
    placement: QueuedPlacement
    message: Message

    @property
    def id(self) -> str:
        """消息 id（撤回、认领、前端去重都用它）。"""
        return self.message.id

    @property
    def rpc_id(self) -> str:
        """提交身份（对齐 dsh 的 SessionQueuedItem.rpcId）。

        取自 message.source：同一条消息在"队列项"和"durable 消息"两个形态下
        带的是同一个 rpc_id，前端据此把本地回显原子地换成真身。
        """
        source = self.message.source
        return source.rpc_id if isinstance(source, UserSource) else ''


def with_text(message: Message, text: str) -> Message:
    """返回"同 id、同 source、文本被替换"的新消息（队列区 edit 用）。

    为什么必须保住 id：队列项的身份就是消息 id——撤回、认领、前端行的 key、
    rpc_id 关联全靠它。就地改文案只是换内容，不是换一条消息。
    没有文本块时补一个在最前（用户输入正常不会走到）。
    """
    blocks: list[ContentBlock] = []
    replaced = False
    for block in message.content:
        if isinstance(block, TextBlock):
            if not replaced:
                blocks.append(TextBlock(text=text))
                replaced = True
            continue          # 多文本块只保留改写后的第一个
        blocks.append(block)
    if not replaced:
        blocks.insert(0, TextBlock(text=text))
    return replace(message, content=tuple(blocks))


@dataclass(frozen=True)
class SessionEvent:
    """一条会话事件：日志（唯一事实源）的最小单位。

    seq 单调递增；surface_op 是枢纽字段——标记"这条事件在 surface 上
    如何浮上水面"：
    - 'append'：追加到 surface 尾（user/message、assistant/message、
      tool/result 三类 surface 事件，compaction 的 checkpoint 也是普通
      user/message + append 之外的选择见下）
    - 'replace'：遮蔽一段旧 surface 区间并原位顶替（compaction 用）——
      被遮蔽的 seq 记录在 shadowed 字段，原始事件仍在日志（append-only
      不删行），只是从投影（模型可见）中消失
    - None：痕迹数据（chunk、边界、todo），永不浮上水面
    只有三类 surface 事件能带 surface_op；其余事件（chunk、边界、todo）
    是痕迹数据。
    """
    seq: int
    time: float
    type: str
    data: object = None
    surface_op: str | None = None
    shadowed: tuple | None = None  # surface_op='replace' 时：被遮蔽的 (start_seq, end_seq)
    ignorable: bool = False


@dataclass(frozen=True)
class StreamFrame:
    """瞬时流帧（issue #42）：**不进日志、不占 seq、不进投影**。

    它是"传输中的字节"，不是状态——模型可见性由 `assistant/message`（正文 + 工具调用参数）
    与 `assistant/reasoning`（思维链全文）保证，帧只喂给实时订阅者（终端打字机、Web SSE）。
    与 `SessionEvent` 分开是因为两者的**消费者不同**：事件给日志/投影/重放，
    帧只给"此刻正在看的人"。
    """

    type: str
    data: object = None


def new_event(seq: int, type_: str, data=None, surface_op: str | None = None,
              shadowed: tuple | None = None, ignorable: bool = False) -> SessionEvent:
    """事件工厂：打上当前时间戳，seq 由调用方（Session）保证单调。

    `ignorable=True` 表示"**旧读取者可以不认识它**"：词汇增长（加新事件类型）走这条路，
    不 bump 格式版本；结构性变化才 bump（判据见上面那段锚点说明）。
    """
    return SessionEvent(seq=seq, time=time.time(), type=type_, data=data,
                        surface_op=surface_op, shadowed=shadowed, ignorable=ignorable)


# ---- 日志格式锚点（DSH `SessionHeader` + 方向感知拒绝的复刻）----
#
# 为什么需要：日志是唯一事实源，但**格式本身没有版本**——写日志的进程升级、
# 事件类型增删之后，旧读取者会静默地按自己的理解重建一份**语义不同**的会话。
# 三条设计原则（对齐 DSH 的 session-log-version-mechanism）：
#   1. **一个单调整数，不分主次版本**：能否自动升级是那一步 upgrader 的属性，
#      不该由版本号形态预先承诺；
#   2. **写入者决定 bump**：判据不是"能否解析"，而是"旧运行时还能否**语义正确**
#      地处理"；拿不准就 bump（近乎恒等的 upgrader 几乎免费，漏 bump 会静默毁掉
#      旧读取者）；
#   3. **按方向读**：相等 → 正常；日志版本 > 读取者 → **拒绝**（指明升级方向，
#      与"损坏"分开——什么都没坏）；日志版本 < 读取者 → 内存里跑迁移链，源文件不动。
#
# 配套第二轴：**未知事件默认"必读"**——不认识的、又没标 `ignorable: true`
# 的事件 → 拒绝重建。"忘标记 → 过度拒绝（麻烦）"远好于"默认忽略 → 静默恢复出
# 一份被掏空的会话（安全事故）"。**只有结构性变化才 bump 版本；词汇增长靠
# `ignorable` 标记，不 bump。**
SESSION_FORMAT_VERSION = 1

# 没有会话头的日志按这个版本读（本机制落地前写的文件，与 v1 的事件形状相同）。
LEGACY_SESSION_FORMAT_VERSION = 0

# 流式帧：**纯痕迹**事件类型（issue #42 起新日志不再写它们；老日志里占 98.9% 的事件）。
# 它们不在检索面里（`SURFACE` 不含），内容也不是独有的——`assistant/message` 有正文与
# 工具调用参数全文、`assistant/reasoning` 有思维链全文（#42 有逐字节等价的机械证明）。
# 所以"把会话重放给**读**的人"时整类跳过：既省内存也省解析（老日志两个数量级）。
TRACE_FRAME_TYPES = ('assistant/chunk', 'assistant/reasoning/chunk')

# 已知事件类型（闭集）。读取端拿它做未知事件守卫——**加新事件类型要同时加到这里**，
# 忘了加不会静默：新类型的日志在旧读取者那里会明确报"未知且不可忽略"。
# 判据：事件类型是否出现在 `my_coder/` 的 `session.append(...)` 里。
KNOWN_SESSION_EVENT_TYPES = frozenset({
    'agent/inbox/spliced',
    'assistant/attempt',
    'assistant/chunk',
    'assistant/message',
    'assistant/reasoning',
    'assistant/reasoning/chunk',
    # 流式汇总（issue #42）：每 step 一条，替代"每个流帧一条事件"。
    # 它**不是** surface 事件、也不带内容——内容已经在 assistant/message 与
    # assistant/reasoning 里；这里只留"这次流式发生了多少帧、花了多久"。
    'assistant/stream',
    'compaction/end',
    'compaction/start',
    'compaction/summary',
    'request/header',
    'session/repaired',
    'session/title',
    'session/workspace',
    'step/end',
    'step/start',
    'todo/write',
    'tool/call',
    'tool/result',
    'tool/skipped',
    'turn/end',
    'turn/start',
    'user/message',
    'web/search',
})


@dataclass(frozen=True)
class SessionHeader:
    """会话文件的第一行：**不是事件**，不参与 seq 编号，也不进任何投影。

    为什么不把它做成 `SessionEvent`：`Session._log[seq]` 依赖"seq == 索引"
    （`derive_messages` 直接按下标取），让头占掉 seq 0 会把整库序号平移，
    并且坏掉所有"按 seq 下标取事件"的读取方（如召回审计脚本）。
    头是**文件级元数据**，与事件流正交——这也是 DSH 把 `SessionHeader` 与
    事件信封分开的原因。

    字段刻意只有三个：**头的职责就是钉住格式版本**。工作区已经有
    `session/workspace` 痕迹事件这个家（一个事实只有一个家），塞进头里
    会立刻变成第二个会漂的副本。
    """
    version: int = SESSION_FORMAT_VERSION
    id: str = ''
    time: float = 0.0


def session_header_to_json(header: SessionHeader) -> dict:
    """会话头 → 磁盘形态（JSONL 的第一行）。"""
    return {
        'session': True,
        'format_version': header.version,
        'id': header.id,
        'time': header.time,
    }


def session_header_from_json(data: dict) -> SessionHeader:
    """`session_header_to_json` 的严格逆操作。"""
    return SessionHeader(
        version=int(data['format_version']),
        id=str(data.get('id', '')),
        time=float(data.get('time', 0.0)),
    )


def new_session_header(session_id: str,
                       version: int = SESSION_FORMAT_VERSION) -> SessionHeader:
    """新建会话头（时间戳在此打上）。"""
    return SessionHeader(version=version, id=session_id, time=time.time())


def is_session_header_line(data: dict) -> bool:
    """这一行是不是会话头（用 `session: true` 标记，与事件信封区分）。

    判据刻意用**显式标记**而不是"没有 type 字段"：后者会把将来任何结构变化
    误判成头，而且没法与"损坏的行"区分。
    """
    return data.get('session') is True



# ---- JSONL 编解码：tagged dict 方案 ----
# JSON 没有类型信息，所以用 "$xxx" 前缀 key 做类型标记：
# {"$text": "..."} 是 TextBlock，{"$message": {...}} 是 Message，
# {"$dict"/"$list": ...} 递归包裹任意嵌套容器，其余是普通 JSON 值。
# to/from 严格对称：任何值 to_json 后 from_json 必能还原（测试断言）。


def block_to_json(block: ContentBlock) -> dict:
    """内容块 → tagged dict：$text/$tool-call/$tool-result 三个标记。"""
    if isinstance(block, TextBlock):
        return {'$text': block.text}
    if isinstance(block, ToolCallBlock):
        return {'$tool-call': [block.id, block.name, block.arguments]}
    return {'$tool-result': [block.tool_call_id, block.content, block.is_error]}


def block_from_json(data: dict) -> ContentBlock:
    """block_to_json 的严格逆操作。"""
    if '$text' in data:
        return TextBlock(text=data['$text'])
    if '$tool-call' in data:
        block_id, name, arguments = data['$tool-call']
        return ToolCallBlock(id=block_id, name=name, arguments=arguments)
    call_id, content, is_error = data['$tool-result']
    return ToolResultBlock(tool_call_id=call_id, content=content, is_error=is_error)


def source_to_json(source: MessageSource) -> dict:
    """来源 → tagged dict：$user/$model/$tool/$plugin。

    $user 的值是 rpc_id（空串 = 没有提交身份）。历史日志里是 `true`
    （加 rpc_id 之前的格式），由 source_from_json 兼容读回。
    """
    if source.kind == 'user':
        return {'$user': source.rpc_id}
    if source.kind == 'model':
        return {'$model': [source.provider, source.model]}
    if source.kind == 'tool':
        return {'$tool': source.call_id}
    return {'$plugin': source.plugin}


def source_from_json(data: dict) -> MessageSource:
    """source_to_json 的严格逆操作（兼容旧格式 `{'$user': true}`）。"""
    if '$user' in data:
        raw = data['$user']
        return UserSource(rpc_id=raw if isinstance(raw, str) else '')
    if '$model' in data:
        provider, model = data['$model']
        return ModelSource(provider=provider, model=model)
    if '$tool' in data:
        return ToolSource(call_id=data['$tool'])
    return PluginSource(plugin=data['$plugin'])


def message_to_json(message: Message) -> dict:
    """消息 → 普通 dict（不含 $message 标记，由 data_to_json 统一包裹）。"""
    return {
        'id': message.id,
        'role': message.role,
        'content': [block_to_json(block) for block in message.content],
        'source': source_to_json(message.source),
    }


def message_from_json(data: dict) -> Message:
    """message_to_json 的严格逆操作。"""
    return Message(
        id=data['id'],
        role=data['role'],
        content=tuple(block_from_json(block) for block in data['content']),
        source=source_from_json(data['source']),
    )


def data_to_json(data: object):
    # 递归遍历：Message 套 dict 套 list 任意嵌套都能还原。
    # 比如 tool/result 事件的 data 就是一条 Message（套着 blocks 和 source）。
    if isinstance(data, Message):
        return {'$message': message_to_json(data)}
    if isinstance(data, dict):
        return {'$dict': {key: data_to_json(value) for key, value in data.items()}}
    if isinstance(data, (list, tuple)):
        return {'$list': [data_to_json(value) for value in data]}
    return data


def data_from_json(value):
    """data_to_json 的严格逆操作：按标记还原类型。"""
    if isinstance(value, dict) and '$message' in value:
        return message_from_json(value['$message'])
    if isinstance(value, dict) and '$dict' in value:
        return {key: data_from_json(item) for key, item in value['$dict'].items()}
    if isinstance(value, dict) and '$list' in value:
        return [data_from_json(item) for item in value['$list']]
    return value


def event_to_json(event: SessionEvent) -> dict:
    """事件 → 磁盘上的最终形态：一行 JSON（JSONL 的 L），save_event 调用。"""
    return {
        'seq': event.seq,
        'time': event.time,
        'type': event.type,
        'data': data_to_json(event.data),
        'surface_op': event.surface_op,
        'shadowed': list(event.shadowed) if event.shadowed else None,
        'ignorable': event.ignorable,
    }


def event_from_json(data: dict) -> SessionEvent:
    """event_to_json 的严格逆操作：load_events 读回一行时调用。"""
    shadowed = data.get('shadowed')
    return SessionEvent(
        seq=data['seq'],
        time=data['time'],
        type=data['type'],
        data=data_from_json(data['data']),
        surface_op=data.get('surface_op'),
        shadowed=tuple(shadowed) if shadowed else None,
        ignorable=data.get('ignorable', False),
    )
