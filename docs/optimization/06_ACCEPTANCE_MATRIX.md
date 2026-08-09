# 全量验收矩阵 — 执行状态

> 历史矩阵降级声明（2026-08-09）：下表保留早期切片记录，不是当前认证。独立执行、量化和 SRE 红队已证明这些端到端 PASS 缺少可重放生产事实或存在假阳性；当前权威结论以 `docs/optimization/08_ACCEPTANCE_MATRIX.md`、`13_FINAL_ACCEPTANCE_REPORT.md` 和 `15_EXECUTION_DELIVERY.md` 为准。

| 任务 | 名称 | 级别 | Gate | 状态 | 备注 |
|---|---|---|---|---|---|
| BD-T00 | 冻结基线并修复构建门 | P0 | G0 | **PASS** | 基线 73e11b2；修复 AVAILABLE bug + MATIC→POL + Endpoint 常量 |
| BD-T01 | 密钥与审批 Fail-Closed | P0 | G0 | **PASS** | 内存 _approved 绕过已删除；签名-only 验证；无硬编码密钥 |
| BD-T02 | 统一配置提供器 | P0 | G0 | **PASS** | ConfigProvider SAFETY_ONLY 回退正确；无明文凭证 |
| BD-T03 | 单一 Exchange Gateway | P0 | G1 | **NOT_VERIFIABLE** | Adapter 已收敛入口并接入 user-stream listen-key/WS 生命周期，但完整 Algo/reconnect/gap-fill/错误分类未形成生产证据 |
| BD-T04 | ClosedBar 与点时特征链 | P0 | G2 | **NOT_VERIFIABLE** | 特征链已显式携带闭合与可用时间且缺失即阻断；仍需独立全链 PIT/数据重放 |
| BD-T05 | Typed Strategy Kernel | P0 | G3 | **NOT_VERIFIABLE** | typed graph 是唯一运行边界、全提案哈希与 NO_ACTION/VETO 证据已覆盖；仍缺 Backtest/Paper/Testnet 真实窗口一致性 |
| BD-T06 | 证据驱动因子生命周期 | P0/P1 | G3 | **NOT_VERIFIABLE** | 启动自动晋级已关闭，但没有完整 sealed OOS/provenance 证据 |
| BD-T07 | 持久化 Risk Snapshot 与 Approval | P0 | G3 | **NOT_VERIFIABLE** | 已补本地恢复切片，生产签名审批与快照绑定仍缺 PostgreSQL/跨进程证据 |
| BD-T08 | 持久化 Intent 与 Outbox | P0 | G1 | **NOT_VERIFIABLE** | 已新增 PostgreSQL v3 schema、runtime records、Approval+Intent+Outbox 原子提交、lease/fencing/UNKNOWN Worker 合同并接入引擎选择路径；尚未有真实数据库、migration head、双 worker 和崩溃恢复证据 |
| BD-T09 | 统一订单聚合与 UNKNOWN 恢复 | P0 | G1 | **NOT_VERIFIABLE** | 订单聚合未覆盖完整运行时生命周期与模糊提交恢复 |
| BD-T10 | Fill 与 Position 权威链 | P0 | G1 | **NOT_VERIFIABLE** | 本地增量 fill/projection 与 user-stream 事件接入已有，授权 replay/gap-fill/手续费资金费未验 |
| BD-T11 | 交易所原生保护与安全监督 | P0 | G4 | **NOT_VERIFIABLE** | ACK/owner/generation/逐持仓覆盖已收紧，真实交易所保护与恢复未验 |
| BD-T12 | 真正的复式账本 | P0 | G1 | **NOT_VERIFIABLE** | 本地 journal 可重放，生产数据库、资金费和跨源平衡未验 |
| BD-T13 | 独立对账与差异门禁 | P0 | G1 | **NOT_VERIFIABLE** | 三方比较骨架存在，真实独立 exchange/event facts 未形成闭环 |
| BD-T14 | 生命周期、恢复和控制面 | P0/P1 | G4 | **NOT_VERIFIABLE** | fail-closed 单测通过，Mac 实机故障/LOCK/恢复证据不足 |
| BD-T15 | CI 与证据系统加固 | P0 | G0 | **PASS** | mypy 豁免 18→14 子模块；7 个 P0/P1 包已清理 |
| BD-T16 | 真实 Paper 撮合与成本模型 | P1 | G4 | **NOT_VERIFIABLE** | 执行观测不再自标方向，成本证据独立；真实跨环境撮合、成本与 parity 窗口仍缺 |
| BD-T17 | 因子统计与组合优化 | P1 | G3 | **NOT_VERIFIABLE** | 评估已按原始 bar index 对齐并保留 WFO 键；真实 WFO/CPCV/FDR、容量和 OOS 主链尚未执行 |
| BD-T18 | Binance Testnet 认证 | P1 | G5 | **NOT_VERIFIABLE** | 旧 7/7 证书缺少计划 16 场景，且绑定旧 SHA/权限 WARN |
| BD-T19 | 30 天无人值守认证 | P2 | G6/G7 | **NOT_VERIFIABLE** | fast-forward 仅为模拟框架；真实窗口因 P0/证据缺口重置 |

### 历史验证快照（不可认证）

- **编译**: compileall 200+ 文件 PASS
- **引擎导入**: `import beidou_core.engine` OK
- **测试**: 历史 953 passed, 1 skipped；当前最新本地回归为 1264 passed, 1 skipped（1265 collected）
- **mypy**: 豁免从 18→14 子模块；P0 包 clean；新增 7 个包从豁免移除
- **PostgreSQL 运行时合同**: 迁移/Worker/原子提交 15 个测试、runtime store 4 个测试、兼容后端 2 个测试通过；仅为 E2/DB-API fake 证据，不能替代真实 PostgreSQL、PITR、崩溃、双 Worker 和经过时间的运行时证据
- **G5 证书**: `NON_CERTIFYING_HISTORICAL`（7/7 不等于计划 16 场景，且非当前 SHA）
- **G7 窗口**: `NON_CERTIFYING_SIMULATED`（fast-forward，不折算真实时间）
- **Mainnet**: PROHIBITED

### 全部 Gate 状态

| Gate | 状态 | 
|------|------|
| G0 Baseline | ⚠️ 当前工作树/秘密门禁 FAIL |
| G1 Single Truth | ❌ FAIL / NOT_VERIFIABLE |
| G2 Data | ❌ FAIL / NEED EVIDENCE |
| G3 Strategy/Research | ❌ FAIL / NEED EVIDENCE |
| G4 Paper/Protection | ❌ FAIL / NEED EVIDENCE |
| G5 Testnet | ⏸ HOLD / NOT_VERIFIABLE |
| G6 Shadow | ⏸ HOLD |
| G7 Unattended | ⏸ NOT_VERIFIABLE（真实窗口重置） |
| G8 Mainnet | 🔒 PROHIBITED — 需独立人工批准 |
