# V3 执行事实链收敛

## 结论

本轮把若干危险的“本地看起来成功”路径改成可持久、可拒绝、可重验的链路切片，但没有把项目宣称为生产就绪。Testnet 继续 `HOLD`，Mainnet 继续 `PROHIBITED`。

## 已落地的安全边界

### 1. 唯一交易传输边界

- `AutonomousEngine` 的同步/异步读写调用均经 `BinanceUsdmAdapter.request`。
- 下单经 `BinanceUsdmAdapter.create_order(OrderRequest)`，`reduce_only` 映射到交易所 `reduceOnly=true`。
- Adapter 缺失 transport、请求失败或写互锁未通过时返回错误/UNKNOWN；不会伪造 ACK。
- 架构测试禁止引擎直接调用 REST `request/reset`，并验证订单写入口。

这仍不是完整的生产 Adapter 合同：Algo 订单生命周期、用户数据流、trade/funding/手续费补洞、精确错误分类和按 client ID 的恢复仍需完成。

### 2. Durable Intent/Outbox

- `PersistentStore` 与 `IntentOutbox` 绑定同一个配置数据库路径；SQLite 使用 WAL、`synchronous=FULL`。
- `PENDING → SENDING → ACKED/FAILED/UNKNOWN` 状态持久化；进程重启会把中断中的 `SENDING/SENT` 变为 `UNKNOWN`，禁止盲重发。
- 只有 `claim()` 取得的 PENDING 意图进入发送边界；UNKNOWN 必须先用交易所事实查询裁决。
- 风险批准签名、nonce、过期时间、策略/快照哈希以及 `net_alpha_bps/predicted_cost_bps` 随意图序列化，重启后不可丢失。
- 最终写边界重新验证审批状态、签名、nonce、有效期和 reduce-only 语义。

未完成：PostgreSQL 事务 Outbox、审批/意图原子事务、租约 fencing、双写者测试、交易所事件 gap-fill 和完整 UNKNOWN 恢复合同。

### 3. 订单—成交—账本持久化切片

- 复式 `LedgerTransaction` 与全部 `Posting` 写入 `ledger_transactions/ledger_postings`，按交易 ID/Posting ID 幂等。
- 启动时从 durable journal 重建内存 ledger；只有旧聚合分录而没有新 journal 时拒绝启动，避免静默迁移。
- 成交后的保护定义先持久为 `PENDING`；只有交易所返回 `algoId` 才持久为 `ACTIVE`。
- 交易所精度、盘口深度、行情、成本或 Alpha 输入未知时，风险增加订单被拒绝；不再用固定报价、默认精度或默认最小数量补齐。
- Paper 撮合异常、缺少双边盘口或未闭合 K 线时拒绝/不验证，不回退到即时成交。

未完成：成交增量以 trade ID/事件序列幂等、手续费/资金费入账、权威 PositionProjection、重启后订单/保护回灌、三方只读对账和保护 owner/position generation。

### 4. 启动与保护安全

启动恢复只读盘点未归属的条件单，不再无条件取消交易所全部 Algo 单。任何取消或替换必须由持久 owner、position generation 和人工/治理状态机授权。

## 验收证据

- `tests/unit/test_binance_adapter.py`：Adapter 订单、撤单、查单和缺失 transport。
- `tests/unit/test_execution.py`：Outbox 重启 UNKNOWN、幂等和经济字段保留。
- `tests/unit/test_store.py`：保护 PENDING 不恢复为 ACTIVE、账本 journal 往返/重放。
- `tests/unit/test_market_data.py`：Adapter Result 解包、双边盘口、未闭合 REST bar 拒绝。
- `tests/architecture/test_architecture.py`：唯一 Adapter、无合成行情/精度/启动取消旁路。
- 全套 pytest、Ruff、`py_compile` 必须在当前 commit 重新执行；任何一项失败都不能进入 G5/G7。

## 当前阻塞

1. 运行中的 Mac 实例曾出现心跳、卡死订单链和 READY 语义矛盾；本轮未重启、停机或读取交易所事实，不能把本地测试当作运行态修复。
2. SQLite 只是本地 durable slice，不是 PostgreSQL/PITR 生产事实库；对账仍不是独立三方事实源。
3. 保护路径仍缺 owner/generation/完整条件单 ACK 语义，部分恢复分支仍需改为只读分类后治理。
4. 真实 G5 16 场景和全新真实 30 日 G7 尚未执行；旧证书不可继承。
