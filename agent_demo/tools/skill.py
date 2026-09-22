"""应用层工具：skill——按**名字**取一份技能的正文。

为什么需要它（而不是让模型用 `read_file` 按路径读）：技能有两个来源，其中
**bundled** 随 agent 发布、位于包内（工作区之外），而 `read_file` 被沙箱锁在工作区
内——包内技能根本读不到（实测：`path outside workspace`）。这个工具让模型给**名字**，
由 host 把名字解析到具体文件（自带技能是 host 自己的资源）：

- **沙箱承诺不受影响**：模型没有机会拼出路径；名字查不到就是一条 `is_error` 结果
  （失败降级为结果，不变式 5），不需要给沙箱开任何例外
- **正文作为 tool/result 进日志**：与读任何文件机制一致——可重建、可被 compaction
  折叠、前端画成"读取技能"工具卡
- **目录与工具共用同一张技能表**（`factory.build_agent` 里算一次、传两处），
  所以"目录里有的"和"工具能取到的"永不漂移——和 `ToolSpec` 的
  "schema 与 executor 绑在一条注册里"是同一个原则
"""
from __future__ import annotations

from ..registry import ToolOutcome, ToolSpec
from ..skills import read_skill_body, resolve_skill


def register(registry, skills) -> None:
    """注册 skill 工具；skills 是 build 时算好的合并技能表（bundled + workspace）。"""
    async def skill_tool(args, agent, signal):
        name = str(args['name']).strip()
        skill = resolve_skill(skills, name)
        if skill is None:
            available = ', '.join(item.name for item in skills) or '(none)'
            return ToolOutcome(
                content=f'no skill named {name!r}; available: {available}', is_error=True)
        try:
            body = read_skill_body(skill)
        except OSError as error:
            return ToolOutcome(content=f'cannot read skill {name!r}: {error}', is_error=True)
        return ToolOutcome(content=body)

    registry.register(ToolSpec(
        name='skill',
        description=(
            'Load the full text of one skill by name. The skill catalog in your system prompt '
            'lists the available names; call this when a task matches a skill description, then '
            'follow the returned playbook. Procedures live in skills instead of in every prompt, '
            'so load the matching one instead of guessing how the task is done.'
        ),
        parameters={
            'type': 'object',
            'properties': {
                'name': {
                    'type': 'string',
                    'description': 'Skill name exactly as listed in the catalog.',
                },
            },
            'required': ['name'],
        },
        execute=skill_tool,
        # 纯读文件、不碰 agent/session 状态：可并发 + 卸载到线程（同步读盘）
        execution_mode='parallel',
        offload=True,
    ))
