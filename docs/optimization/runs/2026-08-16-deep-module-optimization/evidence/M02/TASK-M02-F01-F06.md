# M02 Universe Selection — 修复证据（F01-F06）

## 修复矩阵

| 任务 | 缺陷 | 修复 | 测试 |
|---|---|---|---|
| F01 | `seed_historical_observation` 用 365d 魔数捷径（观察期起点与证据覆盖无关） | 观察期起点派生自 `evidence["days"]`（钳制 ≤365d）；无天数证据 → 不加速（保守） | 4 个测试（派生/钳制/无证据/阈值） |
| F02 | `BEIDOU_MIN_OBSERVATION_HOURS` 无登记无审计 | 登记 EXEMPT-19 + 加载时审计日志（非法值回退 24h 也记日志） | 登记表 19 项完整性 |
| F03 | UniverseEntry 用 0.0 作 UNKNOWN 哨兵（合法零费率/零 OI 被误判） | 三字段 `float | None = None`；`any_unknown_critical` 判 None；既有测试按新语义更新（零值不再误报） | test_zero_values_are_not_unknown + 修订 test_unknown_critical |
| F04 | 诊断打印限频硬编码 3 交易对三元组 | `_diag_throttle(key, 30s)` 通用 per-key 节流；3 处调用点替换 | 编译+回归 |
| F05 | 评分权重模块常量，P1-037 注释声称可覆盖但无接线 | TradingPool.set_score_weights（键集/数值/范围校验）+ score() 使用；引擎从策略 `universe_score_weights` 读取（缺失→默认+审计） | test_score_weights_policy_override |
| F06 | 回归 | 全量 | 2565 passed |

## 事实修正

侦察报告声称 engine.py 恢复路径硬编码 3 symbol —— 实为**诊断打印限频条件**（非宇宙逻辑）。已核实并记录（F04 处理）。

## 测试证据

- 目标测试 101 passed；全量 **2565 passed / 0 failed**
- ruff：改动文件 All checks passed

## 设计决策记录

- **保留显式候选宇宙**（CLI 30 symbols + EXPLICIT_SYMBOL_UNIVERSE_REQUIRED fail-closed）：全量交易所动态发现在共享 demo 账户下风险大于收益；动态性由池内五维评分/晋级/隔离/容量门禁提供。若未来引入动态发现需 operator 门禁立项。
- 策略文件 `universe_score_weights` 键：代码侧已支持，策略文件补键需用户确认（与 F08 同模式）。

## 残余（登记）

- 历史预筛选三维质量分统计口径（无历史订单簿，点差/深度不可推导）—— 设计正确，保留
