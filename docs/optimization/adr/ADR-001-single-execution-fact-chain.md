# ADR-001：唯一风险增加执行事实链

## 状态

Accepted for the convergence branch; production adoption is blocked until the real PostgreSQL, venue and restart evidence gates pass.

## 决策

风险增加只能沿着：

`StrategyKernel → Portfolio/Risk → signed Approval → durable Intent → transactional Outbox → fenced Worker → ExchangeAdapter → typed ACK/user-stream → Order/Fill/Position projections → Protection → Ledger → three-way reconciliation`。

引擎只负责编排、生命周期、时钟、控制命令和健康聚合。策略、研究、控制面不得直接调用交易所；紧急退出也必须使用同一 Outbox/Adapter 路径，只能通过显式 reduce-only/close-position 语义降风险。

## 原因

多条执行路径会让审批、幂等、订单身份和事实投影失去共同边界；网络超时、重复 ACK、重启或成交竞态时，进程内状态不能证明交易所事实。单路径便于审计、重放和 fail-closed。

## 后果

- 任一 UNKNOWN、未保护仓位、未归属订单或对账差异都只能降低风险并阻断新增风险。
- 旧的直接 REST、内存 outbox 或本地自报成功路径不能作为生产实现。
- 迁移期间允许显式诊断 SQLite，但它不能满足 Testnet/生产 READY。

## 验收与回滚

本地合同由 `tests/unit/test_engine_readiness_contract.py`、`tests/unit/test_execution.py`、`tests/unit/test_binance_adapter.py` 和 `tests/unit/test_reconciliation_contract.py` 覆盖。真实采用前必须完成 PostgreSQL migration head、user-stream/gap-fill、重复/超时/部分成交/撤单竞态、重启和三方对账证据。回滚只能回到 schema 兼容的已验证制品并保持 `NO_NEW_RISK`，不得切回未经治理的旁路。
