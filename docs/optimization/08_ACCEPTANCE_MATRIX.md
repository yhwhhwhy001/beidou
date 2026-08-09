# 验收矩阵

| Gate | 必须满足 | 失败处置 |
|---|---|---|
| G0 | 范围、权限、当前 SHA/进程/DB 证据齐全 | 停止变更，补取证 |
| G1 | 需求可追溯到任务/测试/证据 | NOT_VERIFIABLE |
| G2 | 目标架构无双执行链、无 fail-open | HOLD |
| G3 | P0 task package 有依赖、rollback、owner | 不进入实现 |
| G4 | pytest、lint、format、mypy、compile、diff、覆盖率（当前配置要求 ≥85%）全部通过 | 不交付；当前覆盖率 66.53%，FAIL |
| G5 | 真实 Testnet 16 场景、订单/保护/对账/恢复证据 | NO_NEW_RISK |
| G6 | 独立红队无未关闭 P0/P1 | HOLD |
| G7 | ≥30 日/≥200 周期 fresh SLI、无 P0、成本与 parity 通过 | 窗口重置 |
| G8 | 用户验收、回滚演练、证据完整 | 不发布 |
| G9 | 签名制品、备份/PITR、watchdog、权限与告警 | 不部署 |
| G10 | 仅在明确授权后允许生产候选；Mainnet 仍需另行批准 | PROHIBITED |

当前补充判定（2026-08-10 00:24）：PostgreSQL v3 执行事实链已有迁移、runtime records、原子写入、`SKIP LOCKED`、lease/fencing、UNKNOWN/死信与不可变事件的代码合同；`beidou_core.engine` 已按 PostgreSQL URL 选择 `PostgresPersistentStore`/`PostgresIntentOutbox`，并在启动时把陈旧代际（包括相同 token 且未过期 lease 的旧 owner）或过期租约的 `SENDING/SENT` 转为持久 `UNKNOWN`，同时要求 001–005 forward-only migration head 完整存在，但连接/schema/fencing 缺失时仍显式 `state_backend_supported=false`。监督器新增新鲜三方对账事实门，监控层 UNKNOWN/unsupported 对账与重复订单身份统一 P0 fail-closed。旧交易所快照自愈入口已显式 fail-closed，行情 WS 停止遵循 `close()` 契约；启动保护恢复现在还会对比交易所 Algo 库存的完整语义（ID、symbol、方向、类型、数量、触发价、`reduceOnly`），Binance Algo 布尔字段采用严格解析；审批订单摘要绑定、挂单 owner 隔离、typed StrategyKernel、闭合 K 线点时元数据、原始 bar index 对齐和 Paper 独立执行观测也已有本地合同测试；R10 近 24 小时重复订单查询、REST/WS 行情输入校验、Adapter ACK 与 typed client-order recovery、ProtectionManager 自身止损方向/有限性边界、余额结构校验和签名策略 preflight 新增了失败优先合同。新增 user-stream 运行时通过 Adapter 创建/续期 listen-key、WS 解析订单/账户事件，并将断线/过期/解析/连续性异常接入 `NO_NEW_RISK` 与 READY/Liveness 阻断；研究 YAML 现强制全字段成本与 policy version 绑定，EvidenceBundle 哈希覆盖稳定性/成本容量/警告，ACTIVE 需要 sealed bundle；Testnet preflight 另外独立校验 G5 证书/计划，最新为 34 项、28 PASS、6 个 P0 FAIL，G7 starter 退出 1。因此这些切片只能提升可交付性，不能把 G1/G3/G5/G7 置为 PASS；真实 PostgreSQL、运行时 schema head、崩溃/双写者、PITR、user-stream replay/gap-fill、跨环境 parity、策略签发和 Testnet 证据仍是硬阻断。

补充：G7 实时追踪器仅接受 canonical `cost_and_pnl_reporting` 证据；运行错误率不能替代成本/PnL。告警 webhook 的持久 delivery sidecar、重试、死信和 UNKNOWN/P0 语义已有本地合同，但外部送达确认仍缺失。

续审（2026-08-10）：本地补齐了交易旁路隔离、G5 plan/Adapter fail-closed、certification provenance/required-evidence、ChaosEngine 独立观测、资本阶梯未知阻断、Feed `MarketDataUnknownError`/monotonic、Health/G7 经过时间 monotonic、PostgreSQL factor-store 显式 DSN/schema 门和 R0-R9 风险策略缺失即 UNKNOWN。新鲜全量回归为 1273 passed、1 skipped（1274 collected）；覆盖率实测 66.77%，G4 仍 FAIL。Bandit/pip-audit 未安装，G3 安全证据仍 NOT_VERIFIABLE；G5/G6/G7 及真实 PG/PITR/user-stream/replay 继续 HOLD。
