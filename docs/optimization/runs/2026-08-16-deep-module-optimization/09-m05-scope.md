# M05 因子挖掘 — 范围定义

## Phase A 结论（已核实）

| 缺陷 | 位置 | 判定 |
|---|---|---|
| 全样本 IC 符号决定翻转方向，翻转后 p 值进多重检验分母（方向选择与检验同一样本 → p-hacking） | runner.py:525-534, 789-798 | **P0** |
| statistics/validation.py "BD-CV20 统计验证内核"为占位实现（PurgedWFCV 0.0 分/scores_fn 从不调用、CPCV [0.0]*n_combos、PBO/DSR 简化）—— 无生产消费者但可误接 | validation.py:79, 139 | **P0** |
| build_promotion_chain 全部 approved=True 自证（晋升证据链不经 FactorPromotionGate 逐级验证） | runner.py:1463-1503 | **P1** |
| PBO 退化为单次比较（multiple_testing.py:261-277 循环无随机性） | multiple_testing.py | **P1** |
| 表达式自简化对 NaN 不健全（SafeDiv(x,x)→1、Sub(x,x)→0、Eq(x,x)→True） | expression_ast.py:1074-1075 等 | **P1** |
| Residualize 全样本 OLS 系数 = look-ahead（标 NOT_VERIFIABLE 但 raw IC 基于泄漏数据） | expression_ast.py:1300-1344, runner.py:965-1028 | **P1** |
| DSR 双侧 p 值非规范；策略文件与代码漂移（pbo_threshold 0.30 vs 0.20；multiple_testing/stability/resources 段未读） | multiple_testing.py:190-194, 432; runner.py:131-212 | P2 |

## 任务

- F01（P0）：翻转计入试验预算 —— n_trials 分母 = 生成候选数 + 翻转次数（BH/Holm/DSR/PBO 全部使用）；翻转向量写入 EvidenceBundle 审计
- F02（P0）：statistics/validation.py 改为 fail-closed 桩（raise + 指向 mining/evaluation 真实实现）；结构性测试改断言 raise
- F03（P1）：PBO 组合随机化修复（真实 n_combos 次独立组合分区，seed 固定）
- F04（P1）：build_promotion_chain 逐级经 FactorPromotionGate.validate_evidence 验证（真实 performance 输入），失败步 approved=False 且链终止
- F05（P1）：表达式自简化 NaN 语义修正（x 含 NaN → 不简化；恒等式仅在有限值上成立）
- F06（P1）：Residualize 改滚动/扩展窗口系数（PIT 安全）或全样本路径直接拒绝评估（不产 IC）；本轮选择后者（保守），滚动实现登记 M05-R2
- F07（P2）：DSR 单侧 p 值 + 策略文件段绑定（multiple_testing/stability 段读取）+ 阈值漂移登记
- F08：回归 + 对抗审查 + 证据

## 不做（登记残余）

- SymbolicGP 骨架/ensemble/pareto 选择器完善（生成器迭代属研究演进,非正确性阻断）
- Neutralization/turnover/decay 接线（M04 已登记,随流水线演进）
- models/ 空壳填充
