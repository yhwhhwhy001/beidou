# 最终验收报告（当前轮）

> 证据刷新（2026-08-10 00:24）：当前 HEAD（最近观测）`6b335d3246dd8597d7d636d1067e971b94813450`，工作树有 116 个 Git status 条目；含凭据存在性检查的只读 preflight 共 34 项，28 PASS、6 个 P0 FAIL（脏工作区、G5 证书不可验证、signing key、签名风险策略、PostgreSQL authority connection、fencing token）。Testnet 账户独立只读事实为 `canTrade=true/canWithdraw=true/canDeposit=true`、非零仓位 0、普通/Algo 挂单均为 0，因此账户能力门禁 FAIL；PostgreSQL 只读连接/迁移头探针因未注入密码失败，外部 Testnet 运行仍被门禁阻断。

## 结论

**HOLD / NOT READY FOR UNATTENDED TRADING**。

本轮代码切片完成了若干 fail-closed 修复并有本地测试证据，但没有完成真实交易所快照、真实 G5/G7、PostgreSQL/PITR、gap-fill、parity、观察窗口或盈利证据。不能交付“24 小时持续盈利运行”声明。

历史 06:39 UTC+8 只读运行态曾反证证书语义：旧 SHA 进程心跳 stale、BNBUSDT 保护 P0、监督 `trading_ready=false`，却同时暴露 `/health=HEALTHY`、`/ready=true`/`RESUME`；`/trading-ready` 才返回 503。该实例证据不能继承为当前健康证明。

本轮本地门禁：`.venv/bin/pytest tests/ -q -W error::ResourceWarning` 为 **1264 passed, 1 skipped（1265 collected）**；全仓库 Ruff/format、全关键包 mypy、compileall、`git diff --check` 通过。覆盖率命令实测 **66.53%（终端总计约 67%）< 85%**，因此 G4 仍 FAIL；资源警告门已清零。上述只证明当前代码/合同测试，不证明新代码已部署或运行实例已切换。

本轮授权后的外部只读核验：Binance Testnet server time、exchangeInfo、position mode、account、普通挂单与 Algo 查询成功，当前非零仓位 0、普通/Algo 挂单数均为 0。账户权限事实显示 `canTrade=true/canWithdraw=true/canDeposit=true`，修复后的能力检查返回 FAIL；本次只读输出未落盘密钥或原始账户余额，未执行任何订单、撤单、仓位或余额修改。

最新 preflight 脱敏证据写入 `artifacts/evidence/testnet/preflight-20260809T145003Z.json`，只记录检查数量、P0 检查 ID、错误类型和迁移版本，不记录 DSN、错误原文或任何密钥值。

G7 starter 已修正证据语义：启动前必须通过 Testnet preflight；G5 场景从独立 G5 plan 校验；新窗口不再注入合成 PASS SLI，首批样本必须来自真实 Supervisor monitoring cycle。本轮真实执行退出码为 1，6 个 P0 preflight blocker 阻断窗口创建，未形成 G7 窗口。

本轮新增研究准入修复：YAML mining policy 现在完整绑定 policy version 与 taker/maker/spread/slippage/funding/impact 成本字段；EvidenceBundle hash 覆盖 stability/cost-capacity/warnings；ACTIVE 因子晋级必须提供封存且与 factor/dataset/policy 匹配的 bundle。缺少 provenance、sealed bundle 或 policy 绑定时保持 `NOT_VERIFIABLE`，默认候选网格不再伪装成生产研究事实。

此前 REST 大响应出现 `IncompleteRead` 的根因已修复：同步边界使用 `httpx` 处理压缩/分块 framing，并把 HTTP >=400 转回原有错误分类；新增确定性回归覆盖大响应与 503 taxonomy。该修复已通过全量回归，但尚未部署。

本轮新增的 PostgreSQL 切片证明：迁移定义了 v3 Approval、Intent、Transactional Outbox、runtime records 和不可变事件表；`PostgresPersistentStore`/`PostgresIntentOutbox` 在 PostgreSQL URL 下被引擎选择，连接/schema/fencing 任一缺失仍保持 `state_backend_supported=false`；`OutboxWorker` 具备 `SKIP LOCKED`、lease、fencing、ACK/UNKNOWN/死信和旧代恢复合同。恢复现在按当前进程代际隔离旧 owner，即便 token 相同、lease 未过期也转为 UNKNOWN。Testnet preflight 现在还会在写引擎构造前只读核对必需表、001–005 migration head 和 checksum，任何失败都生成 P0；相关单测仍为 DB-API fake，真实 PostgreSQL、崩溃、双 Worker、PITR 或 Testnet 证据仍缺失，故 BD-T08/G1/G3 仍为 `NOT_VERIFIABLE`。

G7 证据切片新增：无人值守窗口状态现以 schema v2 原子写入完整 SLI/事故/日报，进程重启可恢复；旧的 counter-only 快照和 fast-forward 模拟样本显式标记为 `NOT_VERIFIABLE`，不能因重启或计数回填而签发真实证书。该切片仍不替代真实 30 日 Testnet 窗口。

告警切片新增：webhook 送达状态写入持久 sidecar，失败可重试、超过上限进入死信；CRITICAL/LOCKDOWN 的待送达、死信或状态 UNKNOWN 会生成运行时 P0，不能继续作为“告警可见”证据。

G7 producer 已接入 Supervisor：仅对人工启动的 RUNNING 窗口持久化每个监控周期和幂等日报；P0 产生事故并重置窗口，不会自动启动或签发 G7。

G7 fast-forward 评估入口已改为通过公开窗口创建 API 绑定 operator-supplied ID，消除随机孤儿状态文件和私有映射篡改；该路径仍只用于框架验证，任何模拟结果都不能计入真实经过时间。

账户权限监控已接入每次 Supervisor monitoring cycle：可写模式持续要求新鲜 `canTrade=true/canWithdraw=false`，提现权限重新开启、交易权限关闭或权限快照 UNKNOWN/stale/future 均立即生成 P0 并撤销新增风险；只读模式仅记录写能力关闭。该门禁仍未形成可写 READY，因为当前 Testnet 只读快照显示 `canWithdraw=true`。

订单审批与归属切片新增：HMAC 额外绑定 `risk_intent_hash`，最终发送会拒绝订单字段篡改；执行计划会验证总量不超过批准量、符号/方向/订单语义与确定性 slice client ID。启动恢复的 venue 挂单不自动认领，关机只撤销当前 worker 已确认归属的挂单；未归属订单保持未触碰并触发 `NO_NEW_RISK`。尚未有真实 venue/重启证据。

本轮追加的研究/运行语义收敛：运行时算法探针只调用 typed StrategyKernel，VETO/NO_ACTION 保留确定性证据；行情特征显式携带闭合 K 线与点时可用时间；因子挖掘在清洗后按原始 bar index 交集对齐 WFO、交互和残差样本；Paper/Shadow 的成交记录只作为独立执行与成本观测，不再把预测方向复制成实际标签。上述均有本地回归，但仍不等于真实跨环境窗口、独立标签或盈利证明。

本轮新增的缺失事实阻断：FeatureNode 对缺失/非有限输入返回 `BLOCK`，ClosedBarNormalizer 对缺失/非法 OHLCV 返回 `INVALID`；R7 仅在明确 flat 事实时接受无清算价，R8 要求显式保护/持仓计数，Nearline 风险上下文缺失 Sharpe、重复订单证据时保持 `UNKNOWN`。这些修复有 106 项定向回归和全量回归证据，但会继续暴露未接入的生产事实源，不能被解释为交易能力已恢复。

本轮追加的保护恢复幂等边界：重启遇到 durable ACTIVE 保护时，先用 venue Algo inventory 对照 owner、generation、symbol、方向、类型、数量、trigger 和 reduce-only，再重建本地 `PositionProtection`；已 ACK 保护不会再次创建。保护重试保留已批准 trigger，幽灵/超量清理要求新鲜匹配三方对账、`RESUME` 和完整订单年龄；任何未知均保持 `NO_NEW_RISK`，新增合同测试已通过。

本轮补充的事实边界：R10 已从持久化 SQLite/PostgreSQL Outbox 查询带身份的近 24 小时 `client_order_id`/`idempotency_key` 重复计数；内存队列、缺身份或查询失败仍返回 `UNKNOWN`。REST K 线解析拒绝结构/时间/有限性/OHLCV 一致性违规；WS ticker 只有明确有效双边报价才进入实时执行，异常时回退 REST。定向回归为 106 项；这些均是本地合同证据，未证明真实数据库、交易所断流回补或实盘链路已生效。

本轮追加的重启与自愈边界：旧 `_sync_exchange_state` 入口已改为显式 fail-closed，不能从单一交易所快照删除本地状态或重建保护；行情 WS 关闭使用真实 `close()` 协议；可写监控对账必须引用新鲜三方 `MATCHED` 结果，否则即使 REST 与本地投影相等也报告 P0 FAIL。新增切片通过全量回归，但真实 user-stream/replay/gap-fill 和恢复状态机仍未接入实机。

本轮新增的 user-stream 运行时切片：可写引擎现在必须由 Adapter 创建并续期 listen-key，通过 Binance user-data WS 解析订单/账户事件；连接、续期、断线、过期或解析异常均进入 `DEGRADED/NO_NEW_RISK`，没有 REST-only 可写回退。READY、Liveness 和 Supervisor runtime safety 还要求新鲜事件、连续性投影无 GAP/UNKNOWN 且事实完整；未完成授权 replay/gap-fill 时仍然 `NOT_READY`。该切片有本地生命周期、keepalive、解析和就绪边界测试，但没有真实 Testnet listen-key、重连、回补、崩溃或部署证据。

订单事实边界继续收紧：Adapter 订单 ACK 必须绑定完整身份、状态、数量及 reduce-only 语义；重复 client ID 恢复通过 typed client-order query 后还要再次验证 side/type/数量。新增合同测试仍不能替代真实 Binance 模糊提交、超时、重复响应和重启重放证据。

保护与策略配置边界继续收紧：ProtectionManager 自身拒绝非有限/非正仓位、零距离或反向止损、无止损止盈和非法止盈目标；Adapter 余额回包拒绝结构缺失和非有限 Decimal，并保留已验证的有符号余额。Testnet preflight、READY 和最终发送现在都要求有效签名风险策略；缺失策略只允许经过控制面/事实链的显式 reduce-only/close-position 退出，不允许风险增加。这些是本地合同证据，不替代真实密钥托管、策略签发和部署验证。

紧急平仓路径已移除 `PositionManager` 对引擎 REST 的直接调用，改为调用 `enqueue_reduce_only_market`；该方法生成短期、绑定策略签名和订单事实的 Approval，仍须经过正常控制门与持久化 Outbox。此处有单元回归，但没有真实交易所平仓证据。

## 必须先完成

- 先按已授权范围保存旧运行实例全部证据，再核对活跃订单归属后进行安全处置；
- fresh authority/readiness 与实际写入互锁一致；
- 真实 PG/WAL/PITR、运行时 Outbox 接线、user-stream listen-key/replay/gap-fill、保护 exact match、crash/chaos；
- 真实 Paper/Shadow/Testnet 窗口和独立红队复核。
- 注入独立 Beidou PostgreSQL 的 DSN/密码、签名密钥、签名策略和 fencing token 后，重新运行 preflight；当前提现权限必须先在交易所侧关闭并由新的只读快照证明。
