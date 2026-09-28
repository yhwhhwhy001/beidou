# backtest-guard · 2026-09-29 独立体检（HEAD `9de77787`）

审查触发：操作者在项目线程里直接调用 `backtest-guard`，未限定范围。受检对象取「此刻在跑的那本书」：
k = 0.175 的 tsmom 主书 + flow_short sleeve + exits + book guards，构造 `e32f3856ac1e`（重启 #62，2026-09-28T16:29Z，
载入 G11），以及它引用的证据 `reports/research/tsmom-validation-20260925T143836Z.json`（verdict **WEAK_PASS**）。

与 09-25 那份的差别：本机有归档（`.beidou/data`）与实盘记录（`cycles.jsonl` 673 个 armed 周期），所以能跑测试、能读实盘读数；
仍不重跑任何回测、不花 ledger、不改代码。09-25 的 `54da72a` 到今天合入了 286 个 commit、源码 +5,397 行，第一遍以这段 diff 为主，
旧文件只复核模式与行号。此前四份同类审查（09-05、09-08、09-14、09-25）的结论不重复；仍成立的列在「遗留项」，已关闭的列在「已排除」。

工具说明：本次派出的四个子代理全部因账户周额度（HTTP 429）中断，第一遍的扫描由审查者本人完成，覆盖面以文末「读过的文件」为准。

版本说明：审查读的是 `9de77787`。写稿期间 main 前进到 `b74abb57`（#240–#243、#246：pre-push hook、`live_cmd.py` 的周报函数、
`store.py` 与 `bar_sanity.py` 的 docstring、重启 #62 的记录），本文引用的每一处行号都在 `b74abb57` 上重新核过，一处没动；
四道门在 `b74abb57` + 本文的树上全绿（2,988 passed，10 skipped）。

```
══════════════════════════════════════════
   策略体检报告 · backtest-guard
══════════════════════════════════════════
受检对象: beidou @ 9de77787（tsmom 主书 + flow_short，k=0.175，构造 e32f3856ac1e）
扫描文件: 约 40 个源文件与脚本 + 2 份证据 + 最新日报 + cycles.jsonl     代码行: ~46k（七个包）+ ~18k scratchpad

总评: 🔴 致命 0 项 · 🟠 高危 3 项 · 🟡 中 2 项 · 🔵 低 4 项  (含逻辑项 5 项)
判语: 「09-14 的方法错误与 09-25 的漂移偏差都修进了 k 的决策脚本，在跑的构造第一次按自己的尺子
        守住了预算。但让它上线的证据仍是一条没做过选择的尾巴；而唯一能裁 edge 的实盘时钟，
        24 天里被 15 次构造变更清零，一次都没跑满。」
```

---

## 第一遍 · 工程审查（均带 文件:行）

### [🟡 中] 崩盘窗口与 VaR/ES 没有在 k=0.175 上重放，日报里那台仪器已经变暗　※ 作者已披露一半

位置：
- `config/alpha_registry.yaml:226-230`：「k = 0.175 上没有重放过，所以这里不给 0.175 的数」
- `reports/daily/2026-09-28.md`「Tail beside the sigma ruler (G4)」：`status n/a`，「常量在 vol_target 0.6 下量得，profile 是 0.175：不换算，不计数」
- `scratchpad/g4_stress_windows_and_var_at_k060.py:91-98`：七个窗口，仍无 2025-10-10（09-25 🟡 的原项）

问题：registry 把「没重放」写下来了（披露）。没写下来的是二阶后果：G4 那一节是日报里唯一把实盘一日亏损放到回测尾部旁边读的仪器，
它在 0.175 上按设计沉默。于是 09-27 起，任何一天的亏损都没有「相对回测 VaR99 的倍数」这个读数。窗口表也还是 09-25 指出的那七个：
样本里唯一一次「从上升趋势直接砸下来」的小时级崩盘（2025-10-10）不在里面，而这本书 56% 的 bar 是全多头（日报 D-045 一节）。

修复方向：零 ledger。`g4_stress_windows_and_var_at_k060.py --to <今天>` 在 profile 的 k 上重跑一次，窗口表补
`("2025-10 tariff liquidation cascade", "2025-10-08", "2025-10-20")`，VaR99/ES99 常量按 0.175 重出并写进日报读的位置，
并单列「崩盘前 7 天书的净敞口」。

### [🔵 低] metrics 列：研究按「桶在 bar 收盘时已关」可得，实盘在收盘后约 27 秒读不到那个桶　惰性

位置：
- 规则：`beidou_data/metrics.py:183-196`（`align_to_bars`：bar 读「收盘时已关」的最新桶）；同文件 `usable_from_ms` 与
  `beidou_data/alignment.py:155-170` 明写「可得边界是桶的收盘，不含延迟；实测延迟 2–3 分钟属于健康报告」
- 实盘：`beidou_live/engine.py:383-396, 849`（每周期 `_snapshot_metrics`）、`beidou_live/composition.py:27-44`（同一条 `align_to_bars`）

问题：bar t（开盘 T）的研究值是关在 T+1h 的那个 5 分钟桶。循环在 T+1h+27s 左右跑（CLAUDE.md「重启实盘循环」），
而交易所要在桶关后 2–3 分钟才发布它（alignment.py 自述的实测）。所以实盘在决策时刻拿到的是前一个桶，研究拿到的是当根桶：
同一个 registry 参数，两边差一个桶。M-011 比的是两个 store 里**已经存下的**同一桶（`beidou_data/metrics_snapshot.py` 的
`metrics_parity`），不比「决策时刻模型读到的是哪个桶」，所以这条分歧 M-011 看不见。

为什么只判低：今天没有任何在跑的信号读 metrics 列（`beidou_live/report_data.py:52`；lsr_timing 09-28 REFUTED，mining 的
oi / lsr 叶子没有上线）。升级条件：第一个读 metrics 列的候选进 queue 那天，它就是陷阱 1.15「执行时点泄漏」，按高危读。

修复方向：不改可得边界（它是对的），加一个读数：每周期记「决策 bar 的最新桶开盘时刻」与「bar 收盘时刻」之差，印在日报
「数据族 parity（M-PR06）」旁；研究侧给 `align_to_bars` 一个可选的 `lag_buckets=1` 臂，候选进 queue 前两臂各跑一次。

### [🔵 低] 诚实漂移减在算完 overlay 之后的净序列上，exit 与日亏暂停没有在漂移后的路径上重放　方向未定

位置：`scratchpad/k_remeasure_honest_drift_20260925.py:214-217`（`mark - c`，`mark` 是 `p32h.panel` 交出的成品净收益）、
`:233-248`（阶梯逐 draw 重放，读的是减过 c 的 `m`、`r`）；`k_bisect_0175_20260925.py:126, 156-158`

问题：09-14 的致命项（对已节流序列做 bootstrap）在这条链上修好了——阶梯在每条重抽路径上重放（方法 B）。
但 6σ 止损 / 止盈、D2 + D3 的带、`daily_loss_pause` 都是在原漂移的路径上算完的，减常数不会让它们在更差的世界里多触发或少触发。
这是 09-25 审查自己给的做法，写在这里是为了标出它的边界，不是翻案。方向说不准：漂移更低时 exit 会多平仓，也会少拿到反弹。
幅度按 0.30 下的触发频率估计很小（`config/live.demo.yaml:347-348`：止损 5.6 年触发 18 次），但没有量。

修复方向：不值得单独重跑。下一次 k 或 exit 参数重出时，加一臂「按 sign 打折的收益面板」再走一遍 `apply_exits` 与 guards，
与减常数臂比 q95。

### [🔵 低] 资金费记在调仓后的仓位上（09-25 遗留，未动）

位置：`beidou_alpha/backtest.py:262`（`executed = decided.shift(1)`）、`:266-268`、`:294`；`beidou_alpha/panel.py:55-73`（结算 floor 到所在 bar）

状态：文件自 09-25 无 diff，读数与方向同 09-25：偏乐观，五年半合计在权益 1% 以下（推理）。新增一处相关事实：
carry_hedged 的预登记特意把决策放在 00:00 那根 bar，让「执行的那根 bar 不含结算」（`beidou_alpha/signals/carry_hedged.py:10-14`），
说明作者已在新书上绕开了这条，但主书的记账没改。

### 遗留项（此前审查已提，本次在 HEAD 上复核）

| 项 | 位置 | 状态 |
| --- | --- | --- |
| 🟡 CPCV embargo 50 < 特征回看 720 / 1442 | 证据 JSON `embargo: 50`；09-14 第三轮在 0.60 的 2 格上量到关门不动那道门（q05、`fraction_negative` 逐位不变） | ※ 已披露。0.175 那份仍是 2 格、embargo 50，量级按 09-14 的读法仍是零；网格变大时重量 |
| `liquidation_touches` / `min_margin_buffer` 恒等式 | 证据 JSON `full_sample.guards`：touches 0 / buffer 107.30；`beidou_alpha/backtest.py` 未变 | ※ 已披露（D-035 段）。真正的爆仓通道仍是 BTC 抵押品（今天占权益 43.0%，`cycles.jsonl` 最后一行 `collateral.share`） |
| 滑点 4.43 bps 那一格在门下 | 证据 JSON `slippage_stress_gate.slip4.43`：clears **true**，margin +0.182；成本 x2 的 margin +0.036 | **在 0.175 上关闭**：门下的是 0.60 的书（4,070 根 capped bar 拖低了 Sharpe），0.175 上 capped 0 根 |
| 720h horizon 在接缝后读假跳变 | 09-25 同日实测 7 个名字接缝后成员全 False | 已排除（09-25 后续），本次不重跑 |

### 已排除（命中可疑模式，读上下文后判为合法用法）

09-25 之后新增或改动的：

- ✓ `beidou_alpha/hedged.py:55` `ffill(limit=day)` 只向前填、最多一天；`:56` `shift(1)`；`:68-69` 合成价 cumprod 与 open 互推——两腿价差面板无前视；`:61-67` 价差 ≤ −1 直接抛错
- ✓ `beidou_alpha/signals/carry_hedged.py:76-77` `rolling(window, min_periods=window).sum()`，`:81` 上市时长用 `cumsum`（只数过去），`:90-92` 只在决策小时给分再 `ffill()`——因果；09-27 REFUTED（样本外 0.68 对门 1.03），未上线
- ✓ `beidou_alpha/signals/lsr_timing.py:86-87, 92, 99` 市场偏离取成员均值、`held_side` 用 `where(...).ffill()`——因果；09-28 REFUTED（1.28 对门 1.65）
- ✓ `beidou_alpha/mining/expr.py:449-452`（`oi` 的 `log().diff(window)`）、`:485-486`（`lsr` 的 `rolling(...).mean()`）、`:405-406`（`hod` 的组内 `shift(1)`）——全部只读过去
- ✓ `beidou_alpha/signals/chanlun.py:250-319` 单次前向遍历，分型由下一根合并 bar 确认（`:269-275`），4h 级别 `label="right", closed="right"` 再 `ffill`（`:381-383, 394`）；`tests/alpha/test_chanlun_scores_only_what_is_confirmed.py` 是截断检验。未启用
- ✓ `beidou_data/repair.py:19-20, 240-247`：不插值、不前填，只并入缺口内部严格在两端之间的行，源头与已存行不一致就整段不用
- ✓ `beidou_cli/research_feature_store.py:343-347` 键含面板全部字段的 digest 与 `beidou_alpha/**` 代码 digest，`:241-242` 进程内代码被改就整个停用——旧缓存进不了新数据
- ✓ G11：`beidou_live/guards.py:89-100` 只在实盘把 `max_gross` 乘 `min(1, usdt/equity)`，回测无抵押品时两个分母同一个数（`:6-8`，`tests/live/test_the_gross_cap_reads_the_usdt_balance.py`）；k=0.175 下目标 gross 最高是 USDT 的 0.699 倍（RESEARCH_LOG「risk-g11-denominator」一节），门 2.0，一根没截。若哪天截了，方向是实盘比回测保守
- ✓ WP-C8：`beidou_live/exits.py:129-131` 加 `state.direction != held` 后，cooldown 内「反向→翻回」两侧同语义；本次跑 `tests/live/test_the_live_overlay_is_the_backtest_overlay_on_one_symbol.py` 与 `tests/alpha/test_causality.py`：7 passed
- ✓ taker 页快照早记 5 分钟（09-12 至 09-27）：`beidou_data/metrics.py` 的 `REST_STAMPED_AT_OPEN`、`beidou_data/alignment.py:215-227` 的 `METRICS_TAKER` 已改；那 15 天里没有任何在跑的读者，是惰性的前视，已关
- ✓ k=0.175 决策链：`k_remeasure_honest_drift_20260925.py:233-248` 每条 draw 内重放阶梯（09-14 致命项的修法），`:220-221` 起点配对、`:224-236` 档位按可动用口径重标、零 ledger 断言 `:411-416, 465-469`；`k_bisect_0175_20260925.py:141-151` 对照 0.20 逐位
- ✓ 零 ledger 的「看」（#193 两个新数据族、#199 / #200 LS 叶）：规则先钉后看（`873b67b`），没有一个写法带进候选；lsr_timing 预登记按 658 笔申报 prior-trials（N=662），把看过的都计了价
- ✓ 资金费流水滞后窗口（#190）：实盘归因此前只记到约 1% 的 FUNDING_FEE 行（6/507），09-27 起全记——这是实盘账本修正，不是回测项；M-010 在 09-27 之前那段是偏乐观的
- ✓ 09-25 的十条「已排除」：`backtest.py`、`pool.py`、`features.py`、`overlays/exits.py`、`stability.py`、`validation/metrics.py`、`walk_forward.py`、`labels.py` 自 `54da72a` 无 diff，原判成立；`model.py` 与 `archive.py` 的 diff 只加了 hedged 拒绝（`model.py:92-97`）与日归档路径（`archive.py:76-78`）

### 偏差影响（方向判断，非收益预测）

没有一条是「回测看到了未来」。在跑的 k=0.175 在诚实漂移、可动用口径下 q95 是 pit −63.30% / static −65.60%
（`reports/research/k-bisect-0175-20260925.json`），在 −70% 内；这是 09-25 致命·逻辑项的关闭动作。
方向未定的两条：G4 在 0.175 上没有数，诚实漂移没有重放 overlay。资金费那条仍略偏乐观。metrics 延迟那条今天没有读者，不动任何数字。

---

## 第二遍 · 逻辑对抗审查（非代码证据）

本段结论来自对策略经济逻辑的质询，**不定位 文件:行**；引用的读数给出处。未获作者答复的项标「待作者答复」。

### [🟠 高危·逻辑] 唯一能裁 edge 的实盘时钟，24 天里被 15 次构造变更清零，一次都没跑满

维度：Ⅰ.4 衰减 / Ⅴ.4 复盘

依据（`cycles.jsonl` 全部 673 个 armed 周期按构造指纹分段，本次统计）：

- 09-04 以来 15 个构造指纹（含几次只活了一个周期的过渡），最长的一个活了 **8.8 天**（`b8f215ab706c`，09-18T17 → 09-27T13），
  中位 1.0 天；最近 7 天换了 4 个（`b8f215ab` → `4b2dc74b` → `2ee491c1` → `e32f3856`）。
- 日报 09-28：M-010 `INSUFFICIENT_DATA days=0.00`；衰减规则「不早于 2026-11-27」；M-G06「最早可判 2028-03-28」；
  `realised vol not enforced (窗口内有 8 个构造)`。
- 仓库自己的定位：KILL-006「实盘期就是 holdout」；`beidou_alpha/signals/tsmom.py` docstring「The live arbiter is M-010 income
  attribution over 30 days」；09-14 第三轮「M-010 的 30 天归因窗口是唯一的仲裁者」。
- 09-25 报告的 `oos_selection.power`：真实 Sharpe 1.2 时功效 0.196，1.5 时 0.433——仲裁者本来就弱，现在还从没开过庭。

失效场景（可证伪）：若构造变更保持 09-04 至 09-28 的节奏（中位一天一换），则 M-010 永远停在 `INSUFFICIENT_DATA`，
这本书会一直处于「靠一条无选择的尾巴上线、从未被实盘裁过」的状态；edge 若在衰减，没有任何仪器会先于回撤说出来。
证伪线：`e32f3856ac1e` 不变地活到 2026-10-28T16:00Z，M-010 给出第一个 z 值；在那之前再有一次构造变更，本条即命中一次。

注意每一次变更都各有其理由（k、尺子、G11、C8 都写了裁定），本条不评价任何单次变更；它评价的是**总量没有预算**。
`docs/MAINNET_READINESS.md` §8 的 Q-M7（要不要冻结到 10-27）正是这个决定，今天还开着。

请作者回答：构造变更的预算是多少——一个季度允许几次清零，M-010 才还算仲裁者？没有这个数，「实盘是唯一干净的样本外」这句话没有兑现日期。

### [🟠 高危·逻辑] 在跑的证据是一条没做过选择的尾巴；诚实读数只在 0.60 上量过，低于门 0.36　※ 作者已披露，二阶效应未披露

维度：Ⅰ.1 / 陷阱 2.1（仅样本内寻优）

依据：

- 09-25 报告：`grid_size 2`，`best_params_selected_by: full-sample argmax`，五个 fold 同选一格，`oos_is_full_sample_tail` 为真，
  D-043 封顶 WEAK_PASS（`config/alpha_registry.yaml:361-363`）。
- 唯一真做过选择的读数：09-19 的 16 格，各 fold 自选的混合 1.2306 对门 1.5945（N=335），差 0.36，verdict FAIL
  （`config/alpha_registry.yaml:322-349`）。0.175 上没有跑 16 格（`:365`）。
- 换指针本身是机器会拒的：「治理 §3 只收 PASS，这种换指针会在 D-020 上被拒；`governance replay` 按 D-043 归因这条拒绝」
  （`docs/ARCHITECTURE.md` D-043 段）。这是具名例外，披露得很清楚。

披露没盖住的两处：

1. **10-13 不再挡任何事。** `beidou governance calendar` 今天读：`deploy/run_live.sh:47` 的 bridge 到期「startup gate 今天干净，
   到期不改任何事」，状态 `inert`。09-18 写 bridge 时的前提是「两个指针都不通」；现在通了，通的方式不是证据变好，
   而是换到一份规则允许的封顶证据。09-25 准备文档第 1 节写的那道到期门，已经没有门。
2. **诚实读数在 0.175 上大概率仍在门下（推理，未跑）。** 同一配置自己的样本外从 0.60 的 1.53（09-19 那次的 `best_key_oos_sharpe`）
   到 0.175 的 1.83，+0.30 来自护栏不再绑定（capped bar 4,070 → 0，`config/alpha_registry.yaml:296` 对报告 `full_sample.guards`）
   与六天新数据，与选择无关。把同样的 +0.30 加到 16 格的 1.23 上约是 1.53，仍在 1.57 的门下，差距小于门随 N 的一次抬升——只有跑了才知道。
   唯一能把这句推理换成读数的动作是在 0.175 上跑一次 16 格：16 行 ledger，门约抬 0.01。`docs/MAINNET_READINESS.md` Q-M2
   （真钱要不要 PASS）今天还开着。

失效场景：若在 0.175 上跑 16 格得到的混合样本外低于门，则这本书按仓库自己的 D-028 是 FAIL，armed 启动合法的唯一依据就是那份封顶证据；
届时 D-043「WEAK_PASS 仍可上线」这条规则支撑的就不再是「弱通过」，而是「已知不过」。

请作者回答：跑不跑那 16 格？不跑，就把「demo 到 10-27 一直跑在封顶证据上」写进 Q-M2 的裁定，别让它以 inert 的姿态过 10-13。

### [🟠 高危·逻辑] 此刻的书仍分不清 edge 与 beta，新仪器把它量成了「一注」　待作者答复（09-25 遗留）

维度：Ⅰ.2 / Ⅳ.3 / Ⅳ.4

依据（日报 2026-09-28，均为「只报告不告警」）：

- Market beta（D-045）：窗口 21 天、491 根 bar；constant beta 1.05（t 8.86）；conditional alpha 1.19 bps/h（**t 0.55**），
  残差部分 **6.00%**；全多头的 bar 56.25%。
- Factor loadings：市场 0.90（t 15.11）、低波 0.14（t 5.93）、size 0.08（t 3.38）、alpha t 1.17；R² 0.92。
- Holdings correlation（#3.4，09-25 新仪器）：12 个持仓全多头，30 天加权两两相关 **0.65**，**effective bets 1.55 of 12**；
  与 BTC 的 30 天相关 0.38–0.86。
- `cycles.jsonl` 最后一行：gross 3,960.67 U 对权益 13,336.55 U，即 0.30×；`min_liq_distance.unreachable 12`。

k 从 0.60 降到 0.175 把这一注缩小到了原来的约三成，但没有改变它是一注：tsmom 自述的对手盘是「late leveraged long」，
而它此刻持有的就是一篮子高相关的多头。09-25 的问题原样成立。

失效场景：若未来 90 天市场横盘（没有趋势可跟），则书在翻多翻空之间付换手与滑点、拿不到方向收益；
若 M-010 第一次读数（最早 10-28）仍是 conditional alpha t < 1，则「实盘在验证一个 edge」这句话没有证据支撑。

请作者回答：什么读数能把 tsmom 的收益和「0.175 × 等权成员多头」分开？有了它，才谈得上「衰减」——分不开的东西衰不衰减无从判断。

### [🟡 中·逻辑] flow_short 的 10-03 复审没有书面判据，而复审要读的三个数都被 09-27 的清零归零了

维度：Ⅳ.4

依据：

- 复审日：`beidou governance calendar` 读 `config/alpha_registry.yaml:516`，2026-10-03 起日报报 REVIEW_DUE。
- 判据：09-12 裁定「不动，等 10-02 的 30 天复核」，并「明写不做：不给 M-014 设门」——复审是裁量（RESEARCH_LOG「操作者裁定两条：
  M-Q08 改判主书；探针留到 10-02 复核」一节）。
- 读数：日报 09-28 Probe books `flow_short pnl_30d=-3.06 (-0.02%)`、盯市 −1.24% / 155 bars；Probe correlation `n/a over 0 bars`；
  `cycles.jsonl` 最后一行 `probes[1].rows 3, stale_basis_rows 33`——同口径的归因行只有 3 行。
- registry `:509-510`：「2026-10-03 复审时该权衡的就是这条趋势：探针的理由每重出一次就薄一层」。

失效场景：若 10-03 复审时 P&L、停损 headroom、相关性三个数都读不出 30 天的量，则复审会默认续留——这 1/3 的探针预算继续占着
（RESEARCH_LOG「D-018 的回撤门按 EXP-AE2 在 k=0.175 上行使」一节：新书上线仍要等 flow 10-02 的复核），
而它的组合层理由已经是 REJECT、总书 delta 0.00。

请作者回答：10-03 那天，什么读数会让它退役？至少写一条，例如「同口径归因行不足 N 行即按 D-019 原始 30 天窗口顺延，顺延不超过一次」。

### [🔵 低·逻辑] Ⅴ 类运维：有实现；宿主离线的残余风险操作者已具名接受

维度：Ⅴ.1–Ⅴ.4

按第一遍规则给位置：

- kill switch：`beidou_exchange/guard.py:43`（`is_risk_reducing`，只放行降风险写入）、`beidou_cli/live_cmd.py:178-189`；
  连续失败熔断 `beidou_live/engine.py:163`（12 个周期）、`:728-729`（`breaker_stop`）
- 对账：`beidou_live/reconciler.py:118`（`startup_reconcile`）；每周期 registry digest 进心跳（`heartbeat.json`）
- 发现延迟：`deploy/com.beidou.check.plist:22-25` 每小时第 10 分钟巡检，心跳阈值默认两个周期（`beidou_cli/live_cmd.py:479`），即最坏约两小时
- 预案：`docs/MAINNET_READINESS.md` §5（密钥与 kill switch 分离）、§7（回退顺序）、§8（八个未决问题，都带选项与代价）——设计已写，启用不在范围
- 复盘：每次重启按 CLAUDE.md 写进 RESEARCH_LOG（#59 至 #61 都有）；日报「Restart cost」「Event risk」两节

09-25 的第三问（循环缺席那约两小时靠什么保护）已有答案：操作者 09-28 裁定「不额外建告警」，D-P4 的宿主离线残余风险回到 ACCEPTED
（RESEARCH_LOG「O-2、O-3 不做」一节）。本审查记录这个裁定，不重问。变化的是敞口：k=0.175 下裸露的是权益的 0.30 倍，不是 09-25 的约 1.0 倍。

### 不适用 / 已充分回答

- **Ⅱ 容量**：demo 资金，KILL-Q12 把真实资金挡在范围外。日报「Liquidity to close」：整本书一次完全平仓冲击 0.20 bps、
  最难平的名字参与率 0.0042%——demo 规模下不构成约束。真钱前的校准方案在 `docs/MAINNET_READINESS.md` §6。判不适用。
- **Ⅳ.1 伪装的卖波动率**：0.175 的证据 `full_sample`：skew +1.00、hit_rate 0.510、kurtosis 28.4——右偏、胜率过半，再次否证。
- **Ⅳ.2 carry**：主书不是 carry；carry_hedged 09-27 REFUTED，未上线。
- **Ⅴ.2 对账**：有实现且有记录（D-030、D-032），本次未见新问题。

风险评级: 🟡 中（demo）／ 🟠 高（真实资金前）
理由：在跑的构造按它自己的尺子第一次守住了预算，09-25 的致命项关闭；三条高危全在证据与仲裁那一侧——
上线依据是封顶证据、仲裁时钟从未跑满、收益与 beta 分不开。demo 资金下这些不改变今天的仓位；真钱前它们每一条都是门。

上实盘资金前（以及 10-03、10-27 两个日期前），作者必须回答的三个问题：

1. 构造变更的预算是多少？`e32f3856ac1e` 要活到 10-28 才有第一个 M-010 读数——这中间还打算换几次？
2. 在 0.175 上跑不跑 16 格（16 行 ledger）？不跑，就把「demo 一直跑在封顶证据上」写进 Q-M2，别让 10-13 以 inert 过去。
3. 10-03 那天，什么读数会让 flow_short 退役？三个数都被清零了，不写判据的复审会默认续留。

────────────── 建议优先级 ──────────────
先定构造变更预算（不花钱，Q-M7 就是它）→ 把 10-03 的退役判据写下来 → 决定 16 格跑不跑 →
G4 在 0.175 上重放并补 2025-10 窗口（零 ledger）→ 资金费对齐那一行搭下一次构造变更。
**三条高危·逻辑都有答案之前，不建议投入真实资金；demo 照跑。**
══════════════════════════════════════════

本报告是审查工具的输出，不构成投资建议；「偏差影响」与「失效场景」只给方向与条件，不承诺任何数字。

## 读过的文件（第一遍的覆盖面）

- 09-25 后的新增与 diff：`beidou_alpha/hedged.py`、`signals/carry_hedged.py`、`signals/lsr_timing.py`、`signals/__init__.py`、`signals/base.py`、
  `model.py`、`mining/expr.py`（叶子）；`beidou_data/repair.py`、`alignment.py`、`metrics.py`、`metrics_archive.py`、`metrics_snapshot.py`、
  `spot.py`、`live_feed.py`、`archive.py`、`binance_public.py`；`beidou_cli/research_feature_store.py`、`research_panel.py`；
  `beidou_live/exits.py`、`guards.py`、`composition.py`（metrics 列）、`config.py`（启动门）；`config/*.yaml`、`deploy/com.beidou.check.plist`
- 复核模式：`beidou_alpha/panel.py`、`backtest.py`（资金费行）、`signals/chanlun.py`（全文）、`signals/tsmom.py`（docstring）
- 决策脚本：`scratchpad/k_remeasure_honest_drift_20260925.py`、`k_bisect_0175_20260925.py`、`p32d_ladder_bootstrap_pathwise.py`（函数表）、
  `g4_stress_windows_and_var_at_k060.py`（窗口表）
- 证据与记录：`reports/research/tsmom-validation-20260925T143836Z.json`、`book-tsmom-flow-20260908T105322Z.json`（键）、
  `reports/daily/2026-09-28.md`、`.beidou/live/cycles.jsonl`（673 个 armed 周期）、`heartbeat.json`、`state.json`
- 文档：`CONTEXT.md`、`docs/ARCHITECTURE.md`（D-002 至 D-047）、`docs/GLOSSARY.md`、`docs/PREREGISTRATION.md`（第九项）、
  `docs/MAINNET_READINESS.md`（§8）、`docs/analysis/2026-09-25-october-13-readiness.md`（§1.4、§3、§4、§7）、
  `docs/RESEARCH_LOG.md` 2026-09-25 至 09-28 的 13 节、此前四份 backtest-guard 审计
- 跑过的：`tests/alpha/test_causality.py` + `tests/live/test_the_live_overlay_is_the_backtest_overlay_on_one_symbol.py`（7 passed）、
  `beidou governance calendar`（只读）
- 没做的：没有重跑任何回测或 bootstrap；没有对 chanlun 与 mining 叶子做本次自己的截断检验（子代理中断），依据是代码阅读与仓库既有的截断测试
