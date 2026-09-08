# 北斗 V5 · 自治治理 + 51 条策略 · 总体执行方案（v2，Phase 7 之后）

日期：2026-09-08 ｜ 等级：L ｜ 状态：**Phase 8–9 修订稿；Phase 7 已跑（独立子代理），G6 由 FAIL 转 PARTIAL**
v1 → v2 的变更全部来自 Phase 7 的 Kill Register（KILL-AR-01…20），逐条处置见 §16。操作者裁定（2026-09-08）：**选项 b** —— 全自动保持，probe→main 改为时间规则并诚实标注"不是证据裁决"；先重出 tsmom / flow 证据（已做，§17）。

```text
Reading Check：本次理解为——把 51 条候选策略接进一套人不在运行时操作的治理机制：机器评测、机器晋级/降级、风控防死循环，以 Testnet（demo-fapi）为主验证；交付对象是操作者，交付物是可执行方案。最高风险预设为 Pre-A′「写死的规则能替代人在运行时的**决策**（不是观察）」；若不成立，机器会把假阳性合法化后送去下单——所以进 probe 的候选受 R3 的 1/3 预算与 P&L stop 兜底，且首两次事务由人执行。
Interaction：Yellow（🟢🟢🟡🟡🟢🟢）｜等级：L｜当前决策上限：Weak GO（H3 真实资金 UNKNOWN；H2 由选项 b 解除——C-G2 改写为不依赖证据的规则）｜输出状态：DONE_WITH_CONCERNS｜外部动作授权：证据重出与重启 #6 已由操作者授权并执行（§17）
```

## 0. Decision Memo

| 项 | 结论 |
| --- | --- |
| Final Decision | **Weak GO（受控执行）**。Phase 0（回放）零风险立即开工；Phase 1–2 落治理骨架；**机器写 registry 只在 Phase 4 之后，且前两次事务由人执行 `governance apply`**（KILL-AR-18 的确认点）。真实资金 HOLD & DEFERRED 不变。 |
| 操作者裁定 | ① 人退出运行时**决策**（观察与告警保留）；② 机器自动晋级/降级；③ 风控防死循环；④ 51 条作候选输入——**实际进 Phase 1–3 的是 32 条（+7 条块 6 有前置），块 0 的 12 条 Out，这是范围缩减，写在 §1**（KILL-AR-16）；⑤ Testnet 为主；⑥ **选项 b**：probe→main 用时间规则。 |
| Phase 7 结果 | 独立子代理：G6 FAIL，PIVOT；2 P0 / 10 P1 / 8 P2。P0-1（家族门杀在位者）由 §4 的 N 口径决定 + 证据重出关闭；P0-2（probe→main 证据不可达）由选项 b 改写关闭。10 条 P1 处置见 §16。G6 现记 **PARTIAL**（AR-08 相对价值、AR-11 alpha 占比均由操作者 ACCEPTED）。 |
| 三条核心设计判断（不变） | (1) 评测模块已存在，缺的是"判定→registry"的写入主体；(2) 机器担任主体的前提：分位数门的 N 口径明确、只进 probe、批次窗口、降级即时；(3) **Canary 是部署健康检查，不是 alpha 过滤器**（KILL-AR-04）——假阳性由 R3 预算 + P&L stop 兜底，不由 Canary。 |
| 人类确认点（D.8） | 治理规则版本合并；自治开关；**Phase 4 前两次事务**；R8 首次触发前告警 + 宽限；真实资金。运行时的单次晋级/降级从第三次事务起交给机器。 |
| 预期结果 | 分位数门在 N≈675（全库）≈1.66、按策略桶 ≈1.49–1.51；两臂协议的 tsmom 1.81 两者都过，诚实 16 点网格 1.485 全库门下不过。**51 条全喂进去，正常结果是 0–2 条进 probe；probe→main 按时间规则，预期多数存活的 probe 会到 main——这是"没被停掉"的意思，不是"被证明有 alpha"。** |

## 1. 范围（含范围对账）

**In**：`beidou_governance` 包；分位数门 N 口径改动（§4 R0）；时间规则晋级；验证阶梯 L1–L5；块 1–3 的 32 条候选；缠论第 51 条；块 6 的 7 条在块 5 完成后按 pit·0.30·D-034 后重测。
**范围对账（KILL-AR-16）**：操作者原话"51 条"→ 本方案 Phase 1–3 实际推进 **32 条**；块 6 **7 条**设前置；块 0 **12 条** Out。合计 51。

| Out-of-Scope | 原因 | 重开条件 |
| --- | --- | --- |
| 真实资金 | 09-05 裁定；KILL-A 未建 | Q-CRITICAL + 09-08 审计三问关闭 |
| 块 0（做市/盘口/多场所/期权/深度学习/RL） | 另一套时间尺度与执行架构 | 另立项目 |
| LLM 提案者 | P17 PIVOT | A-004 |
| 窗口外改构造 | 每次清零 M-010 | 只在批次窗口 |
| 机器改自己的阈值 | R10 | 永不 |
| **probe→main 的证据裁决** | KILL-AR-02：demo 噪声下 365 天不可达（Δ=1.7 需 2.1 年，"不劣于 q10"需 39 年） | mainnet 阶段有真实成本数据后重开 |

## 2. 架构（与 v1 同，两处改动）

`beidou_governance`：`policy.py` / `lifecycle.py` / `scheduler.py` / `promote.py` / `canary.py` / **`tenure.py`（替代 v1 的 `sequential.py`：时间规则 + 存活计数）** / `replay.py` / `budget.py`。自带源码上限（KILL-AR-11：加入 `test_source_budget.py`、`test_import_rules.py`、`test_tests_never_touch_the_real_app_support.py` 的 PACKAGES）。
既有包改动：`multiple_testing.py`（N 口径，见 R0）；`ledger.py`（`search_space_version` 必填）；`signals/chanlun.py`；`engine.py`（`governance_digest` 每周期落盘；R8 快通道**改归因口径**）；`beidou_cli` `governance {status,enable,disable,replay,plan,apply,transactions}`；`deploy/` 研究机 + Canary。

## 3. 生命周期状态机（选项 b）

| 转移 | 规则 | 时钟 | 通道 |
| --- | --- | --- | --- |
| candidate → validated | 预登记时间戳 < 报告 ∧ PASS ∧ 分位数门（N 口径按 R0）∧ **证据构造 ≡ 实盘构造（guards/exits/小书，KILL-AR-07）** | 调度批次 | 研究机 |
| validated → booked | book 六项 ∧ `slippage_stress` 5.5 档 ∧ 与每本在跑的书 corr < 0.5 ∧ 换手 ≤ 3× tsmom（E5，见 R 表） | 同上 | 研究机 |
| booked → queued | 面板平价义务（M-011） | 同上 | 研究机 |
| queued → probe | 批次窗口 ∧ R3/R4 未满 ∧ Canary 健康检查过 | 季度 | 慢：事务 + 重启（前两次由人 apply） |
| **probe → main（时间规则）** | 连续 **3 个批次窗口**存活 ∧ 期间从未触发 P&L stop ∧ 家族门重算仍过。**main 保留 P&L stop 与 fraction 不变**——晋级只解除 365 天时钟与 R7 计数，不加预算；加预算是设计时决定 | 窗口末 | 慢 |
| probe → retired | P&L stop ∨ 家族门重算不过 ∨ 3 窗口内被停 | 即时 | 快 |
| main → probe | 触发 P&L stop 一次 → 回 probe 重新计时（不 retired） | 即时 | 快 |
| 任何 → retired | 终身进 probe ≥ 3 次（R7 由 2 改 3，因 main→probe 回流合法） | — | — |

**时间规则的诚实标注**：按 D-019 校准（−2% ≈ 探针 30 天 P&L 的 −2σ），Sharpe-0 的 sleeve 9 个月存活概率约 0.8，Sharpe-1.7 约 0.95（E5 估算，独立窗口近似）。**它区分的是"炸没炸"，不是"有没有 alpha"**；假阳性的代价由 fraction ≤ 1/3 与 stop 兜底。

**Grandfather（KILL-AR-06）**：tsmom 记为 main（起点 2026-09-03 registry 生效日）；flow 记为 probe，起点 2026-09-03，其 30 天复审（2026-10-03）按 D-029 的 P&L 规则；flow 占用 R3 的 1 个并发名额与 1/3 预算——**第二本 probe 要么等 flow 退，要么 fraction 1/6 并出新 book 报告**。

## 4. 规则集（每条标来源：推导 / 先例 / E5 拍定，KILL-AR-13）

| # | 规则 | 数值 | 来源 |
| --- | --- | --- | --- |
| **R0** | 分位数门的 N 口径：**保持按策略桶**（`ledger_scope`），mined 候选加共享 `mined` 桶（现状）；全库 N 与 N_eff **只报告**。理由：全库 N 会让诚实网格的在位者 FAIL（KILL-AR-01），而 N_eff 只降门（`multiple_testing.py:207`）。**家族级风险改由 R3/R4/R5 在暴露侧控制**，不在门侧 | — | 推导（KILL-AR-01） |
| R1 | 每季新增账本行 ≤ B | B = 500 | **E5**（09-08 一次 mine 的 514） |
| R2 | mine 只在 `search_space_version` 变化时跑；同版本重跑拒绝；**"记录缺口"重开路径**：操作者以 D-决策显式授权一次 | 版本 = hash(节点+列+网格) | 先例（K-EX07 / Q7） |
| R3 | probe 并发 ≤ 2；probe 总分数 ≤ 1/3 主账本 | — | 先例（D-018 fraction；flow 已占 1） |
| R4 | 每窗口 ≤ 1 queued→probe、≤ 1 probe→main | — | **E5** |
| R5 | 连续 2 个 probe 被 stop → 冻结 2 窗口；**no-decision 条件**：stop 发生在 TRANSFER 重基或 ERROR 相的周期不计 | k=2, n=2 | **E5**（假停率进 EXP-G6′） |
| R6 | registry 事务 + 回滚 | — | 推导（KILL-Q15） |
| R7 | 终身进 probe ≤ 3；降级后冷却 ≥ 1 窗口 | 3 | **E5** |
| R8 | 回撤梯 −35%/−50% → 快通道缩 vol_target；**口径改为归因 P&L 回撤（income 行），不用交易所权益**；首次触发告警 + 2 周期宽限；触发前后各落一行事务 | 0.225 / 0.15 | 先例（P13）+ 推导（KILL-AR-05：抵押品 52%、"度量 bug 缩书"→ 宽限） |
| R9 | `governance_digest` 每周期落盘 | — | 推导（KILL-Q15 同形） |
| R10 | 阈值只在代码里，改动带版本 + 测试 + 预登记 | — | 推导 |

## 5. 验证阶梯（六层）

| 层 | 环境 | 验什么 | 改动（v2） |
| --- | --- | --- | --- |
| L1 | pytest（240 s） | 三测 + 属性测试 + 事务回滚 | **因果性测试面板随 warmup 伸缩**（KILL-AR-15，独立任务已挂） |
| L2 | 历史账本 + 报告 + 裁定 | **回放输出 = 规则结论 + 例外清单 + 差异归因**（不再要求 100% 一致，KILL-AR-03） | AC-G0 重写 |
| L3 | `--paper` | 状态机/调度/事务 7 天 | — |
| L4 | Canary `--dry-run`（**新增 `--state-dir` flag**，KILL-AR-04） | **部署健康**：0 闸拒绝、digest 稳定、guard 率、无 ERROR 连败、订单 ⊆ cap | 从 Pre-A 防线移除，改标签 |
| L5 | Testnet armed | M-Q03/Q08/Q09/Q10/M-015 + DRILL-G1..G6 | DRILL-G2 加 no-decision 场景 |
| L6 | 真实资金 | Out | — |

Testnet 伪像（§5 v1 三条）扩为**每条治理判据逐个写 no-decision 条件**（KILL-AR-20）：P&L stop 用归因不用 `pnl/equity`；M-015 日读数取当天最后一个**非 ERROR** 周期；R5 计数排除重基周期。

## 6. 51 条候选按块 → Phase（同 v1；范围对账见 §1）

## 7. 第 51 条：缠论（同 v1，两处改动）
- T-S51-1 的合成面板长度 ≥ warmup + 600（KILL-AR-15）。
- "三类买点 ≥ 300 次"改为推导：5 年 × 205 币 × 4h 级别 ≈ 每币每年 ≥ 0.3 次是**最低可判定样本**（走前 5 折每折 ≥ 60 次事件），标 E5。

## 8. Phase 与顺序（v2：拆开首次晋级与构造改动，KILL-AR-12）

```text
Phase 0 回放（1–2 周）→ 规则 v1 定稿 + 例外清单
Phase 1 尺子（2–4 周，无重启）：R0 落地为"报告全库 N"、search_space_version 必填、budget/lifecycle/governance_digest
Phase 2 调度器 + 事务 + Canary（3–5 周，无重启；paper 上跑 DRILL）
Phase 3 数据宽度 + 节点 + 手写含缠论（4–8 周，并行）
Phase 4a 批次窗口 #1（构造）：块 4 必改项一次改完 → 重启 → 攒 30 天干净窗口（K-EX14）
Phase 4b 批次窗口 #2（晋级）：队首候选 → Canary → 事务（人 apply，第 1 次）→ 重启 → probe
Phase 5 稳态：季度窗口；第 3 次事务起机器 apply
```

## 9–10. DL 表与验收（v1 的 DL-G6 / AC-G6 作废，替换如下）

| DL | 实现 | 测试 | 验收 |
| --- | --- | --- | --- |
| DL-G6′ 时间规则 | `tenure.py`：窗口存活计数、stop 事件读 `stopped_books` 与归因 | T-G6′-1 三窗口无 stop → main；T-G6′-2 main 触发 stop → 回 probe 重计；T-G6′-3 TRANSFER 周期的 stop 不计 | AC-G6′：paper 注入三种序列，状态转移与预期一致 |
| DL-G0（改） | `replay.py` | T-G0-1 输出规则结论 + 例外清单（D-019/D-029/K-EX07/Q7…）+ 每条差异归因 | AC-G0：差异清单无"未归因"项 |
| DL-G1（改） | `multiple_testing.py` 报告全库 N 与 N_eff（不作门） | T-G1-1 现有报告重算判定不变 | AC-G1：tsmom 的两个口径都写进报告 |
| 其余 DL-G2..G8、DL-S51、DL-D4/D5 | 同 v1 | 同 v1 | 同 v1 |

## 11. Learning Contract（v2：加一条能证伪 Pre-A′ 的指标，KILL-AR-09）

| Metric | 指标 | 阈值 | 失败动作 |
| --- | --- | --- | --- |
| M-010 / M-Q03 / M-Q08 / M-Q09 / M-Q10 / M-015 | 同 v1 | 同 v1 | 同 v1 |
| M-G01 probe 被 stop 比例 | ≤ 50% | R5 |
| M-G02 回放：未归因差异数 | 0 | 版本不发布 |
| M-G03 Canary 通过率 | ≥ 80% | 查 Canary 判据 |
| M-G04 账本预算使用率 | ≤ 500/季 | 停 validate |
| **M-G05 机器裁决与事后人工复核的分歧率（方向分开记）** | 季度复核 ≥ 10 条裁决；分歧 > 20% 或系统性偏一侧 → 规则版本复审 | 这是 Pre-A′ 的 falsifier |

## 12. Risk Register（v1 + 两条）
RISK-G9 时间规则把 Sharpe-0 的 probe 送进 main（概率约 0.8）——缓解：main 不加预算、stop 保留；RISK-G10 flow 占满 R3 → 第二本 probe 等待——缓解：Grandfather 条款。

## 13. Claim Register（KILL-AR-19，v1 缺失）

| Claim | 命题 | 等级 | Falsifier | 状态 |
| --- | --- | --- | --- | --- |
| C-G1 | 分位数门按策略桶 + 暴露侧 R3/R5，能把假阳性的**代价**控制在 1/3 预算内 | E1（D-018/D-019 机制）+ E5（R 值） | 一个 probe 在 stop 前造成 > 2% 权益损失 | OPEN |
| C-G2′ | 时间规则在 demo 阶段是可执行的晋级依据，且被诚实标注为非证据 | E5 | 机器把连续被 stop 的 sleeve 送进 main（规则 bug） | OPEN |
| A-G1 | registry 事务 + 闸 + 回滚能防"自动写入把循环写死" | E1（KILL-Q15 仪表）+ 前两次人 apply | 回滚目标与事务目标同病（两份证据都 FAIL）——**已由证据重出关闭**（§17） | MITIGATED |
| Pre-A′ | 写死的规则能替代人在运行时的**决策** | E3 | M-G05 分歧率 > 20% | OPEN |
| A-S51 | 缠论与 tsmom 高相关，预期阴性 | E5 | corr < 0.5 且 PASS | OPEN |

## 14. Assumptions & Open Questions
Q-CRITICAL 真实资金；Q2 盈利判据；Q3 窗口（默认季度）；Q4 新数据源（默认进）；Q5 研究机（**KILL-AR-17：跨机契约待写，默认先单机**）；~~Q6~~ 缠论按 §7；~~Q7~~ 已裁定回退；**~~Q8~~ 已裁定（2026-09-08，操作者）：治理代码授权通过，"新增工作 90% 是 alpha"是希望不是硬要求——KILL-AR-11 ACCEPTED，H6 解除**。

## 15. 明确未做
块 0；User Story Draft；真实资金 pre-flight；跨机契约（DL-G8 待补）。

## 16. Phase 7 Kill 处置表

| Kill | 处置 | 落点 |
| --- | --- | --- |
| AR-01 P0 | **CLOSED**：R0 保持按策略桶；证据已在现行门下重出（PASS 1.81 / 门 1.49） | §4 R0、§17 |
| AR-02 P0 | **CLOSED（改写）**：选项 b，probe→main 时间规则，标注非证据 | §3 |
| AR-03 | CLOSED：AC-G0 改例外清单 | §9–10 |
| AR-04 | CLOSED：Canary 改标签；补 `--state-dir` | §5 |
| AR-05 | MITIGATED：R8 改归因口径 + 告警 + 宽限 | §4 |
| AR-06 | CLOSED：Grandfather 条款 | §3 |
| AR-07 | CLOSED：证据构造 ≡ 实盘构造进转移条件 | §3 |
| AR-08 | **ACCEPTED（操作者）**：选 L3 而非 L2；补偿 = 前两次事务人 apply | §0 |
| AR-09 | CLOSED：M-G05 | §11 |
| AR-10 | MITIGATED：Pre-A 收窄为 Pre-A′（决策 vs 观察） | 头部 |
| AR-11 | **ACCEPTED（操作者，2026-09-08）**：90% 是希望不是硬要求 | §14 |
| AR-12 | CLOSED：Phase 4a/4b 拆开 | §8 |
| AR-13 | CLOSED：R 表标来源 | §4 |
| AR-14 | CLOSED：R2 加重开路径 | §4 |
| AR-15 | MITIGATED：独立任务修测试；T-S51-1 面板加长 | §5、§7 |
| AR-16 | CLOSED：范围对账 | §1 |
| AR-17 | OPEN：Q5 默认单机 | §14 |
| AR-18 | MITIGATED：前两次事务人 apply；R8 宽限 | §0 |
| AR-19 | CLOSED：Claim Register | §13 |
| AR-20 | CLOSED：逐判据 no-decision | §5 |

## 17. 证据重出与重启 #6（2026-09-08，操作者授权）

| 项 | tsmom | flow |
| --- | --- | --- |
| 协议 | 两臂 {crowding_window:[0,72]}、pit、5 折、4000/50/6、`--prior-trials 60`，**guards + exits 默认开** | `--main tsmom --sleeve flow`、1/3、pit + static、5 折、4000/50/6、`--prior-trials 39` |
| 结果 | **PASS**：OOS 1.81（093705Z 1.77）、门 1.49 @ N=146、p_family 0.00、CPCV 1.86/q05 1.36、成本×2 1.69、DSR p 0.31（只报）、PBO 0.09；DL-R4 标注仍在 | **REJECT**（同四项）：delta 0.00（原 +0.0046）、sleeve WEAK_PASS 0.59、static −0.08 |
| 报告 | `tsmom-validation-20260908T105259Z` sha256 `11f91787…` | `book-tsmom-flow-20260908T105322Z` sha256 `385b2239…` |
| 账本 | +2 | +2（pit / static 各一） |
| registry | 指针已换；`registry_evidence_problems` = []；`registry_digest` **不变**（16671c63a12e）→ 无分叉、M-010 窗口不清零 | 同左 |
| 重启 #6 | `launchctl kickstart -k gui/$UID/com.beidou.live` 排在 2026-09-08T11:00:02Z（DL-L4 窗口内）；结果见 RESEARCH_LOG 同日条目 | — |

## Checkpoint

| 项 | 值 |
| --- | --- |
| Phase | 7 ✔（G6 PARTIAL）；8–9 v2（本文件）；执行中：证据重出 ✔、重启 #6 排定 |
| 已冻结 Decision | D-G1 机器主体（前两次事务人 apply）；D-G2′ N 口径按策略桶、全库只报告；D-G3 只进 probe + 批次 + Canary 健康检查；**D-G4′ 时间规则（选项 b）**；D-S51 |
| G0–G7 | G0 PASS · G1 PASS · G2 PASS · G3 **ACCEPTED**（AR-08）· G4 PASS（Q8 已裁）· G5 PARTIAL · G6 **PARTIAL** · G7 PARTIAL（Claim Register 已补；测试矩阵压缩） |
| 开放 | Q-CRITICAL / Q2 / Q3 / Q4 / Q5；AR-17 |
| 下一动作 | (a) 确认重启 #6 首周期；(b) Phase 0 回放；(c) Phase 0 选型（RESEARCH_LOG 同日补记列了四个选项） |
