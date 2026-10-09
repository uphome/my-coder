"""题库冻结门禁（P1-4）：`eval/recall/*.jsonl` 是**历史基线快照**，被手改必须响。

为什么需要它：那两份题库的数字（36 题 / 13 题）到处被引用，而
`navigation.jsonl` 的重建要花 API（`build_navigation.py` 调 LLM 造题）。
在加这道门禁之前，**改一行、删两条、重排顺序都不会被任何自动检查发现**——
挂在墙上的评测结论会静默对不上，而且没人知道。

设计上刻意分清楚两件容易混的事：

1. **本文件冻结的是"当时那份"，不是"应该长什么样"。** 冻结值从 **git 的 HEAD 内容**
   算出（`git show HEAD:<path>`），不是从工作区读——否则"改坏工作区再改 freeze.json"
   就能自证通过。
2. **哈希前先做换行归一**（`\\r\\n` → `\\n`）。Windows 检出是 CRLF、git 里存 LF
   （本仓库 `core.autocrlf=true`），直接哈希工作区字节会和 HEAD 对不上——那是检出层
   的噪声，不是内容改动。归一后两种形态同哈希，而真实改动一定变哈希。

**这道门禁不保证可复现**，只保证没被手改：题库是旧版 `render_turn` 产出的，产品渲染
其后改过（删静默截断、改按行分页、删 `max_events`），今天重跑生成器必然对不上。
所以另有一条测试**钉住"它已经漂移"这个事实**，免得有人把 freeze 误读成"能重跑出同一份"。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FREEZE = REPO / 'eval' / 'recall' / 'freeze.json'

# 反静默通过：题库被整体删除 / freeze.json 被删成空壳时，循环会空转变绿。
MIN_DATASETS = 2


def _normalized_sha256(path: Path) -> str:
    """按 freeze.json 记的口径算哈希：UTF-8 解码（通用换行）后重新编码再哈希。"""
    text = path.read_text(encoding='utf-8')
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _committed_sha256(rel: str) -> str:
    """同一口径，但取 **git 里的那一份**——冻结值必须锚在已提交的内容上。"""
    import subprocess

    blob = subprocess.run(
        ['git', 'show', f'HEAD:{rel}'],
        cwd=REPO, capture_output=True, check=True,
    ).stdout
    return hashlib.sha256(blob.decode('utf-8').encode('utf-8')).hexdigest()


def _freeze() -> dict:
    assert FREEZE.exists(), (
        f'{FREEZE.name} 不见了——题库冻结门禁的锚点不能无声消失'
        f'（它记的是"当时那份题库"的 sha256/行数/字段集）')
    return json.loads(FREEZE.read_text(encoding='utf-8'))


def test_freeze_manifest_covers_the_whole_question_bank():
    """反静默通过：冻结清单必须点名每一份题库，且条目数达标。"""
    data = _freeze()
    datasets = data['datasets']
    assert len(datasets) >= MIN_DATASETS, (
        f'只冻结了 {len(datasets)} 份题库（期望 ≥ {MIN_DATASETS}）——'
        '扫描面塌了会让下面的循环空转变绿')
    assert {d['path'] for d in datasets} == {
        'eval/recall/questions.jsonl',
        'eval/recall/navigation.jsonl',
    }


def test_question_bank_is_unchanged_since_it_was_frozen():
    """内容校验：工作区那份必须与提交的那份逐字节同哈希。

    红了说明题库被改了。**不要改这个测试来消红**——先判断这次改动是不是有意的：
    - 有意的（重建了题库）→ 同步更新 `freeze.json` 里的 sha256/行数/字段集，
      并在提交信息里写明"评测基线变更"，顺带检查引用题数的文档要不要跟着改；
    - 无意的（手滑、格式化、编辑器重排）→ 用 `git checkout` 把题库还原。
    """
    failures = []
    for item in _freeze()['datasets']:
        rel = item['path']
        worktree = _normalized_sha256(REPO / rel)
        if worktree != item['sha256']:
            failures.append(
                f'{rel}：工作区 sha256 {worktree[:16]}… ≠ 冻结值 {item["sha256"][:16]}…\n'
                f'    冻结于：{item["generator"]}（{item["how"]}）')
        committed = _committed_sha256(rel)
        if committed != item['sha256']:
            failures.append(
                f'{rel}：**提交的**那一份 sha256 {committed[:16]}… ≠ 冻结值 '
                f'{item["sha256"][:16]}…——说明改动已经被提交了，冻结清单过期')
    assert not failures, (
        '题库与冻结清单不符（题库是评测基线，改动会让历史结论静默失效）：\n  '
        + '\n  '.join(failures))


def test_freeze_records_shape_not_just_hash():
    """行数与字段集也要钉：哈希只能证"不同"，行数/字段才能说清"哪里不同"。"""
    failures = []
    for item in _freeze()['datasets']:
        rows = [json.loads(line) for line in
                (REPO / item['path']).read_text(encoding='utf-8').splitlines() if line.strip()]
        if len(rows) != item['lines']:
            failures.append(f'{item["path"]}：{len(rows)} 行 ≠ 冻结的 {item["lines"]} 行')
        fields = sorted({key for row in rows for key in row})
        if fields != item['fields']:
            failures.append(
                f'{item["path"]}：字段集变了\n     现在 {fields}\n     冻结 {item["fields"]}')
    assert not failures, '题库形状与冻结清单不符：\n  ' + '\n  '.join(failures)


def test_freeze_admits_the_question_bank_cannot_be_rebuilt_in_place():
    """把"已漂移"这个事实钉住——freeze 是"防手改"，**不是**"可复现"。

    题库由**旧版** `render_turn` 产出；产品渲染其后改过三次（加回合头、删单块静默
    截断改按行分页、删 `max_events`）。所以"重跑生成器得到同一份"是**假命题**。
    这条测试不检查代码行为，它防的是**误读**：后来人若打算靠重跑生成器来更新 freeze，
    会在这里读到为什么不行（以及要复现旧数字得回到产出它的提交）。
    """
    contract = _freeze()['render_contract']
    assert contract['status'] == 'drifted', (
        '渲染契约的状态被改成了 '
        f'{contract["status"]!r}——如果产品渲染真的回到了可原地复现，')
    assert contract['rebuildable_in_place'] is False, (
        '题库是旧版 render_turn 的产物，今天重跑生成器不会得到同一份；'
        '要复现旧数字得回到产出它的提交')


def test_bank_generators_are_still_in_the_repo():
    """生成器必须还在：freeze 记的 `generator` 路径指向不存在的脚本时，红。"""
    missing = [item['generator'] for item in _freeze()['datasets']
               if not (REPO / item['generator']).is_file()]
    assert not missing, (
        f'冻结清单指向的生成器不存在了：{missing}——'
        '要么补回脚本，要么更新 freeze.json 并说明重建路径')
