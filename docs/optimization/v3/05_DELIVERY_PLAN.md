# 北斗优化重构与生产化交付方案

## 目标拆解

“24 小时无人值守并持续盈利”必须拆成四个互相独立的命题：

1. **不误下单**：任何 UNKNOWN、过期审批、陈旧行情、裸仓风险或事实链不一致都只能减少风险，不能增加风险。
2. **可恢复且可审计**：每个 Intent、交易所 ACK、成交、保护、账本和对账差异能在崩溃/重启后重放，不能同源自证。
3. **策略有统计优势**：在 PIT、sealed OOS、净成本、容量和多重检验下仍可证伪地为正；不能从历史 PASS 推出持续盈利。
4. **无人值守可运营**：进程、数据库、备份、告警、时间、磁盘、权限和锁状态在故障时 fail-closed，并有独立 watchdog。

## 依赖序列与退出条件

### P0：冻结与取证

- 保持 Mainnet 禁止；Testnet HOLD。
- 对当前实例先保存本地 DB/WAL、日志、supervisor state、Git SHA；需要交易所快照或停机时单独取得授权。
- 任何 P0 都持久化 `NO_NEW_RISK`，不把 `/health`、`/ready` 或历史证书当作交易所事实。

### P1：唯一事实链

- PostgreSQL schema + migration + WAL/PITR；Approval+Intent+Outbox 原子事务。
- 本轮已增加前向 V3 schema/checksum runner 与本地 SQLite 在线备份校验；退出条件仍要求真实
  PostgreSQL 事务存储、外部加密备份/PITR、异机恢复演练和密钥托管，不能把本地副本当作灾备完成。
- Fenced executor、唯一 Adapter、typed 用户数据流、显式 replay baseline 与 REST gap-fill。
- OrderAggregate、FillStore、PositionProjection、保护 owner/generation、双式账本、三方只读对账。
- 保护覆盖必须绑定交易所 ACK 的 `algoId/orderId`；监控器自身异常必须输出阻断事实，不能通过“没有结果”形成 PASS。
- 退出条件：崩溃前/后发送、ACK 丢失、重复 client ID、部分成交、撤单竞态、保护拒绝和双 worker 测试全部通过；未知状态不自动恢复。

### P2：研究与环境一致性

- ClosedBar/PIT/manifest、Purged WFO/CPCV/DSR/PBO/FDR、净成本和容量。
- 一个 StrategyKernel 覆盖 Backtest/Paper/Shadow/Testnet；禁止 Paper 即时成交兜底。
- 退出条件：研究门和 parity 门均 PASS；没有任何“仅单元函数通过”的替代证据。

### P3：真实 Paper → Shadow → Testnet

- 先 safety-only，再 ≥7 天 Shadow；之后当前 commit 从第 0 天开始真实 Testnet G5/G7。
- 16 项 G5 场景全部真实执行；≥30 天、≥200 周期，无 P0、孤儿单、未保护仓位、未解释账本差异。
- 任一 P0、证据中断、重启未重放、成本低估或 parity 差异都重置窗口。

### P4：生产候选（本轮不授权）

- 专用账号/主机、Keychain/受控秘密提供器、loopback 控制面、版本化签名制品、独立 watchdog、加密备份/PITR 和恢复演练。
- 必须有具名人工批准；G7 PASS 不是 Mainnet 授权，更不是盈利保证。

## 回滚与停止规则

- P0：先停止新风险，保留交易所原生保护，保存证据；不自动撤单/平仓，除非另有治理授权。
- 数据库只做向前兼容迁移；灾难恢复先在隔离实例验证，再重放和三方对账。
- 代码回切只允许 schema 兼容的已签名制品；恢复交易仍需重新对账和人工批准。

## 本轮交付边界

已完成的是 fail-closed、Adapter/Outbox/账本/保护状态、三方对账骨架、typed 用户账户事件与显式 replay gate、闭合 bar/研究门和可验证证书切片；未完成 PostgreSQL、真实交易所 gap-fill、完整保护 owner/generation、真实 G5/G7、生产运维和 Alpha 证明。因此本轮的正确交付决策是 **PIVOT / HOLD**，而不是“已可 24×7 盈利运行”。
