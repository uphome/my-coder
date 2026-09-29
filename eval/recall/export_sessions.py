"""把验收产物导出成**能用 Web UI 浏览的会话**（issue #3 端到端验收的配套工具）。

为什么需要这么一层：`.eval/recall/` 里的东西是**证据**，但它们散在各处、且全都用同一个
session id（`<item>`）：直接拷进 `.sessions/` 会有两个问题——① 标题全一样
（`[eval] value-port`），列表里分不清哪条是"压缩前原文"、哪条是"R3 第二次"；
② `session/workspace` 记的是**受控脚手架**目录，而那次 run 真正动手的是
`runs/<arm>-r<rep>/ws`——在 UI 里接着聊会打到错目录。

所以导出时只改两处（标题、工作区），**原件一个字不动**（它们是唯一证据）：

    .eval/recall/viewer/.sessions/
        <item>__base.jsonl        压缩前的完整原文（R1 臂的起点）
        <item>__compacted.jsonl   压缩后（checkpoint + 保留回合；R2/R3 的起点）
        <item>__R2-r1.jsonl       某一个臂某一次的真实探针回合

用法：
    python eval/recall/export_sessions.py
    cd .eval/recall/viewer && python -m my_coder.web --workspace <repo 根>
（Web 宿主只认 cwd 下的 `.sessions/`，没有 `--sessions` 参数，所以用 cwd 指定。）
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DEFAULT_OUT = REPO / '.eval' / 'recall'


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def _export(source: Path, target: Path, title: str, workspace: str | None) -> int:
    """拷一份日志并改写标题（/工作区），返回事件数；源不存在就跳过并说明。

    **源可能合法地不存在**：某道题正在重建（压缩还没落盘）时，它的
    `sessions/<id>.jsonl` 就是缺的——导出是"看证据"的入口，不该因为一道题
    没造完就整份失败（其余题的日志照样要看）。
    """
    if not source.exists():
        print(f'  [跳过] {source} 不存在（这道题可能正在重建）')
        return 0
    events = _load(source)
    for event in events:
        payload = event.get('data')
        if not isinstance(payload, dict) or '$dict' not in payload:
            continue
        inner = payload['$dict']
        if event['type'] == 'session/title':
            inner['title'] = title
            inner['source'] = 'eval-export'
        elif event['type'] == 'session/workspace' and workspace:
            inner['workspace'] = workspace
    target.write_text('\n'.join(json.dumps(event, ensure_ascii=False) for event in events) + '\n',
                      encoding='utf-8')
    return len(events)


def export(out_root: Path) -> Path:
    items_path = out_root / 'items.json'
    if not items_path.exists():
        raise SystemExit(f'缺 {items_path}：先跑 make_controlled_session.py')
    items = json.loads(items_path.read_text(encoding='utf-8'))
    viewer = out_root / 'viewer'
    sessions = viewer / '.sessions'
    if sessions.exists():
        shutil.rmtree(sessions)
    sessions.mkdir(parents=True)

    total = 0
    for item in items:
        name = item['id']
        loss = '真损失' if item.get('labels', {}).get('loss_item') else '对照'
        total += _export(Path(item['base_log']), sessions / f'{name}__base.jsonl',
                         f'[eval] {name} · 压缩前原文（{item["archetype"]}/{loss}）',
                         item.get('workspace'))
        total += _export(Path(item['compacted_log']), sessions / f'{name}__compacted.jsonl',
                         f'[eval] {name} · 压缩后（只剩 checkpoint）', item.get('workspace'))
        runs_dir = out_root / name / 'runs'
        for record_path in sorted(runs_dir.glob('*/record.json')):
            record = json.loads(record_path.read_text(encoding='utf-8'))
            arm, rep = record['arm'], record['rep']
            mark = '✓' if record['success'] else '✗'
            total += _export(record_path.parent / 'log.jsonl',
                             sessions / f'{name}__{arm}-r{rep}.jsonl',
                             f'[eval] {name} · {arm}-r{rep} {mark} {record.get("detail", "")}',
                             record.get('workspace'))
    (viewer / 'README.md').write_text(
        '# 验收会话的浏览目录（自动生成，`export_sessions.py`）\n\n'
        'Web 宿主只认 cwd 下的 `.sessions/`，所以在这里起服务：\n\n'
        '```sh\n'
        f'cd {viewer}\n'
        f'conda run --no-capture-output -n agent-demo python -m my_coder.web --workspace {REPO}\n'
        '```\n\n'
        '会话命名：`<题>__base`（压缩前原文）/ `<题>__compacted`（压缩后）/ '
        '`<题>__<臂>-r<次>`（真实探针回合，✓/✗ 是判据结果）。\n'
        '**原件在 `.eval/recall/<题>/` 下，这里只是拷贝**——在 UI 里接着聊不会污染证据。\n',
        encoding='utf-8')
    return sessions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    sessions = export(args.out)
    files = sorted(sessions.glob('*.jsonl'))
    print(f'导出 {len(files)} 个会话 → {sessions}')
    for path in files:
        print(f'  {path.name}')
    print(f'\n起服务：cd {sessions.parent} && conda run --no-capture-output -n agent-demo '
          f'python -m my_coder.web --workspace {REPO}')


if __name__ == '__main__':
    main()
