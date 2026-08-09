# Trading production readiness

- Research validity: FAIL — PIT、标签因果、Purged WFO/CPCV、DSR/PBO/FDR、成本容量证据未完成。
- Market-data integrity: BLOCKED — 当前仍存在运行时 stale heartbeat；闭合 bar/全链 DQ 尚未形成准入证据。
- Testnet/bounded-live evidence: FAIL — 旧 G5 与 fast-forward G7 不具备当前认证效力；当前窗口必须重置。
- Paper-live parity: FAIL — 当前 Paper/Shadow 撮合和生产路径未证明同一执行语义。
- Execution/reconciliation: FAIL — 主链仍含内存 Outbox/账本/对账及未知订单恢复缺口。
- Permissions/exposure/risk limits: BLOCKED — 本轮不读取或修改交易所权限；历史证据不能证明提款已禁用。
- Kill switch/protective controls/recovery: FAIL — 当前只完成本地 authority fail-closed 切片；保护 owner/ACK/generation 尚未完成。
- Logs/metrics/alerts/operators: BLOCKED — LaunchAgent、告警送达、备份恢复和独立 watchdog 尚未完成。
- Profitability disclaimer: 无法证明或保证持续盈利；当前不存在统计可信 Alpha 准入证据。
- Trading authorization status: NOT_AUTHORIZED；Testnet HOLD，Mainnet PROHIBITED。

## Decision: NOT_READY
