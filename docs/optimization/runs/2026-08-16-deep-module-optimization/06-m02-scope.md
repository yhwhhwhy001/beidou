# M02 活跃交易对 Universe Selection — 范围定义

## Phase A 结论（已核实）

- 动态评分+生命周期真实：TradingPool 五维评分（spread/depth/volume/stability/capacity）、连续 3 次 <0.3 → QUARANTINED（迟滞）、QUARANTINED 回归路径、容量门禁（is_tradable）、持久化（event_sink 观测期起点/评分恢复）、晋级三重验证（finite/range/threshold，NaN/Inf 无法晋级）—— 机制本身是好的
- **候选集由 CLI 显式配置**（launchd 30 symbols）—— 这是有意的 fail-closed 设计（engine.py:1172 EXPLICIT_SYMBOL_UNIVERSE_REQUIRED，拒绝 ALL/DEFAULT 自动扩展）。动态发现（exchangeInfo 全量+流动性筛选）在共享 demo 账户+无 operator 配置下风险大于收益；保留显式配置，动态性由池内评分/晋级/隔离提供
- **事实修正**：侦察报告声称 engine.py:7737/7771/7958 "恢复路径硬编码 3 symbol"—— 实际是诊断打印限频条件（`if symbol in ("APRUSDT","ARCUSDT","BNBUSDT") and ... > 30`），非宇宙逻辑。清理为通用按 symbol 限频
- `seed_historical_observation`：`observing_since = now - 365d` 魔数捷径 —— 应派生自证据的数据天数（evidence["days"]）
- `BEIDOU_MIN_OBSERVATION_HOURS` 环境变量绕过观察期 —— 无登记、无审计
- contracts.py UniverseEntry：`funding_rate == 0.0` 哨兵把合法零费率/零 OI 误判 UNKNOWN critical
- 评分权重 `_DEFAULT_SCORE_WEIGHTS` 是模块常量（P1-037 注释声称"可被签名策略覆盖"但无接线）

## 任务

- F01：`seed_historical_observation` 观察期起点派生自证据数据天数（evidence["days"]→observing_since=now-days*24h，钳制上限 365d）；魔数 365d 消除；测试
- F02：`BEIDOU_MIN_OBSERVATION_HOURS` 登记 EXEMPT-19（观察期环境变量捷径）+ 加载时审计日志
- F03：UniverseEntry 三字段 → `float | None = None`（None=未知）；`any_unknown_critical` 改判 None；测试覆盖零费率不误报
- F04：诊断打印限频改为通用 per-symbol 节流字典（消除 3 symbol 硬编码）
- F05：评分权重经签名策略可覆盖（pool 接受 weights 参数，引擎从 _policy_params 读取可选键 `universe_score_weights`——JSON 值由策略文件提供；缺失用默认）—— 策略文件更新与重签名待用户确认（同 F08 模式）
- F06：回归 + 对抗审查 + 证据

## 不做

- 全量交易所动态发现（与 EXPLICIT_SYMBOL_UNIVERSE_REQUIRED fail-closed 设计冲突；若引入需 operator 门禁，另行立项）
- TradingPool 机制重构（机制正确）
- 历史预筛选三维质量分的统计口径（无历史订单簿，点差/深度不可推导 —— 现设计正确）
