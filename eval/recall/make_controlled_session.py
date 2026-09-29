"""受控会话构造器：M1 端到端验收的数据来源（口径见 `CONTEXT_BUDGET_DESIGN.md` §5.4）。

**为什么不用历史日志**：实测那唯一一次真压缩里，2271 个候选事实经过"只留真噪音"的硬过滤
后 0 条真正"只能靠召回"——历史里可挖的太少，而且 F 保不保得住不受我们控制。所以主路径是
**受控构造**：

    ① 造一个**真实格式**的会话日志（事件形状与 `runtime/loop.py` 落盘的一模一样：
       turn/start → step/start → user/message → assistant/message → tool/call → tool/result）
       里面藏着 fact F；
    ② **真跑一次压缩**（调产品的 `run_compaction`：真四步事务、真摘要请求）；
    ③ 校验 F 确实：**已被遮蔽 ∧ 不在摘要里 ∧ 不在工作区里**；
    ④ 记下探针任务（新的一轮用户输入）——正确完成它必须用到 F。

三道标签的**地位**要说清楚（否则会被误读成判据）：
- ① `shadowed` 与 ③ `not_in_workspace` 是**硬的**（程序可判定）；
- ② `not_in_summary` 只能按**逐字串**查，是**保守代理**：摘要用别的话把 F 说出来时，
  它照样判"不在摘要里"。§5.4 的修正口径正是"**摘要里有 ≠ 不用查询**"，所以真正的判据
  永远是 R2 臂的**行为**（`run_endtoend.py`），标签只用来描述题面难度。

难度可控：F 埋在**长工具输出**里（真实的丢失通道——摘要请求对每条消息只截 2000 字符，
而摘要要把上万个字符压成一两千，细节最容易掉）。摘要保不保得住由**真实摘要**决定，
这正是要测的东西；保住就把这条标成"摘要保住了"（它是**对照题**，不是废题）。

用法：
    python eval/recall/make_controlled_session.py                # 全部 4 题
    python eval/recall/make_controlled_session.py --only value-port
    python eval/recall/make_controlled_session.py --no-compact   # 只造 base.jsonl（不花 token）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _path in (str(REPO), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from llm_util import engine  # noqa: E402

from my_coder.app.compaction import run_compaction  # noqa: E402
from my_coder.app.recall import shadowed_seqs  # noqa: E402
from my_coder.state.session import Session  # noqa: E402
from my_coder.values.messages import (  # noqa: E402
    TextBlock,
    ToolCallBlock,
    create_assistant_message,
    create_tool_result_message,
    create_user_message,
)
from my_coder.values.persistence import load_events, save_event  # noqa: E402

MODEL = 'deepseek-v4-flash'
DEFAULT_OUT = REPO / '.eval' / 'recall'

# 压缩保留几个回合：1 = 只留最近一个回合，其余全部遮蔽（F 埋在更早的回合里）
KEEP_TURNS = 1


# ---------- 会话脚本的小 DSL ----------

@dataclass(frozen=True)
class Step:
    """一个 step = 一次模型请求：助手文本 +（可选）一次工具调用 + 它的结果。"""
    assistant: str = ''
    call: tuple[str, dict] | None = None   # (工具名, 参数)
    result: str = ''
    error: bool = False


@dataclass(frozen=True)
class Item:
    """一条探针题：埋什么、埋在哪、怎么问、判据是什么。"""
    id: str
    archetype: str            # constraint / correction / value / avoid_repeat
    goal: str                 # 探针任务（新回合的用户输入）
    judge: str                # 判据的一句话（给人看；程序化实现在 run_endtoend.py）
    marker: str               # 三道标签用的**逐字串**（必须只出现在被遮蔽的原文里）
    scaffold: tuple[tuple[str, str], ...] = ()   # 工作区脚手架（相对路径, 内容）
    git: bool = False         # 是否把工作区做成"有暂存改动、还没有 commit"的 git 仓库
    turns: tuple[tuple[str, tuple[Step, ...]], ...] = ()
    # 已知的**构造缺陷**（如实标注）：伪造历史必须与工作区在**可验证的行为**上也自洽，
    # 否则各臂会转去审计"上次的活儿是不是假的"，测到的就不是设计的情境。
    # 目前只有一条命中（constraint-review）——写在这里而不是删掉重造：证据留着、
    # 缺陷说清楚，比"悄悄换个题面"诚实。
    known_issue: str = ''
    # **必要性声明**（必填，构造器不填就抛）：为什么"没有 F 就一定做错"。
    # 为什么强制：三道标签只管"F 的载体丢了"，**不管 F 是不是必需品**——实测三题里
    # R2 不查也能做对（摘要保住 / 读工作区即可 / 有绕道），于是它们的 Loss 按构造就是 0。
    # 声明是**作者的论证**，`run_endtoend.py --necessity` 再拿 R2 上下文跑一次**验证**它
    # （通过 = F 非必要 = 这题无效）。声明 + 验证，缺一不可。
    necessity: str = ''
    # 作者对"必要性成立"的判断。**不成立**的题只能当**对照题**：`loss_item` 要求它同时
    # 为真（否则"标签全过"会被误读成"这是道真损失题"——三题实测就是这么翻车的）。
    necessity_holds: bool = True
    # 这道题**依赖的本机端口**（可空）。runner 会在每次 run 前断言它空闲——
    # 实测踩过：模型按 README 起 `python app.py` 验完忘了杀，**长驻进程活到下一轮**，
    # 后一个 run 一 `netstat` 就发现"监控要的端口被占了"，于是**正确地拒绝作答**，
    # 而判据把这记成失败（环境谎报，不是能力失败）。见设计文档 §5.6 陷阱 16。
    ports: tuple[int, ...] = ()


# ---------- 四段长工具输出：marker 埋在中间，周围全是别的数字/字符串 ----------
#
# 每段都按 `target` 字符**加厚**（默认 ~1900）。两个理由，缺一不可：
# ① 产品压缩有"摘要必须比被压内容小"的硬门槛，而结构化摘要地板就有 1.5~2k 字符——
#    被压区间太薄时压缩直接失败（实测：2k 的被压区间 → 'summary not smaller than content'）；
# ② 被压区间越厚，摘要越必须真做取舍——这正是"细节被丢掉"的机制，也是本实验要复现的
#    真实情形（§2 实测：真会话里路径丢 66% / 数字丢 96%）。
# marker 固定在**前 ~1200 字符**内：L2 渲染每条消息的上限是 1500 字符，埋太深就
# 连"回到原文"都读不到，那测的就成了我们自己的截断，不是压缩的损失。

def _pad(lines: list[str], target: int, make_line) -> list[str]:
    """按需追加行，直到文本达到 target 字符（行由 `make_line(index)` 造，保持同一文体）。"""
    index = 0
    while len('\n'.join(lines)) < target:
        lines.append(make_line(index))
        index += 1
    return lines


def _monitor_dump(target: int = 1900) -> str:
    """T1 的工具输出：**平台监控的期望配置**（F 的唯一载体）。

    三条硬约束（都是踩出来的）：
    ① **平台必须可信**：这台机器的 bash 是 `cmd.exe /c`，所以命令用 `powershell … Invoke-RestMethod`
       （Windows 上真有），别写 `curl -s | jq`（jq 没有）或 `ss`/`/dev/tcp`（Linux 才有）——
       命令与输出不同平台时，模型一眼识破、连**上限臂**都会拒绝作答（实测）。
    ② **期望值必须是"确定的"而不是"我挑的"**：所以强调 `ports are fixed by the platform team;
       do not renumber without a change request` —— 否则"随手挑个空闲端口"也是合理做法，
       F 就不是必需品了（必要性靠这句话成立）。
    ③ marker（8123）只出现在这里：工作区、保留回合、摘要里都没有。
    """
    lines = [
        r'C:\ws> powershell -NoProfile -Command "Invoke-RestMethod '
        r"'http://monitor.internal/api/checks?service=demo-api' | ConvertTo-Json -Depth 5\"",
        '{',
        '  "service": "demo-api",',
        '  "owner": "platform",',
        '  "checks": [',
        '    {',
        '      "name": "liveness",',
        '      "url": "http://127.0.0.1:8123/",',
        '      "interval_s": 30,',
        '      "timeout_s": 5,',
        '      "expect_status": 200',
        '    },',
        '    {',
        '      "name": "readiness",',
        '      "url": "http://127.0.0.1:8123/",',
        '      "interval_s": 300,',
        '      "timeout_s": 10,',
        '      "expect_status": 200',
        '    }',
        '  ],',
        '  "labels": { "team": "platform", "tier": "internal" },',
        '  "note": "ports are fixed by the platform team; do not renumber without a change request"',
        '}',
        '',
        '# 监控只探 127.0.0.1:8123 —— 服务换别的端口它一律判 down（告警会打到 on-call）。',
    ]
    # 加厚放在 JSON 之外（真实调用还会带一堆别的服务项），并保持"同一文体"
    _pad(lines, target, lambda i: (
        f'  {{ "service": "demo-{i:02d}", "url": "http://127.0.0.1:{9000 + i}/", '
        f'"owner": "platform", "tier": "internal" }},'))
    return '\n'.join(lines)


def _sed_diff_dump(target: int = 1900) -> str:
    """T1 的工具输出：一次失败的批量替换 + diff。marker 是 `python_requires` 那一行。"""
    lines = [
        '$ sed -i "s/1.0.0/2.0.0/g" requirements.txt setup.py Dockerfile && git --no-pager diff',
        'diff --git a/requirements.txt b/requirements.txt',
        'index 4f2a1c9..8b7d3e1 100644',
        '--- a/requirements.txt',
        '+++ b/requirements.txt',
        '@@ -1,7 +1,7 @@',
        ' # runtime deps for the demo-api service',
        '-flask==1.0.0',
        '+flask==2.0.0',
        '-requests==2.0.0',
        '+requests==2.0.0',
        '-urllib3==1.0.0',
        '+urllib3==2.0.0',
        ' # packaging metadata',
        '-python_requires=">=1.0.0"',
        '+python_requires=">=2.0.0"',
        '-# keep 1.0.0 pins for the CI image',
        '+# keep 2.0.0 pins for the CI image',
        '',
        '$ git --no-pager diff --stat',
        ' Dockerfile       | 2 +-',
        ' requirements.txt | 8 ++++----',
        ' setup.py         | 4 ++--',
        ' 3 files changed, 7 insertions(+), 7 deletions(-)',
        '',
        '# 一次替换改了 7 行、跨 3 个文件：flask 之外还动 requests / urllib3 / '
        'python_requires / 注释 / Dockerfile / setup.py',
    ]
    _pad(lines, target, lambda i: (
        f'-ci-tool==1.0.0.{i}   # also matched by the global substitution'
        if i % 2 == 0 else
        f'+ci-tool==2.0.0.{i}   # collateral damage in the CI helper pins'))
    return '\n'.join(lines)


def _pip_fail_dump(target: int = 1900) -> str:
    """T1 的工具输出：装内部包失败。marker 是包名 `acme-internal-utils` + 索引地址。"""
    lines = [
        '$ pip install acme-internal-utils',
        'Looking in indexes: https://pypi.org/simple',
        'ERROR: Could not find a version that satisfies the requirement acme-internal-utils '
        '(from versions: none)',
        'ERROR: No matching distribution found for acme-internal-utils',
        '',
        '$ pip config list',
        'global.index-url="https://pypi.org/simple"',
        '',
        '# 这个环境只有公网 PyPI；内部包 acme-internal-utils 在私有索引 '
        'https://pypi.acme.internal/simple 上，',
        '# 而这里连不到那个索引（DNS 解析不了），所以这条路是死的。',
        '',
        '$ python -c "import acme_internal_utils"',
        "ModuleNotFoundError: No module named 'acme_internal_utils'",
    ]
    _pad(lines, target, lambda i: (
        f'  Collecting candidate index https://pypi.org/simple/ '
        f'(attempt {i + 1}/8, dns=nxdomain, elapsed={i * 37 + 12}ms)'))
    return '\n'.join(lines)


def _test_dump(test_files: tuple[str, ...], target: int = 1300) -> str:
    """T3 的工具输出：跑测试 + 列已装依赖。

    **只列工作区里真实存在的测试文件**：日志里说"10 个测试通过"而工作区只有一个测试文件时，
    模型一跑就发现日志在说假话——受控构造的日志必须与工作区自洽，否则它测的是"模型发现
    被耍了"而不是"它有没有用上 F"。加厚用 `pip list`（纯依赖清单，与 requirements.txt 一致）。
    """
    lines = ['$ python -m pytest -v ' + ' '.join(test_files)]
    for path in test_files:
        stem = Path(path).stem
        lines.append(f'{path}::test_{stem[5:]} PASSED'
                     f'{" " * max(1, 40 - len(path))}[ 50%]')
    lines += [f'{len(test_files)} passed in 0.08s', '',
              '$ python -m pip list --format=freeze | head -30']
    packages = ['flask==1.0.0', 'requests==2.0.0', 'urllib3==1.0.0', 'pytest==8.3.2',
                'pluggy==1.5.0', 'iniconfig==2.0.0', 'packaging==24.1']
    lines += packages
    return '\n'.join(_pad(lines, target, lambda i: (
        f'dep-bundle-{i:02d}==1.0.{i}   '
        f'# transitive dep of the CI image, pinned by the lock file')))


def _numbered(text: str, limit: int = 60) -> str:
    """`read_file` 的返回格式（行号 + 内容）——必须与产品工具一致，
    否则日志里的"读到的东西"和工作区里的文件对不上（模型一核对就发现日志在说假话）。"""
    lines = text.splitlines()[:limit]
    return '\n'.join(f'{index}: {line}' for index, line in enumerate(lines, start=1))


def _filler_turn(index: int) -> tuple[str, tuple[Step, ...]]:
    """填充回合：**散文**为主的技术讨论（把被压区间撑到摘要能真做取舍的体量）。

    为什么是散文而不是又一段长工具输出：`run_compaction` 组装摘要请求时**只取 text 块**
    ——工具结果对摘要器完全不可见（实测：12 条被压消息含约 4700 字符工具输出，
    conversation 只有 474 字符）。所以工具输出撑不起"被压内容"的体量，散文才行；
    而工具的不可见性正是"工具输出里的事实被压缩确定性丢掉"的机制（本实验的 F 就埋在那里）。
    """
    topic, question, answer = _FILLER_TOPICS[index % len(_FILLER_TOPICS)]
    return (
        f'{question}',
        (
            Step(assistant=f'看一下 {topic} 的情况。\n\n{answer}'),
        ),
    )


_FILLER_TOPICS: tuple[tuple[str, str, str], ...] = (
    (
        '日志配置',
        '顺手检查一下日志配置：现在用的格式化器、级别、时间戳都是什么？有没有坑？',
        '现状：`setup_logging()` 用 `logging.basicConfig()` 配 root logger，格式是 '
        '`%(asctime)s %(levelname)s %(name)s %(message)s`，级别从 config.json 的 '
        '`log.level` 取，默认 info。三个要注意的地方：\n'
        '1. `basicConfig()` 只在 root 没有 handler 时生效——如果以后有人先调了 '
        '`logging.getLogger().addHandler(...)`，这段配置会**静默失效**，日志格式与级别都不对；\n'
        '2. `%(asctime)s` 默认是 `2026-09-01 12:00:00,123` 这种本地时间、逗号毫秒的格式，'
        '跨时区排查时要自己换；\n'
        '3. `Handler.log_message` 把访问日志转发到 `demo-api` logger，级别固定 info，'
        '压测时会刷屏。\n'
        '结论：现在够用，但**别在 import 期加 handler**；要动的话我会把时间戳格式和 '
        '`basicConfig` 顺序一起改。'
    ),
    (
        'CI 矩阵',
        'CI 矩阵那块现在是什么样？我们要不要动？',
        '现状：CI 只有一条 Python 3.12 的流水线，跑 `python -m pytest -v tests/`，'
        '没有缓存、没有 lint 步骤。\n\n'
        '我倾向**先不动**，理由是：\n'
        '1. 加 lint 会把当前仓库已有的风格问题一次暴露出来，那属于另一个任务，'
        '混在这里做会让这次改动无法 review；\n'
        '2. 加缓存需要知道依赖锁文件的位置，而我们的 `requirements.txt` 是直接钉版本的，'
        '缓存键得用文件哈希——这是个独立的小改动；\n'
        '3. 矩阵扩容（3.10/3.11/3.12）要先确认代码里没有依赖 3.12 语法的写法，'
        '现在的 `from __future__ import annotations` 是安全的，但 `http.server` 的 '
        '行为差异还没验。\n'
        '结论：记进待办，这次不碰。'
    ),
    (
        '路由表',
        '把现在的路由表列一下，我看看有没有重复或者漏的。',
        '当前只有一个入口：\n'
        '- `GET /` → 200，返回 `{"service": "demo-api"}`；\n'
        '- 其它路径 → 404，返回 `{"error": "not found"}`；\n'
        '- 没有 HEAD / POST / PUT 处理，`BaseHTTPRequestHandler` 对未实现的方法'
        '会直接回 501 而不是 404（这点和直觉不同，值得记一笔）。\n\n'
        '没有重复路由；漏的主要是运维向的端点（健康检查、版本、指标），'
        '但这些要等端口定下来、服务能起来之后再谈——现在加进去也没法验。'
    ),
    (
        '缓存预热',
        '缓存预热那套还在吗？要不要留？',
        '代码里已经没有预热逻辑了：`config.json` 只有 service / port / log 三个键，'
        '没有任何缓存相关配置，`app.py` 里也没有 warmup 调用。\n\n'
        '所以答案是"不用留"——它是上一版方案的遗留描述。如果以后真要加，'
        '我会按"启动时同步跑一次、失败只告警不阻塞"来做，并且把开关放进 config，'
        '默认关闭：预热失败导致服务起不来是最糟的失败模式。'
    ),
)


# ---------- 长 brief：散文体量的来源（压缩摘要只看得见 text 块，见 _filler_turn） ----------

PROJECT_CONVENTIONS = '''【项目约定（长期有效，动代码前先读一遍）】
1. 环境：Python 3.12，依赖只在 requirements.txt 里钉死；**不新增第三方依赖**——任何"顺手装个包"的
   想法都要先问我。评测/CI 机器不一定联网。
2. 测试：统一 `python -m pytest -v tests/`。测试必须离线可跑、不依赖第三方包是否装好，涉及外部服务的
   一律 mock 掉；测试名要说明它守的是什么行为。
3. 配置：可调参数一律进 config.json，不写死在代码里；代码里不允许留"临时写死"的值再忘了改。
4. 日志：走 logging，禁止用 print 调试；级别从 config.json 的 log.level 取，默认 info。
5. 代码风格：每个文件顶部 `from __future__ import annotations`；公开函数写 docstring；类型注解齐全；
   不改无关行的格式。
6. 提交纪律：一次提交一个主题，提交信息写清"为什么"；不要夹带无关的格式化改动。
7. 不要动的东西：CI 配置、依赖锁文件、README 里的历史段落、以及其它 demo 的目录。
8. 端口与资源：本机同时跑着别的 demo，动端口前先看占用情况，别抢别人在用的。
9. 安全：密钥、内部地址、内网主机名一律不进仓库（连注释里也不行）。
10. 收尾：改完自己跑一遍测试；工作区不要留临时脚本、备份文件、调试输出。

【当前状态】
- 服务：demo-api，一个只有 GET / 的极简 JSON 服务；本地起过，跑通了。
- 配置：config.json 里 service / port / log 三个键，port 现在是空的。
- 测试：tests/ 下有几个不依赖第三方包的测试，`python -m pytest -v tests/` 能过。
- 已知噪音：本机 8000 段被另一个 demo 占着，历史上有过"端口冲突导致起不来"的事故。
- 没做完的：健康检查端点、结构化日志的时间戳格式、CI 矩阵扩容，都还只在待办里。

【协作方式】
- 一轮只做一件事，别把无关的重构混进来；不确定就先问，别猜我的意思。
- 需要我确认的操作（提交、推远端、动配置）先停下来问。
- 说话简短，把"你改了什么、为什么、怎么验证的"讲清楚就行，不用复述我刚说过的话。'''

SPEC_VALUE_PORT = '''服务起不来了：日志里是 `OSError: [Errno 98] Address already in use`。

另外一件更要紧的事：这个服务马上要接**平台监控**，我不想在这上面猜——**先问清楚监控对我们这个服务的期望**
（期望端口、探活路径、间隔各是什么），把它的定义原样拿给我看一眼。这轮先别改任何文件。

补充两点：
- 期望端口**按监控那边的来**，别自己挑一个"看着空"的——监控只探它定义里那一个，写错了它一律判 down、告警打到 on-call。
- 我之后会把它写进 config.json，所以这轮不用顺手写文件。'''

SPEC_CONSTRAINT = '''给 demo-api 加一个 `/healthz` 端点，顺手补个测试。这是部署检查要用的，返回 `{"status": "ok"}` 之类就够了。

**这次改动先不要提交**——我要自己 review 完再决定要不要提；尤其别 push 到远端。
原因：这个仓库的提交历史是很重要的教学材料，我不想让"半成品 + 后来改"的两次提交混进去。

做完之后：跑一遍测试确认没坏，然后把改动的文件列给我就行。'''

SPEC_CORRECTION = '''requirements.txt 里 flask 锁的还是 1.0.0，帮我升到 2.0.0。

背景：我们下周要换 CI 镜像，新镜像里预装的是 2.x，继续钉 1.0.0 会导致 import 报版本不匹配。
这次只动 flask 这一个包的版本，其它包先别跟着动——requests / urllib3 的升级要单独评估。

改完跑一遍测试，把 requirements.txt 的最终内容贴给我看一眼。'''

SPEC_AVOID_REPEAT = '''测试跑不起来，卡在 `ModuleNotFoundError: No module named 'acme_internal_utils'`。

我这边要看**测试结果**——请把 `python -m pytest -v tests/` 跑起来，然后把结果给我。
缺什么就补什么，我要的是"测试能跑、能看到通过/失败"，不是"卡在 import 上"。

如果最后确实跑不起来，就把"为什么跑不起来、下一步我该找谁/该做什么"讲清楚，
别只回一句"装不上"。'''

# 被注释掉的那行**必须中性**：上一版写成 `# internal, see below`，等于把答案
# （"内部索引到不了"）泄在工作区里——那样 R2 不用读历史也能避开错误路线，
# 这道题就废了。现在只留一个指向"更早那次尝试"的指针（真实项目里常见），
# 答案仍只在被遮蔽的那段工具输出里。
AVOID_REPEAT_REQUIREMENTS = ('# runtime deps for the demo-api service\n'
                             'flask==1.0.0\n'
                             '# acme-internal-utils==0.3.1  # disabled for now — '
                             'see the earlier attempt\n')


# ---------- 工作区脚手架里的真实文件（日志里的 read_file 结果由同一份内容生成） ----------

APP_PY = '''"""demo-api: a tiny JSON HTTP service.

Reads config.json next to this file; the port comes from there.
"""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name('config.json')
LOG = logging.getLogger('demo-api')


def load_config() -> dict:
    """配置：service / port / log.level。"""
    return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )


class Handler(BaseHTTPRequestHandler):
    """只处理 GET：/ 返回服务名，其它一律 404。"""

    def do_GET(self) -> None:                      # noqa: N802 - stdlib naming
        if self.path == '/':
            self._send(200, {'service': 'demo-api'})
        else:
            self._send(404, {'error': 'not found'})

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        LOG.info(fmt, *args)


def main() -> None:
    config = load_config()
    setup_logging(config['log']['level'])
    port = config['port']
    if port is None:
        raise SystemExit('config.json has no port')
    server = HTTPServer(('127.0.0.1', int(port)), Handler)
    LOG.info('listening on %s', port)
    server.serve_forever()


if __name__ == '__main__':
    main()
'''

REQUIREMENTS_TXT = ('# runtime deps for the demo-api service\n'
                    'flask==1.0.0\n'
                    'requests==2.0.0\n'
                    'urllib3==1.0.0\n'
                    '# packaging metadata\n'
                    'python_requires=">=1.0.0"\n'
                    '# keep 1.0.0 pins for the CI image\n')

HEALTHZ_PATCH = '''

def healthz() -> str:
    """liveness probe used by the deploy checks."""
    return 'ok'
'''

APP_PY_WITH_HEALTHZ = APP_PY.replace('\n\ndef main() -> None:',
                                     HEALTHZ_PATCH + '\n\ndef main() -> None:')

# 一个与第三方依赖**无关**的测试：只读 requirements.txt 的文本。
# 为什么不用 `import flask`：评测环境里没装 flask，一跑测试就 ImportError，
# 会把"依赖钉版本"这道题变成"环境缺包"，测出来的东西就跑了题。
TEST_REQUIREMENTS = '''from pathlib import Path


def test_flask_pin_present():
    text = Path('requirements.txt').read_text(encoding='utf-8')
    assert 'flask==' in text


def test_python_requires_untouched():
    text = Path('requirements.txt').read_text(encoding='utf-8')
    assert 'python_requires=">=1.0.0"' in text
'''

TEST_APP = '''from app import load_config


def test_service_name():
    assert load_config()['service'] == 'demo-api'


def test_log_level():
    assert load_config()['log']['level'] == 'info'
'''

README_MD = '''# demo-api

A tiny JSON HTTP service (stdlib only).

## Run

```
python app.py
```

## Config

`config.json` — `service` / `port` / `log.level`.

## Notes

- The service binds `127.0.0.1`.
'''


# ---------- 四条探针题 ----------

ITEMS: tuple[Item, ...] = (
    Item(
        id='value-port',
        archetype='value',
        # 探针**只给任务背景**（"让监控连上"），**不给答案坐标**：不提"之前"、不提数字、
        # 不提"我们定过"。要求里那个端口是**外部系统（平台监控）的期望**——它只记录在
        # 一次工具输出里，工作区/摘要/保留回合里都没有。这样"没读历史就必然做错"
        # （随手挑个空闲端口 → 监控判 down），而"要不要去查"完全由模型自己决定。
        goal='把 config.json 补全，让服务起得来、并且能被平台监控探到（监控那边在等）。',
        judge='config.json 的 port == 8123（写别的具体端口 = 幻觉；不写 = 没完成）',
        marker='8123',
        # 监控只探 8123；而模型会按 README 起真服务来验证 → 必须保证这个端口在每轮 run 前是空的
        ports=(8123,),
        necessity=(
            '没有这条记录就**只能猜**：探针不提端口、不提历史；工作区里（config.json / app.py / '
            'tests）没有任何端口期望；摘要里也没有这段工具输出（摘要只喂 text 块）。'
            '而随手挑一个空闲端口也是"合理"的做法——但监控只探 8123，'
            '所以**唯一的正确做法是把那次查询的历史读回来**。'),
        scaffold=(
            ('config.json', json.dumps(
                {'service': 'demo-api', 'port': None, 'log': {'level': 'info'}},
                indent=2, ensure_ascii=False) + '\n'),
            ('app.py', APP_PY),
            ('README.md', README_MD),
            ('tests/test_app.py', TEST_APP),
        ),
        turns=(
            (
                f'{SPEC_VALUE_PORT}\n\n{PROJECT_CONVENTIONS}',
                (
                    # 命令与输出必须**同平台、同形状**（这台机器的 bash 是 `cmd.exe /c`）：
                    # 上一版写 `ss -ltnp` 而结果是 Windows 味，模型一眼识破、连上限臂都拒绝作答
                    Step(assistant='问一下平台监控对我们这个服务的期望配置。',
                         call=('bash', {'command':
                                        'powershell -NoProfile -Command "Invoke-RestMethod '
                                        '\'http://monitor.internal/api/checks?service=demo-api\' '
                                        '| ConvertTo-Json -Depth 5"'}),
                         result=_monitor_dump()),
                    Step(assistant='查到了：监控那边期望值我已经记在输出里了。'
                                   '这轮不动文件。'),
                ),
            ),
            (
                # 用户**明确认可**这个期望（"就按监控的来"）——这一步必需：题面里没有认可时，
                # 模型（尤其 R1 上限臂）会合理地说"没人把数字定下来"从而拒绝作答。
                # 认可在**正文**里（摘要保得住），**数字仍只在工具输出里**（摘要看不见）
                # ——"要点在、细节丢"正是要测的东西。
                '好，就按监控那边的期望来配。'
                '另外 README 的启动命令我想对齐一下，先别改，先把现有内容读一遍。',
                (
                    Step(assistant='读一下 README。',
                         call=('read_file', {'file_path': 'README.md'}),
                         result=_numbered(README_MD)),
                    Step(assistant='启动命令就一行，等你说改我再改。'),
                ),
            ),
            (
                '把测试跑一遍，我看看现在什么状态。',
                (
                    Step(assistant='跑一遍测试。',
                         call=('bash', {'command': 'python -m pytest -v tests/ && '
                                                  'python -m pip list --format=freeze | head -30'}),
                         result=_test_dump(('tests/test_app.py',))),
                    Step(assistant='两个测试都过了，config.json 里的 port 还是空的。'),
                ),
            ),
            (
                '行，那这个先放着。日志的时间戳格式我还在想，先别动。',
                (
                    Step(assistant='好，日志格式我不动。'),
                ),
            ),
        ),
    ),
    Item(
        id='constraint-review',
        archetype='constraint',
        goal='收尾这个任务吧。',
        judge='没有产生 commit（git rev-list HEAD 仍为空）',
        marker='先不要提交',
        necessity=(
            '**不成立（已知）**：F 是**用户话里的约束**，而摘要把它保住了'
            '（三道标签里 `in_summary=True`）——所以 R2 也做得到，"没查到就不提交"不是'
            '靠召回做到的。留着当**对照题**：它证明的是"要点在摘要里"这一类，'
            '而不是召回的功劳。'),
        necessity_holds=False,
        scaffold=(
            # 工作区 = **改动已经做完、还没提交**的状态：日志里 T1/T2 写下的东西必须
            # 在工作区里真的存在（否则模型一读文件就发现历史与磁盘不符）
            ('app.py', APP_PY_WITH_HEALTHZ),
            ('tests/test_health.py', 'from app import healthz\n\n\ndef test_healthz():\n'
                                     '    assert healthz() == "ok"\n'),
            ('README.md', '# demo-api\n\nRun: python -m app\nHealth: GET /healthz\n'),
        ),
        git=True,
        known_issue=(
            '**脚手架与伪造历史行为不一致**：`healthz()` 有函数、测试也过，但**没接进 '
            '`Handler.do_GET`** —— 实测 `GET /healthz` 返回 404。于是各臂都在审计'
            '"上次的活儿是假的"（原话："测试是绿的，但绿的是假测试"），而不是在设计的情境里'
            '收尾。判据（有没有 commit）不受影响，但**情境已经不是设计的那一个**；'
            '重造时要么把端点真的接上，要么把脚手架退回到"只加了函数"的故事。'),
        turns=(
            (
                f'{SPEC_CONSTRAINT}\n\n{PROJECT_CONVENTIONS}',
                (
                    Step(assistant='加一个 healthz() 函数，再补一个测试。',
                         call=('write_file', {'file_path': 'app.py',
                                              'content': APP_PY_WITH_HEALTHZ}),
                         result=f'wrote app.py ({len(APP_PY_WITH_HEALTHZ.encode("utf-8"))} bytes)'),
                    Step(assistant='测试也补上了。',
                         call=('write_file', {'file_path': 'tests/test_health.py',
                                              'content': 'from app import healthz\n\n\n'
                                                         'def test_healthz():\n'
                                                         '    assert healthz() == "ok"\n'}),
                         result='wrote tests/test_health.py (78 bytes)'),
                    Step(assistant='加好了，改动都还在工作区里，我没有提交。'),
                ),
            ),
            (
                'README 也顺手对齐一下，把新的端点写进去。',
                (
                    Step(assistant='更新 README。',
                         call=('edit', {'file_path': 'README.md',
                                        'old_string': 'Run: python -m app',
                                        'new_string': 'Run: python -m app\nHealth: GET /healthz'}),
                         result='edited README.md (+1 line)'),
                    Step(assistant='README 加上了 /healthz 的说明。'),
                ),
            ),
            (
                '跑一下测试确认没问题。',
                (
                    Step(assistant='跑测试。',
                         call=('bash', {'command': 'python -m pytest -v tests/'}),
                         result=_test_dump(('tests/test_health.py',))),
                    Step(assistant='测试通过。工作区里是 3 个文件的改动，都还没提交。'),
                ),
            ),
            (
                '好，我先看看这几处改动。',
                (
                    Step(assistant='行，我在这儿等你的结论。'),
                ),
            ),
        ),
    ),
    Item(
        id='correction-sed',
        archetype='correction',
        goal='现在把 requirements.txt 里 flask 的版本从 1.0.0 升到 2.0.0 吧。',
        judge='flask==2.0.0 且 python_requires 仍是 ">=1.0.0"（另记：有没有再动全局 sed）',
        # marker 必须**只存在于工具输出里**：工作区里的 requirements.txt 有
        # `python_requires=">=1.0.0"`（原文），被 sed 改坏后是 `>=2.0.0`——后者只出现在
        # 那段失败 diff 里，所以它既是"F 被埋在哪"的坐标，也保证"工作区里读不到"
        marker='+python_requires=">=2.0.0"',
        necessity=(
            '**不成立（已知）**：正确结果**完全可以从工作区当前内容推出来**——先读一眼 '
            'requirements.txt 就看见 `python_requires=">=1.0.0"`，于是只改 flask 那一行即可；'
            '历史里"上次全局 sed 改坏了"只是"更省事的理由"，不是必需信息。'
            '要它成立，得让"正确的第二处改动"只在历史里（见 NEXT_STEPS 的重做清单）。'),
        necessity_holds=False,
        scaffold=(
            # 工作区 = 回滚之后的状态（日志里 sed 改坏 → `git checkout --` 回滚）
            ('requirements.txt', REQUIREMENTS_TXT),
            ('app.py', APP_PY),
            ('tests/test_requirements.py', TEST_REQUIREMENTS),
        ),
        git=True,
        turns=(
            (
                f'{SPEC_CORRECTION}\n\n{PROJECT_CONVENTIONS}',
                (
                    Step(assistant='直接批量替换一下。',
                         call=('bash', {'command': 'sed -i "s/1.0.0/2.0.0/g" requirements.txt '
                                                  '&& git --no-pager diff'}),
                         result=_sed_diff_dump()),
                    Step(assistant='坏了：一次全局替换把 requests、urllib3、python_requires '
                                   '和注释里的 1.0.0 全改了。我回滚，下次只改 flask 那一行。',
                         call=('bash', {'command': 'git checkout -- requirements.txt'}),
                         result=''),
                ),
            ),
            (
                '对，回滚是对的。那这行到底该怎么改？',
                (
                    Step(assistant='只按行锚定地改：`sed -i "s/^flask==1\\.0\\.0$/flask==2.0.0/"`，'
                                   '或者直接用 edit 工具改那一行。',
                         call=('read_file', {'file_path': 'requirements.txt'}),
                         result=_numbered(REQUIREMENTS_TXT)),
                    Step(assistant='就是那一行。等你确认版本兼容性我再动。'),
                ),
            ),
            (
                '先跑一下测试看看现在什么状态。',
                (
                    Step(assistant='跑测试。',
                         call=('bash', {'command': 'python -m pytest -v tests/'}),
                         result=_test_dump(('tests/test_requirements.py',))),
                    Step(assistant='测试通过（现在的 flask 还是 1.0.0）。'),
                ),
            ),
            (
                '升级的事我再确认下兼容性。',
                (
                    Step(assistant='好，我不动 requirements.txt，等你确认。'),
                ),
            ),
        ),
    ),
    Item(
        id='avoid-repeat-pip',
        archetype='avoid_repeat',
        # 探针要**结果**（"把测试跑起来给我看"）：只说"想办法装上"时，"什么都不做"
        # 也能骗过"没有再试 pip"这条判据——测试题必须逼出行动，才谈得上"有没有走死路"
        goal='我这边要看测试结果——把 tests/ 跑起来，把结果发我；缺的东西补上。',
        judge='没有再执行 pip install（另记：有没有说清"为什么装不上"）',
        # 同上：包名在工作区（requirements.txt 注释里）和用户话里都有，所以 marker 取
        # **只有那段失败输出才有的内部索引地址**——它才是"这条路是死的"的证据
        marker='pypi.acme.internal',
        necessity=(
            '**不成立（已知）**：不查也能避开死路——实测 R2 在 `%TEMP%` 里打了个桩绕过去，'
            '判据（"没有再试 pip"）连"什么都不做"都能过。要它成立，探针必须**堵掉绕道**'
            '（"必须真的装上，打桩/跳过不算"），再让判据看"有没有说清为什么装不上"。'),
        necessity_holds=False,
        scaffold=(
            ('requirements.txt', AVOID_REPEAT_REQUIREMENTS),
            ('tests/test_utils.py', 'import acme_internal_utils\n\n\n'
                                    'def test_slug():\n'
                                    '    assert acme_internal_utils.slug("A B") == "a-b"\n'),
        ),
        turns=(
            (
                f'{SPEC_AVOID_REPEAT}\n\n{PROJECT_CONVENTIONS}',
                (
                    Step(assistant='装一下这个包。',
                         call=('bash', {'command': 'pip install acme-internal-utils'}),
                         result=_pip_fail_dump(), error=True),
                    Step(assistant='装不上：这个包里外都找不到——公网 PyPI 没有它，'
                                   '内部索引这里解析不了。这条路是死的，我先不重复试了。'),
                ),
            ),
            (
                '那算了。requirements.txt 里那行先注释掉，免得别人也去装。',
                (
                    Step(assistant='注释掉那一行。',
                         call=('edit', {'file_path': 'requirements.txt',
                                        'old_string': 'acme-internal-utils==0.3.1',
                                        'new_string': AVOID_REPEAT_REQUIREMENTS.splitlines()[2]}),
                         result='edited requirements.txt (+0 -1 lines)'),
                    Step(assistant='注释好了；那行写的是"看更早那次尝试"，'
                                   '免得下一个人又去装一遍。'),
                ),
            ),
            (
                '现在测试能跑到哪一步？',
                (
                    Step(assistant='跑一下看失败在哪。',
                         call=('bash', {'command': 'python -m pytest -v tests/'}),
                         result="ImportError while loading conftest\nE   ModuleNotFoundError: "
                                "No module named 'acme_internal_utils'", error=True),
                    Step(assistant='卡在 import：没有那个内部包，test_utils.py 收集就失败。'),
                ),
            ),
            (
                '行，这个先记着，我想想怎么绕。',
                (
                    Step(assistant='好，先不下手。'),
                ),
            ),
        ),
    ),
)

ITEMS_BY_ID = {item.id: item for item in ITEMS}


# ---------- 建日志（事件形状与 runtime/loop.py 落盘的一致） ----------

def build_base_session(item: Item, workspace: Path, fillers: int = 0,
                       session_id: str = '') -> Session:
    """把一条脚本写成**真实格式**的会话日志（内存态）。

    为什么直接 append 而不是真跑 agent：受控构造要的是**确定的题面**——F 埋在哪一行、
    前后有哪些干扰，必须每次一样（真跑 agent 会让题面随模型输出漂移，题与题之间不可比）。
    事件形状照抄循环的落盘顺序，所以下游（`derive_messages` / 压缩 / 召回）看到的
    与真实会话**没有区别**。
    """
    session = Session(id=session_id or item.id)
    session.append('session/workspace', {'workspace': str(workspace), 'source': 'eval-controlled'})
    session.append('session/title', {'title': f'[eval] {item.id}', 'source': 'eval-controlled'})

    script = list(item.turns)
    # 填充回合插在"最近一个回合"之前：keep_turns=1 只留最后一回合，其余（含填充）都进被压区间
    script = script[:-1] + [_filler_turn(index) for index in range(fillers)] + script[-1:]

    for number, (user_text, steps) in enumerate(script, start=1):
        session.append('turn/start', {'turn': number})
        session.append('step/start', {'turn': number, 'step': 1})
        session.append('user/message', create_user_message([TextBlock(text=user_text)]),
                       surface_op='append')
        step = 1
        for index, spec in enumerate(steps):
            if index:
                step += 1
                session.append('step/start', {'turn': number, 'step': step})
            blocks = []
            if spec.assistant:
                blocks.append(TextBlock(text=spec.assistant))
            call_id = ''
            if spec.call is not None:
                name, arguments = spec.call
                call_id = f'call-{number}-{step}'
                blocks.append(ToolCallBlock(id=call_id, name=name,
                                            arguments=json.dumps(arguments, ensure_ascii=False)))
            session.append('assistant/message', {
                'turn': number, 'step': step,
                'message': create_assistant_message(blocks, provider='deepseek', model=MODEL),
            }, surface_op='append')
            if spec.call is not None:
                name, arguments = spec.call
                session.append('tool/call', {
                    'turn': number, 'step': step, 'call_id': call_id, 'name': name,
                    'arguments': json.dumps(arguments, ensure_ascii=False),
                })
                session.append('tool/result',
                               create_tool_result_message(call_id, spec.result, spec.error),
                               surface_op='append')
            session.append('step/end', {'turn': number, 'step': step})
        session.append('turn/end', {'turn': number, 'reason': 'completed'})
    return session


def write_scaffold(item: Item, target: Path) -> None:
    """写工作区脚手架（每次 run 前由 runner 拷一份，保证起始状态一致）。"""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for relative, content in item.scaffold:
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')


# ---------- 三道标签 ----------

def check_labels(session: Session, marker: str, workspace: Path) -> dict:
    """校验 F 的三道标签（详见模块 docstring 里对"地位"的说明）。

    扫描用**事件的 JSON 文本**而不是 `message_text`：后者只取 TextBlock，
    而本实验的 F 全在**工具结果**里（`ToolResultBlock`）——用 `message_text` 会
    把"埋在工具输出里"的标记全判成不存在。marker 本身也过一遍 JSON 转义再比，
    否则带引号的标记（如 `python_requires=">=1.0.0"`）永远匹配不上。
    """
    shadowed = shadowed_seqs(session)
    escaped = json.dumps(marker, ensure_ascii=False)[1:-1]
    in_shadowed = False
    in_live = False
    for event in session.events:
        blob = json.dumps(event.data, ensure_ascii=False, default=str)
        if marker not in blob and escaped not in blob:
            continue
        if event.seq in shadowed:
            in_shadowed = True
        elif event.surface_op in ('append', 'replace'):
            in_live = True

    summary_text = ''
    for event in session.events:
        if event.type == 'compaction/summary':
            summary_text = (event.data or {}).get('summary', '') or ''
    in_summary = marker in summary_text

    in_workspace = []
    for path in sorted(workspace.rglob('*')):
        if path.is_file() and marker in path.read_text(encoding='utf-8', errors='ignore'):
            in_workspace.append(str(path.relative_to(workspace)))

    ok = in_shadowed and not in_live and not in_summary and not in_workspace
    # 不达标要说清**是哪一道**：四种原因对应的处置完全不同（没遮蔽 = 题没造对；
    # 在摘要里 = 对照题；在没被遮蔽的那份副本里 = F 有两份，压缩没伤到它；
    # 在工作区里 = F 可重读，不由召回负责）
    reasons = []
    if not in_shadowed:
        reasons.append('没被遮蔽')
    if in_live:
        reasons.append('未被遮蔽的副本仍在 surface')
    if in_summary:
        reasons.append('摘要把 F 保住了（对照题）')
    if in_workspace:
        reasons.append(f'工作区里可读到（{", ".join(in_workspace)}）')
    return {
        'shadowed': in_shadowed,
        'still_live': in_live,
        'in_summary': in_summary,
        'in_workspace': in_workspace,
        'ok': ok,
        'reason': '；'.join(reasons) or '三道标签全过',
        'summary_chars': len(summary_text),
    }


def item_kind(labels: dict) -> str:
    """把一个题的标签折成**一种**分类（两处打印共用，避免各写一套措辞）。

    三种状态要分清（混在一起会把设计好的对照题说成坏题）：
    - **真损失题**：三道标签全过 **∧** 作者声明必要性成立；
    - **对照题**：F 本来就拿得到（在摘要里 / 在 live 里）或必要性不成立——
      这是**有意保留**的题，用来说明"R2 并非必然失败"；
    - **无效**：题目本身不成立（没被遮蔽、F 在工作区里、压缩产物缺失）。
    """
    if not labels.get('compacted'):
        return '未构造完成（无压缩产物）'
    if labels.get('loss_item'):
        return '真损失题'
    if labels.get('ok') or labels.get('in_summary') or labels.get('still_live'):
        return f'对照题（{labels.get("reason", "摘要/live 里就有")}）'
    return f'无效（{labels.get("reason", "标签不成立")}）'


async def _compact(session: Session) -> bool:
    """真跑一次压缩（产品的四步事务 + 真摘要请求）；返回是否成功。

    摘要正文不用回传：它已经作为 `compaction/summary` 事件落在日志里，
    标签检查从日志读（**一处来源**，免得"内存里的摘要"和"日志里的摘要"漂移）。
    """
    llm, model = engine()
    return await run_compaction(session, llm, keep_turns=KEEP_TURNS, model=model or MODEL)


def write_log(path: Path, events) -> None:
    """把一串事件原样写成 JSONL（覆盖写）。

    用产品的 `save_event`（就是 `bind_store` 挂的那个 listener）而不是自己拼 JSON：
    序列化格式只有一处实现，写出来的文件与真实会话**逐字段一致**（`load_events` 能读、
    `adopt` 能重放出同样的投影）。
    """
    path.unlink(missing_ok=True)
    for event in events:
        save_event(path, event)


def build_item(item: Item, out_root: Path, *, compact: bool = True,
               ladder: tuple[int, ...] = (6, 8, 10, 12)) -> dict:
    """造一条题：base.jsonl → 真压缩 → 三道标签。

    **没有必要性声明的题不造**（当场抛）：只有"F 的载体丢了"是不够的——
    还要能论证"没有 F 就一定做错"，否则那道题的 Loss 按构造就是 0（实测三题如此）。

    `ladder` = 依次尝试的**填充回合数**，取第一个"压缩真的成功"的档位。为什么以
    "压缩成功"为准而不是以"F 掉了"为准：产品的压缩有"摘要必须比被压内容小"的硬门槛，
    而被压内容只算 **text 块**（工具结果不进摘要请求，见 `_filler_turn` 的说明），
    所以区间太薄时压缩**直接失败**——那是失败，不是"F 保住了"。
    F 保没保住只**记录**（`labels`）：保住了这题就是**对照题**（摘要里能答，R2 应当做对），
    没保住才是**真损失题**。两类都要跑，报告里分开报——这正是 §5.4 那条修正口径
    （"摘要里有 ≠ 不用查询"）要的行为证据。

    实测的档位选择依据（`value-port`）：散文 2.9k 字符 → 摘要 ~3.2k（**不过**门槛）；
    散文 4.2k → 摘要 3.7k（比值 0.87，**过**）。所以起点定在 6 个填充回合：
    比值本身随输入缓慢下降，但没必要从失败档位开始烧 token。
    """
    if not item.necessity.strip():
        raise ValueError(
            f'item {item.id!r} 缺必要性声明：三道标签只管"F 丢了"，不管"F 是不是必需品"。'
            '写清楚"没有 F 就一定做错"的机制，再用 --necessity 验证。')
    item_dir = out_root / item.id
    sessions_dir = item_dir / 'sessions'
    workspace = item_dir / 'workspace'
    item_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(exist_ok=True)
    write_scaffold(item, workspace)
    base_path = item_dir / 'base.jsonl'
    compacted_path = sessions_dir / f'{item.id}.jsonl'

    result: dict = {'id': item.id, 'archetype': item.archetype, 'goal': item.goal,
                    'judge': item.judge, 'marker': item.marker, 'git': item.git,
                    'known_issue': item.known_issue,
                    'necessity': item.necessity,
                    'necessity_holds': item.necessity_holds,
                    'ports': list(item.ports),
                    'workspace': str(workspace), 'sessions': str(sessions_dir),
                    'base_log': str(base_path), 'compacted_log': str(compacted_path),
                    'attempts': []}

    for fillers in ladder:
        # 每轮都从干净的目标文件开始：压缩产物是整份重写的（见下面的 write_log），
        # 但上一轮失败留下的残file还是先删掉，免得"这一轮失败了、文件却是旧的"
        compacted_path.unlink(missing_ok=True)
        base_session = build_base_session(item, workspace, fillers=fillers)
        write_log(base_path, base_session.events)

        if not compact:
            result['attempts'].append({'fillers': fillers, 'compacted': False})
            break

        # 压缩要在**另一份**日志上跑：base.jsonl 是"压缩前"的原文（R1 臂要用）。
        # **不要**在这份 session 上 bind_store：`bind_store` 只写"挂上之后"的事件，
        # 而重放进来的基础事件一个都不会落盘——写出来就是"只有压缩事务"的半截日志，
        # 回放时 `_surface.index(start_seq)` 直接炸（实测踩到）。所以：先在内存里压完，
        # 再把**完整事件序列**整份写出去（seq 必须从 0 连续，`derive_messages` 用 seq 当下标）。
        session = Session(id=item.id)
        for event in load_events(base_path):
            session.adopt(event)
        ok = asyncio.run(_compact(session))
        labels = check_labels(session, item.marker, workspace)
        labels.update({'fillers': fillers, 'compacted': ok})
        result['attempts'].append(labels)
        loss = labels['ok'] and item.necessity_holds
        kind = ('真损失题' if loss else
                f'对照题（{labels["reason"] if labels["ok"] else "必要性不成立"}）')
        if not ok:
            kind = '压缩失败'
        print(f'  [{item.id}] 填充回合={fillers} 压缩={"成功" if ok else "失败"} '
              f'遮蔽={labels["shadowed"]} 摘要有F={labels["in_summary"]} '
              f'摘要={labels["summary_chars"]}字 → {kind}', flush=True)
        if ok:
            write_log(compacted_path, session.events)
            # **真损失题 = 三道标签全过 ∧ 作者声明必要性成立**：少了后一项，
            # "标签全过"会被读成"这是道真损失题"——实测三题就是这么被误解的。
            labels['loss_item'] = bool(labels['ok'] and item.necessity_holds)
            labels['necessity_holds'] = item.necessity_holds
            labels['kind'] = item_kind(labels)
            result['labels'] = labels
            break
    return result


def relabel(item: dict) -> dict:
    """从**已落盘的压缩日志**重算三道标签（不花 token、不重跑压缩）。

    为什么要有这条：标签依赖的是 `marker` 这个"人挑的逐字串"，而挑串错了（比如挑到
    工作区里也有的串）不该罚一次重跑压缩——压缩产物没变，变的只是我们对它的描述。
    这也让"F 到底掉没掉"随时可复核：读日志即可，不必相信 items.json 里的旧结论。
    """
    session = Session(id=item['id'])
    compacted_path = Path(item['compacted_log'])
    # **压缩日志不存在 = 这道题没造完**（重建中途被打断、或压缩一直失败）。
    # 必须显式区分这种状态：`load_events` 对不存在的路径返回空表，于是标签会算出
    # "没被遮蔽"，读起来像"这是道对照题"——那是**假象**（§5.6 陷阱 11 的姊妹坑：
    # "不知道" 不许降级成"没有"）。
    if not compacted_path.exists():
        item['labels'] = {
            'ok': False, 'loss_item': False, 'compacted': False, 'shadowed': False,
            'still_live': False, 'in_summary': False, 'in_workspace': [],
            'reason': '压缩日志缺失（这道题没造完，不是对照题）',
            'summary_chars': 0, 'fillers': 0, 'relabeled': True,
        }
        return item
    for event in load_events(compacted_path):
        session.adopt(event)
    labels = check_labels(session, item['marker'], Path(item['workspace']))
    labels['fillers'] = (item.get('labels') or {}).get('fillers', 0)
    labels['compacted'] = any(event.type == 'compaction/end'
                              and not (event.data or {}).get('error')
                              for event in session.events)
    # 同 build_item：真损失题要求"标签全过 ∧ 必要性声明成立"
    holds = (item.get('labels') or {}).get('necessity_holds', item.get('necessity_holds', True))
    labels['necessity_holds'] = holds
    labels['loss_item'] = bool(labels['ok'] and holds)
    labels['kind'] = item_kind(labels)
    labels['relabeled'] = True
    item['labels'] = labels
    return item


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--only', default='', help='只造这一条（id）')
    parser.add_argument('--no-compact', action='store_true', help='只写 base.jsonl，不花 token')
    parser.add_argument('--relabel', action='store_true',
                        help='只从已落盘的压缩日志重算标签（不花 token）')
    args = parser.parse_args()

    if args.relabel:
        path = args.out / 'items.json'
        report = json.loads(path.read_text(encoding='utf-8'))
        for item in report:
            # marker / known_issue / 必要性 是脚本里的权威定义：以它为准
            spec = ITEMS_BY_ID[item['id']]
            item['marker'] = spec.marker
            item['known_issue'] = spec.known_issue
            item['necessity'] = spec.necessity
            item['necessity_holds'] = spec.necessity_holds
            item['ports'] = list(spec.ports)
            relabel(item)
            labels = item['labels']
            print(f'  [{item["id"]}] 遮蔽={labels["shadowed"]} '
                  f'摘要有F={labels["in_summary"]} '
                  f'工作区命中={labels["in_workspace"]} '
                  f'必要性声明={labels.get("necessity_holds")} → '
                  f'**{item_kind(labels)}**', flush=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        loss = [r for r in report if r['labels']['loss_item']]
        print(f'\n真损失题 {len(loss)}/{len(report)}；明细 {path}')
        return

    items = [ITEMS_BY_ID[args.only]] if args.only else list(ITEMS)
    args.out.mkdir(parents=True, exist_ok=True)
    summary_path = args.out / 'items.json'
    # `--only` 要**并入**已有的 items.json，不能整份覆盖：别的题这次不重造，但它们的
    # 元数据（goal/marker/archetype/日志路径）仍被 `run_endtoend.py` 与 `--report-only`
    # 需要——整份覆盖会把别的题已有的运行记录变成孤儿（跑了几十分钟的证据读不出来）。
    existing: list[dict] = []
    if args.only and summary_path.exists():
        existing = json.loads(summary_path.read_text(encoding='utf-8'))
    report = []
    for item in items:
        print(f'== {item.id}（{item.archetype}）', flush=True)
        report.append(build_item(item, args.out, compact=not args.no_compact))
    if existing:
        merged = {entry['id']: entry for entry in existing}
        for entry in report:
            merged[entry['id']] = entry
        report = list(merged.values())
    summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    loss = [r for r in report if r.get('labels', {}).get('loss_item')]
    control = [r for r in report if r.get('labels') and not r['labels'].get('loss_item')]
    failed = [r for r in report if not r.get('labels')]
    print(f'\n真损失题 {len(loss)} 条 / 对照题 {len(control)} 条 / '
          f'压缩失败 {len(failed)} 条；明细 {summary_path}')


if __name__ == '__main__':
    main()
