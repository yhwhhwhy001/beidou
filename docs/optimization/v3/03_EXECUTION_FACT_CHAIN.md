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
- `source_event_id` 在 durable journal 上有唯一约束；同事务重放幂等，跨事务争用同一成交事实会显式冲突并触发 fail-closed。
- 启动时从 durable journal 重建内存 ledger；只有旧聚合分录而没有新 journal 时拒绝启动，避免静默迁移。
- 成交后的保护定义先持久为 `PENDING`；只有交易所返回 `algoId` 才持久为 `ACTIVE`。
- 交易所精度、盘口深度、行情、成本或 Alpha 输入未知时，风险增加订单被拒绝；不再用固定报价、默认精度或默认最小数量补齐。
- Paper 撮合异常、缺少双边盘口或未闭合 K 线时拒绝/不验证，不回退到即时成交。

已补齐一段本地 durable slice：累计 `executedQty` 先转换为增量，`fill_events` 唯一键阻止重复记账；`position_projection` 保存 signed quantity、entry price 和 generation；重启恢复只使用 durable order/fill/projection 事实。成交事件现在经历 `PENDING -> COMMITTED`：只有账本和持仓投影都成功落盘后才关闭事件；账本/投影/索引写失败会冻结账本、切 `NO_NEW_RISK`、发 CRITICAL，并将订单置为待治理 UNKNOWN，不会盲重试。

账户对账的 system side 不能再用零余额占位。新增 `account_opening_projections`，要求完整余额/基线、来源、版本、证据哈希和审批 ID；缺少授权 opening fact 时系统事实保持 `INCOMPLETE`。

Algo/条件单的库存、创建和撤销已统一经过 typed Adapter：必须有 `algoId/symbol/side/orderType/triggerPrice/algoStatus` 等 venue ACK 字段；缺字段的库存或 ACK 直接 UNKNOWN。用户流事件统一解析，缺少单调序列或出现 gap 时由 `UserStreamSequencer` 标记 `SEQUENCE_UNAVAILABLE/GAP`，要求独立 REST replay/对账后才能恢复信任。

新增的本地切片：`UserOrderUpdate` 与 `UserAccountUpdate` 先经 Adapter 校验，再由
`UserStreamProjector` 以 `user_stream_events PENDING → APPLIED` 和
`user_stream_projections` 持久化；重复事件幂等，同一事件 ID 对应不同原文直接冲突。
`ACCOUNT_UPDATE` 的余额/仓位是绝对值增量行，不被误当作完整快照；只有带完整事实、来源/版本、
证据哈希和审批 ID 的显式 replay baseline，且至少收到一条 replay 后用户事件，才会标记第三方
事实 `complete=true`。重启恢复高水位后默认进入 `GAP`，必须重新授权 replay；缺序列、断档或
投影异常均不自动补洞。`ReconciliationEngine.compare_three_way` 现在比较 system / exchange
REST / event-stream 三方，且引擎只提供注入边界，不在本地伪造 WebSocket 或用 REST 复制用户流事实。

仍未完成：交易所真实 gap-fill/replay 取证、手续费/资金费入账、完整 OrderAggregate、
PostgreSQL/PITR、以及全量 owner/generation 条件单精确匹配与治理恢复。

### 4. 独立对账与运行态语义

- `ReconciliationEngine.compare` 是无副作用纯比较：缺少一侧、未来/过期、字段不完整、key 不一致都返回阻断状态。
- 每轮账户与普通挂单快照分别持久到 `reconciliation_snapshots`，结果持久到 `reconciliation_results`；失败立即执行 `NO_NEW_RISK` 并产生 CRITICAL 事故。
- 三方对账结果会分别保存 `SYSTEM`、`EXCHANGE`、`EVENT_STREAM` 快照；缺任一方、序列不连续、
  投影不完整或三方任意差异都保持阻断，不能以两方匹配覆盖第三方缺失。
- 活跃运行路径不再调用自愈/复制交易所状态；`/health`、`/ready`、`/trading-ready` 绑定心跳、控制面、对账结果和保护 owner 语义。
- 本地账户 opening balance 尚未有独立可审计来源，因此当前 system fact 明确为 `INCOMPLETE`，不会伪造 MATCHED。

### 5. 启动与保护安全

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
2. SQLite 只是本地 durable slice，不是 PostgreSQL/PITR 生产事实库；三方比较骨架已接入，但
   user-stream 仍缺完整账户余额事实与独立 replay/gap-fill，因此不能得到生产 MATCHED。
   配置为尚未接入的 PostgreSQL/其他后端时，运行时只保留诊断 SQLite，不再把它报告为 READY；`/ready` 和 `/trading-ready` 返回 `STATE_BACKEND_UNSUPPORTED`。
3. owner/session/generation 字段、ACK 后 ACTIVE、typed Algo 字段回读、事件 journal/projector
   和重启后 replay gate 已接入，但完整 venue user-stream 账户事件、跨进程治理恢复和真实 ACK
   验收仍未完成。
4. 真实 G5 16 场景和全新真实 30 日 G7 尚未执行；旧证书不可继承。
