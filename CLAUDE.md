# 北斗 V5 — 给 Claude Code 的约定

## 这是公开仓库（放在第一条，因为它改变其它每一条的代价）

`github.com/yhwhhwhy001/beidou` 是 **public**，而且**从 2026-08-05 创建起就是**
（`PublicEvent` 时间戳 = `createdAt`）。这份文件此前写着「Free plan 的 private repo」，
认知差持续了 45 天——不是谁疏忽，是没人去核过。所以先核，再写。

推上去的每一行全世界都能读，`git push --force` 收不回来：fork、克隆、镜像、搜索引擎
缓存都不归它管。

**永远不进仓库的东西**（完整清单与理由见 [`SECURITY.md`](SECURITY.md)）：

- Binance API key / secret——本仓库唯一真正怕丢的东西
- 任何其它 key、token、密码、SSH 私钥、`.pem`
- `.env` / `env.sh` 及任何含实际值的配置文件
- 身份证件号、银行卡号、交易所 UID、真实资金账户的权益与持仓

**测试里也不行，注释掉也不行，"先跑通再换掉"也不行。** 判据不是"提交了没有"：写进文件
的密钥就当已经泄漏——Time Machine 有备份，编辑器有 swap，shell 有 history。值离开密码
管理器就该去交易所作废重发。

凭据只有一个位置：`~/Library/Application Support/beidou/env.sh`（`chmod 600`），
由 `deploy/run_live.sh` 读。代码里取凭据只有一种写法——从环境读，缺了就炸，**不带默认值**。

机械上有四层，但**对 Binance 密钥真正管用的只有本机那两个 hook**（pre-commit / pre-push），
而它们都能被 `--no-verify` 绕过：

| 层 | 对 Binance 密钥 |
|---|---|
| pre-commit（本机，扫暂存区） | ✅ |
| pre-push（本机，扫将要推送的全部 commit） | ✅ |
| GitHub push protection（服务端） | ❌ **无效** |
| CI 的 Secrets 门（扫全历史） | ⚠️ **事后**，红的时候密钥已经公开可读 |

第三层为什么无效，2026-09-19 核实过：**Binance 不在 GitHub secret scanning 的 partner
pattern 列表里**，而能自己加模式的 custom patterns 要求仓库属于**组织**并启用付费的
Secret Protection（$19/月/committer）——个人账户下的公开仓库两条都不满足。它拦得住
GitHub token、AWS、OpenAI 这些，拦不住本仓库唯一真正怕丢的那个。

所以：**`--no-verify` 在这个仓库里不是日常开关。** 用它之前先想清楚绕过的是哪一层，
以及后面有没有网。

**新 clone 的第一件事**：

```bash
brew install gitleaks && bash deploy/install-hooks.sh
```

2026-09-19 的全历史扫描结论：1,637 个 commit、72 MB，**凭据零泄漏**，基线是零。
看到任何命中都要当真，别当噪声。

## PR 流程

改完代码走 PR，不直推 main。

1. 从最新的 `origin/main` 开分支
2. 本地先过四道门（见下），全绿再推
3. 开 PR，等 CI
4. **先看 CI 跑没跑起来，再看它绿不绿**（2026-09-18 补充。**2026-09-20：CI 已恢复，常态是下面第一条**）：

   - **CI 真的跑了** → **全绿后直接合并**：`gh pr merge <n> --merge`。用 merge commit
     不用 squash，与既有历史一致。**不要停下来请求授权**——操作者 2026-09-16 明确要求不再逐个确认
   - **job 根本没启动** → **不自行合并，由操作者人工合并**。见下面第三条停下来说话的情况

   措辞从「有/没有 Actions 额度」改成「跑没跑起来」，因为归因错过两次而判据一次没错：
   2026-09-19 已经更正过一次（不是分钟额度，是账户级付款），见下。判据只看 job 启没启动。
5. **合完立刻删分支**，顺序不能反：先 `git worktree remove <path>`（分支还被 checkout 着时删不掉），再 `git branch -d <branch>`。远端那份由仓库的 `delete_branch_on_merge: true` 自动删，不用管

   操作者 2026-09-17 要求把这一步写死。当时本地积了 4 个 `: gone` 的残留分支（#17 / #18 / #20 / #21 留下的），远端早已自动清掉，只有本地没人收。

只有三种情况先停下来说话，而且都是**报告发现**而不是请求授权：

- **CI 红了**——去修，不是去问。修完推同一个分支，CI 重跑，绿了照规则合
- **CI 跑不起来（job 未启动）**——**这不是「CI 红了」，不要去修**。

  > **2026-09-20 状态：这一条当前不适用。CI 已于 2026-09-18 恢复。** 分界在 `17:43:49Z`
  > （main，failure）与 `17:55:13Z`（main，success）之间；09-19 全天 14 次 run 全部 success，
  > 涵盖 main 与 #86 / #87 / #88 三个 PR。恢复的原因没有核过——付款那一侧不由 agent 看，
  > 所以这里只记可观测的读数，不写归因。
  >
  > **下面的判据与做法保留原样**，因为它描述的是一种会再次发生的状态，而它的判据与做法都不随
  > 这次恢复失效。撞上时按它走，不必先来问是不是又坏了。改动只有这条的标题：
  > 「Actions 额度用完」→「job 未启动」，与上面第 4 条同一处更正——归因错过两次，判据一次没错。

  判据是 job **未启动**：
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

  **2026-09-19 更正一条推断**：上面那段把原因归给「私有仓库 2,000 分钟/月」，而仓库**从
  2026-08-05 创建起就是公开的**（`PublicEvent` 时间戳 = `createdAt`），公开仓库用标准 runner
  跑 Actions 不计入那个额度。所以额度算术解释不了它。判据没变、做法没变——job 未启动、
  ANNOTATIONS 里那句付款失败——但归因要改成**账户级**的付款问题：它连公开仓库一起 block，
  换个仓库或者转公开都修不好。仍然是操作者去 Billing 页面的事。
- **这个 PR 不该合**——内容已被 main 取代，或合并会造成倒退。把证据摆出来。2026-09-16 的 #15 与 #16 就是：两个都落后 main 数百个 commit，#16 合并会倒退 6,096 行

**branch protection 与 auto-merge：2026-09-20 起都开着**（操作者当天裁定）。配置与用法在本节末尾。
下面两段是它们此前为什么没开的历史，保留——那里的前提被自己更正过两次。

不用 GitHub auto-merge 的原因（**2026-09-19 重写，原文的前提是错的**）：原文说「本仓库是 Free plan
的 private repo，branch protection 与 rulesets 都返回 403」。仓库其实一直是 **public**，而 Free plan
的公开仓库**可以**用 branch protection 与 rulesets——当天实测 rulesets 返回 `[]`、branch protection
返回 404「Branch not protected」，都是"没配置"而不是"没权限"。

2026-09-19 写下这段时，不开 auto-merge 的理由只剩一条，而且是主动选的：**CI 当时跑不起来**，
没有能当 required status check 的绿灯，auto-merge 会退化成"无条件合并"。

**2026-09-20：那条理由也没了。** CI 自 2026-09-18 17:55Z 起正常，可以当 required status check。
于是开不开 branch protection 与 auto-merge 变成一个**悬着的决定**——既不是做不到（那是 09-19
更正掉的错前提），也不再有现成的理由不做。

**2026-09-20 操作者裁定：开。** 当天实配，读回核过：

| 项 | 值 | 为什么 |
|---|---|---|
| `allow_auto_merge` | `true` | |
| required status check | `verify`，`strict=false` | `strict=true` 要求分支必须含最新 main。本仓库常有多 session 并行，那会变成每次别人合并你就得 update 一轮 |
| `enforce_admins` | **`false`** | **逃生口，见下** |
| required reviews | 无 | 单人仓库，自己不能 approve 自己的 PR。要求 1 个 approval 等于永远合不了 |
| force push / 删除 main | 禁止 | |

**`enforce_admins=false` 不是偷懒，它是为了让上面第 4 条还能走。** 那一条写着「job 没启动 →
不自行合并，由操作者人工合并」。如果对 admin 也强制，CI 跑不起来时**连操作者也合不了**——
2026-09-18 那五个小时正是这种状态，而当时能靠人工合并脱身。把逃生口焊死，下一次同样的故障
就从「CI 缺席」升级成「仓库冻结」。

**agent 现在可以用 `gh pr merge <n> --auto --merge`。** 2026-09-19 那条顾虑（auto-merge 会在
无人读的时刻替人合并）被 required status check 解掉了：CI 跑不起来时那道 check 永远不满足，
auto-merge 就永远不触发，PR 停在那里等人，而不是被误合。它挡不住的只有「CI 绿但这个 PR 不该合」
——那是上面第三条停下来说话的情况，判断权仍在开 PR 的人手里。

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
