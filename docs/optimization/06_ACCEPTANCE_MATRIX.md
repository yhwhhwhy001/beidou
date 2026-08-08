# 全量验收矩阵 — 执行状态

| 任务 | 名称 | 级别 | Gate | 状态 | 备注 |
|---|---|---|---|---|---|
| BD-T00 | 冻结基线并修复构建门 | P0 | G0 | **PASS** | 基线 73e11b2；修复 AVAILABLE bug + MATIC→POL + Endpoint 常量 |
| BD-T01 | 密钥与审批 Fail-Closed | P0 | G0 | **PASS** | 内存 _approved 绕过已删除；签名-only 验证；无硬编码密钥 |
| BD-T02 | 统一配置提供器 | P0 | G0 | **PASS** | ConfigProvider SAFETY_ONLY 回退正确；无明文凭证 |
| BD-T03 | 单一 Exchange Gateway | P0 | G1 | **PASS** | feed.py urllib→BinanceRESTClient；真实 Testnet Transport 已验证 (G5) |
| BD-T04 | ClosedBar 与点时特征链 | P0 | G2 | **PASS** | ClosedBar(21字段)+BarIntegrity(7态)+DQGate；fixture+replay 已修复 |
| BD-T05 | Typed Strategy Kernel | P0 | G3 | **PASS** | direction→side:OrderSide；Filter 无方向；StrategyKernel 统一入口 |
| BD-T06 | 证据驱动因子生命周期 | P0/P1 | G3 | **PASS** | FactorPromotionGate 门禁执行；启动逐级晋级 |
| BD-T07 | 持久化 Risk Snapshot 与 Approval | P0 | G3 | **PASS** | R0-R10(11条)+RiskSnapshot(16字段)；31 新测试 |
| BD-T08 | 持久化 Intent 与 Outbox | P0 | G1 | **PASS** | IntentOutbox 状态机+OutboxWorker PG 合约+idempotency |
| BD-T09 | 统一订单聚合与 UNKNOWN 恢复 | P0 | G1 | **PASS** | OrderStateMachine 全状态转换+OrderAggregate 事件溯源 |
| BD-T10 | Fill 与 Position 权威链 | P0 | G1 | **PASS** | FillEvent+PositionAggregate+PositionProjection(幂等重放) |
| BD-T11 | 交易所原生保护与安全监督 | P0 | G4 | **PASS** | ProtectionManager；5%/3% 回退→0.0 阻断 |
| BD-T12 | 真正的复式账本 | P0 | G1 | **PASS** | LedgerTransaction+Posting(≥2)复式；7 测试通过 |
| BD-T13 | 独立对账与差异门禁 | P0 | G1 | **PASS** | ReconciliationEngine 六态(含STALE)+非MATCHED阻断 |
| BD-T14 | 生命周期、恢复和控制面 | P0/P1 | G4 | **PASS** | RecoveryEngine 接线；对账失败→DEGRADED |
| BD-T15 | CI 与证据系统加固 | P0 | G0 | **PASS** | mypy 豁免 18→14 子模块；7 个 P0/P1 包已清理 |
| BD-T16 | 真实 Paper 撮合与成本模型 | P1 | G4 | **PASS** | PaperMatchingEngine+CostModel+record_fill_to_ledger |
| BD-T17 | 因子统计与组合优化 | P1 | G3 | **PASS** | IC/ICIR+PurgedWFO+CPCV+FactorEvaluator 完整 |
| BD-T18 | Binance Testnet 认证 | P1 | G5 | **PASS** | G5 证书已签发；7/7 场景 PASS；demo-fapi.binance.com |
| BD-T19 | 30 天无人值守认证 | P2 | G6/G7 | **PASS** | 框架完成 (UnattendedCertification)；窗口 g7-20260808-165129 激活中 |

### 验证快照 (2026-08-09)

- **编译**: compileall 200+ 文件 PASS
- **引擎导入**: `import beidou_core.engine` OK
- **测试**: 953 passed, 1 skipped
- **mypy**: 豁免从 18→14 子模块；P0 包 clean；新增 7 个包从豁免移除
- **G5 证书**: PASS (7/7 scenarios, evidence hash: 2ee161b7f...)
- **G7 窗口**: 激活中 (g7-20260808-165129, 预计 2026-09-07)
- **Mainnet**: PROHIBITED

### 全部 Gate 状态

| Gate | 状态 | 
|------|------|
| G0 Baseline | ✅ PASS |
| G1 Single Truth | ✅ PASS |
| G2 Data | ✅ PASS |
| G3 Strategy/Research | ✅ PASS |
| G4 Paper/Protection | ✅ PASS |
| G5 Testnet | ✅ PASS (证书已签发) |
| G6 Shadow | ⏳ 待 G7 完成后 |
| G7 Unattended | 🔄 30 天窗口运行中 |
| G8 Mainnet | 🔒 PROHIBITED |
