# backtest-guard · 2026-09-30 增量体检（`ec2d97a7`）

审查触发：操作者在项目线程里直接调用 `backtest-guard`，没有限定范围。受检对象与 09-29 那份相同，取此刻在跑的那本书：
k = 0.175 的 tsmom 主书、flow_short sleeve、exit overlay 与 book guards。构造 `e32f3856ac1e`，registry digest `7f8adb754962`。
它引用的证据已换成 09-29 重出的 `reports/research/tsmom-validation-20260929T023619Z.json`（WEAK_PASS）。

与 09-29 那份的差别：那份写在 36 小时前（`729ec703`）。此后 main 合入 73 个提交，源码、配置与证据共 39 个文件。
第一遍只审这段 diff。另外补三样此前五份审查都没做的事：

1. 读完 M-Q08 换手对照在日报里的全部读数（09-23 至 09-30）；
2. 在同一份 registry 下，拿研究侧重放逐 bar、逐币对照实盘的信号；
3. 核 10-03 到期的三件事各自清零哪只钟。

此前五份同类审查（09-05、09-08、09-14、09-25、09-29）的结论不重复。仍成立的列在「遗留项」，已关闭的列在「已排除」。

工具说明：这次不派子代理。账户周额度 10-03 才重置，09-29 派出的四个一出发就全断了。全部由审查者本人完成，
覆盖面以文末「读过的文件」为准。没有重跑 validate，没有花 ledger，没有改代码。唯一跑过的新东西是只读的对照脚本
`scratchpad/signal_parity_live_vs_replay.py`，随本文入库。

并行：同日「北斗量化交易系统审查」会话在做运行面体检，报告是 `docs/analysis/2026-09-30-system-audit.md`（写本文时尚未合入）。
分工：它管 launchd、告警、主机与时钟、10-03 的日历条目、#263–#273 的代码；本审查管回测口径与策略逻辑。
两处交叉的读数引自它，文中注明出处，本审查没有复核。

```
══════════════════════════════════════════
   策略体检报告 · backtest-guard
══════════════════════════════════════════
受检对象: beidou @ ec2d97a7（tsmom 主书 + flow_short，k=0.175，构造 e32f3856ac1e）
扫描文件: 09-29 之后改动的 39 个文件 + 证据 1 份 + 日报 7 份 + cycles.jsonl     代码行: diff +3,617 / −111

总评: 🔴 致命 0 项 · 🟠 高危 2 项 · 🟡 中 5 项 · 🔵 低 6 项  (含逻辑项 5 项；本次新增 5 项)
判语: 「代码里这次没找到前视：同一份 registry 下 307 根 bar，研究侧重放与实盘的 tsmom
        方向逐个相同。新的缺口在信号之后：唯一一次换手对照读 2.38 倍，从没判定过；
        10-03 到期的三件事，每件都会把这台仪器再清零一次。」
```

---

## 第一遍 · 工程审查（均带 文件:行）

### [🟡 中] M-Q08 换手：唯一一次实盘对照读 2.38 倍，从没判定过；证据的 headroom 只扛得住约 1.5 倍　※ 作者已披露一半

位置：

- 回测的换手只算目标权重的变化：`beidou_alpha/backtest.py:283-285`（`turnover = |executed.diff()|`）。
  持仓漂移不花钱，`:326-328` 自己写明了这条近似。
- 仪器：`beidou_live/execution_fidelity.py:16-19` 写明，口径管不到的差别正是 M-Q08 要看的；
  `:76` 要满 14 个完整日才判定；`:228-249` 的窗口按构造与 registry digest 一起切。
- 读数：09-23 那一行取自 RESEARCH_LOG，其余取自 `reports/daily/`（本机，不入库）。

| 日报 | 比值（实盘 / 同期回测） | 完整日 | 实盘 / 回测（倍权益） | 窗口内退出 实盘 / 回测 / 两边都有 |
| --- | --- | ---: | --- | --- |
| 09-23 | 2.07 ± 0.50 | 4 | 1.345 / 0.651 | 10 / 4 / 4 |
| 09-24 | 2.25 ± 0.53 | 6 | 1.806 / 0.803 | 12 / 5 / 4 |
| 09-25 | 2.37 ± 0.57 | 7 | 1.836 / 0.776 | 12 / 4 / 4 |
| 09-26 | 2.38 ± 0.56，日报标「可分辨」 | 8 | 1.878 / 0.788 | 12 / 4 / 4 |
| 09-27 至 09-30 | 读不出 | — | 构造换了三次；归档停在 09-28T16:00Z | — |

窗口是决策 bar 09-17T16:00Z 起，构造 `b8f215ab`（按别名即 `0c555e1c`），k = 0.60。

作者的解释与预测：09-23 把差额归到 6 次只在实盘发生的止盈，以及它们 24 根 bar 后的再入场。
拿掉这两部分，实盘 0.640 对回测 0.651。这 6 次的入场锚都是 09-13T20:00Z 平仓后重建那一根。
原文：「持仓轮换之后锚会更新，比值应向 1 回落。这是推测，要看满 14 个完整日之后的读数」（`docs/RESEARCH_LOG.md:15466-15474`）。
同日的方案文档把它定为「读数的解释，不是缺陷」（`docs/analysis/2026-09-23-optimization-plan-from-external-checklist.md:192`）。

问题：

1. **预测没被验证，仪器就失明了。** 09-23 之后的三次读数没有回落，只在实盘发生的退出从 6 次变成 8 次。
   09-27 起构造换了三次，日报一直读不出，从没判定过。`docs/MAINNET_READINESS.md:100`（B10）把换手项记成「未测」。
2. **信号不是原因。** 本次对照（见「已排除」第一条）在同一份 registry 下逐 bar 重放：tsmom 的方向 5,170 个币-bar
   全部相同。所以这 2.38 倍不来自信号方向，而在信号之后的几层：组合权重（本次没比）、exit overlay 的入场锚、
   对着漂移后持仓判的 no-trade band、漏掉的 bar。回测没有平仓重建这类路径事件，它的入场锚永远是连续路径上的那一套。
3. **证据的 headroom 对这个倍数很薄。** 证据自己的成本压力三点在一条直线上：x1 1.8257、x1.5 1.7148、x2 1.6039
   （`cost_stress_gate`）。每多 1 倍成本，样本外 Sharpe 约少 0.222。按这条线，门在约 2.14 倍处被穿过（线性推算）。
   实测单位成本约是模型的 1.41 倍：TCA 滑点 +5.64 bps 加入账手续费 4.26 bps，对模型的 7.0 bps
   （`config/costs.yaml:2-4`；日报 09-30「Per-order TCA」）。两者相除：实盘换手若长期高于回测约 1.5 倍，
   D-028 的 headroom 就归零（推理）。09-26 那个窗口是 2.38 × 1.41 ≈ 3.4 倍。按同一条线约 1.30：
   低于门 1.57，仍高于 D-020 的及格线 1.0（推理，未跑）。
4. **边界。** 读数在 k = 0.60 上量得。退出与再入场的换手随仓位等比例缩放，比值本身不随 k 变；
   no-trade band 的绝对臂 0.005 在 0.175 下更常绑定，这一半往哪边走说不准。成交都打在 demo 的合成盘口上，
   单位成本偏哪边也说不准。按 t 分位读：8 对、自由度 7、t = 2.36，95% 区间约 1.06–3.70。
   「实盘换手高于回测」可分辨，「出了 ±25% 的带」还下不了结论——14 天规则存在的理由就在这里。

修复方向（都不花 ledger）：

- 离线补完 `b8f215ab` 那段窗口（到 09-27T12:00Z，9 个完整日），把只在实盘发生的退出与再入场单列，看剩下的部分落不落在带内。
- 下一次重出证据时加一臂：入场锚按实盘记录里的平仓与重建事件重置，与连续路径那臂比换手和样本外 Sharpe。
- B10 的「未测」改成已有的读数与「未判定」的原因。这是文档漂移，属于并行会话的范围，已告知。

升级条件：`e32f3856ac1e` 满 14 个完整日后的判定读数若仍高于 1.5 倍，按高危读。最早约 10-13，还要等归档追上。

### [🟡 中] k = 0.175 的书在 entry_threshold 上更尖：±10% 两侧各掉 12%，k = 0.60 时是 6–7%

位置：`beidou_alpha/validation/stability.py:23-54` 在每个维度上下各扰动 10%。四份证据的 `stability.parameter_neighbourhood`：

| 证据 | k | 全样本 base | entry_threshold −10% / +10% | worst_neighbour_degradation |
| --- | ---: | ---: | --- | ---: |
| `tsmom-validation-20260913T182325Z` | 0.60 | 1.67 | 1.57 / 1.56 | 0.068 |
| `tsmom-validation-20260919T081914Z` | 0.60 | 1.67 | 1.58 / 1.56 | 0.063 |
| `tsmom-validation-20260925T143836Z` | 0.175 | 1.92 | 1.70 / 1.69 | 0.120 |
| `tsmom-validation-20260929T023619Z` | 0.175 | 1.92 | 1.70 / 1.69 | 0.120 |

问题：entry_threshold 动 10%，两侧都丢 0.22–0.23 的 Sharpe。这接近 D-028 headroom（+0.25）的全部。
k 从 0.60 降到 0.175 之后，同一个扰动的代价翻了一倍——选定点从缓坡变成了峰。这一维在 09-04 之前选过，
已按 prior-trials 172 计价；但邻域读数不进任何门，`beidou_alpha/validation/verdict.py` 不读它。
其余两维：return_scale 两侧 −6% / −5%；vol_window 三个点都是 1.92，±10% 动它看不出作用。

修复方向：下一次重出时，把 entry_threshold 的邻域放宽到 ±20%、±30% 并印出来，只报告，不拿来挑新值。
k 为什么改变敏感度也该量一次。推测与 D3 的入场线（绝对带的 2 倍，即权益的 1%）有关，未验证。

### [🔵 低] 信号参数的改动不清零证据窗口：docstring 说会，代码只看构造指纹　惰性

位置：

- `beidou_live/report_common.py:103-106`：「Every change to the book - a signal parameter, a band, a half-life - resets
  what the live record is evidence *of*」。同一函数 `:115-133` 只比 `canonical_construction`。
- 构造指纹不含信号参数，`beidou_live/engine.py:2355-2361` 自己写明；信号参数在 `registry_digest`（`:2269-2352`）。
- 读这个窗口的：衰减规则（`beidou_live/report_decay.py:328`）、M-G06（`:486`）、`beidou_live/reports.py:208, 513`、
  `beidou_live/report_risk.py:687, 814`。
- 重启纪律要跑的两个构造测试也只钉构造指纹（`tests/live/test_the_construction_is_frozen_until_the_holdout_matures.py:83-100`）。
- 来历：09-09 为「钉住交易总体」有意用了这条豁免：「M-010 只按 `construction_fingerprint` 清零，`registry_digest`
  不清零……所以这条路不动那个时钟」（`docs/RESEARCH_LOG.md:4583-4584`）。豁免同时盖住了信号参数，那一节没讨论这一半。

问题：一次只改信号参数的重启，M-010 会把两本书拼进同一个窗口，再拿新证据的期望去比。例如在 0.175 上按 16 格
选出另一格并采纳，就是这种重启。实盘记录里「registry 变、构造没变」发生过 6 次（09-08 至 09-15）。
那时 M-010 从没出过数，没有读数被污染。`7f8adb754962` 自 09-15T16:00Z 起未变。

修复方向：在 D-026 里二选一写明。(a) 证据窗口按（构造, registry digest）切，与 M-Q08 的 `comparison_window` 一致；
(b) 保留豁免，但只豁免 universe 与 probe stop，信号参数一变就清零。docstring 随之改成与代码一致。

升级条件：M-010 第一次出数（最早 10-28）之后，出现只改信号参数的重启。

### [🔵 低] flow 的分数幅度与研究侧重放有差，方向全对　需人工确认

位置：`scratchpad/signal_parity_live_vs_replay.py`，窗口同「已排除」第一条。

读数：flow 有 180 个非零币-bar，方向 180/180 相同；进出切换 8 次，两边同 bar 同名字；book 状态 307/307 相同。
数值有 96 格差超过 1e-9，最大 0.0253；
分数的量级约 0.2。差异集中在四个名字：TRUMPUSDT 60 格、ENAUSDT 21、CYSUSDT 8、LINKUSDT 7。
抽看 CYSUSDT 与 TRUMPUSDT，差值在连续 bar 上不变，相对差 10.3% 与 4.9%，像是入场那一根就分开了。

已知的一半：CYSUSDT 的归档停在 09-04，重放拿到的是旧数据（RESEARCH_LOG 09-23「`data sync` 漏掉了池子里的名字」）。
其余三个的成因没追。只判低，因为 flow 是 1/3 预算、只做空、book-level REJECT 的 probe，方向一致，只差幅度。
flow 若在 10-03 退役，这条随之作废。

### 遗留项（此前审查已提，本次在 `ec2d97a7` 上复核）

| 项 | 位置 | 状态 |
| --- | --- | --- |
| 🟡 CPCV embargo 50 < 特征回看 720 / 1442 | 证据 `cpcv.embargo` 一行 | ※ 已披露。仍是 2 格、embargo 50，未变 |
| 🔵 资金费记在调仓后的仓位上 | `beidou_alpha/backtest.py:262`、`:266-267`、`:295` | 文件只改了一段注释，读数与方向同 09-25：偏乐观，量级在权益 1% 以下（推理） |
| 🔵 诚实漂移没在 overlay 上重放 | `scratchpad/k_remeasure_honest_drift_20260925.py:214-217` | 未变，按 09-29 的建议搭下一次 k 或构造的重出 |
| 🔵 metrics 桶延迟 | `beidou_live/engine.py:2165-2177`（`metrics_refusal` 的 docstring） | 惰性：在跑的信号仍不读 metrics 列 |
| `liquidation_touches` 恒等式 | 证据 `margin_buffer` 一行 | ※ 已披露。真正的爆仓通道仍是抵押品重定价 |
| 单位成本：实测滑点高于模型 | 日报 09-30：主书 5.1 ± 2.0 bps（101 笔），全书 TCA +5.64 bps；模型 2.0 bps | 已定价：`slippage_stress_gate.slip5.5` 的 headroom +0.14。换手那一半见本遍第一条 |

09-29 以来关闭的：G4 已在 0.175 上重放（09-29 同日），日报「Tail beside the sigma ruler (G4)」今天印 0.175 的 VaR 与 ES。

### 已排除（命中可疑模式或新写的读数，读上下文或跑过之后判为合法）

- ✓ **研究侧重放与实盘的 tsmom 逐 bar 一致（本次跑）。** registry `7f8adb754962` 下的全部 armed 周期，09-15T16:00Z 至
  09-28T16:00Z（归档最后一根），共 307 根 bar。重放走 M-Q08 自己的入口：`ReplayInputs.from_profile`、循环逐 bar 记下的
  universe、`model.evaluate`。5,170 个非零币-bar 方向全部相同，其中空头 36 格两边都是 36；book 状态 307/307 相同。
  只比符号是粗的：tsmom 是 ±1，小的数据差很少能翻号。细的那一半看切换：连续 bar 上方向或进出的切换共 11 次
  （其中 1 次多翻空），两边落在同一根 bar、同一个名字上，11/11。研究若多看一根 bar，或实盘读了正在形成的那根，
  切换会错开一根。实盘在决策时刻只看得到过去，研究拿的是事后的归档，这段 bar 上两者没有分开。样本不大，结论只到这里。
- ✓ `beidou_alpha/validation/basket.py`（N2）：篮子按时点成员取（`beidou_alpha/backtest.py:413-417` 的 `where(membership)`），
  NW 带宽 48（`basket.py:33-35`、`:48-80`）；无条件相关不印，理由是 E-058（`:19-21`）。
- ✓ `beidou_alpha/validation/concentration.py`（N3）：份额用逐 bar 简单收益相加，留一法的近似与误差方向不明写进了 `BASIS`（`:22-26`）。
- ✓ `beidou_alpha/validation/walk_forward.py:214-227` 的 `stitched_oos`：每折的测试段取那一折自己选的配置，净收益逐行加总等于 `oos_returns`。
- ✓ `beidou_cli/research_validate_cmd.py:141-151`、`:438-440`：邻域现在扰动被搜的那一维，列表型的 `horizons` 列为 `not_perturbed`。
- ✓ `beidou_alpha/validation/metrics.py:136-167` 的 `payoff_ratio`、`cagr`、`calmar`：非有限值的 bar 不计时间，Calmar 的分母走同一条路径。
- ✓ `beidou_alpha/signals/xs_lowvol.py:56-63`：只用过去的已实现波动，在参照总体内排序；09-29 REFUTED，未上线。
- ✓ `beidou_alpha/backtest.py` 的 diff 只改了 `:288-292` 的一段注释。
- ✓ 日亏暂停：研究的重放（`beidou_alpha/backtest.py:352`）与实盘（`beidou_live/guards.py:86`）都按当日起点逐 bar 重判，不锁存。
  `config/live.demo.yaml` 那行注释 09-29 改成与代码一致（`6ae23a9c`）。
- ✓ `beidou_live/engine.py` 的停机补行（#265）只动记账，不动交易。
- ✓ 在跑参数的因果测试：`tests/alpha/test_signal_suite.py:93-118` 按 registry 里启用的条目参数化，tsmom 带 `crowding_window 72`，
  夹具默认带资金费（`:23`），flow 带 taker 列，逐位比对。
- ✓ `risk_budget.usdt_baseline` 只供日报读（`beidou_live/config.py:120` 的 `REPORT_ONLY_KEYS`）。

### 偏差影响（方向判断，非收益预测）

没有一条是「回测看到了未来」，本次对照给了实证。偏向乐观的有三处：M-Q08 那段窗口里回测换手低于实盘；
entry_threshold 是峰，点估计高于邻域；资金费记账略偏乐观。信号参数与证据窗口那条今天不动任何数字。
flow 的幅度差方向不定。证据的 headline 在模型成本上算：按实测单位成本 headroom 是 +0.14，换手那一半还没量到。

---

## 第二遍 · 逻辑对抗审查（非代码证据）

本段结论来自对策略经济逻辑的质询，**不定位 文件:行**；引用的读数给出处。未获作者答复的项标「待作者答复」。

### [🟠 高危·逻辑] 10-03 到期的三件事，每件都至少清零一只钟；09-29 的卡 1 仍未裁（更新）

维度：Ⅰ.4 衰减 / Ⅴ.4 复盘

依据：

- `e32f3856ac1e` 自 09-28T16:00Z 起没变，经过了重启 #63 至 #66（RESEARCH_LOG 09-29、09-30 四节）。这是 09-29 证伪线要的方向。
- 日报 09-30「未来 7 天日期翻转」列出 10-03T00:00Z 三件 pending：tsmom（main）的 probe 复核到期
  （`config/alpha_registry.yaml:407-412`）；flow 的 probe 复核到期（`:529-542`）；`probe-stop-caliber` 变 DUE
  （`governance/window_changes.yaml:16`）。
- **更正 09-29 的卡 1**：卡里写「`max_loss` 在构造指纹里（`beidou_live/engine.py:2240`）」。那一行在 `729ec703` 上是
  `registry_digest` 里的 `stop_of`，今天在 `:2295-2331`。构造指纹在 `:2355-2497`，不含 `max_loss`。
- 哪只钟看哪个摘要：M-010、衰减规则、M-G06 只看构造（`beidou_live/report_common.py:100-133`）；M-Q08 两个都看
  （`beidou_live/execution_fidelity.py:228-249`）。所以三件事的后果不一样：

| 10-03 的事 | 改的是 | M-010、M-G06、衰减 | M-Q08 |
| --- | --- | --- | --- |
| tsmom 主书 stop 重定阈 | `max_loss`，在 registry digest 里 | 不清零 | 清零 |
| `probe-stop-caliber` 应用 | 至少改两本书的 `max_loss` | 不清零 | 清零 |
| flow 退役 | `strategy_weights`，在构造里（`:2491`） | 清零 | 清零 |

- 记录钟还会被停机吃掉。日报 09-30 的 M-G06：「日历 1.88 天，其中 0.84 天记录没有覆盖」，最早可判 2028-03-28，
  「前提是循环不再缺」。
- M-Q08 对 `e32f3856` 的第一次判定最早约 10-13（09-29 至 10-12 这 14 个完整日，还要等归档）。10-03 只要改一次 registry，
  就推到约 10-18。

失效场景（可证伪）：若 10-03 分两次重启，先改止损、再定 flow 的去留，则 M-Q08 在 10-18 之前判不了。flow 若退役，
M-010 的第一个完整窗口从 10-28 推到 11-02 之后。证伪线：10-03 至 10-13 之间构造与 registry digest 都不变，
M-Q08 在 10-13 前后给出第一次判定。

请作者回答：09-29 卡 1 的 A（冻结到 10-28）、B（10-03 一次付清，之后 60 天不改）、C（不设预算）选哪个？
更正之后的价钱：止损重定阈不动 M-010，A 与 B 对 M-010 的差别只剩 flow 的去留；而每一次 registry 改动都清零一次 M-Q08，
它是第一遍第一条唯一的仲裁者。

### [🟠 高危·逻辑] 遗留：在跑的证据仍是没做过选择的尾巴，16 格没跑，Q-M2 仍开着

依据：09-29 按同一协议重出（RESEARCH_LOG 09-29「tsmom 证据重出」），`oos_is_full_sample_tail` 为真，D-043 封顶 WEAK_PASS，
headroom +0.2537。09-29 体检第二遍第二条与卡 2 原样成立；`docs/MAINNET_READINESS.md` §8 的 Q-M2 未裁。

本次只补一句：这 +0.25 是在模型成本上算的。按实测单位成本是 +0.14（证据 `slippage_stress_gate.slip5.5`）；
实盘换手若长期高于回测约 1.5 倍，它就没了（第一遍第一条）。

### [🟡 中·逻辑] edge 集中在低波动 regime：高波动三分位的样本外 Sharpe 0.91，低于 D-020 的及格线

维度：Ⅲ.1

依据（证据的 `stability.regime_split_sharpes`，只报告、不进判定）：

| regime（篮子 30 天年化波动） | 09-29 证据 | 09-25 证据 |
| --- | ---: | ---: |
| 低 0.40–0.71 | 2.97 | 3.06 |
| 中 0.71–0.88 | 1.44 | 1.35 |
| 高 0.88–2.23 | 0.91 | 0.93 |

- 同一条样本外序列按时间四等分是 1.66 / 1.75 / 1.66 / 2.22（`time_split_sharpes`），看不出日历效应。
  按波动分出来的梯度因此更像信号本身的性质。这是推理：切点取自样本内三分位，报告自己写明了「切点不是事前的」。
- 实盘这段落在哪：日报 09-30「Event risk」里篮子的 σ_1h 是 0.773%（720 根），折年化约 0.72。这是推算，
  和分段用的不是同一个函数。它落在中段的下沿。同日「Drift」一节：21.6 天实盘 Sharpe 8.14，期望 1.21。
- 09-25 那份证据就有这张表，前两次体检都没写进第二遍。

失效场景：若篮子 30 天年化波动升过 0.88，并维持一个 M-010 窗口，证据自己的分段说这本书约 0.9——低于 D-020 的及格线 1.0，
更低于门 1.57。M-010 拿无条件的 1.83 作期望，会把一个早已量到的 regime 效应读成衰减。反过来，实盘这 21 天落在它最好的
两段里，读数好不说明 edge 在高波动下也在。

请作者回答：M-010 与衰减规则的期望，要不要按 regime 条件化？高波动段偏弱，是 whipsaw 这类信号机制，还是少数几段行情撑出来的？

### [🟡 中·逻辑] flow 的 10-03 复审：还剩三天，退役判据仍没写（遗留）

维度：Ⅳ.4

依据：09-29 卡 3 未裁。今天的读数（日报 09-30「Probe books」「Probe correlation」）：`pnl_30d −3.06`（USDT 的 −0.04%），
盯市 −1.31% / 179 bars，M-014 相关 −0.07（28 根），days 28/30。registry 里 flow 仍是 book-level REJECT（`config/alpha_registry.yaml:526`）。
本次对照：flow 的方向与实盘 180/180 一致，幅度有差（第一遍 🔵 那条）。

并行会话在 k = 0.175 上零 ledger 重估了停损：现行 2.00% 在 flow 30 天 σ 的 1.06 倍处；`window_changes.yaml` 里排队的 7.5%
是 09-12 在 k = 0.30 上量的，已经过期（出处 `docs/analysis/2026-09-30-system-audit.md`，本审查未复核）。

失效场景：若 10-03 只做裁量复审、没有写下的判据，最可能的结果是默认续留。续留与退役都要一次决定，而退役会改构造（上一条）。

请作者回答：写一条退役判据。例如 09-29 卡 3 的 B：「M-014 满 30 天且相关 ≥ 0.5，或 30 天归因 P&L < 0，即退役」。

### [🔵 低·逻辑] Ⅴ 运维：09-28 接受的宿主离线残余风险，09-29 兑现了一次

依据：RESEARCH_LOG「重启 #64」一节：09-29 只有 5 根 bar 完成了周期，19 根没有（`docs/RESEARCH_LOG.md:20542`），
那段时间没有 exit 检查。并行会话核实：09-29T17:20Z 的 data job 赶上关机没跑，launchd 不补跑错过的定时，归档因此停在 09-28T16:00Z。

本审查只记它对两遍的影响：M-G06 少了 0.84 个记录日；M-Q08 与日报里几张读归档的表到今天读不出。裁定（D-P4 ACCEPTED）不重问，
运行面的细节归并行会话。

### 已回答（09-29 的一条高危·逻辑，本次关闭）

09-29 那条「此刻的书分不清 edge 与 beta」有两半，现在都有读数：

- 回测一半由 N2 回答：样本外 45,600 根，对时点等权篮子的常数拟合 beta −0.02（t −3.58），alpha 0.351 bps/bar（t 4.06），
  signal state 全多头只占 12.1%。样本外 5.2 年里，这本书不是被动的多头。
- 实盘一半由本次对照回答：09-15 至 09-28，实盘 90.9% 的 bar 全多头，重放在同一批 bar 上也是 90.9%，307 根逐根同态。
  实盘的「一注」是信号在这段趋势里本来的样子，不是实盘与研究分叉。

剩下的只有功效：30 天的窗口分不出 edge，这一点并进本遍第一条。

### 不适用 / 已充分回答

- **Ⅱ 容量**：demo 资金，KILL-Q12 把真钱挡在范围外。日报「Liquidity to close」：整本书一次完全平仓冲击 0.22 bps。判不适用。
- **Ⅳ.1 伪装的卖波动率**：证据 `full_sample`：skew +1.00、hit_rate 0.510、`payoff_ratio_bar` 1.02。右偏、胜率过半，再次否证。
- **Ⅳ.2 carry**：主书不是 carry。
- **Ⅴ.2 对账**：有实现，本次没见新问题。

风险评级: 🟡 中（demo）／ 🟠 高（真实资金前）
理由：代码侧这次有了实证（研究与实盘逐 bar 同号），09-29 的第三条高危关闭。两条高危都在证据与仲裁那一侧：
在跑的证据是封顶的尾巴，而能裁它的两台仪器（M-010、M-Q08）一直被构造与 registry 的改动清零。
demo 资金下这些不改变今天的仓位；真钱前每一条都是门。

10-03 之前（以及真实资金之前），作者必须回答的三个问题：

1. 10-03 那三件事，执行哪几件、分几次重启？每一次 registry 改动都清零 M-Q08，flow 的去留还清零 M-010（卡 1，按本次更正重算价钱）。
2. M-Q08 的 2.38 倍，是一次平仓重建留下的入场锚，还是实盘常态？零 ledger 就能答：离线补完 `b8f215ab` 的窗口并拆出退出。
3. flow 在 10-03 的退役判据写什么？（卡 3）

────────────── 建议优先级 ──────────────
10-03 之前：定卡 1、写 flow 的退役判据（都不花钱）→ 离线补完 `b8f215ab` 的 M-Q08 窗口（零 ledger）→
10-13 前后：读 `e32f3856` 的第一次 M-Q08 判定 → 下一次重出证据：加入场锚重置臂、放宽 entry_threshold 邻域、
按 regime 给 M-010 期望 → 真钱之前：卡 2 的 16 格。
**两条高危·逻辑都有答案之前，不建议投入真实资金；demo 照跑。**
══════════════════════════════════════════

本报告是审查工具的输出，不构成投资建议；「偏差影响」与「失效场景」只给方向与条件，不承诺任何数字。

## 读过的文件（第一遍的覆盖面）

- 09-29 之后的 diff：`beidou_alpha/backtest.py`、`signals/xs_lowvol.py`、`validation/basket.py`、`validation/concentration.py`、
  `validation/metrics.py`、`validation/walk_forward.py`；`beidou_cli/research_validate_cmd.py`；`beidou_live/engine.py`、
  `risk_budget.py`、`config.py`；`config/alpha_registry.yaml`、`config/live.demo.yaml`
- 复核：`beidou_alpha/backtest.py`（`run_backtest`、`_replay_book_guards`、`benchmark_returns`）、`signals/tsmom.py`（打分与 crowding）、
  `validation/stability.py`、`beidou_live/execution_fidelity.py`、`report_common.py`（`evidence_window`）、`engine.py`
  （`registry_digest`、`construction_fingerprint`）、`guards.py`、`config/costs.yaml`；测试 `tests/alpha/test_causality.py`、
  `test_signal_suite.py`、`test_shuffling_the_future_moves_no_weight_before_it.py`、`tests/live/test_the_construction_is_frozen_until_the_holdout_matures.py`、
  `test_construction_identity.py`（测试名）
- 证据：`reports/research/tsmom-validation-20260929T023619Z.{md,json}`；邻域与分段另读 `20260913T182325Z`、`20260919T081914Z`、
  `20260925T143836Z` 三份的 Markdown
- 实盘：`reports/daily/2026-09-24` 至 `2026-09-30`（本机，不入库）、`.beidou/live/cycles.jsonl`（709 行 armed）、`heartbeat.json`
- 记录：`docs/RESEARCH_LOG.md` 09-23 的 G9 两节、09-09「选 3」、09-29 至 09-30 的九节；`docs/MAINNET_READINESS.md` §8 与 B10；
  `docs/GLOSSARY.md`；此前五份 backtest-guard 审计
- 跑过的：`scratchpad/signal_parity_live_vs_replay.py`（只读，零 ledger）。在主 checkout 上用 `PYTHONPATH=$PWD` 跑，
  代码是 `e4d970e5`；与 `ec2d97a7` 的差别只在 hook、`SECURITY.md` 与 RESEARCH_LOG，不碰被调用的函数
- 没做的：没重跑任何回测、validate 或 bootstrap；没追 flow 另外三个名字的幅度差；M-Q08 的 `b8f215ab` 窗口没离线补完

## 后续（同日）：操作者四条裁定与修了什么

操作者读完本文说「修复所有发现的问题」。09-29 卡里归操作者定的项，本会话定了价再问，四条当场答
（RESEARCH_LOG「操作者四条裁定：backtest-guard 09-30 体检的四张卡」）：

- 10-03 一次付清，之后 60 天不改构造，也不改 registry；
- flow 10-03 直接退役；
- 在 k = 0.175 上跑 16 格，先写预登记；
- 证据窗口默认随 registry digest 清零，非信号改动声明豁免。

### 逐条处置

| 发现 | 处置 | 在哪 |
| --- | --- | --- |
| 第一遍 🟡 M-Q08 换手 2.38 倍 | 离线补完 `b8f215ab` 窗口：整段 2.09 ± 0.54；去掉只在实盘发生的 8 次退出与各自的再入场，剩 0.94 ± 0.13，落在带内。**关闭**，见下面的更正 | RESEARCH_LOG「M-Q08 的 b8f215ab 窗口离线补完」；`scratchpad/mq08_b8f215ab_window.py`；B10 的读数由并行会话在 #277 补上 |
| 第一遍 🟡 entry_threshold 的峰 | 16 格给了曲面：在跑的 horizons 上 0.20 → 0.30，全样本 Sharpe 掉约 0.5；换一组 horizons 方向相反。峰坐实，是 horizons 与 entry_threshold 这一对在赢 | #279 |
| 第一遍 🔵 信号参数的改动不清零证据窗口 | 按裁定实现：registry digest 一变就切；只改 universe 或 probe stop 的在 `REGISTRY_ALIASES` 里声明 | #280 |
| 第一遍 🔵 flow 分数幅度与重放有差 | flow 10-03 退役，这条随之作废 | #282（draft） |
| 遗留 🔵 资金费记在调仓后的仓位上 | 改成结算时刻持有的仓位。在跑配置全样本 Sharpe 1.915202 → 1.914861，5.66 年多付权益的 0.03% | #281 |
| 第二遍 🟠 10-03 的三件事与钟 | 按裁定：10-03 一次重启载入全部，之后 60 天不改。flow 退役的 registry 改动已备成 draft，由操作者 10-03 合并 | #279（裁定入库）、#282 |
| 第二遍 🟠 在跑证据是没做过选择的尾巴 | 16 格按预登记跑了：混合样本外 1.5840 对门 1.5984（N 357），FAIL；在跑配置自己的样本外 1.8029。registry 不动，读数写进 Q-M2 | #279 |
| 第二遍 🟡 regime | 日报「Edge decay」一节印出今天落在哪一段，只报告。今天约 0.72，在 mid 段 | #283 |
| 第二遍 🟡 flow 10-03 复审的判据 | 按裁定：10-03 直接退役 | #282 |
| 第二遍 🔵 宿主离线 | 运行面归并行会话（`docs/analysis/2026-09-30-system-audit.md`，#277） | — |

### 更正：第一遍第一条

原文说「预测没被验证，仪器就失明了」，并推算「按实测单位成本，实盘换手长期超过回测约 1.5 倍，D-028 的 headroom 就归零」。
离线补完之后，前半句不成立：窗口的最后一天，比值已从 2.38 回落到 2.09。只在实盘发生的 8 次退出与再入场拆掉之后，
比值是 0.94 ± 0.13，落在带内。09-23 把差额归到退出路径，这个解释在整段上成立。所以后半句的前提（换手长期高出 1.5 倍）
在这个窗口里没有出现。证据真正要扛的是单位成本（约模型的 1.41 倍），它已由 `slippage_stress_gate` 定价，headroom +0.14。

原文不改，以本节为准。第一遍这条由 🟡 改为已关闭。仍然成立的一点写进了研究日志：实盘每次平仓重建都会让入场锚与回测分开一段，
那段时间里单边退出的换手回测看不见；本窗口一次重建约 1.1 倍权益的换手。

### 还开着的，要操作者定

1. **flow 退役后的治理记录。** `governance_state.json` 里 flow 仍是 probe，占一个 probe 位。在 #282 的分支上干跑
   `governance advance`，读出「state already matches the record; nothing to write」：状态机进 RETIRED 只有三条路
   （R7 三条命、P&L 止损、family gate），没有「操作者退役」。可选：加一个操作者退役事件（改治理规则）；在 transactions
   里手记一笔；维持现状，等 60 天冻结后再议。
2. **M-010 与衰减规则要不要按 regime 条件化。** #283 只把 regime 印出来。要改规则，就得决定期望取哪一段、切点按样本内三分位
   还是事前固定。
3. **10-03 的执行本身。** #282（flow 退役，draft）与并行会话的 main 止损重定阈放进同一次重启，步骤写在 #282 的描述里。

## 后续（10-01）：三件开着的事已答复并落地

上一节末尾那三件，本会话 10-01 按价钱问了，操作者答（RESEARCH_LOG「操作者三条答复」一节，随 #287 入库）：

| 事 | 操作者答复 | 落地 |
| --- | --- | --- |
| 1. flow 退役后的治理记录 | 加「操作者退役」事件 | D-048：裁定写进 `governance/rulings.jsonl`，`advance` 按时间折叠成 `OPERATOR_RETIRE`。#287，改的是治理规则，由操作者合并；用法是 #282 的 10-03 清单第 6 步 |
| 2. M-010 与衰减规则按 regime 条件化 | 现在就条件化 | D-049，#288（10-01T15:28Z 合入）：M-010 的期望按实盘各段占比混合，衰减规则按整窗起点所在段取 q10。证据按预登记重出，`--to` 钉回原桶，0 个新 trial；三段 q10 低 −0.02 / 中 −2.67 / 高 −1.59 |
| 3. 10-03 的执行 | 操作者自己执行 | 按 #282 的步骤。#282 已合入含 #285、#288 的 main，candidate 重新生成；它的 CI 有一条 #277 守卫的预期内红，补救属于 10-03 那批 |

### 更正两处

- **上一节第 1 条说状态机进 RETIRED 有「三条路」（R7 三条命、P&L 止损、family gate），不对。** 在 main 上核过代码，只有一条：
  probe 在 R7 的次数用完之后碰到 P&L 止损（`beidou_governance/lifecycle.py` 的 `PNL_STOP` 分支）。family gate 失败只把 main
  降回 probe，不退役。那一条的结论不受影响：状态机里没有操作者退役这条路，D-048 补的就是它。
- **逐条处置表 regime 那行的「今天约 0.72，在 mid 段」，是 #283 用近似篮子读的。** 按研究自己的口径（D-049 的
  `regime_state`：pit 成员等权篮子、30 天、读到 t−1，成员取循环每根 bar 记下的 universe），同一天读 0.675，在 low 段。
  #283 的近似还有一处缝隙误判：落在两段之间的值会读成「高于最高段上沿」。#288 改读研究口径，两处一起修掉。

原文不改，以本节为准。
