# 冻结产物 · vol_target 阶梯（P32/P32b/P32c） — 供 Phase 7 独立对抗审查

冻结时间 2026-09-14。作者已完成 Phase 1–6。审查者：不要读作者的对话历史。

## 1. Problem（Phase 3 选定框定 F3）

操作者观察到实盘「自适应下单量少、资金占比低」，要求分析并优化。Phase 1 把三个框定并列后选定 **F3「要更高的
绝对收益」**：

- F1「资金利用率低」→ REFUTED：`max_gross 2.0` / `max_weight 0.15` / `max_participation 0.02` / `min_notional`
  在 272 个实盘周期上 **0 次咬合**；它们是护栏不是配额。
- F2「风险预算没兑现」→ REFUTED：合并书事前波动率 0.2590 vs 主书 0.3077（比 0.8416）属实，但**不转化为回撤
  折扣**：采纳 0.30 时主书 q95 = −43.7%(pit)/−49.5%(static)，本次在合并书上量到 −44.1%/−50.3%，略**差**。
- F3 成立并采纳。判据由操作者裁定为 **(乙) 更高绝对收益，回撤预算可重议**；随后裁定新预算 **−70%**，选定 **k=0.60**。

## 2. 定量链条（E1，实盘 272 周期，2026-09-03 → 09-13）

毛敞口/权益中位 0.5595；margin_usage 中位 0.1144（cap 0.40）；Stage-2 波动率标量中位 0.0767，全行同值
（跨币差 1e-17）；stage-1 毛敞口 ≈ 0.30×Σ(1/σ) ≈ 7.3，其组合波动率 ≈ 391%，标量把它压到 0.56。
**尺寸 100% 由 Stage-2 的波动率目标决定**；所有护栏零咬合。订单名义中位 209 USDT（权益 1.9%）。
权益 52.65% 是 BTC 抵押品，USDT 权益 5,110，按 USDT 算毛敞口是 1.15 倍。

## 3. Evidence Ledger（全 E1 除非标注）

- **E-001** `.beidou/live/cycles.jsonl` 272 周期，上节全部数字。
- **E-002** `beidou_alpha/portfolio.py:288-296` 两段定标；`build_weights` 的 scalar 未被 `max_scalar 3.0` 截断（实测 0.0767）。
- **E-003** 独立 EWMA 重算：主书事前年化波动率 0.3077（复现目标 0.30 到 2.6% 内），合并书 0.2590。
- **E-004** `scratchpad/vol_target_drawdown_bootstrap.py:61` 只算 `model.book_names[0]`（= 主书）；`beidou_alpha/model.py:88` 确认 main 在首位。
- **E-005 / P32** k 阶梯，registry 实书（tsmom + flow_short + exits + 书级护栏），两个 universe，
  block/draws/seed 与当初采纳 0.30 那次逐位相同。`scratchpad/p32-{pit,static}.json`
  | k | CAGR pit/static | q95 MDD pit/static | P(破50%) | liq touches | min margin buffer |
  |---|---|---|---|---|---|
  | 0.30 | 77.3/56.7% | −44.1/−50.3% | 1.2/5.3% | 0 | 99.10/99.40 |
  | 0.45 | 108.3/87.2% | −59.0/−63.1% | 20.1/30.3% | 0 | 97.67/98.89 |
  | 0.60 | 126.5/119.1% | −68.8/−69.7% | 53.4/58.6% | 0 | 97.18/96.62 |
  | 0.75 | 141.1/134.8% | −78.1/−76.1% | 81.7/79.2% | 0 | 94.75/94.78 |
- **E-006** 回测实现波动率 ÷ k = 1.15/1.13/1.12/1.09/1.06/1.00/0.95：EWMA 分母在低 k 上低估实现波动率约 12%。
- **E-007** 换手 0.30→0.60 ×1.24(pit)/×1.14(static)；成本占毛利 **下降** 8.00→6.95% / 12.25→9.77%。
- **E-008** 护栏可达性 0.30→0.60：`gross_capped` bar 412→3825(pit, 7.7%)、121→866(static)；
  `daily_loss_pause` bar 51→575 / 40→576（约 ×11–14）。两者**都已在 P32 的回测里回放**。
- **E-009 / P32b（本次最重要）** 实盘的 R8 归因回撤阶梯 **硬编码在 `beidou_governance/policy.py:173**
  `drawdown_ladder = ((-0.35, 0.225), (-0.50, 0.15))`，是 `@dataclass(frozen=True) Policy` 的字段，
  **不从 profile 的 `risk_budget` 块读取**（`engine.py:1466` 以无参 `RiskBudgetParams()` 调用）。
  `run_backtest` 只回放 `BookGuardParams`（max_weight / max_gross / daily_loss_pause），**不回放 R8**。
  重放 R8（`scratchpad/p32b_r8_ladder_replay.py`）：
  | k | CAGR raw→R8 pit | ΔCAGR pit | MDD raw→R8 pit | 被节流 bar 占比 pit/static |
  |---|---|---|---|---|
  | 0.30 / 0.375 / 0.45 | 不触发 | 0.0% | 不变 | 0% / 0% |
  | 0.60 | 126.5%→110.2% | **−16.3pp** | −39.8%→−40.3%（更差） | 7.5% / 0% |
  | 0.75 | 141.1%→108.1% | −32.9% | −48.1%→−47.4% | 29.1% / 3.1% |
  **k=0.60 是 R8 第一次变为可达的那一档**，且它扣掉 16.3pp 收益却让回撤略微变差 —— 纯 whipsaw。
- **E-010 / P32c** 把 R8 按同一条规则重标到 −70% 预算（deescalate_at=70%×预算、rollback_at=预算、
  deescalate_to=75%×k、rollback_to=50%×k → k=0.60 得 ((−0.49, 0.45), (−0.70, 0.30))）。
  `scratchpad/p32c-{pit,static}.json`：
  | k / 臂 | CAGR pit/static | q95 MDD pit/static | P(破50%) pit/static | 被节流 pit/static |
  |---|---|---|---|---|
  | 0.45 任一臂 | 108.3 / 87.2% | −59.4 / −62.3% | 19.2 / 28.5% | 0.0 / 0.0% |
  | 0.60 shipped R8 | **110.2 / 119.1%** | −70.8 / −69.9% | 57.5 / 55.8% | **7.5 / 0.0%** |
  | 0.60 rescaled R8 | **126.5 / 119.1%** | −69.9 / −69.9% | 55.2 / 55.8% | 0.0 / 0.0% |
  | 0.75 shipped R8 | 108.1 / 123.0% | −73.1 / −76.7% | 63.1 / 79.1% | 29.1 / 3.1% |
  | 0.75 rescaled R8 | 141.1 / 134.8% | −77.8 / −76.3% | 81.8 / 79.0% | 0.0 / 0.0% |
  **k=0.60 的 shipped R8 是刀刃**：pit 的样本内 MDD −39.8% 越过 −35% 那档并触发（付 16.3pp），
  static 的 −35.3% 恰好没越过宽限（付 0）。同一档 k、同一条规则，两个 universe 给出完全不同的代价。
  重标后两个 universe 都是 0% 被节流、q95 都是 −69.9%：**重标在 static 上不花钱，在 pit 上省 16.3pp。**
- **E-011** 今日实盘书按 k 线性外推到 0.60：毛敞口 0.5786→1.1572（cap 2.0）；BTCUSDT |w| 0.0738→0.1476，
  = `max_weight 0.15` 的 **98.4%**；margin 0.1085→0.2170（cap 0.40）。0 个币会被 max_weight 截断，但只差 1.6%。
- **E-012** 五个禁用策略全部携带 FAIL 证据；`alpha_registry.candidate.yaml` 自述今天不存在语义上不同的合法候选。
  一条真零相关、OOS 0.72 的新 sleeve 按 1/3 加入最多买到 +8.8% 收益（ρ=0.5 时反而是负的）。
- **E-013** M-002 实现波动率当前 **BLIND**（窗口内 3 个构造）；滑点 19/30 笔，未达门槛。
- **E-014** M-010 窗口自 2026-09-04T14:00 起未断，现 9.21 天；M-G06 判定日 2028-03-04（547 记录日）。
  改 k 是构造改动，两个时钟都清零。
- **E-015（限制，不是发现）** P32b/P32c 的 R8 重放用**回测的逐 bar 盯市净值**做 ruler；实盘的
  `attributed_drawdown_state` 只累计 income 行（已实现 + 资金费 + 手续费），**不含未实现盈亏**，
  且 blind 读数会 HOLD 住已生效的节流。两个偏差方向相反，净效应未量化。

## 4. Claims

- **C-001（P0, SUPPORTED）** 绝对收益 = Sharpe × 实现波动率；毛敞口不在等式内。Falsifier：存在不动 k、
  不动 Sharpe 却提高收益的构造改动 —— 未找到；配置自身实测 k 缩放使净 Sharpe 逐位不变。
- **C-002（P0, REFUTED）** 「合并书的 84.2% 是可白拿的余量」—— 见 F2。
- **C-003（P0, SUPPORTED）** CAGR 对 k 单调递增，两 universe 同向，但次线性（max_gross 2.0 截断）。
- **C-004（P1, SUPPORTED）** 硬边界不是爆仓：全网格 `liquidation_touches`=0，`min_margin_buffer` ≥ 94.75。
- **C-005（P0, SUPPORTED）** 在 shipped R8 下，k 从 0.45 抬到 0.75 **买不到收益**：pit 108.3 → 110.2 → 108.1。
- **C-006（P0, SUPPORTED）** 采纳 k=0.60 必须同时重标 R8，否则付 16.3pp 换到更差的 q95（−70.8% vs −69.9%）。
- **C-007（P1, UNKNOWN）** P26 已发表结论「net Sharpe 的 argmax k = 0.15，在每个资金档」**是 pit 独有**
  （脚本 `p26_vol_target_under_impact.py:53` 写死 pit）；static 上 argmax 是 0.60。k 用 Sharpe 判不出来。

## 5. Options（Phase 6）

| ID | 方案 | CAGR pit | q95 MDD pit | 需要改什么 | 可逆性 |
|---|---|---|---|---|---|
| O-0 | No-Build，维持 k=0.30 | 77.3% | −44.1% | 无 | — |
| O-1 | k=0.45，R8 不动 | 108.3% | −59.4% | profile 一行 | 一行 |
| O-2 | k=0.60，R8 不动 | 110.2% | −70.8% | profile 一行 | 一行 |
| **O-3** | **k=0.60 + R8 按同规则重标** | **126.5%** | **−69.9%** | profile 一行 + `policy.py:173` + 治理事务 | 两处 |
| O-4 | k=0.75 + R8 重标 | 141.1% | −77.8% | 同上 | 同上 |

**推荐 O-3。** 胜出理由：操作者已裁定预算 −70%，O-3 是唯一把 q95（−69.9%）对齐到该预算的方案；O-2 被 O-3
严格支配（少 16.3pp 收益、q95 反而更差）；O-1 是更低预算下的正确答案，若操作者改主意应回到它。
放弃理由：O-4 的 q95 −77.8% 超出已裁定预算 7.8pp。
**关键成立条件**：R8 重标必须与 k 同批次上线；只改 k 不改 R8 即落入 O-2。
**Falsifier（上线后 90 天）**：若 `risk_ladder.acting` 在 90 天内出现超过一次而归因回撤从未到过 −49%，
说明 ruler（E-015）与回测的盯市口径分叉，O-3 的定价作废。

## 6. Scope / Risk Register

In：`config/live.demo.yaml` 的 `portfolio.vol_target`、`risk_budget.{deescalate_at,rollback_at,deescalate_to,
rollback_to,vol_band}`、`beidou_governance/policy.py` 的 `drawdown_ladder`、按 k=0.60 重跑 `research validate`
让 registry 的 evidence 重新描述所交易的构造。
Out：`max_gross` / `max_weight` / `margin_cap` / 无交易带 / 参与率（零咬合，不动）；真实资金（09-08 裁定仍 Out）。

- **RISK-1** q95 −69.9% 且 P(破50%) 55% —— 已裁定接受，Owner 操作者。且 A-003：周块 bootstrap 破坏多月度
  regime 结构，所有 q95 **偏乐观**，真实熊市更长。
- **RISK-2** 顺周期抵押品放大器（权益 52.65% 是 BTC）随 k 同步放大；回测建模的抵押品是零。
- **RISK-3** `daily_loss_pause` 在实盘用**权益**判（`guards.py:73`），回测用书的净值判；权益含抵押品噪声，
  所以实盘触发会比回测的 575 根更多。
- **RISK-4** BTCUSDT 到 0.60 时是 `max_weight` 的 98.4%，行情一动就开始截断。
- **RISK-5** 选择污染：这是同一根轴上的第三次观察（bootstrap、P26、P32）。本次申报 **28 + 10 = 38 个新格子**
  进 `--prior-trials`。
- **RISK-6** 治理时钟清零（E-014）。
