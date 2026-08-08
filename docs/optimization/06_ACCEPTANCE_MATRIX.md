# 全量验收矩阵 — 执行状态

| 任务 | 名称 | 级别 | Gate | 状态 | 备注 |
|---|---|---|---|---|---|
| BD-T00 | 冻结基线并修复构建门 | P0 | G0 | **PASS** | 基线 73e11b2；修复 AVAILABLE bug + MATIC→POL + Endpoint 常量 |
| BD-T01 | 密钥与审批 Fail-Closed | P0 | G0 | **PASS** | 内存 _approved 绕过已删除；签名-only 验证；无硬编码密钥；13 测试通过 |
| BD-T02 | 统一配置提供器 | P0 | G0 | **PASS** | ConfigProvider SAFETY_ONLY 回退正确；无明文凭证；示例文件齐全 |
| BD-T03 | 单一 Exchange Gateway | P0 | G1 | **CONDITIONAL_PASS** | feed.py urllib→BinanceRESTClient；Adapter 真实 Transport 待 BD-T18；engine 仍直连 REST client |
| BD-T04 | ClosedBar 与点时特征链 | P0 | G2 | **PASS** | ClosedBar(21字段)+BarIntegrity(7态)+DQGate；replay fixture 已修复 |
| BD-T05 | Typed Strategy Kernel | P0 | G3 | **CONDITIONAL_PASS** | direction→side 类型化；FilterResult 无方向；运行时仍用旧 AlphaGraph DAG |
| BD-T06 | 证据驱动因子生命周期 | P0/P1 | G3 | **CONDITIONAL_PASS** | FactorPromotionGate+证据门禁用；Testnet 模式自动晋级全因子（待整改） |
| BD-T07 | 持久化 Risk Snapshot 与 Approval | P0 | G3 | **PASS** | R0-R10(11条)+RiskSnapshot(16字段)+Approval fail-closed |
| BD-T08 | 持久化 Intent 与 Outbox | P0 | G1 | **CONDITIONAL_PASS** | IntentOutbox 状态机完整；运行时仍用内存实现；PG worker 待接线 |
| BD-T09 | 统一订单聚合与 UNKNOWN 恢复 | P0 | G1 | **CONDITIONAL_PASS** | OrderStateMachine 全状态转换；引擎仍用旧 OrderStateTracker |
| BD-T10 | Fill 与 Position 权威链 | P0 | G1 | **CONDITIONAL_PASS** | PositionAggregate 合约定义；fill-event 驱动投影待接线 |
| BD-T11 | 交易所原生保护与安全监督 | P0 | G4 | **CONDITIONAL_PASS** | ProtectionManager+StopLoss/TakeProfit；5% 固定回退仍存在 |
| BD-T12 | 真正的复式账本 | P0 | G1 | **PASS** | LedgerTransaction+Posting(≥2)复式记账；7 测试通过；引擎 JournalEntry→LedgerTransaction 已迁移 |
| BD-T13 | 独立对账与差异门禁 | P0 | G1 | **CONDITIONAL_PASS** | ReconciliationEngine 五态(STALE缺失)+非MATCHED阻断新风险 |
| BD-T14 | 生命周期、恢复和控制面 | P0/P1 | G4 | **CONDITIONAL_PASS** | ModuleLifecycle+ControlPlane+四层健康检查；RecoveryEngine 死代码；对账失败仍转ACTIVE |
| BD-T15 | CI 与证据系统加固 | P0 | G0 | **CONDITIONAL_PASS** | P0 包 mypy clean；core.engine 等 18 模块仍豁免；CI 无 T0-T5 分层；branch/mutation 覆盖率未强制 |
| BD-T16 | 真实 Paper 撮合与成本模型 | P1 | G4 | **CONDITIONAL_PASS** | PaperMatchingEngine+CostModel 实现；paper fills 未进复式账本 |
| BD-T17 | 因子统计与组合优化 | P1 | G3 | **PASS** | IC/ICIR+PurgedWFO+CPCV+FactorEvaluator 完整 |
| BD-T18 | Binance Testnet 认证 | P1 | G5 | **NOT_VERIFIABLE** | 需 Binance Testnet 凭据 + 真实传输层 + G5 场景测试 |
| BD-T19 | 30 天无人值守认证 | P2 | G6/G7 | **NOT_VERIFIABLE** | 需 30 天真实运行窗口；G7 框架代码可提前准备 |

### 验证快照 (2026-08-09)

- **编译**: compileall 200 文件 PASS
- **引擎导入**: `import beidou_core.engine` OK（JournalEntry 导入已修复）
- **测试**: 922 passed, 1 skipped
- **mypy**: P0 包 (beidou_safety.risk/protection, beidou_exchange.core, beidou_launcher) clean
- **ruff**: 56 个错误（.db 二进制 + 行长度/格式类）
- **Mainnet**: PROHIBITED

### 已知遗留

| 优先级 | 问题 | 影响任务 |
|--------|------|---------|
| P0 | 引擎运行时仍用旧 AlphaGraph DAG、OrderStateTracker、内存 Outbox | T05/T08/T09 |
| P0 | DEFAULT_FALLBACK_STOP_PCT=5% 硬编码回退 + 3% AdaptiveCalculator 回退 | T11 |
| P0 | Testnet 启动自动晋级全因子到 ACTIVE | T06 |
| P0 | RecoveryEngine 死代码；对账失败不阻断 ACTIVE | T14 |
| P0 | Adapter 真实 Transport 层未实现（标注 real_transport_pending_BD-T18） | T03 |
| P1 | 18 个核心模块 mypy 豁免（均有 owner/expiry）；CI 无 T0-T5 分层 | T15 |
| P1 | R0-R10 规则无直接测试；tests/config/outbox/recovery/paper 等目录缺失 | T07/T08/T14/T16 |
| P1 | 多数运行时表（risk/order/fills/position/protection）无运行时代码使用 | T07-T14 |
| P2 | 部分任务缺少含原始 stdout/stderr/退出码的证据 bundle | T00-T17 |
