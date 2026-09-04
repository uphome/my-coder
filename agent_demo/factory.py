"""应用层：agent 工厂——build_agent() 组装一次完整 agent（CLI / Web 共用）。

system prompt 由 PromptRegistry 的 section 拼装（identity/persona/工具提示，
按 order 排序）；{{model}}/{{workspace}} 是严格插值变量，组装时求值。
真实模型从环境变量取 key；--fake 注入脚本化假模型（离线演示，不需 key）。
渲染订阅（session.on_event → render_event）也在这里挂——UI 是日志投影。
"""
from __future__ import annotations

import os
from pathlib import Path

from .agent import Agent
from .constants import DEMO_SCRIPT
from .llm import FakeLlm, OpenAiCompatibleLlm
from .prompt import PromptRegistry
from .session import Session
from .tools import build_tools
from .ui import render_event


def load_env(path: Path) -> None:
    """把 .env 里的 KEY=VALUE 注入进程环境；已存在的环境变量优先（不覆盖）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def build_agent(session: Session, args, ui_state: dict, hooks=None) -> Agent:
    prompt = PromptRegistry()
    prompt.section('identity', -100, 'You are {{model}}, a coding agent that helps with programming tasks. Read, search, edit, and run commands in the workspace to help the user — verify your work instead of guessing. Never claim to be a different AI model or company than {{model}}; if asked, state the model name exactly as given here.')
    prompt.section('persona', 0, 'You run on the {{model}} model. Your workspace is {{workspace}}; tool paths resolve relative to it, and nothing outside it is readable or writable.\nVerify work by running code or tests. Keep answers brief.')
    prompt.section('tool:todo', 110, 'Use todo_write to plan multi-step work before you start.')
    prompt.section('tool:bash', 105, 'Use bash to verify work (run tests, git status). Output is capped: redirect large outputs to a file and read it with read_file. In this repo run tests with "conda run -n agent-demo python -m pytest -q".')
    prompt.variable('model', lambda ctx: ctx['agent'].options.get('model', ''))
    prompt.variable('workspace', lambda ctx: str(args.workspace))

    llm: object  # FakeLlm / OpenAiCompatibleLlm 鸭子类型共用 stream()，Agent 不校验具体类
    if args.fake:
        llm = FakeLlm(script=DEMO_SCRIPT, provider='fake', model='fake-model')
        options = {'provider': 'fake', 'model': 'fake-model'}
    else:
        api_key = os.environ.get('DEEPSEEK_API_KEY')
        if not api_key:
            raise SystemExit('missing DEEPSEEK_API_KEY (set it in the environment or run with --fake)')
        llm = OpenAiCompatibleLlm(
            base_url=os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com'),
            api_key=api_key,
            model=args.model,
            provider='deepseek',
        )
        options = {'provider': 'deepseek', 'model': args.model}

    agent = Agent(session=session, llm=llm, prompt=prompt, tools=build_tools(workspace=args.workspace), options=options, hooks=hooks)

    def on_event(event) -> None:
        # UI 是日志的投影：渲染逻辑在模块级 render_event（resume 重放共用同一份）
        render_event(event, args.hide_reasoning, ui_state)

    session.on_event(on_event)
    return agent
