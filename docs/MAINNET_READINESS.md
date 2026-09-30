# mainnet 准入设计（Q1 = B：设计进 scope，启用仍不在）

2026-09-28。操作者对 Q1「面向生产指哪一层」的原话是「目前 B，没问题以后将会是 C」
（`docs/analysis/2026-09-28-production-refactor-deep-analysis.md` §14.1，:1092；下称「分析」）。本文是 B 的交付物，
对应执行手册 §3.11（WP-P6，`docs/analysis/2026-09-28-production-refactor-execution-plan.md`）。

- **零代码。** 本文不改任何 `.py`，`beidou_exchange/guard.py` 一字不动。今天 guard 仍在请求离开进程之前拒绝 mainnet（§4.1）。
- **只写设计。** 启用 L-B 是另一次裁定。本文写那次裁定要看的门、读数、来源与 Owner。
- **公开仓库。** 不写账户数字（权益、持仓、UID、余额），不写凭据与 URL 令牌。资金上限只写选法（§6.4）。
  引文里原有的账户数字已删去，删处标〔略〕，其余逐字。
- **行号**以 `origin/main 36ee6341` 为准。

读法：§1 生产三层；§2 原样引 09-05 的解除条件；§3 B → C 的准入门；§4 `guard.py` 放行的形态；
§5 密钥与 kill switch 分离；§6 成本模型校准；§7 回退；§8 未决问题。
本文是 HC-9（「生产」的定义）的落点；§4 描述的改动属于 HC-3（分析 §7.6，:787、:793）。

## 1. 生产的三层定义（D-PR02）

Q1 的三个选项（分析 :115）：A 只做 L-A；B 是 L-A 加 mainnet 小额校准的准入**设计**，不启用，不动 `guard.py`；
C 是现在就要 mainnet 目标资金。裁定的后果，原文（:1092）：

> D-PR02 定为 **B**：写 mainnet 小额准入的**设计文档**，`guard.py` 不动；文档要含「B → C」的准入门（§14.4 WP-P6）。memory 里「production/mainnet explicitly out of scope for now」自此改写为「设计进 scope，启用仍不在」

| 层 | 是什么 | 读数口径 | 当前 |
| --- | --- | --- | --- |
| L-A demo 无人值守 | demo 场地上的 armed 循环。每根 1h bar 自己跑完，不靠人 | M-PR02、M-PR03、M-PR06、M-PR07（下表） | **运行中**。构造 `2ee491c13971`，自重启 #60 起；2026-09-28 裁定的 v12 载入后换成 `e32f3856ac1e` |
| L-B mainnet 小额校准 | mainnet 上一笔有上限的真钱。目的是量冲击系数与真实滑点，不是赚钱 | 开之前：§3.1 全部门。运行中：M-PR03、M-PR07 在 mainnet profile 上的同口径读数，加 §6.3 的校准读数 | **设计中**（本文） |
| L-C mainnet 目标资金 | 在校准过的成本模型下重推 k，再按档加到目标资金 | 开之前：§3.2 全部门。运行中：同 L-B，加 M-010 与容量读数 | **未开** |

「以后将会是 C」在本文拆成两步。冲击系数只能用真钱校准（§6.2），所以 C 的第一步就是 L-B。
§3.1 是开 L-B 的门，§3.2 是从 L-B 到 L-C 的门。

L-A 的四个读数，逐字引分析 §3.5 修订表（:375、:376、:379、:380）与 §10.1 的阈值（:1004、:1005、:1008、:1009）：

| ID | 指标（原文） | 基线（原文，分析 2026-09-28） | 成功阈值（原文） |
| --- | --- | --- | --- |
| M-PR02 | 只在操作者机器上执行、且状态与 CI 不一致的测试数 | E-PR08、子代理 C：7 条 `.beidou/` 读者里红 1 | 0 且 7 条每夜有结果 |
| M-PR03 | **每月丢掉的 bar 数，按来源分**：失败周期（`cycles.jsonl` ERROR 行）/ 重启时机（未注明「already rebalanced」的 SKIPPED 行） | E-PR39：09-15 起 7 / 0 | 换节点后 30 天失败周期列 < 7/13 天的比率；重启列保持 0 |
| M-PR06 | 每个候选数据族的「实盘覆盖 bars ÷ 最长信号 lookback」有日常读数 | 今天：无（E-PR12、E-PR28） | WP-A1 后日报每族一行 |
| M-PR07 | **宿主离线到操作者知道的最长延迟**（演练测得） | 今天：无上限（同机巡检随宿主一起停，E-PR20/37） | Q3 不重开：记录为 ACCEPTED；重开：演练 ≤ 2 个整点 |

M-PR04（三个 digest 逐字不变）与 M-PR05（`trials.jsonl` 不变）是每个 PR 的护栏，三层都适用。
分析没有给 L-B、L-C 单独编 M-PR 号。本文不新编号，它们的读数写在 §3 与 §6。

## 2. 09-05 已写下的解除条件（原样引）

出处都是 `docs/analysis/2026-09-05-system-quality-deep-analysis.md`。

**§12.6 那一段（:643）。** §12.6 的标题是「操作者裁定（2026-09-06）：WEAK_PASS 足以支撑受控执行」。
「真实资金仍 HOLD 且 DEFERRED」在它的「裁定内容」段里：

> **裁定内容**（附录 B 有正式条目）：分位数门下 tsmom 的诚实网格 OOS 1.485 对 N≈141 的阈值 1.483–1.497，headroom 小于阈值本身的估计误差，记 **WEAK_PASS**；该 WEAK_PASS 足以支撑 demo 的受控执行。Final Decision 因此升为 **GO（受控执行）**。**真实资金仍 HOLD 且 DEFERRED，本裁定不触及它**，其解除条件仍为 §11 原文（附录 D 的 P0 全关 + 冲击模型重推 `vol_target` + 30 天干净窗口 + 上线断言四项）。

**它指回的 §11 原文（:549）：**

> - 真实资金：**HOLD 且 DEFERRED**。解除条件（只在操作者显式重开 mainnet 后适用）：附录 D 的 P0 全部关闭 + KILL-Q12 的冲击模型下 `vol_target` 重推 + 构造冻结后 30 天干净窗口（M-Q08 四项达标、M-Q09 ≥ 30 天、构造不变）+ 上线断言四项（CROSSED / multiAssets 与验证口径一致、全新 `state_dir`、账户空仓空挂单、告警端到端演练）。按 §9 的日期推算，最早在第 16 周之后。

**附录 D「mainnet pre-flight backlog」的 P0（:1010-1018）。** 数下来是 7 项，在 :1012-1018。
逐字照抄，只在 :1014 删了账户数字：

| 优先 | 项 | 来源 |
| --- | --- | --- |
| P0 | 冲击成本模型（平方根法则起步，由前审参与率表与 E-19 校准）+ `vol_target` 重推 + 10 万 USDT 容量门槛写进 config | KILL-A / Q12 |
| P0 | 保证金模式断言：每个 managed 符号 `marginType == CROSSED`（或显式设置）且 `multiAssetsMargin` 与验证口径一致 | KILL-R19 |
| P0 | 全新 `state_dir`：启动拒绝 `started_at` 早于武装时刻或已有 `equity_hwm` 的 state（demo HWM〔略〕对〔略〕真实账户意味着首周期 −54% 回撤 → 阶梯误报） | KILL-R19 |
| P0 | 账户空仓空挂单断言（否则拒绝启动，而不是撤单 / 平仓） | KILL-R19 / L1-09 |
| P0 | 告警 webhook 已配置并完成一次端到端演练；第二通道 | KILL-R19 / L1-11 |
| P0 | 密钥：先建 `~/Library/Application Support/beidou/env.sh`（600）并移除 `run_live.sh:18` 对 `~/.zshrc` 的 eval 回退；分权 API key（只读 / 交易，无提现）；IP 白名单 | KILL-R19 |
| P0 | `--armed` + `max_equity_usdt` 双重确认；host 白名单改码 | 审计 |

`run_live.sh:18` 在今天的文件里是 `fi`。那条 eval 在 :17，整个回退分支是 :14-18。

**KILL-R13（附录 C.2，:907）。** 「命题」与「必须改什么」两格，逐字：

> 命题：操作者现行范围是「demo 是测试环境，真实资金推迟，production/mainnet 明确不在范围内」。报告却把两个 P0 之一（Q12）、一个 P0 Claim（C-6）、§7.4 的 5 个 P0 + 6 个 P1、Phase B ⑤、Phase C ④⑤⑥ 全挂在「真实资金前」这个被推迟的目标上，并据此把 Phase B/C 的 alpha 占比拉到 75%/70%，对着 90% 的目标——Won't 列表写了 mainnet，正文却为 mainnet 排了 6 周以上非 alpha 工作。真正服务「24h 无人值守 demo」的只有单实例锁、熔断退避、告警去重和一个只需告警的看门狗。
>
> 必须改什么：所有「真实资金前提」项移出 12 周路线图，单列「mainnet pre-flight backlog」，只在操作者重开 mainnet 时启动；KILL-Q12 改 DEFERRED（范围）而非 OPEN P0；Phase B/C 只保留服务 demo 无人值守的最小集（锁、退避、告警去重、异机告警），alpha 占比据此回到 ≥ 85%。

**本文怎么读这四段。** Q1 = B 重开的是 mainnet 的**设计**，不是 backlog 的**执行**。
附录 D 仍只在操作者裁定启用 L-B 时开工，本文不把任何一项排进路线图。这与 KILL-R13 的处置一致。

## 3. B → C 的准入门

每项一行。「今天的读数」只写仓库或命令里读得到的，读不到的写「未测」并给读法。
Owner 写「会话」的，指写代码或跑测量的 Claude 会话；采纳与合并仍归操作者。

### 3.1 开 L-B 之前（C 的第一步）

| # | 门 | 今天的读数 | 来源 | Owner | 何时可读 |
| --- | --- | --- | --- | --- | --- |
| B1 | 冲击成本模型 + `vol_target` 重推 + 容量门槛（附录 D P0 第 1 项；KILL-Q12） | 模型已建（DL-C1）。系数 1.0 是假设，对本场所记 E5，仓库写明 demo 校准不了它。P26 在冲击模型下量过 k，未采纳；它的 argmax 结论后来撤回。现行 k 0.175 按 D-035 的回撤预算规则选，那次重测用的是平费率成本。容量门槛只写在注释里，没有 config 键执行它 | `config/costs.yaml:74-95`；`docs/RESEARCH_LOG.md:3716-3750`、`:3826-3889`；`config/live.demo.yaml:59-76`、`:118-122`、`:232-249`；`scratchpad/p32d_ladder_bootstrap_pathwise.py:175` | 会话（跑）· 操作者（采纳） | 随时可跑。要先预登记、计 ledger（P26 申报了 35 格，RESEARCH_LOG:3880-3884） |
| B2 | 保证金模式断言（P0 第 2 项） | 已在代码里：启动时读 isolated 与 multiAssets，不符就拒绝启动。残余：日内新进 universe 的币，到下次重启才复查。demo profile 声明 `multi_assets_margin: true`，mainnet profile 要按 mainnet 账户重新声明 | `beidou_live/engine.py:493-501`、`:2170-2186`；`beidou_live/account_shape.py:16-35`；`beidou_exchange/binance_usdm/venue.py:269-284`；`config/live.demo.yaml:10-16` | 操作者（定取值，§8 Q-M6） | mainnet profile 写出时 |
| B3 | 全新 `state_dir`（P0 第 3 项） | 未写：代码里没有这条拒绝。启动时 `equity_hwm` 取旧值与当前权益的较大者，旧 state 会带进新账户 | `beidou_live/engine.py:521` | 会话（写拒绝，另一个 PR） | 那个 PR 合入后，由测试读 |
| B4 | 账户空仓空挂单断言（P0 第 4 项） | 未写成拒绝。外来仓位与挂单在启动时「保持不动 + 告警」；只撤本循环自己的旧挂单；`live flatten` 连外来仓位一起平 | `beidou_live/engine.py:541-567`、`:1127-1128`；`beidou_live/reconciler.py:131-134` | 会话（写拒绝）· 操作者（备专用账户，§8 Q-M5） | 同 B3 |
| B5 | 告警端到端演练 + 第二通道（P0 第 5 项） | `beidou live alert-test` 已有。RESEARCH_LOG 里唯一一次演练在 2026-09-07，单通道送达。第二通道变量本机未导出（E-PR37）。巡检与日报只看 demo profile，mainnet 循环今天没有巡检 | `beidou_cli/live_cmd.py:755-796`；RESEARCH_LOG:2702-2711；分析 :208；`deploy/run_check.sh:80-86`、`:154`（不传 `--profile`，默认 demo） | 操作者（O-2 补变量、跑演练）· 会话（巡检覆盖 mainnet profile） | O-2 之后当天 |
| B6 | 宿主外告警与远端 kill switch（DL-Q8 ①–⑦；D-P4 已重开） | WP-R1（告警侧）未合入，M-PR07 无上限。WP-R2（远端 lease → 本地 reduce-only、`flatten --raw`）未开，后置到 WP-R1 有 14 天干净读数之后。09-05 KILL-R2 的处置原文有一句：「或把看门狗降为同机 run_check.sh + 第二告警通道并推迟到 mainnet 前」 | 分析 §14.4（:1144-1153）；09-05 分析 :896 | 会话（WP-R1/R2）· 操作者（O-3、演练） | WP-R1 演练后；WP-R2 的预登记最早在 R1 有 14 天干净读数之后 |
| B7 | 密钥：`env.sh`（600）、移除 `~/.zshrc` 回退、分权 key、IP 白名单（P0 第 6 项）。**建 `env.sh` 之前先把全部 `BEIDOU_*` 迁过去，否则下一次重启以 78 退出**（见表下） | `env.sh` 不存在：本文写作时 `test -e` 退出 1，与 E-PR37 一致。六个 deploy 脚本同一规则：`env.sh` 存在就只 source 它，否则 eval `~/.zshrc` 的 `^export BEIDOU_` 行。所以凭据今天来自 `~/.zshrc`。分权与白名单：未测，是交易所侧设置，仓库与命令读不到 | `deploy/run_live.sh:11-22`；`deploy/run_check.sh:11-16`；`run_data.sh`、`run_forward_board.sh`、`run_governance_gate.sh`、`run_shadow.sh` 同形 | 操作者（HC-1） | 操作者核对交易所页面后 |
| B8 | `--armed` + `max_equity_usdt` 双重确认；host 白名单改码（P0 第 7 项） | `--armed` 已在：真下单运行不带它就拒绝。`max_equity_usdt` 在代码里零命中。host 白名单今天是常量，改码的形态见 §4 | `beidou_cli/live_cmd.py:283-286`；`beidou_exchange/guard.py:16` | 会话（写）· 操作者（HC-3 合并） | §4 的 PR 合入后 |
| B9 | 30 天干净窗口的「构造不变」（§11） | 起点是**重启 #60**，不是手册写的 #59。#59（09-27 13:21:35Z）载入 `4b2dc74b8f3c`；#60（16:35:46Z）载入 `2ee491c13971`，v11 没有别名，窗口再清零一次。两个钟都从第一条带新构造的周期行算：重启时写的 SKIPPED 行不带 `construction`，所以是 17:00:31Z 写出、处理 16:00Z 那根 bar 的那一行。30 天满在 2026-10-27 16:00Z 到 17:01Z 之间，看用哪个钟。另：构造冻结已于 2026-09-27T07:54Z 到期，此刻没有冻结在执行。2026-09-28 补：同日裁定的 v12（约束侧改按可动用 USDT，`risk-g11-denominator`）也没有别名，载入它的那次重启再清零一次，30 天从那次重启后第一条带新构造的周期行重算，10-27 随之作废 | RESEARCH_LOG:17806-17816、:18084-18124；`beidou_live/construction.py:51-60`；`beidou_live/report_common.py:88-121`（M-010 用 `bar_open_ms`）；`beidou_governance/admission.py:110-148`（K-EX14 用 `at`）；`beidou_live/engine.py:624-637`；`tests/live/test_the_construction_is_frozen_until_the_holdout_matures.py:40-49`、`:72` | 操作者（不改构造；要不要重宣冻结见 §8 Q-M7） | 2026-10-27 起 |
| B10 | M-Q08 四项达标（09-05 分析 :536：换手 ±25%、滑点 ≤ 2× 模型、迟到 ≤ 5%、一致性 100%） | 一致性：09-28 04:07Z `live status --check` 退出码 0。其余三项未测：日报每天算，但写在操作者机器的 `reports/daily/`，不入库。2026-09-30 补读数（本机日报原文，未入库）：换手 09-23 至 09-26 为回测同期的 2.25–2.38 倍，带是 0.75–1.25，09-26 那天已可分辨；同一构造不足 14 个完整日，所以没判定。09-27 起三次构造变更，归档没盖到新构造的第一根 bar，一直读不出。滑点主书 5.1±2.0 bps，限 4.0，带外但与噪声不可分辨。迟到 0.0%，限 5% | RESEARCH_LOG:18628；`beidou_live/execution_fidelity.py:1-8`、`:69-70`；`config/live.demo.yaml:486-507`；`beidou_cli/live_cmd.py:879-881` | 操作者（读日报） | 每天；30 天窗口同 B9 |
| B11 | M-Q09 ≥ 30 天、0 次未处理失联（§11） | RESEARCH_LOG 记到 09-28 04:07Z：PID 仍是重启 #60 的进程，`state.restarts` 仍是 60。此后未测。读法要先定：`live status` 的「连续无故障无重启」遇失败周期也清零（§8 Q-M8） | RESEARCH_LOG:18627；`beidou_live/health.py:108-123` | 操作者 | 按重启口径，2026-10-27 起 |
| B12 | `realised_vol` 的单构造条件与 L3 的 7 天 | `realised_vol`：2026-09-30 起跨构造读（操作者当天裁定），每根收益按当时的 vol_target 折到当前 k，找不到 k 的与跨空档的丢掉；此前 30 天窗口里多于一个构造就不出数。09-30 读 15.3%，在带内。L3：`beidou live soak --check`，按 2026-09-10 的裁定判；当前值未测。口径有一处不一致：RESEARCH_LOG 把 L3 记为随 #60 清零的钟，而 `soak.score` 按记录首尾时间戳算天数，不按构造切窗；它默认读 paper-l3，而 #59、#60 都没动 paper-l3 | RESEARCH_LOG:17838、:18121、:18123-18124；`beidou_live/risk_budget.py:563-633`；`beidou_live/soak.py:23-36`、`:134-164`；`beidou_cli/live_cmd.py:430` | 操作者（读；L3 按哪个口径由操作者定） | `realised_vol` 最早 10-27；L3 按 RESEARCH_LOG 口径最早 10-04 |
| B13 | 证据门严格，mainnet 不带 bridge | demo launcher 在 2026-10-13 之前带 `--allow-unvalidated`。tsmom 的证据是 WEAK_PASS，启动门允许 WEAK_PASS 上线。mainnet launcher 不得带 bridge。WEAK_PASS 够不够真钱见 §8 Q-M2 | `deploy/run_live.sh:47-54`；`config/alpha_registry.yaml:361-363`、`:370-372` | 操作者 | 现在可读 |
| B14 | §4 的三件事：签字文件、`--mainnet`、mainnet profile | 三样都不存在：`governance/MAINNET_ENABLED` 不在仓库；CLI 没有 `--mainnet`；`config/` 下只有 `live.demo.yaml` 一份 live profile | `git ls-files governance config`；`beidou_cli/live_cmd.py:225-263` | 操作者（签字、HC-3）· 会话（写 PR） | §4 的 PR 合入后 |
| B15 | 资金上限已选定，写进签字文件与 `max_equity_usdt` | 未选。选法见 §6.4 | 本文 §6.4 | 操作者 | 签字时 |

**B7 的前置条件单列，因为做错会把实盘停掉。** 建 `env.sh` 之前，必须把 `~/.zshrc` 里**全部** `BEIDOU_*` 变量迁过去。
`env.sh` 一存在，六个脚本就只读它：

- 缺 Binance key：`run_live.sh:19-22` 以 78 退出，下一次重启起不来。
- 缺告警变量更隐蔽：另外五个 job 照常跑，告警却静默（`run_check.sh:35`，两个 URL 都空就不发）。
- 核对只比变量名，不读值。两边各跑一次
  `grep -E '^export BEIDOU_[A-Z0-9_]+=' <文件> | sed -E 's/^export (BEIDOU_[A-Z0-9_]+)=.*/\1/' | sort`，
  名字集合相同才建。建完用同一份环境跑一次 `beidou live alert-test`。
- 移除 `~/.zshrc` 回退是另一个 PR，六个脚本一起改（HC-1）。执行手册 §4 O-1 给 CLAUDE.md 那句凭据的改法。

### 3.2 从 L-B 到 L-C 之前

| # | 门 | 今天的读数 | 来源 | Owner | 何时可读 |
| --- | --- | --- | --- | --- | --- |
| C1 | 冲击系数已校准：前 N 笔真实成交回归，区间达到预登记的宽度 | 未测（L-B 未开） | 本文 §6.3 | 会话（预登记、回归）· 操作者（裁定） | L-B 成交满 N 笔后 |
| C2 | k 在校准系数下按目标资金重推 | 未测 | 协议照 P26：RESEARCH_LOG:3785-3889 | 会话（跑）· 操作者（采纳 = 构造改动） | C1 之后 |
| C3 | 目标资金处的容量：冲击占成本、Sharpe 损失、`max_participation` 拒掉的换手 | 未测。demo 侧有两条曲线，系数都是假设 | `config/live.demo.yaml:64-76`；RESEARCH_LOG:3724-3736 | 会话 | C1 之后 |
| C4 | L-B 真实成交上的 M-Q08 四项，30 天达标 | 未测 | 同 B10，读 mainnet profile 的日报 | 操作者 | L-B 满 30 天 |
| C5 | L-B 期间 0 次未处理失联；真钱上做过一次 flatten 演练 | 未测 | 本文 §7 | 操作者 | L-B 期间 |
| C6 | 采纳新 k 之后，新构造的 30 天干净窗口 | 未测 | 同 B9 | 操作者 | C2 采纳后 30 天 |
| C7 | 资金分档加到目标，每档重读 C3 与 C4 | 未测 | 本文 §6.4 | 操作者 | 每档 |

## 4. `guard.py` 放行的实现形态（只写设计，不实现）

### 4.1 今天的 `guard.py`

- **常量三个**：`ALLOWED_HOSTS`（:16，只有 `demo-fapi.binance.com` 与 `testnet.binancefuture.com`）、`MUTATING_METHODS`（:17）、`ORDER_PATHS`（:18）。
- **检查点一**：`normalize_host`（:25-40）。要求 https、有主机名、URL 不带凭据、端口 443、不带 path/query/fragment。
  主机不在 `ALLOWED_HOSTS` 就抛 `GuardError`（:38-39）。
- **检查点二**：`WriteGuard.authorize`（:88-93）。只管写请求：kill switch 在时，只放行 `is_risk_reducing`（:43-49）为真的写。
- **拒绝的时刻**：构造 `WriteGuard` 时就调 `normalize_host`（:63），早于任何请求。所以今天连签名的读请求也到不了 mainnet。
- **谁调用**：生产路径只有一处，`beidou_live/config.py:296-307` 的 `build_venue`。
  调用它的是 `live run`（`beidou_cli/live_cmd.py:330`）与 `live flatten`（:700）。
- **一个口**：`BinanceRestClient` 的 `guard` 是可选参数（`beidou_exchange/binance_usdm/rest_client.py:54`、:112-113），不传就没有 host 检查。
  今天没有生产调用者不传。
- **公共行情**走 `fapi.binance.com`，不签名，不经过 guard（`beidou_live/config.py:291-293`；D-002）。
- **测试**：`tests/exchange/test_rest_client_and_guard.py:23-32` 断言 guard 拒绝 mainnet。

### 4.2 将来放行：三件事同时成立

- `ALLOWED_HOSTS` 不动。加一个新常量 `MAINNET_HOSTS = frozenset({"fapi.binance.com"})`。
- `normalize_host` 只在下面三件事**同时**成立时，把 `MAINNET_HOSTS` 并入允许集：
  1. 签字文件 `governance/MAINNET_ENABLED` 存在，且内容指名这一份 profile。
  2. 这一次命令带显式旗标 `--mainnet`。形状同 `--armed`（DL-L1）。
  3. profile 的名字含 `mainnet`：文件名与文件里的 `profile:` 键都要含。今天 `config/live.demo.yaml:1` 是 `profile: demo`。
- **签字文件的内容**：profile 路径、资金上限、签字日期、本文所在的 commit。
  可选加固：再绑该 profile 的 sha256。代价是 profile 改一个字（包括注释）都要重签。
- **判定只放在 `guard.py` 一处。** CLI 把旗标与 profile 名传进去，guard 自己读签字文件。
  拒绝点只有一个，才不会重演 L1-07：三处读者各看各的开关。
- **同一个 PR 堵 4.1 的口。** 要么不许不带 guard 构造 `BinanceRestClient`，要么把 host 检查挪进它的构造函数。
- **那个 PR 的测试**：三件事的 8 种组合只有全真放行；`ALLOWED_HOSTS` 逐字不变；`:23-32` 那条照旧绿；`live flatten` 走同一判定。

### 4.3 为什么要三件同时

三件事来自三个不同的地方：仓库里的裁定、这一次的命令行、配置文件自己的声明。
一次失误只能造出其中一件。缺哪一件，都留下一个具体的洞：

| 缺哪一件 | 另外两件都在时留下的洞 |
| --- | --- |
| 签字文件 | 没有操作者裁定的记录。一个会话写出 mainnet profile、敲一条带 `--mainnet` 的命令，就上了 mainnet |
| `--mainnet` | 没有「这一次是有意打真钱」。`--armed` 在 demo 上天天敲；只凭它，任何按这份 profile 带 `--armed` 启动的进程都打真钱，包括 worktree 里手敲的一条 |
| profile 名 | mainnet profile 最顺手的写法是复制 `live.demo.yaml` 再改。复制过来的 `profile: demo`、`state_dir: .beidou/live`、kill switch 路径都指向 demo 那一套。只凭签字与旗标，这份文件照样放行，demo 的高水位与退出锚点一起带进 mainnet（KILL-R19 (b)）。要求 `profile:` 键含 mainnet，就逼着写的人改这一处声明 |

审查稿指出过：签字文件与旗标本身也在同一个仓库、同一套合并流程里
（`docs/analysis/2026-09-28-production-refactor-adversarial-review.md:388`，KILL-08）。
所以三件缺一不可。它们挡的是误用，挡不住改代码：谁能改 `guard.py`，谁就能绕过这三件。那一层由 §4.4 的 HC-3 管。

### 4.4 改 `guard.py` 的 PR 怎么合

- 任何改 `beidou_exchange/guard.py` 的 PR 列 HC-3。审查稿 :505 那一行的原文：任何改动 `beidou_exchange/guard.py`，包括「参数化但不放行」。
- 新建或修改 `governance/MAINNET_ENABLED` 的 PR 也列 HC-3。
- 这两类 PR 排除自动合并：不开 `--auto`，由操作者合并。
- 这条今天只是规则。会话与操作者用同一个 GitHub 账号，GitHub 侧分不出是谁在合。加不加机械检查见 §8 Q-M3。

## 5. 密钥与 kill switch 分离

今天两者绑在一起，读代码可见：

- 账户级 kill switch 的路径由 API key 的 SHA-256 前 16 位派生（`beidou_live/lock.py:37-49`）；单实例锁同一种派生（:31-34）。
  换 key 就换了两个文件的位置。旧 key 下落着的开关，用新 key 的进程读不到。
- `beidou live kill-switch --release` 在环境里没有 key 时拒绝执行（`beidou_cli/live_cmd.py:813-819`）。
  `--engage` 没有 key 时只写配置路径，并报错说这次停止比看上去弱（:820-824）。
- 读开关不走网络：guard 每个写请求查一次文件（`guard.py:71-79`、:92-93），循环每个周期查一次（`beidou_live/engine.py:839`）。

L-B 的设计：

1. **两把 key。** 交易 key 只放在 mainnet 循环进程的环境里。它开合约交易，关提现，绑 IP 白名单。
   只读 key 给巡检、日报与将来的看门狗，没有交易权限。
   09-06 问远端介质时写下的条件就是「凭据可与交易 key 分离」（`docs/analysis/2026-09-06-remediation-execution-plan.md:363`）。
2. **kill switch 的地址不随 key 变。** 改由 profile 声明的账户标签派生。这是 live 代码的改动，另一个 PR。
3. **踩下开关不需要任何凭据。** 解除开关要操作者在场，但不以「环境里有交易 key」为前提。
4. **远端 kill switch 的凭据与交易 key 分离**（DL-Q8 ③，WP-R2）。取不到 lease 就本地 engage，进 reduce-only。
5. **IP 白名单与代理。** 到币安的路径经系统代理（`beidou_live/soak.py:7` 记着端口 1082）。
   白名单要登记的是代理远端节点的出口 IP。换了节点，交易所就拒绝这把 key，循环进 ERROR。
   这是朝安全方向的失败，但会丢 bar（M-PR03）。操作者 09-28 裁定「代理节点先不换」（分析 :1098）。
6. **变量名分开。** mainnet 的 key 用自己的变量名，profile 用 `api_key_env` 指名（今天 `config/live.demo.yaml:7-8`）。

## 6. 成本模型校准

### 6.1 原始记录

真钱之前要补冲击模型、k 要在它下面重推——这一条最早记在 P13 里（`docs/RESEARCH_LOG.md:1161`，2026-09-04）：

> 成本模型对任何下单量都收固定 7 bps，`max_participation` 根本不在回测里。在 demo 名义（〔略〕）下这是好近似；**真实资金前必须在含冲击的成本模型下重新推导 k**，这一条记为「范围之外」，不是「已完成」。

同一节预登记的降档表里有一行（:1198）：指标「实现滑点（30 天）」，阈值「> 10 bps」，动作「冲击成本模型必须先于真实资金完成」。
那道 10 bps 的门 09-07 已按 L1-04 退役，换成 M-Q08 的滑点不超过模型两倍（`config/live.demo.yaml:486-492`）。
第一段引文的英文版在 profile 注释里（`config/live.demo.yaml:59-62`）。
2026-09-08 把触发点写成了数字（`config/live.demo.yaml:64-70`，那一行标着「re-derive k at or above here」）。
同日 DL-C1 建了模型，并写明系数校准不了（`docs/RESEARCH_LOG.md:3721-3722`）：

> **先说清系数是什么**：它是假设，不是测量，而且这套系统**没法校准它**——唯一的成交是 demo 的〔略〕。`config/costs.yaml` 里写的 1.0 出自股票文献（Almgren 等），对本场所记 E5。

`config/costs.yaml:91` 至今写着：「Change this number only with fills large enough for it to be measurable, which demo cannot produce.」

### 6.2 为什么 demo 校准不了

- demo 的成交量是合成的，价格有偏差（`docs/ARCHITECTURE.md:105`，D-002）。
- demo 成交价对 mainnet 决策 bar 收盘的基差：|中位| 14.8 bps，p10/p90 −52/+37 bps
  （`docs/analysis/2026-09-07-exits-adaptive-tp-sl-deep-analysis.md:112`）。
- demo 规模下，模型冲击名义加权约 0.48 bps。它落在同一批成交滑点的 [2.01, 9.19] bps 区间里。
  原话是冲击「too small to separate from spread」（`config/costs.yaml:80-85`）。

### 6.3 校准方案：前 N 笔真实成交回归

- **样本。** L-B 的每一笔成交，用 M-Q08 同一把尺子：对 `decision_close` 的滑点（`beidou_live/engine.py:1033-1039`）。
  flatten 的成交单列，不进回归。
- **模型。** `s_i = a + c · x_i + e_i`。`x_i` 是系数取 1 时模型给这笔单的冲击，即 `σ_daily · sqrt(q_i / ADV_i)`（`config/costs.yaml:78`）。
  `c` 是要校准的系数；`a` 吸收半价差这类与单量无关的部分。
- **N 怎么定。** 先预登记 `c` 的目标标准误，用 `SE(ĉ) ≈ sd(e) / (√N · sd(x))` 反推 N。
  `sd(e)` 先用 demo 的离散度估（`config/costs.yaml:24-25`：p10/p90 −4.32/+29.40 bps）。
  头一段真实成交出来后换成真实值，再算一次 N。这条停止规则写进预登记，不看结果改。
- **零 ledger。** 回归只读成交记录，不给任何配置打分。
- **三种结局先写下。** 区间含 1.0：维持 1.0，记下更窄的区间。区间整体高于 1.0：容量曲线左移，C3 必须重读。
  区间整体低于 1.0：只记录，不因此加资金；加资金是另一次裁定。

### 6.4 资金上限怎么选

不写金额，只写选法。上限要同时满足四条：

1. **上界一：操作者愿意整笔亏掉的钱。** 证据是 WEAK_PASS（B13），09-05 只裁定它支撑 demo（§2）。
   k 0.175 的取值规则本身接受深回撤。它只要求自助 q95 回撤不越过声明的 −70% 预算（`config/live.demo.yaml:232-246`）。
2. **上界二：低于重推触发点**（`config/live.demo.yaml:70`）。高于它要先有校准过的系数，而系数正是 L-B 要量的。
3. **下界一：能分辨冲击。** 模型冲击随单量的平方根涨。资金是 demo 的 m 倍，`sd(x)` 约是 √m 倍。
   选最小的 m，让 6.3 的目标 SE 在操作者愿意等的天数内达到。
   换算天数用成交频率：2026-09-12 实测 7 天均值 4.3 单/天（`docs/analysis/2026-09-12-fill-frequency-deep-analysis.md:20`，当时的构造）。
4. **下界二：书的形状不走样。** 无交易带按权重计，不随资金变；随资金变的是交易所的最小名义额（`beidou_live/rebalancer.py:277`）。
   资金太小，很多单会低于这个下限而发不出去，换手偏离回测，M-Q08 的换手项失败。取让 M-Q08 换手比落在 ±25% 的最小资金。

下界高于上界时，结论是「L-B 校准不了」：写下来，停在 B。这是一个诚实的结局，不是故障。

上限落在两处：签字文件（§4.2）与 profile 的 `max_equity_usdt`（B8）。
`max_equity_usdt` 的设计：启动时权益超过上限就拒绝启动；运行中超过上限就只减不加。

### 6.5 k 的重推

- 形状照 P26：先写后跑，网格与读法写在前面，本次只测量，采纳另裁（RESEARCH_LOG:3785-3824）。
- 系数取 6.3 的点估计与区间两端，各跑一次。资金轴取 L-C 的目标资金与它的分档。
- 采纳新 k 是构造改动。M-010 的钟清零（`realised_vol` 自 2026-09-30 起按 k 折算、不再清零；L3 按 RESEARCH_LOG 的口径清零，见 B12），所以 C6 排在 C2 之后。
- P26 的两条教训照搬：它的控制行 1 写错过；argmax 落在网格边界，不能读成最优（RESEARCH_LOG:3831-3878）。

## 7. 回退

### 7.1 今天代码里的两条路

**拒绝启动**（`beidou live run`）：

- 真下单运行不带 `--armed` 就拒绝（`beidou_cli/live_cmd.py:283-286`）。
- armed 运行不许带 `--state-dir` 或 `--registry`（:296-299）。
- 证据门与数据集门有问题、又没带 `--allow-unvalidated`，就拒绝（:304-317）。
- 同一账户的第二个实例拿不到锁，以 0 退出并告警（:397-410）。
- 启动时六处拒绝（`beidou_live/engine.py`）：没有可交易的币（:455-456）、双向持仓模式（:462-464）、
  metrics 覆盖不够（:468-483）、spot 未核（:488-492）、保证金模式不符（:493-501）、`canTrade=false`（:505-506）。
- 熔断后以 0 退出，launchd 不再拉起（`beidou_cli/live_cmd.py:414-420`）。

**flatten**（`beidou live flatten`，`beidou_cli/live_cmd.py:687-737`）：

- 必须带 `--yes`（:693-694）。
- 先踩 kill switch，再平仓（:705-708）。开关写到指向这个账户的全部路径（:204-212）。
- `state.json` 读不动也照样平（:715-719）。
- 平仓取交易所仓位，受管的与外来的一起，下 reduce-only 市价单（`beidou_live/engine.py:1122-1140`）。
- kill switch 在时 guard 只放行降风险的写（`guard.py:43-49`、:92-93）。平仓单出得去，加风险的单出不去。
- 限制：flatten 要能建模型、读 registry 与 universe（`beidou_cli/live_cmd.py:696-699`）。
  不依赖这些的 `flatten --raw` 属于 WP-R2（DL-Q8 ④）。

### 7.2 mainnet 的回退顺序（设计）

从 L-B 退回 L-A，顺序写死：

1. `beidou live kill-switch --profile <mainnet profile>`：停止加风险。不走网络。
2. `beidou live flatten --profile <mainnet profile> --mainnet --yes`：平掉全部仓位。
3. 读交易所仓位，确认为空。
4. `launchctl bootout` mainnet 的 launchd 任务。
5. 开 PR 删掉 `governance/MAINNET_ENABLED`（HC-3，操作者合并）。

第 5 步必须在第 2 步之后。签字一撤，guard 在构造时就拒绝 mainnet（§4.1）。flatten 也就到不了 mainnet。
那时仓位平不掉，要重新签字才能平（§8 Q-M4）。

从 L-C 退回 L-B：调低签字文件与 `max_equity_usdt` 里的上限，重签。
资金划出是操作者在交易所的动作。会话不做任何资金划转。

## 8. 未决问题

每个都带选项与代价。未答的不执行；本文不替操作者选。

**Q-M1「没问题」由什么判**

- A：§3.1 的门全部满足，操作者签字。代价：不看任何盈利读数；最早 2026-10-27 之后。
- B：A 加 M-010 不劣于回测滚动 q10，即 09-08 定的「没炸」判据（RESEARCH_LOG:3662）。
  代价：日期同 A；30 天窗口的 Sharpe 标准误约 3.5（:3666），这一条几乎只挡灾难。
- C：A 加 M-G06：构造不变 ≥ 18 个月、归因 Sharpe 点估计 ≥ 0（:3662-3664）。
  代价：从重启 #60 算，最早 2028-03 下旬；任何构造改动再推 18 个月。

**Q-M2 真钱的证据标准：WEAK_PASS 够不够**

- A：够，与 demo 同。启动门今天就允许 WEAK_PASS（`config/alpha_registry.yaml:363`）。
  代价：09-05 只让 WEAK_PASS 支撑 demo（§2）；真钱建在 headroom 落在噪声里的证据上。
- B：要 PASS。代价：D-043 把 tsmom 今天的证据封顶在 WEAK_PASS，因为 5 个 fold 都选了同一格（:361-362）。
  拿 PASS 要一次真做选择的 validate：新预登记，计 ledger。拿到之前 L-B 不开。

**Q-M3 HC-3 要不要一道机械检查**

- A：只靠规则（本文 §4.4 与 D-PR06）。代价：KILL-08 的洞还在，能开 auto-merge 的会话就能合。
- B：CI 加一道 required check：PR 触碰 `guard.py` 或 `MAINNET_ENABLED` 就红。auto-merge 永远不触发，只能 admin 合并；
  `enforce_admins=false` 本来就留着这个口。代价：会话用同一个 gh 账号，`--admin` 对它一样可用；
  这道检查挡 auto-merge 与疏忽，不挡有意绕过。工作量估约 30 行 CI 与测试。
- C：签字文件不入库，放在 `~/Library/Application Support/beidou/`，由操作者手建；仓库只存它的 sha256。
  代价：偏离执行手册 §3.11 的路径；Mac 上的会话同样写得到那个目录。

**Q-M4 撤销签字时还有仓位怎么办**

- A：回退顺序写死（§7.2）。代价：顺序一错就平不掉，要重签才能平。
- B：guard 分两档：签字在时全放行；签字撤销但留一个撤销标记时，只放行降风险的写。
  代价：guard 多一个文件状态。HC-3 的 PR 更大，测试组合从 8 种变 16 种。

**Q-M5 mainnet 用专用子账户还是主账户**

- A：专用子账户，只给循环。代价：操作者要建子账户、划资金，都是操作者的动作。
- B：主账户。代价：B4 的空仓断言会挡住有手工仓位的启动；`live flatten` 会连手工仓位一起平（`beidou_live/engine.py:1127-1128`）。

**Q-M6 mainnet 账户的保证金模式**

- A：单资产 USDT（`multiAssetsMargin` 为 false）。代价：与 demo 证据的口径不同（demo 是 true，`config/live.demo.yaml:10-16`）。
- B：多资产，与 demo 同。代价：09-08 具名接受的顺周期放大器带进真钱。权益随抵押品涨跌，仓位同比例放大（RESEARCH_LOG:3891-3902）。

**Q-M7 要不要为 2026-10-27 重宣构造冻结**

- A：冻结到 10-27。代价：期间不能改构造，任何晋级都要等。
- B：不冻结。代价：任何构造改动都让 B9、B11、B12 的钟一起清零重算。

**Q-M8 M-Q09「≥ 30 天」用哪个读数**

- A：人为干预口径：`state.restarts` 30 天不变，且 0 次未处理失联。代价：失败周期不算在里面，要另看 M-PR03。
- B：`live status` 的「连续无故障无重启」（`beidou_live/health.py:108-123`）。
  代价：09-15 起 13 天里 5 个 UTC 日有失败周期（分析 :212，E-PR39 列的日期）。按天独立粗算，连续 30 天全无的概率在百万分之一以下。
  这 7 次失败全来自传输层：6 次代理 503、1 次 venue 错误（RESEARCH_LOG:18651）。换代理节点，操作者已推迟。
