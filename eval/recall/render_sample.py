"""重新生成 `RENDERING_SAMPLE.md`：三层投影的真实渲染样本。

用**产品实现**（`my_coder/app/recall.py`）渲染，而不是临时探针——样本必须等于产品输出，
否则它描述的就不是模型真正会看到的东西。改完渲染逻辑（清单字段、截断规则）就跑一次这个。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from my_coder.app.recall import (build_turns, load_session_log,  # noqa: E402
                                 render_manifest, render_turn,
                                 session_index_text)

OUT = HERE / 'RENDERING_SAMPLE.md'
SESSIONS = REPO / '.sessions'
WORKSPACE = str(REPO)


def main() -> None:
    paths = sorted(SESSIONS.glob('*.jsonl'), key=lambda p: p.stat().st_size, reverse=True)
    current = load_session_log(paths[0])
    turns = build_turns(current)
    manifest = render_manifest(turns)
    # L0 走产品自己的入口（它负责"扫别的会话 + 当前会话走内存增量游标"这一套）
    index = session_index_text(current, SESSIONS, WORKSPACE)

    lines: list[str] = []
    add = lines.append
    add('# 三层投影渲染样本（真实日志，产品实现输出）')
    add('')
    add('> 生成：`conda run --no-capture-output -n agent-demo python eval/recall/render_sample.py`')
    add('> —— 用 `my_coder/app/recall.py`（产品实现）渲染，样本 = 模型真正会看到的东西。')
    add(f'> 数据源：仓库 `.sessions/`（当前最大会话 `{paths[0].name}`，'
        f'{paths[0].stat().st_size / 1e6:.1f} MB）。')
    add('')
    add('## L0 会话目录（产品里放状态栏；每会话一行，只列同一工作区）')
    add('')
    add('```')
    add(index or '(空)')
    add('```')
    add('')
    add(f'## L1 用户话清单 —— `{paths[0].name}`（{len(turns)} 回合，'
        f'共 {len(manifest.splitlines())} 行，前 20 行）')
    add('')
    add('```')
    lines.extend(manifest.splitlines()[:20])
    add(f'…（其余 {len(manifest.splitlines()) - 20} 行省略）')
    add('```')
    add('')
    add(f'**该清单 {len(manifest):,} 字符（≈{len(manifest) // 2:,} token）。**')
    add('')

    small = next((p for p in paths if p.stem == 'web'), None)
    if small is not None:
        small_turns = build_turns(load_session_log(small))
        small_manifest = render_manifest(small_turns)
        add(f'## L1 用户话清单 —— `{small.name}`（{len(small_turns)} 回合，全文）')
        add('')
        add('```')
        lines.extend(small_manifest.splitlines())
        add('```')
        add('')
        add(f'**该清单 {len(small_manifest):,} 字符（≈{len(small_manifest) // 2:,} token）。**')
        add('')

    target = max((t for t in turns if t.user_texts), key=lambda t: len(t.files) + len(t.tools))
    full = render_turn(current, target.turn)
    add(f'## L2 回合明细 —— `{paths[0].name}` · turn {target.turn}')
    add('')
    add(f'（足迹：{target.footprint()}）')
    add('')
    add('```')
    lines.extend(full.splitlines()[:8])
    add('…（截断展示；本回合完整渲染 {:,} 字符）'.format(len(full)))
    add('```')
    add('')
    add('截断规则：`max_events=80` / `max_chars=12000` 双上限，超限追加一句'
        '"…（本回合内容超过上限被截断；用 step 参数精读某一步）"。')
    add('')
    add('## 规模统计')
    add('')
    add('| 会话 | 回合（有用户话） | L1 清单 | L2 中位 | L2 最大 |')
    add('|---|---|---|---|---|')
    for path in paths:
        session = load_session_log(path)
        session_turns = build_turns(session)
        with_user = [t for t in session_turns if t.user_texts]
        if not with_user:
            continue
        sizes = sorted(len(render_turn(session, t.turn, max_events=999, max_chars=10 ** 9))
                       for t in with_user)
        text = render_manifest(session_turns)
        add(f'| `{path.name}` | {len(session_turns)}（{len(with_user)}） | '
            f'{len(text):,} 字符 | {sizes[len(sizes) // 2]:,} | {sizes[-1]:,} |')
    add('')
    add('## 评审时看到的五个问题（待定）')
    add('')
    add('1. **L1 行序**：现在「结论摘录 + 足迹」在前、用户原话在后（缩进）。要不要反过来？')
    add('2. **结论摘录长度**：现在 60 字符；缩到 40 能省约 1k 字符/会话。')
    add('3. **L2 里的工具结果**：现在原样回灌（一次 `read_file` 可能就是一整份文件，'
        '单回合最大 6 万字符）。建议按工具类型分级：`read_file`/`list_files`/`glob` '
        '只给指针（文件还在工作区、且是**当前版本**），`bash`/`grep` 这类不可重得的才回灌。')
    add('4. **重复用户话**：同一请求被反复发时清单里会出现几行近似（导航歧义）——'
        '要不要折叠成一行 + `×N`？')
    add('5. **`N step` 在旧日志里不准**（旧实现 step = 整段工具循环）。'
        '要不要改用"工具调用次数"当足迹（更稳、与 step 语义无关）？')

    OUT.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'已写入 {OUT}（{len(lines)} 行）')


if __name__ == '__main__':
    main()
