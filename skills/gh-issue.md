---
name: gh-issue
description: 处理 GitHub issue（读全文、独立验证、只分析不实现，除非用户明确要求实现）
---

# 处理 GitHub issue 的工作流

当用户让你处理某个 GitHub issue（提到 issue 编号、URL，或"处理 #N"）时，
按下面的步骤执行。全程用 `bash` 工具跑 `gh`（已登录）；issue 属于本仓库
`uphome/my-coder`。

## 1. 读 issue 全文（含评论与关联）

不要只看标题或 issue 正文的摘要——用 gh 拉结构化全文：

```sh
gh issue view <编号> -R uphome/my-coder --json title,body,comments,labels,state,url
```

若用户提到 PR 或 issue 间关联，也一并 `gh issue view` 读关联项。

## 2. 独立验证，不信任 issue 里写的分析

issue 里的"根因分析"或"修复建议"很可能是错的（提问者常常误判）。规则：

- **忽略 issue 里写的根因**，把它当待验证的假设
- 用 read_file / grep / glob 读相关代码**全文**（不截断）
- 沿执行路径追到真实根因，或判断 feature 的最简实现位置

## 3. 输出

- **bug**：真实根因（哪段代码、为什么）+ 修复方案（改哪个文件、怎么改）
- **feature 请求**：最简实现思路 + 受影响文件清单
- 每点结论给出来源文件:行号（模型可见 ⟺ 可重建：让用户能核对）

## 4. 默认不实现

默认只做分析与方案；**不要动代码、不要写文件**，除非用户明确说"修复它"/
"实现它"。用户要求实现时才改代码（edit/write_file 会走 approval 门）。

## 汇报

完成后在对话里简明汇报：读了什么、结论、建议的下一步。不要长篇复述 issue
原文。
