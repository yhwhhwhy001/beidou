# 深度分析：因子挖掘及其使用——门的分母数的是「看了多少次」，而那是假设数的 4.04 倍（Need Evidence）

> 分析头部（deep-analysis V3.7）
> - Interaction Mode: **Yellow**。关键事实全部来自代码与已提交的研究报告；无外部来源。
> - S/M/L: **L**。命中三维：风险（资金／实盘 registry／append-only 账本不可撤回）、AI/策略（DL-G8 自主循环会自行排 MINE）、功能模块（mining + validation + governance + cli ≥3 模块）。
> - 当前 Gate 决策上限：**Need Evidence**（H3：C-001 由 REFUTED 降为 UNKNOWN；同时命中 H2 / H1 / H5 / H7 / H9，取最低者）。
> - 外部动作授权：**无**。本轮只读代码与既有报告，未跑 `mine` / `validate` / `book`，未写 `trials.jsonl`，未改 registry 与实盘配置。
> - 缘起：操作者提问「策略对应的因子是固定的吗？是写死的还是能挖掘生成的？」。查证之后，真正需要决定的是挖掘这条路径的存废。
> - 操作者前置裁定（输入，非结论）：Q1=C（两套指标）、Q2=B（`594a12f9` 的 PASS 按当前 N 失效）、Q3=授权为 N_eff 实验定价、Q4=C（并行分析 D-018 回撤条款）。
> - **本轮结论的一半被自己的 Phase 7 对抗审查推翻。** 被推翻的部分与推翻它的证据都保留在 §6，不删改。

---

## 0. Decision Memo

| 项目 | 结论 |
| --- | --- |
| Final Decision | **Need Evidence**。需要的不是一次实验，是一次**计费口径裁定**（§7），且它是人类确认点，不得由分析或代码默认 |
| 本轮最重要的发现 | **`mined` 桶的 2731 行只含 676 个不同表达式（4.04×）；其中 1316 行（48.2%）新增了零个假设。而 D-028 的门越过史上最好候选（OOS 1.7862）的那个交叉点 N=2170，正落在两轮"零新增"的中间。** 三周前项目为同一形状裁定过一次相反的结果（Q7 回退 514 行，理由「没有产生任何新的选择自由度」），09-09 这两轮从未被裁定 |
| 第二重要的发现 | 在 1h 生产采样上，**676 个表达式没有一个的 standalone 越过在跑的书**（最好 1.7144 对基线 1.8184）。这一条独立于 N 的口径，是本轮唯一不受争议的负结果 |
| 被推翻的自己的判断 | C-001「挖掘路径今天仍能产出可上线候选」由 REFUTED **降为 UNKNOWN**：它的 Falsifier 在四种可辩护的 N 口径里有三种不成立 |
| 维持的判断 | C-002「候选供给是瓶颈」**REFUTED 维持**（P17 之后第二次）。它不依赖 N 的口径 |
| 最大限定 | 全文 §1 回答的「因子是不是写死的」是描述现状，**不是**对该设计的评价；`register()` 拒绝非 `mined_*` 的运行时注册是刻意的安全属性 |
| 交付状态 | 分析完成，**不得进入开发**（H9：G7 FAIL）。§7 的五项裁定请求交操作者 |

---

## 1. 先回答提问本身

| 问题 | 答案 | 证据 |
| --- | --- | --- |
| 策略共用一套固定因子吗 | **否。** 共享的只有底层原语——`beidou_alpha/features.py` 约 20 个因果函数（returns / realized_vol / atr / donchian / robust_zscore / cross_sectional_rank / taker_buy_ratio / funding…） | E-003 |
| 每个策略对应不同因子吗 | **是。** 9 个信号模块各自在代码里算自己的因子：tsmom 读多周期收益+斜率+vol；flow 读 taker-buy 失衡×成交量扩张；breakout 读 ATR-归一 Donchian；carry 读资金费；residual 读对 BTC 的 beta 残差；chanlun 读分型/笔/段/中枢；pairs 在打分前先做配对搜索 | E-001 |
| 因子写死吗 | **组合方式写死在代码里，只有数值参数在 YAML。** `alpha_registry.yaml` 能配的是 `horizons` / `vol_window` / `entry_threshold` 这类数字；换因子＝改代码＋重走一遍验证。`signals.register()` 明确拒绝任何非 `mined_*` 前缀的运行时注册 | E-001, E-002 |
| 能挖掘生成吗 | **能，路已修好。** `beidou_alpha/mining` 是一套带量纲类型的表达式语言（20 个节点），`enumerate_candidates()` 默认枚举 676 个候选，`to_signal()` 把表达式编译成普通 `SignalSpec`，之后被**同一套** walk-forward / CPCV / 试验账本 / D-020 判决审判 | E-005, E-013 |
| 挖出来的用上了吗 | **一个都没上。** 8 份 shortlist、914 个表达式（1h 676 + 1d 238）、7 个进 validate（1 PASS / 6 FAIL），唯一的 PASS 在 `research book` 被回撤条款挡了四次。实盘跑的仍是 tsmom（main）+ flow（flow_short sleeve） | E-002, E-006, E-011 |

registry 现状：7 条策略，2 条 enabled。`chanlun` 与 `pairs` 在 `SIGNALS` 里但不在 registry。

---

## 2. Evidence Ledger

全部 E1（代码、已提交报告、可复现复算）。复算一律用 `.venv/bin/python`（3.12；系统 python3 是 3.9，导入 `StrEnum` 会失败）。

**E-001** `beidou_alpha/signals/__init__.py` — 9 个手写信号。`register()` 只接受 `mined_*` 前缀，其余抛 `ValueError`。

**E-002** `config/alpha_registry.yaml` — 7 条策略，2 条 enabled：tsmom（main）、flow（flow_short，fraction 0.3333）。

**E-003** `beidou_alpha/features.py` — 约 20 个因果原语；所有信号（含 mined）都建在其上。

**E-004** `reports/research/trials.jsonl` — 2971 行；`mined` 桶 2731、tsmom 132、flow 46、meanrev 27、breakout 9、residual 8、pairs 4、carry 4，另 10 行散在 7 个 `mined_<hash>` 键。

**E-005** 8 份 `mine-shortlist-*.json` — `evaluated` 合计 3461；折叠后 914 个不同表达式，**其中 1h 676 / 1d 238，两者重叠为 0**。

**E-006** 7 份 `mined_*-validation-*.json` — 1 PASS / 6 FAIL。PASS 的是 `mined_594a12f9307a15d9` = `cs_rank((ret(336) / semi(ret(1), 168)))`。

**E-007** `mined_594a12f9307a15d9-validation-20260906T175515Z.json` — OOS Sharpe **1.786223252863809**；`oos_selection`：`n_trials` 575、`threshold_annual` **1.645341936067292**、`variance` 2.200184145624143e-05、`n_obs` 45072、`p_family` 0.0135；`fold_consistency` 1.0；`cpcv.fraction_negative` 0.0 / `q05` 1.268；`cost_stress` x1 1.7312 / x1.5 1.3711 / x2 1.0111。
**限定（K-03）**：该报告 `ledger_rows: 0`，575 来自手工申报的 `prior_trials_declared: 574`；`mined` 桶的首行是 2026-09-07T04:37，**比报告晚 11 小时**。575 与今天的 2731 不是同一条自动累计的曲线。

**E-008** `beidou_governance/policy.py` 0.3.1 注释（作者自记）— 该轮使桶 2488→2731，「D-028 gate at that bucket goes 1.7990 → 1.8085」。

**E-009 · 门的复算** 用 E-007 自己的 variance 与 `bars_per_year=8760`：

```
.venv/bin/python -c "
import math
from beidou_alpha.validation.multiple_testing import max_sharpe_quantile
var=2.200184145624143e-05; scale=math.sqrt(8760)
for n in (575,676,1415,2073,2170,2731): print(n, round(max_sharpe_quantile(n,var,0.05)*scale,4))"
```

N=575 的复算与 E-007 报告自记的 `threshold_annual` **逐位相同**——这是对整套方法最强的内部验证。
派生读数：`sampling_variance` ≈ 1/(n_obs−1)（2.2002e-05 对 1/45071 = 2.2187e-05），小时频下 Sharpe 项可忽略 ⇒ **这道门近似只由样本外长度与桶内 N 决定，不随候选变好而让路**。E-008 与本复算在同一 N 上相差 0.0012（0.13%），印证 A-001。

**E-010 · 空间读数（按采样分开，K-02 修正后）**

| 采样 | 不同表达式 | 正**等权双流**边际 | standalone > 当轮基线 |
| --- | ---: | ---: | ---: |
| **1h（生产口径）** | **676** | 24 = **3.6%** | **0** |
| 1d（P19 已判 C-004 REFUTED 那轮） | 238 | 30 = 12.6% | 1 |

1h 上最好的 pit standalone 是 `594a12f9` 的 **1.7144** 对基线 **1.8184**。取法稳健性：按「每 hash 最好边际 / 最好 sharpe / 首次 / 末次」四种取法，`standalone > baseline` 的计数恒为 1（皆落在 1d）。

**E-011 · 书门（口径：fraction 0.209、pit、OOS）** `book-tsmom-mined_594a12f9307a15d9-20260912T185333Z.json`，`book_verdict` REJECT，六项检查五项过，唯一挂 `oos_mdd_worsening`：

| `universes.pit.by_fraction` | Δ OOS Sharpe | `oos_mdd_worsening` |
| --- | ---: | ---: |
| `0.2000` | +0.2820 | 0.013031 |
| **`0.2090`（报告自己的 fraction）** | **+0.2931** | **0.025509** |
| `slippage_stress.slip5.5` | +0.2655 | 0.028899 |

允差 `rule.max_oos_mdd_worsening` = **0.0100** ⇒ 在声明口径下超标 **155%**（slip5.5 下 189%）。
**同报告 static 面 `0.2090`：Δ +0.2456，`oos_mdd_worsening` −0.017415（回撤改善）。跨 universe 符号翻转。**

**E-012 · 规则级发现（口径：fraction 1/3、全样本、**pit** universe）** `docs/RESEARCH_LOG.md:2811-2870` — D-018 在两个**不同风险水平**之间比较：主书 vol 32.24% → 总书 35.66%（+10.6%）。缩回同波动率后 Sharpe **+0.288 一分不少保住**，回撤代价 **+4.27pp → +1.87pp**，仍 >1pp。原因是**尾部同向**：全样本相关 0.236，但最坏 90 天主书 −22.67% 而 sleeve×1/3 −6.27%，主书与总书的 MDD 同在 2023-05-03。该节 §五 已裁定：改不改规则是操作者的事，且**只适用此后候选**。
**更正**：本文初稿把该节口径写成 static，错的——数值指纹（1.8204 / −23.68% / 1.7301）逐位匹配 **pit**。

**E-013** `ledger.py:148-165` — `ledger_scope("mined_X") = (X, "mined")`。mined 候选吃整个共享搜索桶；手写策略只吃自己的键。

**E-014 · 分母的组成（决定性）**

```
mined 行数 2731  |  distinct param_key 676  |  重复率 4.04×

run_id 时间              行数   新增表达式   累计表达式   累计行
2026-09-07T04:37         514        514         514      514
2026-09-09T08:29         658        144         658     1172
2026-09-09T09:50         658          0         658     1830   ← 零新增
2026-09-09T17:25         658          0         658     2488   ← 零新增
2026-09-10T08:58         243         18         676     2731
```

签名的上下文维度（`range_end` × `symbols` × `construction_digest`）共 5 组，是 4.04× 的来源。这不是 bug：`signature` 与 `_construction_digest` 的 docstring 都为它辩护，`policy.py` 0.3.1 自记「225 of the 243 were scored on 09-09 and are charged again」，`research_cmd.py:3160` 自记「conservative by design (KILL-Q5)」。但同一文件 3128 行写着相反方向的原则：「A candidate examined in a 267-wide search and again in a 514-wide one is **one hypothesis looked at twice, not two**」。

**E-015 · 门在四种口径下的读数**

| N 的口径 | N | 门 | 对 1.7862 | 判定 |
| --- | ---: | ---: | ---: | --- |
| ① 行数（今天的规则） | 2731 | 1.8096 | −0.0234 | FAIL |
| ② 剔掉两轮零新增 | 1415 | 1.7420 | **+0.0442** | PASS |
| ③ 只剔重复构造那一轮 | 2073 | 1.7815 | **+0.0047** | PASS |
| ④ 不同假设数 | 676 | 1.6631 | **+0.1231** | PASS |

交叉点 N=**2170**，落在 1830 → 2488 那一轮之内，而那一轮新增 0 个假设。

**E-016 · 抬门速率（K-23 修正）** 无任何一轮产生过 676 行；实际是 514 / 658 / 658 / 658 / 243，对应抬门 +0.0174 / +0.0217 / +0.0217 / +0.0217 / +0.0086。增量在 N 上**单调衰减**：2731→3407 为 +0.0222，4083→4759 为 +0.0151。满负荷（R1 允许 4 轮/窗口）12 个月合计约 **+0.24**。

**E-017 · 账本可被环境变量整体重定向** `ledger.py:27,52`：`BEIDOU_TRIALS_LEDGER` 非空即返回该路径。`resolve_ledger_path` 的 docstring 自述 `out` 被「accepted and ignored」——CLI 的口被堵死了，环境变量没有。`research mine` 的选项全集只有 `--reauthorize --top --max-complexity --max-lookback --baseline --baseline-params`，无 `--no-ledger` / `--dry-run`。

**E-018 · N_eff 的禁止令是既有裁定，不是空白** `effective_trials` 的唯一非测试调用点是 `grid_effective_trials`（候选自己的网格）。其 docstring：「Reported, never substituted into the gate … Lowering a bar on an estimator that has never been validated against this ledger is the failure this round exists to prevent.」`oos_selection_threshold` docstring：「deliberately NOT reduced … recorded as owed rather than guessed.」`policy.py` R0 同述。`tests/alpha/test_the_other_caliber_is_reported_not_applied.py` 整个文件的存在理由就是把「未被选用的那把口径」钉成 provably inert。
**但方向已知**：`TrialRecord` 不存收益序列，Li–Ji 的 N_eff 算不出**量级**；而 `param_key` 已在盘上，给出 N_eff 的硬上界 **676**（降 75.2%），远低于让 1.7862 通过所需的 2170（降 20.6%）。

**E-019 · 工程事实（实测）** `research_cmd.py:2966-3000` 的打分循环里 `net = result.portfolio_net` 已是完整收益序列，算完 4 个汇总统计后丢弃；账本写入是循环之后的**单点** `_record_trials`（:3141）；`_record_trials` 只追加账本里尚不存在的签名。

**E-020 · `basis` 族的第一次观测已完成** 09-10 轮新增的 18 个形状全部含 `basis`：正边际 **0/18**，best standalone **0.3150** 对基线 1.6900，best marginal −0.0871。同轮非 basis 225 个里正边际 5。

**E-021** `docs/analysis/2026-09-05-mining-proposer-pivot.md` — C-001「瓶颈是候选供给」已于 P17 判 REFUTED。

**E-022** `beidou_governance/scheduler.py:79-85` — `search_space_digest != last_mined_space_digest` 且 mine 预算未尽即返回 MINE，**无任何停机条件**。

**E-023** `beidou_alpha/registry.py:330-360` — 启动证据门校验报告路径、sha256、构造指纹、参数一致性，**不校验签发时的 n_trials 与今天的桶**。

---

## 3. Claim Register

| ID | 命题 | P级 | Falsifier | 状态 |
| --- | --- | --- | --- | --- |
| C-001 | 挖掘路径今天仍能产出可进 registry 的候选 | P0 | 门 ≥ 空间史上最好 OOS | **UNKNOWN**（四种口径三种不成立，E-015） |
| C-002 | 候选供给是瓶颈 | P0 | 已有提案者产出大量候选而无一通过 | **REFUTED**（E-010 + E-021，第二次；不依赖 N 口径） |
| C-003 | 当前表达式空间已搜尽（1h、当前叶节点集合、当前构造） | P1 | 加新叶节点族后出现越过同口径基线的候选 | **SUPPORTED**，且 `basis` 族已提供第一次确认（E-020） |
| C-004 | `594a12f9` 的 PASS 今天仍有效 | P0 | 按当前 N 重判 | 操作者 Q2=B 裁定失效；**但所依据的「当前 N」正是 §7 的争议量** |
| C-005a | D-018 等风险口径是**这个候选**的瓶颈 | P1 | 同波动率下仍不过 | **REFUTED**（E-012：+1.87pp > 1pp） |
| C-005b | D-018 回撤条款对未来所有 sleeve 是规则级缺陷 | P1 | 找到等风险下不加回撤的加 Sharpe sleeve | **PARTIAL**——只在 pit 面成立；static 面同候选回撤改善 −0.0174（E-011） |
| C-006 | N_eff 能把门降回 1.786 以下 | P1 | N_eff ≥ 2170 | **方向已知**（上界 676 < 2170），量级未知且被一条测试钉死（E-018） |

---

## 4. 问题框定与 Success Definition

三个候选框定：F1 供给问题（被 E-021 + E-010 **证据**排除）、F2 记账问题、F3 书门问题。
本轮初选 F2，**Phase 7 指出 F3 是被推理而非证据排除的**：在已发生的历史上，账本门从未杀死过任何候选，唯一过了 D-028 的候选死于书门（E-011）。且 C-003 若成立，F2 描述的冲突在定义上只对"还要继续加叶节点族"时成立。
**裁定：框定悬置，等 §7 的口径裁定。** 口径若落在 2731，F2 成立；若落在 676 或 1415，则 F3 才是历史上真正在咬人的那道门。

**Success Definition**（Q1=C ⇒ 两套；M-A2 已按 K-09 更正）

- **M-A1**（A 线滞后）：进入 registry 并存活 30 天的 mined 策略数。基线 **0**。阈值：下一 90 天窗口 ≥1。
- **M-A2**（A 线领先）：每轮 shortlist 里**按 `baseline_marginal_sharpe`**（代码实际排序的口径）为正的候选数。基线 **24/676 = 3.6%（1h）**。
  **原稿用 standalone，错的**：`594a12f9` 的 standalone 1.7144 < 基线 1.8184，会被判 0 分；而 `research_cmd.py:3000` 明写按 full-sample Sharpe 排序「produces a false 'the space is empty' verdict」。
- **M-A3**（A 线护栏）：该轮抬门幅度 ÷ 该轮最好候选相对上一轮最好的提升。**单位不自洽（门是 OOS 单位、分母是全样本单位），且 M-A2 更紧，本轮记为装饰性护栏，待重设计。**
- **M-B1**（B 线滞后）：每新增一族叶节点后，正边际占比与 best standalone 的变化。基线 **3.6% / 1.7144（1h）**。**第一次观测已完成：`basis` 族 0/18，best standalone 0.3150。**
- **M-B2**（B 线成本护栏）：该次测量抬高 A 线门的幅度。今天无独立记账。

---

## 5. Option Set

| 选项 | 账本 / Δ门 | 预期产出（D-018 逐折边际） | 可逆性 |
| --- | --- | --- | --- |
| **O-A 口径裁定**（本轮推荐的前置） | 0 行；可能使门由 1.8096 落到 1.7420 或 1.6631 | 让 54 个正边际候选与 `594a12f9` 重新可判 | 裁定可改，但**一旦锁死 2731 就不可逆** |
| O-0 冻结挖掘 | 0 行，门锁 1.8096 | 0 | 可逆——**但锁的是一个口径未裁定的 N** |
| O-1 维持现状 | +514~658 行/轮，抬门递减（12 个月约 +0.24） | 1h 先验 0/676 ⇒ ≈0 | 不可逆 |
| O-2 照常收费＋留收益矩阵报 N_eff | 同 O-1 | C-006 的量级；A 线仍 ≈0 | 不可逆 |
| O-3 不写账本的测量模式 | 0 行，Δ门 0 | 同上，且能免费读出 M-A2 | 可逆——**但 E-017 显示能力已存在，缺的是治理；且它打开的是一条"不计数地看"的通道** |
| O-4 D-018 改等风险比较 | 0 行（重测旧候选另计） | 对 `594a12f9`：0；缺口真值 2.55pp 而非 1.30pp | 可逆 |
| O-5 `mined` 桶按族分桶 | 0 新行，结构性降 N | 最大，但它就是降门 | 难回滚 |

**O-3 的价差在 Phase 7 被打掉**：它相对 O-0 的增量（B 线读数 + C-006）没有按给 O-1 用的那把先验折现尺折过，而这些答案的消费者先验就是 O-1 的 ≈0。O-3 唯一不可替代的作用是**免费读出 M-A2**——但在 O-A 未裁定之前，M-A2 该不该是解冻门本身就未定。

---

## 6. Phase 7 对抗审查：被推翻的部分

独立子代理只读冻结产物与原始证据，提出 24 条 Kill（9 条 P0），判 **G6 = FAIL**。我逐条复核，**10 条纠正全部坐实**。原判与重判并列保留：

| 我原来写的 | 真实情况 | Kill |
| --- | --- | --- |
| 「2731 个候选」「A-005：2731 个**不同**表达式」 | 2731 **行**，676 个不同表达式，4.04×；1316 行零新增 | K-01 |
| E-011「fraction 0.209 … 0.0130 对 0.0100」 | 0.209 那一格是 **0.025509**；0.0130 是 fraction 0.20 那一格 | K-11 |
| C-005b SUPPORTED | static 面同候选回撤**改善** −0.0174，符号跨 universe 翻转 | K-13 |
| E-015「没有 no-ledger 模式」 | `BEIDOU_TRIALS_LEDGER` 可整体重定向账本 | K-06 |
| Falsifier「前后读数之差 = 0」 | 被该环境变量平凡满足，分不出测量模式与被重定向的真 mine | K-07 |
| E-021「不会让后续少收」 | 答错方向：危害是"免费看让你挑哪一轮付钱" | K-08 |
| M-A2 用 standalone | 会把管线唯一一次成功判为 0 分 | K-09 |
| 「0/914 在 1h」「M-B1 基线 5.9%」 | 1h 是 676；基线应为 3.6%；5.9% 掺了已判 REFUTED 的日线轮 | K-02 |
| 「M-B1 无数据」 | `basis` 族 0/18 的观测已在盘上 | K-20/21 |
| E-014「N_eff 算不出来（决定性）」 | 是既有裁定的复述，且有测试钉死；方向已由 `param_key` 给出 | K-14/15 |
| E-009 表「桶 N → 门」 | 575 是手工申报，桶首行比报告晚 11 小时——两条曲线 | K-03 |
| 「每轮 +0.0222」 | 无一轮 676 行；增量单调衰减 | K-23 |

**未被推翻的**：`scheduler` 无停机条件（E-022，逐行核实）；分母机制确会单调抬门；C-002 供给不是瓶颈；1h 上 0/676 越过基线。

---

## 7. 裁定请求（人类确认点，不得由分析或代码默认）

| # | 裁定事项 | 三个可辩护的答案 | 后果 |
| --- | --- | --- | --- |
| **1** | `mined` 桶 N 的口径 | 2731 行 / 1415（剔零新增）/ 676（不同假设） | 门 1.8096 / 1.7420 / 1.6631——**后两者让 1.7862 通过** |
| **2** | 2026-09-09 两轮 1316 行是否按 Q7 先例回退 | 回退 / 不回退 | Q7 的判据「没有产生任何新的选择自由度」逐字适用；这两轮从未被裁定 |
| **3** | O-4 是否推进 | 是 / 否 | 先按真值 2.55pp（非 1.30pp）重定价；并把 static 面 −0.0174 写进 C-005b |
| **4** | Q3 重述 | 改 R0 并删那条测试 / 维持 | 不是「N_eff 能不能算」，而是「要不要改一条已裁定、有测试执行的规则」 |
| **5** | `registry.py` 是否补「签发时 N vs 今天的桶」的陈旧检查 | 是 / 否 | 独立 DL 项（E-023）；今天无 mined 在册，但 Q2=B 之后这是一个没人看的维度 |

裁定 1 与 2 在完成之前，**§5 的 O-0「锁 1.8096」不得执行**——那会把一个未裁定的口径变成既成事实。

---

## 8. Scope

**In**：上述五项裁定；`scheduler.next_action` 的停机条件（E-022）；M-A2 的口径更正；本报告与校准记录的落盘。
**Out**：实盘 / registry / live 循环（零接触）；`ledger_scope` 的分桶规则（O-5）；`594a12f9` 的最终处置（口径裁定之后的另一次决定）；手写信号路径；**任何代码改动**（H9：G7 FAIL，不得进入开发）。

**Scope Firewall**：本轮不得因为"顺手"而改 `signature` / `_construction_digest` / R0 / `verdict.decide` —— 它们都是判决口径，任何变动都要独立预登记。

---

## 9. Learning Plan

| Metric | 基线 | 阈值 | 窗口 | 停止条件 |
| --- | --- | --- | --- | --- |
| M-A1 | 0 | ≥1 | 90 天 | 连续 2 窗口为 0 且门仍升 → 关闭 A 线 |
| M-A2（marginal 口径） | 24/676 = 3.6%（1h） | ≥1 个正边际且其 book 边际过 D-018 | 每轮 | 连续 2 轮为 0 → A 线停机 |
| M-B1 | 3.6% / standalone 1.7144 | 新族使任一项上升 | 每族 | `basis` 族已记为第一次观测（0/18） |
| M-B2 | 每轮 +0.0086~0.0217（递减） | 独立记账存在 | 每轮 | 无记账即 B 线不得再跑 |

**验证 Claim 而非功能**：M-A2 用来证伪 C-003；M-A1 用来证伪 C-001。两者都不测"挖掘器是否被使用"。

---

## 10. Final Decision

**Need Evidence。** 命中的硬门禁：**H3**（C-001 UNKNOWN，决策上限来源）、H2（K-01/K-11/K-14 是未处理的强反证）、H1（9 条 P0 未关闭）、H5（G3 FAIL）、H7（L 级 G6 FAIL）、H9（G7 FAIL，不得开工）。取最低者。

Gate 终态：G0 PARTIAL｜G1 PARTIAL｜**G2 FAIL**｜**G3 FAIL**｜G4 PASS｜G5 PARTIAL｜**G6 FAIL**｜**G7 FAIL**。

方向那一半成立（`scheduler` 要加停机条件；供给不是瓶颈；1h 空间在当前叶节点集合下确实没留下钱），支撑它的数字不成立。等 §7 的裁定 1 与 2 完成后，本分析可在新口径上重开——若口径仍落在 2731，§5 的 PIVOT 推荐可原样恢复。

---

## 11. 本轮我犯的错

按 `analysis-calibration.md` 第一行记过的同一类错——**引用一个数时没带产生它的那一格**——我这次犯了两次：E-011 把 fraction 0.209 的标签配了 0.20 的读数；E-012 把 pit 的口径写成 static，而那一段恰恰是全文唯一专门讲"不要混尺子"的地方。

第三处更重：E-014 的 grep 加了"非测试调用点"这个限定词，于是没看见住在测试里的禁止令，把一条**已经裁定过的事**写成了一个**待发现的空白**——而 Q3 的授权正是基于那个空白给出的。

三处都不是数据不足，是读法。修的办法写进校准表。
