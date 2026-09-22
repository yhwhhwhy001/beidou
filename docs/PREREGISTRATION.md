# 预登记模板

预登记是**在跑之前**写下的那份文本：假设、协议、判据、预期。它落在 `docs/RESEARCH_LOG.md` 的一节里，
用 `--prereg <commit>` 把那一节的 commit sha 与提交时刻写进报告（DL-G9 / DL-K3），顺序此后可核。

**这份模板存在的理由只有一条**：预登记里少写一样东西，就要在跑完之后临时发明一个读法。
2026-09-17 的 EXP-SL1 少写了容差，结果 `5.55e-16` 的浮点差一步就要把 REFUTED 报成主判据通过。

---

## 必写的八项

### 1. 假设

一句可证伪的话：**什么成立、为什么、若它不成立会看到什么**。不写「试试看 X 会不会更好」。

### 2. 这是「新信息」还是「新网格」

`governance/reopen.yaml` 给每个被否策略写的重开条件，多数是「新的信息，不是新的网格」。
两种读法都写下来，再写你偏向哪一种、为什么。选择污染已经发生时**先声明再跑**（P31 的先例）。

### 3. 协议

```
--strategy <族>  --params '<单点>'  --grid '<网格 JSON>'  --charge <笔数>
--universe pit  --to <日期>  --folds N  --min-train N  --purge N  --cpcv-groups N
```

三条踩过的坑，写在这里省得再踩：

- **要单点必须显式传 `--grid '{}'`。** 「不传 `--grid`」拿到的是
  `DEFAULT_GRIDS[strategy]`（`research_grids.py` 的 `_grid()`），tsmom 是 16 格，residual 是 8 格。
  2026-09-17 一次 A/B 按 2 笔定价、实计 32 笔，headroom 92 → 约 58。
- **`--charge` 与 `--grid` 都不传就拒跑**（`_refuse_an_undeclared_charge`，PR #40）。
  拒跑守的是「没人算过要花多少」，不是「算错了」——数还是要自己先算一遍。
- **`--to` 钉住不再等于免费复现**：construction digest 变过，同一条命令今天是一次新 trial。

### 4. 计费与桶

写清 **哪个桶**（`ledger_scope`）、**今天 N 是多少**、**跑完 N 是多少**。
按格子计费不是按次：一次 16 格网格是 16 笔。

### 5. 功效读数 ← 2026-09-18 新增（M-SY01，Q-SY1 操作者裁「要」）

**和网格、桶写在同一段。** 命令零 ledger，跑之前就能算：

```bash
.venv/bin/python -m beidou_cli research power \
  --evidence reports/research/<同族最近一份报告>.json \
  --trials <今天的 N> --charge <这次要花的笔数>
```

把它印出来的两张表（今天的 N、跑完的 N）抄进预登记。读这张表的三条：

- 门是 `max(D-028 选择门, D-020 的 1.0 PASS 线)`。**空桶里挡路的是 PASS 线，不是 D-028**，
  输出里的 `binding` 会说是哪一半。
- 样本外 Sharpe 估计的年化标准误在当前五年窗口上是 **0.44**。所以一个真年化 Sharpe 1.0 的策略，
  在**空桶**里过线的概率是 **50%**；真 1.5 在 N=299 的 tsmom 桶里是 **43%**。
- 这张表是**上界**：它只含选择门与 PASS 线，不含 CPCV 负路径、PBO、fold 一致性、成本 ×2。
  真实联合功效只会更低。

功效低**不是**放宽门的理由。α 是操作者签的 0.05；功效是样本长度的函数，能动它的只有更长的样本
（前向板攒年）或更大的真 Sharpe。它能改变的是**要不要跑**：一次只有 43% 把握的 validate 花的是同一笔
ledger，而且会把同桶所有候选的门再抬高一点。

### 6. 判定规则（数字出来之后一个字不改）

每条判据一行，**每条都要带容差**。只写方向的判据会把浮点噪声读成通过——
`abs(a - b) < tol` 或 `a > b + tol`，并把 tol 写进表里。判据全过才算 PASS，缺一即维持原判。

同时写死**判负之后做什么**：不重跑、不换网格、不申诉门，以及 `reopen.yaml` 要不要改。

### 7. 预期

跑之前写下你认为最可能挂的是哪一条、为什么，以及**两种结果各自值多少**。
一个两种结果都说不出价钱的实验不值得花那笔 ledger。

### 8. 本次服务四个目标里的哪一个 ← 2026-09-18 新增（M-LD01）

**一行，写在最前面，在假设之前就想清楚。** 四个目标是操作者 2026-09-18 亲自选定的，而且他**四个
全选**——所以它们会互相抢资源，写下这一行是为了让每一笔投入事后能被归到某一个目标上。

| ID | 目标 | 它的护栏（不能为了它牺牲的东西） |
| --- | --- | --- |
| **G-A** | 更高样本外 Sharpe | tsmom family gate 余量不得为负（今天 +0.0181，剩 52 笔） |
| **G-B** | 同收益下更小回撤 | D-018 是 R10 规则，改它要走规则事务 |
| **G-C** | 更高吞吐（单位时间能诚实验证多少新假设） | 不得靠「不计数地看」买吞吐（RISK-AM03） |
| **G-D** | 可安全迁移到真实资金 | 不得在 demo 成交上校准只有真钱才能校准的系数 |

写法：`服务：G-A（更高样本外 Sharpe）`。服务多个就都写，但**要排序**——一个说自己同时服务四个目标
的实验，通常是没想清楚它到底要证明什么。

**为什么加这一项。** 2026-09-18 的分析（`docs/analysis/2026-09-18-ledger-denominator-and-four-objectives-deep-analysis.md`
C-LD08）发现这四个目标互相冲突，其中 G-A 与 G-B 在 D-018 门附近**直接对立**——唯一过了 selection gate
的挖掘候选 `594a12f9` ΔOOS Sharpe +0.2931 却因回撤恶化 0.0255 被拒。在此之前，每一轮研究方向由当天
最响的那个怀疑决定，没有任何地方记录它服务的是哪个目标。

**这一项的强度要说清楚**：它是一条**带绊线的约定**，不是 PR #40 那种「不传 `--grid`/`--charge` 就
拒绝跑」的机制。`tests/live/test_the_preregistration_template_keeps_its_items.py` 防的是**这些条目被
悄悄删掉**，防不了**写的人不写**。要做成真机制得让 `research validate` 在缺这一行时拒绝跑，那是一次
CLI 改动，等四个目标排完序再决定值不值得。

---

## 读结果时的两条

- **单点 FAIL 不等于族 FAIL。** 2026-09-18 的分析把七个族的单点值与 residual 的网格值并排，
  推出了一个错的比例（K-SY01）。写「这一族过不了」之前先看它跑的是网格还是一个参数点。
- **`oos_is_full_sample_tail: True` 时样本外是全样本尾巴**（D-043），封顶 WEAK_PASS。
  固定配置的样本外永远是尾巴，重跑一个不会分歧的网格是白花笔数。

---

## 从前向板 PASS 走到 probe（2026-09-18 新增，Q-SY2 的前置 (a)）

板 PASS 之前没有出口。K-SY08 的原话：「板 PASS 只产生一条读数，**没有后续契约**」。
没有出口的观察机制是停车场——候选进来、年限到了、没有任何人被要求做任何事，
而 `years_to_decide` 还会随着别人上板继续变长。出口与入口一起写死，两个方向都写。

### 过门：四步，缺一不可

1. **写一份新预登记**，按上面八项。假设必须是**前向的**：「这个板位自 `entered_at` 起交付了 X」。
   **不得回头再搜历史网格**——那会同时触发原族的 reopen 条件与选择污染。
2. **功效读数用板自己的 N** 与 `board_threshold`，不是 validate 的桶。板的门是
   `max(选择门, 单边 95% 临界值)`，与 D-028 的桶不是一回事。
3. **申请的是 probe 位**（`Policy.max_concurrent_probes = 2`），不是主书。probe 要 registry 里一个
   `probe` 块（D-019 / D-029：`accepted_by` / `accepted_on` / `reason` / `review_after_days` / `stop`），
   以及**一份被引用的报告**。**板读数不是那份报告**——`forward_board` 不导出任何能喂给
   `validate` / `book` 的东西（不变式 4）。所以仍然要一次按正常规则计费的 `validate`。
   板买到的不是那一笔的豁免，是「这一笔值不值得花」的证据。
4. **原族的 `reopen.yaml` 条件不因板 PASS 而解除。** 板 PASS 是向操作者提出重开的**理由**，
   由 `check: operator` 裁，不是自动重开。写死这一条是因为不写的话板就是绕过 reopen 的后门。

### 到点没过门：退役，不延期

`long_enough` 为真而 `verdict` 仍是 `OBSERVING`，就 `research forward retire` 退役这个板位。
**不延期、不换 `claimed_sharpe`、不换参数。** 延期是事后把判定年限改成「再等等看」，
而那个年限正是上板时钉死 claimed 要防的循环，只是换了个方向。

退役**不退门**：`census` 数的是曾经上过板的板位数，退役的也算。看过就是看过。

### 这四条住在哪

`beidou_alpha/validation/forward_board.py` 的 `BOARD_PASS_CONTRACT` / `BOARD_EXIT_CONTRACT`，
并由 `forward_reading` 写进**每一份**读数的 `next_step` 字段。
`research forward status` 只在要求有人动手时把它印出来（PASS 或到点），其余时候不印——
每天给一条还在观察的板位印四步契约是噪声。

## 相关

`docs/ARCHITECTURE.md`（D-020 / D-028 / D-043 的实现位置）、`governance/reopen.yaml`（重开条件）、
`docs/analysis/2026-09-18-system-optimization-and-factor-module-deep-analysis.md` §5.2.1（功效表的推导）。
