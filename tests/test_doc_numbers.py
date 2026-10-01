"""文档数字门禁：文档里声称的**测试数 / 行数 / 机制数**必须等于现场实测。

**为什么要有这个文件**：同一个数字在文档里有三个版本——`README.md` 与
`docs/architecture.md` 说 175 个测试，`NEXT_STEPS.md` 说 85，`docs/prior-art.md`
说 131，而实测是 203；`docs/architecture.md` 还自称"约 1000 行"，实际 `my_coder`
近 8000 行。数字自相矛盾是评审者最容易抓的破绽，而"记得改"从来不管用——
所以把最容易漂的几类声明钉死（同 `tests/test_docs.py`、`tests/test_architecture.py`
的思路：**规则写成测试才有约束力**）。

**扫描面**（只扫"描述现状"的层）：
- L0 入口：`README.md` / `USAGE_zh.md`
- L1 规范：`AGENTS.md`
- L1.5 台账：`NEXT_STEPS.md`
- L3 现状长文：`docs/architecture.md` / `docs/prior-art.md`

**不扫**：
- L2 `docs/notes/**`——那是"某一天成立的决定"记录，数字是当时快照，改它是篡改历史；
- `DSH_0.2_GAP_AND_PLAN.md`——它自己用醒目标注声明"本文数字全部过期，执行时现场重测"。

**两类声明**：
- `约 N` 前缀 → 允许 ±10%（数字本来就是近似）；
- 裸 `N` → 必须**精确相等**。

**历史叙述**（如验收实录里的 `31 passed`）不删也不改，登记进 `HISTORICAL`
并写明理由；`test_historical_exemptions_are_still_real` 保证豁免表不长僵尸条目
（对齐 `tests/test_architecture.py` 的 `KNOWN_VIOLATIONS` 纪律）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SCANNED_DOCS = (
    'README.md',
    'USAGE_zh.md',
    'AGENTS.md',
    'NEXT_STEPS.md',
    'docs/architecture.md',
    'docs/prior-art.md',
)

# 声明形态 → 实测项。`(?:约\s*)?` 决定是否允许近似。
CLAIM_PATTERNS = (
    (r'(?:约\s*)?(\d[\d,]*)\s*个测试(?!文件)', 'tests'),
    (r'(?:约\s*)?(\d[\d,]*)\s*passed', 'tests'),
    (r'(?:约\s*)?(\d[\d,]*)\s*个核心机制', 'mechanisms'),
    (r'(?:约\s*)?(\d[\d,]*)\s*行 Python', 'my_coder_loc'),
    (r'(?:约\s*)?(\d[\d,]*)\s*行零构建前端', 'web_loc'),
    (r'(?:约\s*)?(\d[\d,]*)\s*行测试', 'tests_loc'),
)

# 近似声明的容差（约 N → ±10%）。
APPROX_TOLERANCE = 0.10

# 历史叙述豁免：(文件, 行内片段) → 为什么它不该被算作"现状声明"。
# 每条都必须仍然真实存在（见 test_historical_exemptions_are_still_real）。
HISTORICAL = {
    ('NEXT_STEPS.md', '31 passed in 1.12s'):
        '阶段一验收实录——记录 2026-08 某一次真实运行的输出，不是当前测试数',
    ('docs/prior-art.md', 'pytest 131 passed, 3 skipped'):
        '对照章节里的门禁快照——记录当时的状态与"132 → 134"的来历',
}


def _count_loc(paths) -> int:
    return sum(len(p.read_text(encoding='utf-8').splitlines()) for p in paths)


@lru_cache(maxsize=1)
def measure() -> dict:
    """现场测量所有被校验的量。**不要**把这些数字写死进测试——它们就是被测对象。

    **必须惰性调用**（只能在测试函数体里）：`measure()` 会 spawn 一个
    `pytest --collect-only` 子进程来数测试条数，而收集阶段也会 import 本模块——
    若在模块级调用，内层 pytest 又导入本模块又 spawn，**无限递归**（实测：进程
    炸开、60s 不返回）。缓存 `maxsize=1` 保证一次跑测试只 spawn 一次。
    """
    package = [p for p in (REPO / 'my_coder').rglob('*.py') if '__pycache__' not in p.parts]
    tests = sorted((REPO / 'tests').glob('test_*.py'))
    architecture = (REPO / 'docs' / 'architecture.md').read_text(encoding='utf-8')
    collected = subprocess.run(
        [sys.executable, '-m', 'pytest', '--collect-only', '-q'],
        cwd=REPO, capture_output=True, text=True,
    )
    match = re.search(r'(\d+)\s+tests? collected', collected.stdout)
    assert match is not None, (
        '数不出测试条数，说明 pytest --collect-only 的输出格式变了'
        f'（stdout 末尾：{collected.stdout[-200:]!r}）')
    return {
        'tests': int(match.group(1)),
        'mechanisms': len(re.findall(r'^### 3\.\d+', architecture, re.M)),
        'my_coder_loc': _count_loc(package),
        'web_loc': _count_loc([REPO / 'web' / 'index.html']),
        'tests_loc': _count_loc(tests),
    }


def collect_claims() -> tuple[list[tuple[str, int, str, str, int, bool]], int]:
    """扫出所有数字声明。返回 (待校验声明列表, 命中豁免表的条数)。

    每条声明是 (文件, 行号, 原文行, 实测项, 声明值, 是否近似)。
    """
    claims: list[tuple[str, int, str, str, int, bool]] = []
    exempted = 0
    for name in SCANNED_DOCS:
        path = REPO / name
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if any(f == name and fragment in line for f, fragment in HISTORICAL):
                exempted += 1
                continue
            # 一行可以声称多个数字（如 README 开头同时说代码/前端/测试规模），
            # 所以这里不 break——每个模式各自匹配。
            for pattern, kind in CLAIM_PATTERNS:
                match = re.search(pattern, line)
                if match is None:
                    continue
                value = int(match.group(1).replace(',', ''))
                approximate = match.group(0).lstrip().startswith('约')
                claims.append((name, lineno, line.strip(), kind, value, approximate))
    return claims, exempted


def test_documents_do_not_disagree_with_the_code():
    """文档里每个数字声明都必须与实测一致（近似声明允许 ±10%）。"""
    claims, exempted = collect_claims()

    # 反静默通过：扫描面塌了（文件搬走 / 正则失效）会让下面的循环空转变绿。
    # 学 test_architecture.py 的 `assert scanned >= 30`——先证明自己真的扫到了东西。
    assert len(claims) + exempted >= 8, (
        f'只扫到 {len(claims)} 条声明 + {exempted} 条豁免，说明扫描面不对'
        f'（检查 SCANNED_DOCS：{SCANNED_DOCS}）——数字门禁不能无声消失')

    measured_values = measure()
    mismatches = []
    for name, lineno, line, kind, claimed, approximate in claims:
        actual = measured_values[kind]
        if approximate:
            if abs(claimed - actual) > actual * APPROX_TOLERANCE:
                mismatches.append(f'{name}:{lineno} 约 {claimed} ≠ 实测 {actual}（{kind}，容差 ±10%）\n    {line}')
        elif claimed != actual:
            mismatches.append(f'{name}:{lineno} {claimed} ≠ 实测 {actual}（{kind}）\n    {line}')
    assert not mismatches, (
        '文档数字与代码不符（改文档，不要改这个测试）：\n  '
        + '\n  '.join(mismatches)
        + '\n\n历史叙述请登记进 HISTORICAL 并写明理由。')


def test_historical_exemptions_are_still_real():
    """豁免表不许有僵尸条目：那段文字没了就该删掉豁免，否则它替不存在的声明开脱。"""
    for (name, fragment), reason in HISTORICAL.items():
        text = (REPO / name).read_text(encoding='utf-8')
        assert fragment in text, (
            f'{name} 里已经找不到 {fragment!r}——豁免条目作废，请从 HISTORICAL 删除'
            f'（原理由：{reason}）')


def test_the_test_count_promise_is_machine_checked():
    """`NEXT_STEPS.md` 不再承诺"人工保持同步"——那句承诺失效过（它写着 85，实测 203）。"""
    text = (REPO / 'NEXT_STEPS.md').read_text(encoding='utf-8')
    assert 'tests/test_doc_numbers.py' in text, (
        'NEXT_STEPS.md 应说明测试数由 tests/test_doc_numbers.py 机械保证，'
        '而不是靠人工同步（原文那句"AGENTS.md 里的数字保持同步"已经失效过）')
