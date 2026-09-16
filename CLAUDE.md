# 北斗 V5 — 给 Claude Code 的约定

## PR 流程

改完代码走 PR，不直推 main。

1. 从最新的 `origin/main` 开分支
2. 本地先过四道门（见下），全绿再推
3. 开 PR，等 CI
4. **CI 全绿后直接合并**：`gh pr merge <n> --merge`。用 merge commit 不用 squash，与既有历史一致。**不要停下来请求授权**——操作者 2026-09-16 明确要求不再逐个确认
5. 合完清理 worktree 和本地分支

只有两种情况先停下来说话，而且都是**报告发现**而不是请求授权：

- **CI 红了**——去修，不是去问。修完推同一个分支，CI 重跑，绿了照规则合
- **这个 PR 不该合**——内容已被 main 取代，或合并会造成倒退。把证据摆出来。2026-09-16 的 #15 与 #16 就是：两个都落后 main 数百个 commit，#16 合并会倒退 6,096 行

不用 GitHub auto-merge 的原因：本仓库是 Free plan 的 private repo，branch protection 与 rulesets 都返回 403，没有 required status checks，auto-merge 无从触发。桌面版那个开关只是同一功能的前端，同样开不起来。

## CI 监控开关：开完 PR 立刻打开

开 PR 之后**立即**调用 `mcp__ccd_pr__set_monitor`，把三个开关一次打开，不要等操作者去界面里勾：

- `auto_fix` 与 `address_comments`（两者必须相等）——CI 失败、合并冲突、review 评论会主动叫醒 session，不必轮询
- `auto_archive_on_close`——PR 合并或关闭后自动归档这个 session

这些开关**不随 session 继承，是逐个 PR 的**：2026-09-16 的 #19 上 `auto_fix` 开着，绑定 #22 时它回到 false。所以每开一个新 PR 就重新打开一次，这是开 PR 动作的一部分，不是一件单独要请示的事。

操作者 2026-09-16 明确要求：CI 的自动修复与评论、PR 的归档都要自动选择，不要让他手动去选。

## 本地预检：必须走 `.venv/bin/`

裸命令走的是 homebrew 全局工具链，与 `requirements.lock` 全部对不上（2026-09-16 实测）：

| | 裸命令 | `.venv/` |
|---|---|---|
| python | 3.9.6 | 3.12.14 |
| ruff | 0.15.13 | 0.16.5 |
| mypy | **1.20.2** | **2.3.1** |
| pytest | 8.4.2 | 9.1.1 |

四道门的本地等价命令，顺序与 `ci.yml` 一致：

```bash
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/python -m pytest -m "not network"
```

全套约 120s。mypy 差着一整个大版本，而本仓库 2026-09-09 至 09-13 那次 23 次推送、4 天红 CI 的事故根因正是 mypy 版本漂移——用裸命令预检等于绕开 `requirements.lock` 这个锚，也就绕开了 `ci.yml` 开头那条不能再退的性质。另外 `requires-python` 是 `>=3.12,<3.14`，某些终端里的 3.14 超出支持范围，本仓库有过「3.14 静默掩盖了本该暴露的真实失败」的教训。

## 并行工作

这个仓库常有多个 session 同时开着，共用一个工作树。

动手前先确认当前分支与工作树状态。要在另一个分支上工作时用 `git worktree` 另开一份，**不要在主工作树上切分支**——那会动到别人正在编辑的文件。2026-09-16 就发生过：一个 session 建分支后另一个切走了，两个 commit 落到了别人的分支上，最后靠独立 worktree 把它们 cherry-pick 回正确的分支才理清。

## 改 ratchet 要带理由

`CEILING_SECONDS`（`tests/architecture/suite_duration.py`）和 source budget 表（`tests/architecture/test_source_budget.py`）都是 ratchet：**抬顶只允许发生在写明理由的那个 commit 里**，理由写进紧挨着常量的注释，带上测量数据。

不要为了过顶把注释 golf 掉。第九次抬顶的注释记下了原因：a ratchet with no headroom stops being a ratchet and becomes a tax on the first honest change, paid in deleted comments.
