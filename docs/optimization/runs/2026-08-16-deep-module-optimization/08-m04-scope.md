# M04 因子系统 — 范围定义

## Phase A 结论（已核实）

| 缺陷 | 位置 | 判定 |
|---|---|---|
| 晋级门禁 NaN/Inf 穿透：`icir < min_icir` 对 NaN 恒 False（通过）、Inf 通过；`sample_count < min_samples` 对 NaN 同样通过 | factor.py:431/435 | **P0** |
| `compute_icir` 零方差返回 ±inf，穿透 `icir < 0.3` 阈值门禁 | factor.py:575 | **P0** |
| `compute_ic` 第二返回值标注"标准误"实为收益 std | factor.py:511-529 | P1 |
| Neutralization 完全未实现（LabelSpec.neutralization 无消费者） | contracts.py:335 | P1 → 登记残余（M05 表达式中性化/回归中性化另立项） |
| decay 未实现（FactorPerformance.capacity_decay 从不填充） | factor.py:112 | P1 → 登记残余（M05 流水线） |
| turnover 半实现未接线 | metrics.py:769-786 / runner 不调用 | P1 → 登记残余（M05） |
| FactorRegistry 未接入 MiningRunner（runner 直写 JSON store） | — | P2 → 登记残余 |

## 任务

- F01：晋级门禁 NaN/Inf 防护 —— `validate_evidence` 对 performance 的 icir/ic_mean/sample_count 做 isfinite 检查，非有限 → 拒绝（failures 明确记录）；测试 NaN/Inf 无法晋级
- F02：`compute_icir` 零方差返回 0.0（与 metrics.py 新实现一致），杜绝 ±inf 穿透；测试
- F03：`compute_ic` 返回值语义修正 —— 第二返回值为"收益标准差"（重命名/文档化），或改为返回标准误 `std_r/sqrt(n)`？消费方检查后决定；测试
- F04：`FactorEvaluator` 与 `mining/evaluation/metrics.py` 双轨关系澄清 —— 老评估器标注 deprecated 指向新实现（不做合并，合并属 M05）
- F05：性质测试 —— IC 边界（常数序列、相关 ±1）、RankIC 单调性、ICIR 样本性质
- F06：回归 + 对抗审查 + 证据

## 不做（登记残余）

- Neutralization/decay/turnover 接线（M05 挖掘流水线统一处理）
- FactorRegistry×MiningRunner 集成（M05）
- 旧 factor.py 评估器删除（需先迁移消费者——FactorRegistry.evaluate 使用方，M14 生命周期联动）
