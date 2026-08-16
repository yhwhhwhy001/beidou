# M05 因子挖掘 — 修复证据（F01-F07）

## 修复矩阵（2 个 P0 + 5 个 P1/P2）

| 任务 | 缺陷 | 修复 | 测试 |
|---|---|---|---|
| F01 (P0) | 全样本 IC 符号决定方向翻转，翻转后 p 值进多重检验分母（方向选择与检验同样本 = p-hacking） | 翻转计入试验预算：n_trials = 候选数 + 翻转数；pvalues 数组追加翻转数个保守 1.0 项（BH/Holm 分母一致）；bundle 审计 `direction_flipped` 字段 | test_gate_semantics 更新（n_total_trials ≥ 候选数、n_evaluated ≥ 候选数） |
| F02 (P0) | statistics/validation.py 假内核（0.0 分/CPCV 全零/PBO 简化/DSR 双侧）可被误接静默放行 | 退役为 fail-closed 桩（raise + 指向 mining/evaluation 真实实现）；Holm（真实现）保留；DSR 双侧 p 值问题随退役消除 | test_statistical_validation.py 重写 15 个（正常输入 raise、小样本边界、Holm 功能） |
| F03 (P1) | PBO 循环零随机性（n_combos 次重复同一比较） | 组合子集语义修正：随机半数子集内 IS 选优 vs 子集 OOS 中位数（固定种子确定性） | TestPBOSemantics 3 个（全相关→0、反向→1、随机→(0,1)） |
| F04 (P1) | build_promotion_chain 全链 approved=True 自证 | 逐级经 FactorPromotionGate.validate_evidence（真实 performance）；失败步 approved=False 且链终止 | test_chain_is_honest_for_below_threshold_icir（icir=0.05 → 链 2 步终止） |
| F05 (P1) | 表达式自简化 NaN 不健全（SafeDiv(x,x)→1、Sub(x,x)→0、Eq(x,x)→True、Ne(x,x)→False） | 四处自简化全部移除（warmup 期 NaN 不得伪装成有效值/恒真/恒假条件）；常数折叠保留 | test_bf03_ast 两测试更新 + 全量 |
| F06 (P1) | Residualize 全样本 OLS 系数 look-ahead 且 raw IC 基于泄漏数据 | 残差候选不再产出 ic_mean/sharpe（仅样本计数）；滚动窗口实现登记 M05-R2 | test_fw03_e2e_mining 回归 |
| F07 (P2) | DSR 双侧 p 值、策略文件段漂移 | DSR 双侧随 F02 退役消除；single-sided 已在 mining/evaluation 正确；策略段绑定登记残余 | — |

## 测试证据

- 受影响测试：195 passed（AST/挖掘/泄漏/晋级链/统计/评估）
- 全量 **2587 passed / 0 failed**；ruff 38（基线，零新增）
- 过程中修正：gate_semantics 断言随翻转预算语义更新（分母覆盖 ≥ 候选数，方向强化）

## 残余（登记）

- Residualize 滚动/扩展窗口系数（PIT 安全替代）→ M05-R2 或 M08 联动
- factor_mining_policy.yaml 的 multiple_testing/stability/resources 段未绑定 → 流水线配置治理
- screened[:50] 评估上限硬编码 → 容量治理
