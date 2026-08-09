# 优化重构 PRD（证据优先）

## P0 必须实现

1. 新鲜 authority contract：任何 heartbeat、reconciliation、protection、order trace、DB/clock failure 都撤销 RESUME。
2. 原子交易事实：Approval、Intent、Outbox、ACK、fill、position、protection、ledger 可重放。
3. UNKNOWN 语义：不猜测、不自动补发、不自动恢复；人工处置后从三方事实重新验证。
4. 研究可证伪：closed bar、PIT、manifest、Purged WFO/CPCV、FDR/DSR/PBO、成本容量。

## P1 必须实现

5. 单一 StrategyKernel parity；Paper/Shadow 真实撮合和独立成本；Testnet challenger window。
6. user-stream sequence/replay/gap-fill、PostgreSQL、外部加密备份/PITR、恢复演练。

## P2 可优化项

7. 只有在 P0/P1 完成后，研究因子、容量、路由、adaptive sizing、告警降噪和资源成本。

## 非目标

- 不承诺持续盈利；不自动启用 Mainnet；不绕过人工恢复或风险审批；不以 UI/健康端点替代交易所事实。

