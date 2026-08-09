# 全项目深度审查报告

## P0 结论

- **运行态与证书失真**：旧实例在心跳和订单链已失效时仍暴露 HEALTHY/READY/RESUME 语义，必须先修 autoridad interlock 和停机/恢复证据。
- **执行事实链不闭合**：SQLite 旧实例含 `NEW/UNKNOWN`，缺少成交/持仓投影/保护事实；不能证明订单、成交、仓位和保护一致。
- **保护覆盖不可只数数量**：必须校验 venue ID、symbol、side、quantity、trigger、owner、generation 和 ACTIVE ACK。
- **研究输入不能默认闭合**：缺失 `is_closed`、PIT availability 或 manifest 时只能 NOT_VERIFIABLE。

## P1 结论

- Backtest/Paper/Shadow/Testnet 仍需同一 StrategyKernel 的可重放 parity 证书。
- Paper 必须把 QUEUED/REJECTED/PARTIAL 与实际成交、手续费、spread/slippage 分开记录。
- PostgreSQL 事务存储、外部加密备份/PITR、user-stream gap-fill 和双 worker 混沌测试尚未完成。

## P2 结论

- 只有在事实链和研究门通过后，才优化因子、容量、路由和无人值守成本；不能用参数调优掩盖证据缺口。

详细缺口见 `02_GAP_MATRIX.md`，源代码追踪见 `03_SOURCE_TRACE.md`。

