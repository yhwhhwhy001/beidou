# 全量验收矩阵 — 执行状态

| 任务 | 名称 | 级别 | Gate | 状态 | 备注 |
|---|---|---|---|---|---|
| BD-T00 | 冻结基线并修复构建门 | P0 | G0 | **PASS** | 基线 73e11b2；修复 AVAILABLE bug + MATIC→POL + Endpoint 常量 |
| BD-T01 | 密钥与审批 Fail-Closed | P0 | G0 | **PASS** | 内存 _approved 绕过已删除；签名-only 验证；无硬编码密钥 |
| BD-T02 | 统一配置提供器 | P0 | G0 | **PASS** | ConfigProvider 取代硬编码 YAML |
| BD-T03 | 单一 Exchange Gateway | P0 | G1 | **PASS** | feed.py urllib 已移除→BinanceRESTClient；无硬编码路径；Adapter 作为唯一网络边界 |
| BD-T04 | ClosedBar 与点时特征链 | P0 | G2 | **PASS** | ClosedBar(21字段)+BarIntegrity(7态)+DQGate+FeatureVector+PIT；fixture+replay 已创建 |
| BD-T05 | Typed Strategy Kernel 切换 | P0 | G3 | **PASS** | direction:str→side:OrderSide\|None；FilterResult 无方向字段；12/12 TypedGraph 测试通过 |
| BD-T06 | 证据驱动因子生命周期 | P0/P1 | G3 | **PASS** | FactorPromotionGate 门禁执行；promote_to_active 绕过已消除 |
| BD-T07 | 持久化 Risk Snapshot 与 Approval | P0 | G3 | **PASS** | R0-R10(11条)+RiskSnapshot(16字段)+Approval fail-closed |
| BD-T08 | 持久化 Intent 与 Outbox | P0 | G1 | **PASS** | IntentOutbox 状态机 (PENDING→SENT→ACKED) + idempotency_key |
| BD-T09 | 统一订单聚合与 UNKNOWN 恢复 | P0 | G1 | **PASS** | OrderStateMachine 全状态转换 + UNKNOWN 恢复路径 |
| BD-T10 | Fill 与 Position 权威链 | P0 | G1 | **PASS** | FillEvent+PositionAggregate+projection 重建 |
| BD-T11 | 交易所原生保护与安全监督 | P0 | G4 | **PASS** | ProtectionManager+StopLoss/TakeProfit calculator |
| BD-T12 | 真正的复式账本 | P0 | G1 | **PASS** | LedgerTransaction+Posting(≥2)复式记账；7 测试通过 |
| BD-T13 | 独立对账与差异门禁 | P0 | G1 | **PASS** | ReconciliationEngine 六态+非MATCHED阻断 |
| BD-T14 | 生命周期、恢复和控制面 | P0/P1 | G4 | **PASS** | ModuleLifecycle+控制面+四层健康检查+DegradationLevel |
| BD-T15 | CI 与证据系统加固 | P0 | G0 | **PASS** | mypy 豁免减至 18 子模块（均带 owner/expiry）；P0 包 beidou_safety.risk/protection + beidou_exchange.core 无豁免 |
| BD-T16 | 真实 Paper 撮合与成本模型 | P1 | G4 | **PASS** | PaperMatchingEngine+CostModel+PaperShadowRunner |
| BD-T17 | 因子统计与组合优化 | P1 | G3 | **PASS** | IC/ICIR+PurgedWFO+CPCV+FactorEvaluator 完整 |
| BD-T18 | Binance Testnet 认证 | P1 | G5 | **NOT_VERIFIABLE** | 需 Binance Testnet 凭据 |
| BD-T19 | 30 天无人值守认证 | P2 | G6 | **NOT_VERIFIABLE** | 需 30 天真实运行窗口 |

### 验证快照 (2026-08-06)

- **编译**: compileall PASS
- **格式**: ruff format 200 files PASS
- **测试**: 823 passed, 1 skipped
- **包校验**: validate_package.py PASS
- **硬编码扫描**: 0 blocking issues
- **Mainnet**: PROHIBITED
