"""跨文件共用的测试 helper（拆分 tests/test_demo.py 时提出来的）。

只有被**多个**目标文件用到的 helper 才放这里；只被一个文件用到的留在那个文件里，
读者不必为了看懂一个用例跳到 conftest。
"""
from __future__ import annotations

from my_coder.capability.llm import FakeLlm
from my_coder.runtime.agent import Agent
from my_coder.state.prompt import PromptRegistry
from my_coder.state.registry import ToolRegistry
from my_coder.state.session import Session


def make_agent(script, session=None, hooks=None):
    session = session if session is not None else Session(id='test')
    llm = FakeLlm(script=script, provider='fake', model='fake-model')
    return Agent(
        session=session,
        llm=llm,
        prompt=PromptRegistry(),
        tools=ToolRegistry(),
        options={'provider': 'fake', 'model': 'fake-model'},
        hooks=hooks,
    ), session
