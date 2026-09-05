"""值层：消息与事件的不可变词汇表 + JSONL 编解码。

Python 的 frozen dataclass 只冻结字段赋值，不深冻结嵌套容器——
约定：所有内容一律用 frozen dataclass 与 tuple，禁止把可变容器放进
消息/事件。这就是"不可变值对象"在 Python 里的落地方式（harness 用
deepFreeze，这里用类型 + 约定）。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
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


ContentBlock = TextBlock | ToolCallBlock | ToolResultBlock


# 消息来源：回答"这条消息是谁产生的"。
# user=用户输入；model=某个模型（记录 provider/model，audit 用）；
# tool=工具结果（记录 call_id，回指工具调用）；plugin=插件注入。


@dataclass(frozen=True)
class UserSource:
    """用户输入的消息。"""
    kind: Literal['user'] = 'user'


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


def new_event(seq: int, type_: str, data=None, surface_op: str | None = None,
              shadowed: tuple | None = None) -> SessionEvent:
    """事件工厂：打上当前时间戳，seq 由调用方（Session）保证单调。"""
    return SessionEvent(seq=seq, time=time.time(), type=type_, data=data,
                        surface_op=surface_op, shadowed=shadowed)


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
    """来源 → tagged dict：$user/$model/$tool/$plugin。"""
    if source.kind == 'user':
        return {'$user': True}
    if source.kind == 'model':
        return {'$model': [source.provider, source.model]}
    if source.kind == 'tool':
        return {'$tool': source.call_id}
    return {'$plugin': source.plugin}


def source_from_json(data: dict) -> MessageSource:
    """source_to_json 的严格逆操作。"""
    if '$user' in data:
        return UserSource()
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
