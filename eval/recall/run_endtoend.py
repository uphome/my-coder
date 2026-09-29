"""端到端验收 runner：三臂对照，量"压缩毁了多少 / 召回挽回多少"（§5.2 / §5.3）。

    R1 上限   完整原文（base.jsonl，压缩前）+ **不注册**召回工具
    R2 基线   只剩摘要（sessions/<id>.jsonl，压缩后）+ **不注册**召回工具
    R3 处理   摘要 + L0 状态栏 + `session_manifest` / `read_turn`

    Loss      = acc(R1) − acc(R2)      # 压缩毁了多少
    Recovered = acc(R3) − acc(R2)      # 召回挽回多少
    Residual  = acc(R1) − acc(R3)      # 还剩多少救不回来

**为什么必须端到端**（评审定的）：真实链路是五步——① 意识到"我这里缺了东西" → ② 把信息
需求表述出来 → ③ 用清单/工具定位 → ④ 读回原文 → ⑤ 正确用进回答/动作。把信息需求直接
交给模型（`run_selection.py`）只测第 ③ 步，**漏掉最容易失败的第 ① 步**（unknown-unknowns）。

每题的重复次数（`--reps`）至少 2：端到端方差大，单次数字不可信（§5.3 成本一节）。

判据**程序化**，不看模型自称：
- 约束型：`git rev-list --count HEAD` 仍为空（没产生提交）；
- 精确值型：写完的 config.json 里端口 == 8123（写别的数字 = 幻觉）；
- 纠正型：flask==2.0.0 **且** python_requires 那行没被动（另记"有没有再动全局 sed"）；
- 避免重复型：没有再执行 pip install（另记尝试次数）。
过程指标一并记：召回工具调用次数、模型请求数、token 用量、"做对了但没查"的条数。

用法：
    python eval/recall/run_endtoend.py --reps 2                 # 全跑
    python eval/recall/run_endtoend.py --items value-port --reps 1 --arms R2,R3
    python eval/recall/run_endtoend.py --report-only            # 只重算表（不花 token）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _path in (str(REPO), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from my_coder.app.factory import build_agent, load_env  # noqa: E402
from my_coder.capability.hooks import Hooks  # noqa: E402
from my_coder.state.recovery import repair_dangling_tool_calls  # noqa: E402
from my_coder.state.session import Session  # noqa: E402
from my_coder.values.persistence import load_events, save_event  # noqa: E402

MODEL = 'deepseek-v4-flash'
DEFAULT_OUT = REPO / '.eval' / 'recall'
ARMS = ('R1', 'R2', 'R3')
RECALL_TOOLS = ('session_manifest', 'read_turn')
RUN_TIMEOUT_S = 300.0


def write_log(path: Path, events) -> None:
    """整份写日志（覆盖）。

    **不能靠 `bind_store`**：它只写"挂上之后"的事件，而这里的基础事件是**重放**进来的
    （`adopt` 不触发 listener）——那样写出来的 log.jsonl 只有探针回合那几条，seq 还不从 0
    开始，既回放不了也当不了证据。所以跑完把完整事件序列整份落盘。
    """
    path.unlink(missing_ok=True)
    for event in events:
        save_event(path, event)


# ---------- 判据（每条都必须能只看日志/工作区就判出来） ----------

def _flagged(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def judge_value(item: dict, record: dict) -> dict:
    """精确值型：config.json 里的端口是不是 F（8123）。"""
    path = Path(record['workspace']) / 'config.json'
    text = path.read_text(encoding='utf-8') if path.exists() else ''
    ports = [int(number) for number in re.findall(r'"port"\s*:\s*(\d{2,5})', text)]
    success = '8123' in text
    # 幻觉 = 写了一个**具体但不是 F** 的值（凭空编一个端口）
    hallucinated = bool(ports) and not success
    return {
        'success': success,
        'hallucinated': hallucinated,
        'detail': f'config.json port={ports or "未写"}',
    }


def judge_constraint(item: dict, record: dict) -> dict:
    """约束型：有没有产生提交（工作区 git 是地面真相，工具痕迹是旁证）。

    **两个判据都要**，缺一不可：
    - 地面真相（`git rev-list --count HEAD`）抓"真的提交了"；
    - 工具痕迹抓"**试过**提交但失败了"（比如仓库没配 user.email，git 报错退出）——
      那也是违反约束（用户说的是"别提交"，不是"别提交成功"），只看地面真相会把它
      误判成通过。所以痕迹用**精确模式**（`git … commit`）而不是"参数里含 commit 字样"，
      否则 `git log --grep=commit` 这种无害命令会造成假阳性。
    """
    workspace = Path(record['workspace'])
    count = subprocess.run(['git', '-C', str(workspace), 'rev-list', '--count', 'HEAD'],
                           capture_output=True, text=True)
    head = (count.stdout or '').strip()
    committed = count.returncode == 0 and head not in ('', '0')
    tried = any(_tried_git_commit(call['arguments']) for call in record['tool_calls']
                if call['name'] == 'bash')
    success = not committed and not tried
    return {
        'success': success,
        'hallucinated': False,     # 违约与"说做了没做"分开记在 claimed 上
        'claimed_commit': _flagged(record['final_text'], ('已提交', '提交完成', 'committed to')),
        'git_head': head or '(空)',
        'detail': f'commit={committed} 试过提交={tried} (git HEAD={head or "空"})',
    }


def _tried_git_commit(arguments: str) -> bool:
    """参数里有没有一条**真的会提交**的 git 命令（`git -C x commit …` / `git commit …`）。"""
    return bool(re.search(r'\bgit\b[^\n;|&]*\bcommit\b', arguments))


def judge_correction(item: dict, record: dict) -> dict:
    """纠正型：flask 升上去了、python_requires 没被动；另记有没有再走失败路线。

    判据用**行级正则**而不是逐字串：模型把文件重排了行序、或给 `=` 两边加了空格，
    语义没错却会被逐字串判成失败——那测的是格式而不是行为。
    """
    path = Path(record['workspace']) / 'requirements.txt'
    text = path.read_text(encoding='utf-8') if path.exists() else ''
    upgraded = bool(re.search(r'^flask\s*==\s*2\.0\.0\s*$', text, re.M))
    intact = bool(re.search(r'python_requires\s*=\s*">=1\.0\.0"', text))
    sed_again = any(
        'sed' in call['arguments'] and 'requirements.txt' in call['arguments']
        for call in record['tool_calls'] if call['name'] == 'bash')
    success = upgraded and intact
    return {
        'success': success,
        'hallucinated': _flagged(record['final_text'], ('已升级', '升级完成', 'updated to 2.0.0'))
                         and not upgraded,
        'retried_failed_approach': sed_again,
        'detail': f'flask2={upgraded} python_requires 完整={intact} 再动 sed={sed_again}',
    }


def judge_avoid_repeat(item: dict, record: dict) -> dict:
    """避免重复型：有没有再执行那条已知走不通的路（pip install）。

    主判据是"没有再试"（设计口径 §5.3），外加一个**次要**指标 `explained`：
    有没有把"为什么装不上"说出来。次要也要记的原因：主判据单独看有个洞——
    "什么都不做"也能过。所以探针改成"把测试跑起来给我看"（逼出行动），
    再记下"有没有解释阻塞原因"；报告里两个一起看，但**不拿它单独判成功**
    （关键字是启发式，不该让它决定成败）。
    """
    installs = [call for call in record['tool_calls']
                if call['name'] == 'bash' and re.search(r'\bpip3?\s+install', call['arguments'])]
    no_retry = not installs
    explained = _flagged(record['final_text'], (
        '内部索引', '私有索引', 'internal index', 'unreachable', '解析不了', '连不上',
        '无法安装', '装不上', '装不了', 'not available', 'no matching distribution',
        '没有这个包', '内部包'))
    return {
        'success': no_retry,
        'hallucinated': _flagged(record['final_text'], ('安装成功', '装好了', 'installed successfully'))
                         and bool(installs),
        'no_retry': no_retry,
        'explained': explained,
        'pip_attempts': len(installs),
        'detail': f'pip install 次数={len(installs)} 解释了原因={explained}',
    }


JUDGES = {
    'value': judge_value,
    'constraint': judge_constraint,
    'correction': judge_correction,
    'avoid_repeat': judge_avoid_repeat,
}


# ---------- 跑一次 ----------

def rmtree(path: Path) -> None:
    """删目录（Windows 版）：git 对象/包文件是**只读**的，`shutil.rmtree` 会 WinError 5。

    所以删之前逐个改回可写。不这么做的话"重跑一次评测"会直接崩在清目录上
    （第一次跑没事，第二次必炸——实测踩到）。
    """
    if not path.exists():
        return

    def force(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=force)


def clear_stray_app_processes() -> list[str]:
    """清掉**本评测自己**起的长驻服务：`<本解释器> app.py`。

    为什么必须做（实测踩到）：模型会按 README 起 `python app.py` 来验证，`HTTPServer`
    是**常驻**的；它以为杀干净了，实际有时没杀成 → 下一个 run 一 `netstat` 就看到
    "监控要的端口被占了"，于是**正确地拒绝作答**（还在答复里问用户"那个 PID 能不能停"），
    而判据把这记成"失败"——**环境谎报，不是能力失败**（设计文档 §5.6 陷阱 16）。

    签名故意收得很紧，宁可漏杀不可误杀：**进程 exe == 本解释器** 且命令行里有 `app.py`。
    仓库根没有 `app.py`（脚手架里有 28 份），所以这个签名只可能是评测留下的。
    返回被杀的 PID 描述（打印出来当证据）。
    """
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*app.py*' -and "
        f"$_.ExecutablePath -eq '{sys.executable}' }} | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }")
    try:
        found = subprocess.run(['powershell', '-NoProfile', '-Command', script],
                               capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    killed = []
    for line in (found.stdout or '').splitlines():
        line = line.strip()
        if '|' not in line:
            continue
        pid, _, command = line.partition('|')
        result = subprocess.run(['taskkill', '/PID', pid.strip(), '/F'],
                                capture_output=True, text=True)
        killed.append(f'PID {pid.strip()} ({command.strip()[:60]}) '
                      f'→ {"已杀" if result.returncode == 0 else "杀失败"}')
    return killed


def assert_ports_free(item: dict) -> list[str]:
    """断言这道题依赖的端口**现在是空的**；占用就报出来（不静默）。

    为什么是"报"而不是"跑":端口被别人（或我的残留）占着时，模型会合理地拒绝写配置，
    那时候跑出来的记录是**环境污染**，不该混进实验证据里。
    """
    occupied = []
    for port in item.get('ports') or ():
        result = subprocess.run(['netstat', '-ano'], capture_output=True, text=True)
        for line in (result.stdout or '').splitlines():
            if f':{port} ' in line and 'LISTENING' in line.upper():
                occupied.append(f'{port} 被占用：{line.strip()}')
    return occupied


def _prepare_workspace(item: dict, run_dir: Path) -> Path:
    """每次 run 一份干净的工作区：从受控脚手架拷一份（起始状态必须每次一样）。

    **每个工作区都 `git init`**（不只是 `item['git']` 那几道题），两个理由：
    ① 工作区躺在仓库树里（`.eval/` 虽然 gitignore，但**仓库根仍是外层仓库**）——
       模型在里面跑 `git status` 会看到外层仓库的状态、跑 `git commit` 会提交
       **外层仓库的改动**（实测：R3 那次模型反复 `git status`/`git diff`，
       只是读还好，真提交就会污染我的工作树）。嵌套一个自己的仓库，
       git 命令就永远解析到内层，外层不可达；
    ② 模型按提示词纪律"用 `git status` 自证收尾"，有自己的仓库时这条才成立
       （否则它看到的是外层仓库的一堆无关改动，反而误导它）。
    顺带把 staging 做成"有改动、未提交"：`git add -A`（HEAD 还是空的）——
    这正是约束型那道题要的起始状态。
    """
    workspace = run_dir / 'ws'
    rmtree(workspace)
    shutil.copytree(item['workspace'], workspace)
    env = {'GIT_AUTHOR_NAME': 'eval', 'GIT_AUTHOR_EMAIL': 'eval@example.com',
           'GIT_COMMITTER_NAME': 'eval', 'GIT_COMMITTER_EMAIL': 'eval@example.com'}
    subprocess.run(['git', 'init', '-q'], cwd=workspace, check=True, env=env)
    # 身份写进**仓库本地配置**（不是只给 setup 命令用的环境变量）：模型真去提交时
    # 提交要能成功——"试过但失败"会走另一条判据（见 judge_constraint），
    # 而地面真相只有在提交真能落地时才有意义
    subprocess.run(['git', 'config', 'user.email', 'eval@example.com'], cwd=workspace, check=True)
    subprocess.run(['git', 'config', 'user.name', 'eval'], cwd=workspace, check=True)
    subprocess.run(['git', 'add', '-A'], cwd=workspace, check=True, env=env)
    return workspace


def _tool_calls(session: Session, since: int) -> list[dict]:
    """run 期间（seq >= since）模型发起的工具调用：名字 + 参数原文。"""
    calls = []
    for event in session.events_since(since):
        if event.type == 'tool/call':
            data = event.data if isinstance(event.data, dict) else {}
            calls.append({'name': data.get('name', ''),
                          'arguments': data.get('arguments', '') or ''})
    return calls


def _final_text(session: Session, since: int) -> str:
    """run 期间最后一条 assistant 可见文本（判定"它自称做了什么"用）。"""
    text = ''
    for event in session.events_since(since):
        if event.type != 'assistant/message':
            continue
        message = (event.data or {}).get('message')
        for block in getattr(message, 'content', ()):
            if getattr(block, 'type', '') == 'text' and block.text.strip():
                text = block.text
    return text


def _usage(session: Session, since: int) -> dict:
    prompt = completion = 0
    for event in session.events_since(since):
        if event.type != 'assistant/message':
            continue
        usage = (event.data or {}).get('usage')
        if isinstance(usage, dict):
            prompt += int(usage.get('prompt_tokens') or 0)
            completion += int(usage.get('completion_tokens') or 0)
    return {'input_tokens': prompt, 'output_tokens': completion}


async def run_once(item: dict, arm: str, rep: int, out_root: Path,
                   timeout_s: float = RUN_TIMEOUT_S, guidance: bool = True,
                   tag: str = '') -> dict:
    """跑一条：R1 用压缩前的原文，R2/R3 用压缩后的日志；R3 注册召回工具。

    `guidance` = **提示词供给面**（通用纪律的 Continuity 条 + `tool:recall` 段 + L0 的
    压缩提示，见 `app/factory.py`）。它必须能独立开关，才能把"没教它回查"与"它不需要查"
    分开——实测无提示情境里 10 个 run 一次都没查（设计文档 §5.7.2）。

    `tag` 进 run 目录名：同题可以在不同配置下跑多轮而**互不覆盖**（2×2 实验要并存）。
    """
    name = f'{arm}-r{rep}' + (f'-{tag}' if tag else '')
    run_dir = out_root / item['id'] / 'runs' / name
    # 跑前隔离：先清自己起的长驻服务，再断言这道题依赖的端口空闲（不清就等着被"环境谎报"骗）
    for line in clear_stray_app_processes():
        print(f'   [隔离] {line}', flush=True)
    occupied = assert_ports_free(item)
    if occupied:
        print('   [隔离] ⚠️ 端口被占，这一轮的结果会被污染：' + '；'.join(occupied), flush=True)
    rmtree(run_dir)
    run_dir.mkdir(parents=True)
    workspace = _prepare_workspace(item, run_dir)

    source = Path(item['base_log'] if arm == 'R1' else item['compacted_log'])
    session = Session(id=item['id'])
    for event in load_events(source):
        session.adopt(event)
    repair_dangling_tool_calls(session)

    args = SimpleNamespace(workspace=workspace, sessions=Path(item['sessions']),
                           model=MODEL, fake=False, hide_reasoning=True, compact_at=0)
    ui_state = {'reasoning_started': False, 'request_no': 0, 'tool_no': 0}

    async def approve(name: str, arguments: dict) -> bool:
        """自动放行：评测里没有人工可问。放行是**记录在案**的（工具调用进日志）。"""
        return True

    # `Hooks` 是普通类（类属性 + 无 `__init__`），只能构造后赋值挂载
    hooks = Hooks()
    hooks.approval = approve
    agent = build_agent(session, args, ui_state, hooks=hooks,
                        recall=(arm == 'R3'), recall_guidance=guidance)
    since = session.event_count
    timeout = False
    console = run_dir / 'console.txt'
    with console.open('w', encoding='utf-8') as handle, redirect_stdout(handle):
        agent.followup(item['goal'])
        try:
            await asyncio.wait_for(agent.when_idle(), timeout=timeout_s)
        except TimeoutError:
            timeout = True
            agent.cancel()
            await agent.when_idle()

    calls = _tool_calls(session, since)
    requests = sum(1 for event in session.events_since(since) if event.type == 'request/header')
    record = {
        'item': item['id'], 'archetype': item['archetype'], 'arm': arm, 'rep': rep,
        'guidance': guidance, 'tag': tag,
        'goal': item['goal'], 'judge': item['judge'],
        'workspace': str(workspace), 'log': str(run_dir / 'log.jsonl'),
        'timeout': timeout, 'model_requests': requests, 'tool_calls': calls,
        'recall_calls': [call for call in calls if call['name'] in RECALL_TOOLS],
        'final_text': _final_text(session, since), 'usage': _usage(session, since),
    }
    record.update(JUDGES[item['archetype']](item, record))
    # 完整日志（含被重放的基础事件 + 本回合）当证据：没有它，"判据为什么成立"无法复核
    write_log(run_dir / 'log.jsonl', session.events)
    (run_dir / 'record.json').write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    return record


# ---------- 汇总 ----------

def collect(out_root: Path, items: list[dict]) -> list[dict]:
    records = []
    for item in items:
        for run_dir in sorted((out_root / item['id'] / 'runs').glob('*/record.json')):
            records.append(json.loads(run_dir.read_text(encoding='utf-8')))
    return records


def _rate(records: list[dict], key: str = 'success') -> str:
    if not records:
        return '—'
    return f'{sum(1 for r in records if r.get(key)) / len(records):.0%}'


def _pct(value: float | None) -> str:
    """缺臂（没跑）不能当 0% 报——那会把"没测"说成"全错"。"""
    return '—' if value is None else f'{value:.0%}'


def report(records: list[dict], items: list[dict]) -> str:
    lines: list[str] = []
    add = lines.append
    add('| 题 | 类型 | R1 上限 | R2 基线 | R3 处理 | R3 召回调用 | R3 请求数 | R3 输入 token |')
    add('|---|---|---|---|---|---|---|---|')
    seen: list[str] = []
    for item in items:
        rows = {arm: [r for r in records if r['item'] == item['id'] and r['arm'] == arm]
                for arm in ARMS}
        if not any(rows.values()):
            continue
        seen.append(item['id'])
        r3 = rows['R3']
        recall_total = sum(len(r.get('recall_calls', [])) for r in r3)
        req = sum(r.get('model_requests', 0) for r in r3) / len(r3) if r3 else 0
        tokens = sum(r.get('usage', {}).get('input_tokens', 0) for r in r3) / len(r3) if r3 else 0
        add(f'| `{item["id"]}` | {item["archetype"]} | {_rate(rows["R1"])} | '
            f'{_rate(rows["R2"])} | {_rate(rows["R3"])} | {recall_total} 次 | '
            f'{req:.1f} | {tokens:,.0f} |')

    def arm_rate(arm: str, subset: list[dict]) -> float | None:
        rows = [r for r in records if r['arm'] == arm and r['item'] in subset]
        return sum(1 for r in rows if r['success']) / len(rows) if rows else None

    everything = seen
    acc = {arm: arm_rate(arm, everything) for arm in ARMS}
    add('')
    add('### 机器汇总（**只用来定位，不作结论**）')
    add('')
    add('> 这一节是给机器看的：它把结果压成百分比，而百分比会把要紧的差别抹平'
        '（"编一个像真的假值"与"明确说查不到"都算 0）。**结论看 `DOSSIER.md`。**')
    add('')
    add('| 臂 | 成功率 | 说明 |')
    add('|---|---|---|')
    add(f'| R1 上限（完整原文，无召回工具） | {_pct(acc["R1"])} | 这些任务本来能不能做对 |')
    add(f'| R2 基线（只剩摘要，无召回工具） | {_pct(acc["R2"])} | 当前系统的真实水平 |')
    add(f'| R3 处理（摘要 + L0 + 召回工具） | {_pct(acc["R3"])} | 召回救回多少 |')
    add('')
    if None not in acc.values():
        add(f'- acc(R1) − acc(R2) = {acc["R1"] - acc["R2"]:+.0%}')
        add(f'- acc(R3) − acc(R2) = {acc["R3"] - acc["R2"]:+.0%}')
        add(f'- acc(R1) − acc(R3) = {acc["R1"] - acc["R3"]:+.0%}')
        add('')
        add('> ⚠️ **不要读成 Loss / Recovered / Residual**：这三个量需要**查询分布 Q**'
            '（§5.1），而这里是几个手挑的点；上面的差值只能读成'
            '"**工具可用 + 模型愿意用（+ 题面提示）**带来的成功率差"。'
            '另外 R1 若不在天花板，差值连这个都读不出来。')
    add('')
    add('### 分类与过程指标')
    add('')
    add('| 类型 | n | R2 | R3 | 幻觉/违约（R2 / R3） | 备注 |')
    add('|---|---|---|---|---|---|')
    archetypes = sorted({item['archetype'] for item in items if item['id'] in seen})
    for archetype in archetypes:
        subset = [item['id'] for item in items if item['archetype'] == archetype
                  and item['id'] in seen]
        r2 = [r for r in records if r['arm'] == 'R2' and r['item'] in subset]
        r3 = [r for r in records if r['arm'] == 'R3' and r['item'] in subset]

        def bad(rows: list[dict]) -> int:
            """"错得很自信"与"重蹈覆辙"的合计（幻觉 / 自称提交 / 又走失败路线 / 又试装包）。"""
            return sum(1 for r in rows if r.get('hallucinated') or r.get('claimed_commit')
                       or r.get('retried_failed_approach') or r.get('pip_attempts'))

        note = ''
        if archetype == 'correction':
            note = ('再动全局 sed：R2 {} / R3 {}'.format(
                sum(1 for r in r2 if r.get('retried_failed_approach')),
                sum(1 for r in r3 if r.get('retried_failed_approach'))))
        if archetype == 'avoid_repeat':
            note = ('又试 pip install：R2 {} / R3 {}；说清为什么装不上：R2 {} / R3 {}'.format(
                sum(1 for r in r2 if r.get('pip_attempts')),
                sum(1 for r in r3 if r.get('pip_attempts')),
                sum(1 for r in r2 if r.get('explained')),
                sum(1 for r in r3 if r.get('explained'))))
        add(f'| {archetype} | {len(r2)} | {_rate(r2)} | {_rate(r3)} | '
            f'{bad(r2)} / {bad(r3)} | {note} |')
    add('')
    add('> 过程指标：**"做对 ≠ 查了"**。R3 里召回调用为 0 而成功的题，是"摘要里本来就有"'
        '或"从工作区推出来的"，不算召回的功劳——上面那列 `R3 召回调用` 就是为此留的。')
    return '\n'.join(lines)


def rejudge(out_root: Path, items: list[dict]) -> int:
    """用**当前**判据重判已有 run 记录（不重跑 agent、不花 token）。

    为什么需要：判据是代码，会随复核改（收紧假阳性、放宽格式误判）。而跑一轮端到端
    要花几十分钟与几十万 token——不能因为"判据写歪了"就作废证据。record.json 里存的
    是**原始证据**（工具调用、最终文本、工作区路径、日志），判据随时可以重放；
    所以判据与证据分开：跑一次落证据，判据随便迭代。
    """
    by_id = {item['id']: item for item in items}
    count = 0
    for path in sorted(out_root.glob('*/runs/*/record.json')):
        record = json.loads(path.read_text(encoding='utf-8'))
        item = by_id.get(record['item'])
        if item is None:
            continue
        for key in ('success', 'hallucinated', 'detail', 'claimed_commit',
                    'retried_failed_approach', 'pip_attempts', 'git_head'):
            record.pop(key, None)
        record.update(JUDGES[item['archetype']](item, record))
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        count += 1
    return count


def classify(record: dict) -> str:
    """失败模式归类（**预注册**：先写死这些类别，再看结果属于哪一类）。

    为什么要分类而不是只报成功/失败：同是"没做对"，**"编一个像真的假值"**与
    **"明确说我查不到"**在后果上差一个数量级；把它们都算 0 是成功率表最大的信息损失。
    """
    recalled = bool(record.get('recall_calls'))
    if record['success']:
        return '做对且查了' if recalled else '做对但没查'
    if record.get('hallucinated'):
        return '未查而编（很自信地给了假值）'
    if recalled:
        return '查了但没用对'
    text = record.get('final_text', '')
    if _flagged(text, ('查不到', '没有依据', '不确定', '不敢', '无法确认', '未写',
                       '没有记录', '定不下来', '找不到', '我不写')):
        return '未查而说明缺席（诚实但不完成任务）'
    return '未查而失败（没说清原因）'


def _tool_trace(record: dict, limit: int = 8) -> str:
    """行为轨迹：工具序列（名字 + 参数摘要），超出的折叠成 +N。"""
    calls = record.get('tool_calls', [])
    shown = []
    for call in calls[:limit]:
        args = ' '.join(call['arguments'].split())[:70]
        shown.append(f'`{call["name"]}({args})`')
    if len(calls) > limit:
        shown.append(f'…（另有 {len(calls) - limit} 次）')
    return ' → '.join(shown) or '(没有工具调用)'


def _one_line(text: str, limit: int = 260) -> str:
    flat = ' '.join((text or '').split())
    return flat[:limit] + ('…' if len(flat) > limit else '')


def dossier(out_root: Path, items: list[dict]) -> str:
    """情境档案：**主判据**（§5.3）。每条结论都挂着可复核的原始证据。"""
    records = collect(out_root, items)
    lines: list[str] = []
    add = lines.append
    add('# 情境档案：压缩之后，模型能不能把丢掉的东西找回来')
    add('')
    add('> 自动生成（`run_endtoend.py --report-only`）。**这是主判据**——不是成功率表：'
        '每条情境给出题面、F 在哪、各臂的**行为轨迹**与失败模式归类。'
        '原始证据：`<题>/runs/<臂>-r<次>/{log.jsonl,console.txt,record.json}`（可重放）。')
    add('')
    add('判据是程序化的、看结果不看自称（`JUDGES`）；类别是**预注册**的：做对且查了 / '
        '做对但没查 / 未查而编 / 未查而说明缺席 / 未查而失败 / 查了但没用对。')
    add('')
    for item in items:
        rows = [r for r in records if r['item'] == item['id']]
        labels = item.get('labels', {})
        # 分类由构造器统一算好（`labels['kind']`）：真损失题 / 对照题 / 无效 / 未构造完成。
        # 别在这里再写一套判断——两处措辞一旦分叉，"设计好的对照题"就会被读成坏题。
        kind = labels.get('kind') or ('对照题' if labels.get('loss_item') is False else '—')
        add(f'## `{item["id"]}`（{item["archetype"]}，{kind}）')
        add('')
        if item.get('necessity'):
            add(f'- **必要性声明**（作者的论证；`--necessity` 会拿 R2 上下文验证它）：'
                f'{item["necessity"]}')
            check = item.get('necessity_check')
            if check is None:
                add('- **必要性预检**：还没跑（`run_endtoend.py --necessity`）')
            elif check['success']:
                add(f'- **必要性预检结果：✗ 不通过**（不查也能做对 → **这题无效**；'
                    f'`{check["detail"]}`）')
            else:
                add(f'- **必要性预检结果：✓ 通过**（不查就做不对；`{check["detail"]}`）')
            add('')
        if item.get('known_issue'):
            add(f'> ⚠️ **已知构造缺陷（读这条时要带上）**：{item["known_issue"]}')
            add('')
        if not rows:
            # 没有 run 记录也要出现：**静默跳过**会让读者以为"这题不存在"，
            # 而实际是"没跑/没造完"——缺失必须显式可见（文档不许留过期声明）
            add(f'> ⚠️ **这道题没有 run 记录**：要么还没跑，要么构造中途作废'
                f'（当前标签：{labels.get("reason", "无")}）。**别把它读成"没有损失"。**')
            add('')
            continue
        add(f'- **探针**（压缩后新加的一轮用户输入）：{item["goal"]}')
        add(f'- **F（要找回的东西）**：`{item["marker"]}`；判据：{item["judge"]}')
        add(f'- **F 埋在哪**：{labels.get("reason", "—")}'
            f'；被遮蔽={labels.get("shadowed")} 在摘要里={labels.get("in_summary")}'
            f' 在工作区里={bool(labels.get("in_workspace"))}')
        add('')
        add('| 臂 | 次 | 供给面 | 判据 | 归类 | 召回调用 | 请求 | 输入 token |')
        add('|---|---:|---|---|---|---|---:|---:|')
        for arm in ARMS:
            for record in sorted((r for r in rows if r['arm'] == arm), key=lambda r: r['rep']):
                recall = ', '.join(f'`{c["name"]}({c["arguments"][:40]})`'
                                   for c in record.get('recall_calls', [])) or '—'
                usage = record.get('usage', {}).get('input_tokens', 0)
                # 供给面（提示词有没有教它回查）是实验的第二个因子：老记录没这个字段，
                # 标成"旧（无）"——那时**确实没有**任何提示（这就是 §5.7.2 的 0/10）
                guidance = record.get('guidance')
                face = '—（旧：无）' if guidance is None else ('有' if guidance else '无')
                add(f'| {arm} | r{record["rep"]} | {face} | {"✓" if record["success"] else "✗"} '
                    f'{record.get("detail", "")} | {classify(record)} | {recall} | '
                    f'{record.get("model_requests", 0)} | {usage:,} |')
        add('')
        for arm in ARMS:
            subset = sorted((r for r in rows if r['arm'] == arm), key=lambda r: r['rep'])
            for record in subset:
                face = ('（旧：无供给面）' if record.get('guidance') is None
                        else ('（供给面：有）' if record['guidance'] else '（供给面：无）'))
                add(f'**{arm} r{record["rep"]}**{face}（{classify(record)}）')
                add('')
                add(f'- 轨迹：{_tool_trace(record)}')
                add(f'- 最终答复：{_one_line(record.get("final_text", ""))}')
                add('')
    return '\n'.join(lines)


def precheck_necessity(out_root: Path, items: list[dict], tag: str = 'necessity') -> int:
    """**必要性预检**：拿 R2（只剩摘要、无召回工具）跑一次探针。

    判据：**通过 = F 非必要 = 这题无效**（不读历史也能做对，那它测不出召回的价值）。
    为什么造完题就要跑：三道标签只管"F 的载体丢了"，**不管 F 是不是必需品**——
    实测三题里 R2 不查也能做对（摘要保住 / 读工作区即可 / 有绕道），
    于是它们的 Loss 按构造就是 0，白跑一轮才看出来（见设计文档 §5.6 陷阱 12）。

    结果写回 `items.json` 的 `necessity_check`，档案与机器汇总都会据此标注。
    成本：每题 1 个 run（R2 臂最便宜的那一档）。
    """
    load_env(REPO / '.env')
    checked = 0
    for item in items:
        if not item.get('labels', {}).get('compacted'):
            continue
        print(f'== 必要性预检 {item["id"]}：{item["goal"]}', flush=True)
        record = asyncio.run(run_once(item, 'R2', 1, out_root, guidance=True, tag=tag))
        item['necessity_check'] = {
            'success': record['success'],          # True = F 非必要 = 题无效
            'detail': record.get('detail', ''),
            'requests': record.get('model_requests', 0),
            'log': record.get('log', ''),
        }
        verdict = ('✗ F **非必要**（不查也能做对）→ 这题无效，需重做' if record['success']
                   else '✓ F 必要（不查就做不对）→ 题成立')
        print(f'   {verdict}｜{record.get("detail", "")}', flush=True)
        checked += 1
    return checked


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--items', default='', help='逗号分隔的 id（默认全部）')
    parser.add_argument('--arms', default=','.join(ARMS))
    parser.add_argument('--reps', type=int, default=2)
    parser.add_argument('--timeout', type=float, default=RUN_TIMEOUT_S)
    parser.add_argument('--report-only', action='store_true')
    parser.add_argument('--rejudge', action='store_true',
                        help='只按当前判据重判已有记录（不花 token）')
    parser.add_argument('--necessity', action='store_true',
                        help='必要性预检：每题用 R2 上下文跑一次；通过 = F 非必要 = 这题无效')
    parser.add_argument('--guidance', choices=('on', 'off'), default='on',
                        help='提示词供给面（Continuity 纪律 + tool:recall 段 + L0 压缩提示）；'
                             'off = 只装工具不教它回查（2×2 实验的第二个因子）')
    parser.add_argument('--tag', default='',
                        help='给这轮 run 打标记（进 run 目录名，便于同题多配置并存）')
    args = parser.parse_args()

    items_path = args.out / 'items.json'
    if not items_path.exists():
        raise SystemExit(f'缺 {items_path}：先跑 make_controlled_session.py')
    all_items = json.loads(items_path.read_text(encoding='utf-8'))
    ready = [item for item in all_items if item.get('labels', {}).get('compacted')]
    missing = [item['id'] for item in all_items if item not in ready]
    if missing:
        print(f'跳过没有压缩产物的题：{missing}')
    if args.items:
        wanted = {name.strip() for name in args.items.split(',') if name.strip()}
        ready = [item for item in ready if item['id'] in wanted]
    arms = [arm.strip() for arm in args.arms.split(',') if arm.strip()]

    if args.necessity:
        changed = precheck_necessity(args.out, ready)
        items_path.write_text(json.dumps(all_items, ensure_ascii=False, indent=2),
                              encoding='utf-8')
        print(f'\n预检了 {changed} 道题；结果写回 {items_path}')

    if args.rejudge:
        changed = rejudge(args.out, ready)
        print(f'按当前判据重判了 {changed} 条记录')

    if not args.report_only and not args.rejudge and not args.necessity:
        load_env(REPO / '.env')
        for item in ready:
            loss = '真损失题' if item.get('labels', {}).get('loss_item') else '对照题'
            print(f'== {item["id"]}（{item["archetype"]}，{loss}）：{item["goal"]}', flush=True)
            for arm in arms:
                for rep in range(1, args.reps + 1):
                    record = asyncio.run(run_once(item, arm, rep, args.out, args.timeout,
                                                  guidance=(args.guidance == 'on'),
                                                  tag=args.tag))
                    mark = '✓' if record['success'] else '✗'
                    print(f'   {mark} {arm} r{rep}: {record["detail"]} | '
                          f'请求 {record["model_requests"]} 次，'
                          f'召回 {len(record["recall_calls"])} 次，'
                          f'工具 {len(record["tool_calls"])} 次'
                          + ('  [超时]' if record['timeout'] else ''), flush=True)

    records = collect(args.out, ready)
    if not records:
        raise SystemExit('没有 run 记录')
    (args.out / 'DOSSIER.md').write_text(dossier(args.out, all_items) + '\n', encoding='utf-8')
    table = report(records, ready)
    (args.out / 'RESULTS.md').write_text(
        '# 端到端验收（自动生成，`run_endtoend.py`）\n\n'
        '> ⚠️ 这是**机器汇总**，不是主判据——结论看 `DOSSIER.md`。\n\n' + table + '\n',
        encoding='utf-8')
    print('\n' + table)


if __name__ == '__main__':
    main()
