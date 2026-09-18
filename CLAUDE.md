# 北斗 V5 — 给 Claude Code 的约定

## PR 流程

改完代码走 PR，不直推 main。

1. 从最新的 `origin/main` 开分支
2. 本地先过四道门（见下），全绿再推
3. 开 PR，等 CI
4. **先看 CI 跑没跑起来，再看它绿不绿**（2026-09-18 补充）：

   - **有 Actions 额度**（CI 真的跑了）→ **全绿后直接合并**：`gh pr merge <n> --merge`。用 merge commit
     不用 squash，与既有历史一致。**不要停下来请求授权**——操作者 2026-09-16 明确要求不再逐个确认
   - **没有 Actions 额度**（job 根本没启动）→ **不自行合并，由操作者人工合并**。见下面第三条停下来说话的情况
5. **合完立刻删分支**，顺序不能反：先 `git worktree remove <path>`（分支还被 checkout 着时删不掉），再 `git branch -d <branch>`。远端那份由仓库的 `delete_branch_on_merge: true` 自动删，不用管

   操作者 2026-09-17 要求把这一步写死。当时本地积了 4 个 `: gone` 的残留分支（#17 / #18 / #20 / #21 留下的），远端早已自动清掉，只有本地没人收。

只有三种情况先停下来说话，而且都是**报告发现**而不是请求授权：

- **CI 红了**——去修，不是去问。修完推同一个分支，CI 重跑，绿了照规则合
- **CI 跑不起来（Actions 额度用完）**——**这不是「CI 红了」，不要去修**。判据是 job **未启动**：
  `verify` 3–4 秒结束、零步骤执行，且 `gh run view <id>` 的 ANNOTATIONS 里写着
  「The job was not started because recent account payments have failed or your spending limit needs
  to be increased」。这种状态下推任何提交都只会再得到一次 3 秒失败——**没有可推的修复**。做三件事：
  (1) 不自行合并，上面第 4 条的前置条件不成立；(2) 在收尾里报告，并把本地四道门的读数摆出来；
  (3) **合并之后在 main 上跑一遍完整四道门**——CI 缺席时它是唯一的机械检查，别让改动落在无人读的
  绿灯上。合并由操作者手动做。

  **为什么它要单独一条**：2026-09-18 撞上时，「CI 红了」那一条把人引向「去修」，而这里没有东西可修。
  当天的读数：本月 420 次 run、均值 6.4 分钟 ≈ **2,690 分钟**，而 Free plan 私有仓库是
  **2,000 分钟/月**。分界点在 11:41:37Z（PR #70，success 5m33s）与 12:09:36Z（main 的 push，
  failure 4s）之间——**在那之后连 main 自己的 CI 也是红的，而它红的原因同样不在代码里**，别被它吓到。
  额度恢复要操作者去 Billing 页面提额或修付款方式，那是付款相关的动作，不由 agent 做。
- **这个 PR 不该合**——内容已被 main 取代，或合并会造成倒退。把证据摆出来。2026-09-16 的 #15 与 #16 就是：两个都落后 main 数百个 commit，#16 合并会倒退 6,096 行

不用 GitHub auto-merge 的原因：本仓库是 Free plan 的 private repo，branch protection 与 rulesets 都返回 403，没有 required status checks，auto-merge 无从触发。桌面版那个开关只是同一功能的前端，同样开不起来。

## CI 监控开关：开完 PR 立刻打开

开 PR 之后**立即**调用 `mcp__ccd_pr__set_monitor`，不要等操作者去界面里勾：

- `auto_fix` 与 `address_comments`（两者必须相等）——CI 失败、合并冲突、review 评论会主动叫醒 session，不必轮询
- `auto_archive_on_close`——**保持关闭，不要打开**。操作者 2026-09-17 明确要求：合并之后不要关闭 session。合并不是工作的终点，窗口要留着继续用

这些开关**不随 session 继承，是逐个 PR 的**：2026-09-16 的 #19 上 `auto_fix` 开着，绑定 #22 时它回到 false。所以每开一个新 PR 就重新设一次，这是开 PR 动作的一部分，不是一件单独要请示的事。

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

## 重启实盘循环

操作者 2026-09-17 要求把这条写死。当时 13.4 天里 `state.restarts` 已经到 **51**（约 3.8 次/天），
而 M-010 要 30 天连续记录、`realised_vol` 要单构造窗口、L3 要 7 天无 ERROR。

**重启不清构造指纹，但每次都可能吃掉一根 bar 的退出检查。** exit overlay 在 bar 收盘判定
（`beidou_live/exits.py`），一根没跑的周期就是那根 bar 没有退出检查，而且不会补——判据不看
`extreme`。2026-09-15 量到命中概率约 1.16%，AKEUSDT 逼近 6σ 时它第一次有价钱。

三条，顺序不能反：

1. **选周期之间的窗口**。循环在每根 1h bar 收盘后约 20–35s 跑完。安全窗口是**整点后 5 分钟到下一个
   整点前 10 分钟**。不要在整点前几分钟重启。
2. **重启前跑两个构造测试**，确认这次重启不改构造：
   ```bash
   .venv/bin/python -m pytest tests/live/test_the_construction_is_frozen_until_the_holdout_matures.py tests/live/test_construction_identity.py -q
   ```
   红了就不是「重启」，是构造变更，要按 K-EX14 与冻结裁定走。
3. **重启后把「谁、为什么」记进 `docs/RESEARCH_LOG.md`**，只写可观测事实（PID、`restarted_at`、
   `restarts`、源文件 mtime 对进程启动时刻、两个测试的结果）。**不按时间相关性给实盘动作归因**——
   多会话并行下，「我改完代码，循环就重启了」不构成「是我重启的」。

重启命令与幂等性见 `docs/RUNBOOK.md`（`launchctl kickstart -k gui/$(id -u)/com.beidou.live`；
clientOrderId 按 bar 派生，先查后下）。要确认循环此刻跑的是哪份代码，比较源文件 mtime 与进程启动
时刻（`ps -eo pid,lstart,command | grep "live run"`），不要读 `cycles.jsonl` 的最后一行——重启后
那是已死进程的。

## 改 ratchet 要带理由

`CEILING_SECONDS`（`tests/architecture/suite_duration.py`）和 source budget 表（`tests/architecture/test_source_budget.py`）都是 ratchet：**抬顶只允许发生在写明理由的那个 commit 里**，理由写进紧挨着常量的注释，带上测量数据。

不要为了过顶把注释 golf 掉。第九次抬顶的注释记下了原因：a ratchet with no headroom stops being a ratchet and becomes a tax on the first honest change, paid in deleted comments.
