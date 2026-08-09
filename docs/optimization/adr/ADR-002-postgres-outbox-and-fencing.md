# ADR-002：PostgreSQL 作为执行事实源，Outbox 使用租约与 fencing

## 状态

Accepted for implementation; not verified against a live PostgreSQL instance.

## 决策

Testnet/生产使用同一配置的 PostgreSQL 持久化 Store 和 Intent Outbox。Approval、Intent、Outbox 初始事件、运行投影和不可变事件在事务边界内写入；Worker 使用 `FOR UPDATE SKIP LOCKED`、lease owner、fencing token、ACK/UNKNOWN、有限重试和死信。旧代 worker 的发送和 ACK 必须被 fencing 拒绝。

连接、schema head、租约或 fencing 任一 UNKNOWN 时，应用可以建立显式诊断 SQLite 以保存阻断证据，但 `state_backend_supported=false`，不能 READY，也不能提交风险增加订单。

## 原因

进程内字典和双 SQLite 文件无法在崩溃、双实例、网络分区和重启后证明唯一事实。事务 Outbox 与 fencing 是防止重复发送、旧 worker 复活和投影分裂的最小基础。

## 后果

- 需要 forward-only migration、备份/PITR、恢复 replay 和双 worker chaos 证据。
- 真实数据库不可用时系统更可能停机或进入 `UNKNOWN`，这是资金安全优先的预期行为。
- 本地 fake DB-API 合同只能证明接口形状，不得升级为生产证明。

## 验收与回滚

使用 `migrations/003_execution_outbox.up.sql`、`004_runtime_records.up.sql`、`005_risk_approval_intent_hash.up.sql`，并通过 `tests/unit/test_postgres_outbox.py`、`tests/unit/test_postgres_store.py`、`tests/unit/test_migrations.py`。真实验收必须覆盖 migration head、连接池、崩溃、ACK 丢失、UNKNOWN 裁决、PITR 和恢复后 replay。回滚采用前向补偿迁移，不执行未经验证的逆向删除。
