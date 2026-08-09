# 剩余风险

> 证据刷新（2026-08-10 00:24）：当前 HEAD（最近观测）`6b335d3246dd8597d7d636d1067e971b94813450`，工作树 116 个 Git status 条目（95 个已跟踪、21 个未跟踪）；只读 preflight 为 34 项、28 PASS、6 个 P0 FAIL（脏工作区、G5 certificate、signing key、签名风险策略、PostgreSQL authority connection、fencing token）。独立 Testnet 账户事实为 `canTrade=true/canWithdraw=true`，账户能力门禁 FAIL；PostgreSQL 只读连接/迁移头探针因未注入密码失败，外部 Testnet 运行已被门禁阻断，G7 starter 退出 1，未形成 READY 证书。以下风险仍未解除。

- 当前工作树 DIRTY，代码未提交/部署；外部 Testnet 启动进程已失败，不能视为已通过门禁。任何未来 Testnet 启动仍应被 preflight/构造验证阻断直到制品与秘密边界齐全。
- 当前 Testnet API key 具备 venue 提现权限（`canWithdraw=true`）。这是 P0 凭据治理失败；在交易所侧关闭提现并取得新鲜只读证明前，禁止任何 writable Testnet/G5/G7 窗口。代码不再把该事实改写成 `False`。
- Binance 大响应的 `urllib` `IncompleteRead` 已由 `httpx` framing 修复并有单元证据；该代码尚未部署，运行实例仍不能继承修复结论。
- 5432 端口虽有 PostgreSQL 监听，Beidou DSN 未注入密码；新的 preflight 只读 authority probe 以 P0 `preflight.state_backend_connection` 阻断连接失败、表缺失、migration head 缺失或 checksum drift。不得把宿主上的其他 PostgreSQL 服务或未认证连接当作 Beidou 权威库。
- SQLite 不是外部灾备；没有已验证的 PG/PITR、异机恢复和密钥托管。
- PostgreSQL v3 Outbox/`PostgresPersistentStore` 已进入引擎的后端选择路径，但只有 DB-API fake 证据；缺真实 migration head、连接池/租约、崩溃、双 Worker fencing、ACK 丢失、UNKNOWN 裁决、PITR 与恢复后 replay 证据。连接/schema/fencing 不完整时仍应保持 blocked，不能把诊断 SQLite 当作生产事实源。
- 交易所 user-stream 已有本地 listen-key/WS/故障降级合同，但真实 user-stream、REST gap-fill、Algo owner/generation 恢复尚未验证。
- 交易池和因子证据不足时会保持关闭，这是正确结果，不应手工激活。
- 本地测试、历史证书、健康端点、模拟成交和正 IC 都不能证明未来盈利。
- Python/运行时、远端 CI、时钟、磁盘、网络、watchdog 和权限混沌仍需实机证据。
- CI 覆盖率门当前实测 66.53%（阈值 85%）；`-W error::ResourceWarning` 全量回归为 1264 passed、1 skipped 且已清零资源警告，但不能用全量测试通过替代覆盖率与生产恢复验收。
- 研究配置与证据绑定缺口已在本地收紧：policy version、generation/fast-screen/WFO/cost 全字段均须来自 YAML；`EvidenceBundle` 哈希覆盖稳定性、成本/容量和警告；ACTIVE 因子须提供 sealed 且 factor/dataset/policy 一致的 artifact。仍缺真实外部数据 manifest、独立 OOS/成本/容量结果及重放证据，不能把本地合同通过当作 Alpha 或盈利证明。
- G5 证书校验现已前置到 Testnet preflight；当前证书/计划无法独立验证，因此新增 P0 `preflight.g5_certificate`，G7 不能绕过该门创建窗口。
- G7 窗口状态已改为 schema v2 完整证据原子持久化并支持重启恢复；历史只含计数的快照与 fast-forward 模拟样本均被 fail-closed 标记为 `NOT_VERIFIABLE`，真实窗口尚未开始。
- G7 starter 已删除启动时的合成 PASS SLI，并复用 Testnet preflight；但真实 Supervisor 周期、user-stream、保护、对账、成本/PnL 和 30 日经过时间证据仍未形成，因此 G7 继续 `NOT_VERIFIABLE`。
- G7 fast-forward 入口已通过公开窗口 ID API 创建模拟状态，避免随机孤儿文件和私有映射篡改；模拟窗口仍明确 `NOT_VERIFIABLE`，真实 30 日窗口尚未开始。
- 告警 webhook 已有持久 delivery sidecar、重试/死信和 P0/P1 检查合同；真实双通道、外部送达确认、轮转和独立 dead-man 尚未在 Mac 实机验证。
- Supervisor 已接入人工启动的 G7 窗口生产链，但当前没有权威 `cost_and_pnl_reporting` 检查，真实窗口仍会被成本/PnL SLI fail-closed 阻断，必须先接入独立账本/成本事实。
- 运行时探针已禁止绕过 typed StrategyKernel；闭合 K 线元数据、原始 bar index 样本对齐和 Paper 执行观测去自标已通过本地合同，但真实跨环境 parity、独立未来标签和 OOS 统计仍未验证。
- 全仓库 Ruff/format 已通过（419 files）；这只关闭静态格式阻断，不等于关闭事实链、数据库、Testnet 或收益门禁。
- 账户权限监控已在运行时持续检查 venue `canTrade/canWithdraw`；但当前 Testnet 仍报告 `canWithdraw=true`，因此该 P0 是真实配置阻断而非监控误报。关闭提现并取得新鲜只读快照前，禁止 writable/G5/G7。
- 硬编码扫描本轮无阻断项；生产制品仍需逐项审计 localhost/tmp 路径与批准边界。
- 本地 `.venv` 未提供 Bandit；`pip-audit` 需要外部 PyPI 查询，本轮因无结果/超时中止，安全供应链门仍 `NOT_VERIFIABLE`。
- 风险审批订单摘要与当前 worker 挂单归属已加入本地 fail-closed 合同；真实多切片执行、重启后的 owner adoption、交易所 ACK/订单归属和关机语义仍未验证。任何未归属 venue 挂单都必须保持 `NO_NEW_RISK`，不得人工改内存集合绕过。
- 旧 `scripts/certify_72h.py` timer-only 认证入口已退役并 fail-closed；现有/历史 manifest 只能显示为 `NOT_VERIFIABLE`，不能转化为 G7 证书。
- 特征缺失、非法 OHLCV、活动持仓无清算价、保护计数缺失、未校准 Sharpe 或重复订单计数无权威查询现在都会阻断新增风险；R10 已提供持久化 Outbox 的身份计数查询，但内存/缺身份/查询失败仍是 UNKNOWN，这暴露了真实快照、独立账本和 durable outbox 查询仍未在 Testnet/生产验证，不能用“UNKNOWN 全部通过”绕过。
- REST K 线与 WS 双边报价现在有有限性、时间、OHLCV 一致性和 `0 < bid <= ask` 门禁；仍缺真实 Binance 回包异常、WS 断流、REST gap-fill、序列缺口和告警送达的实机证据。
- 订单 ACK 与 client-order recovery 已有 Adapter 身份/语义绑定；仍缺真实模糊提交、重复响应、网络分区、查询竞态、user-stream gap-fill 和 venue 重启重放证据。
- PostgreSQL Outbox 已增加当前进程代际 owner 隔离；真实 PostgreSQL 中仍需验证同 token/未过期 lease 的旧进程恢复、双 Worker fencing、锁竞争、提交后崩溃和 UNKNOWN 裁决，不得把 SQL fake 结果当作恢复证明。
- 旧 `_sync_exchange_state` 自愈入口已 fail-closed，WS 停止已修正为 `close()`；可写引擎已有本地 user-stream listen-key 创建/续期/解析/故障降级契约，但真实 listen-key、重连、授权 replay、gap-fill、三方恢复状态机和部署切换仍未实测，写模式因此继续 HOLD。
- ProtectionManager 与余额 Adapter 已增加本地 fail-closed 边界，签名风险策略也已成为 Testnet preflight/READY/最终发送的必要证据；仍缺真实策略签发、密钥托管、部署切换和交易所回包验证。
- 保护重试已删除运行时 trigger widening；启动恢复会在 ACK-backed durable rows 与 venue Algo inventory 语义一致时重建本地投影，避免重启重复下发。若 inventory、持仓、owner/generation 或订单年龄不完整，自动清理和重建保持阻断；真实重启、崩溃、触发/部分成交仍未在 Testnet 验证。
- 先前外部自动提交 `ab5897c` 清理了若干已跟踪的自动生成 evidence artifacts；本轮未用 destructive Git 操作恢复，历史证据保留/恢复需单独授权和核对，不能把当前目录缺失视为“从未发生”。
