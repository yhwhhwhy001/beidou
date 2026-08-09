# 北斗全项目优化审查基线

更新时间：2026-08-10 00:24（本地工作树；安全覆盖尚未部署）

当前 HEAD（最近观测）：`6b335d3246dd8597d7d636d1067e971b94813450`；工作树有 116 个 Git status 条目（95 个已修改/已跟踪、21 个未跟踪，含 PostgreSQL 运行时存储、Outbox、fencing 预检、审批订单摘要、挂单归属隔离、紧急平仓执行收敛、告警送达、保护恢复幂等、研究评估和契约测试）。该 SHA 未部署，运行事实不纳入本轮证据。

## 决策

| 层级 | 决策 |
|---|---|
| Project | PIVOT：先收敛事实链与证据语义 |
| Paper/Shadow | HOLD |
| Testnet | HOLD |
| Mainnet | PROHIBITED |
| 24×7 持续盈利 | 不作保证；只有净成本、容量、OOS 和真实窗口证据才可讨论 |

## 当前事实

- 分支 `codex/full-system-convergence-v3`，工作树 DIRTY；外部 Testnet 启动曾以 mock signing-key 运行，但在脏工作区门禁下被阻断，未形成本轮授权证据。
- 最近监督状态为 `PREFLIGHT/BLOCKED`、`trading_ready=false`、`passed=false`，没有形成 READY 证书。
- 2026-08-10 00:24 `run_preflight(Path('.'), 'testnet', 0)` 返回 34 项检查：28 PASS、6 个 P0 FAIL（工作区脏、G5 证书不可验证、签名密钥、签名风险策略、PostgreSQL authority connection、fencing token）；API 凭据只检查存在性。PostgreSQL 新增只读连接/迁移头/checksum 探针，因未注入密码认证失败。随后独立 Testnet 只读查询确认 `canTrade=true/canWithdraw=true/canDeposit=true`、非零仓位 0、普通/Algo 挂单均为 0；账户能力门禁 FAIL，提现权限已成为显式 P0。
- 新代码已将 UNKNOWN、未跟踪在途单、无交易所 ACK 的保护、逐符号/方向/数量/generation 不足的保护覆盖、孤立保护、缺失持仓事实、缺失闭合标志、无研究 provenance 置为阻断；监督循环统一使用 monotonic 时钟。
- G7 实时追踪器不再把运行错误率当作成本/PnL 证据；告警 webhook 失败进入持久 delivery sidecar 和运行时 P0/P1 检查。
- 运行时探针只调用 typed StrategyKernel，VETO/NO_ACTION 结果保留可审计哈希；行情特征显式携带 `bar_is_closed`、点时可用时间，缺失元数据仍 fail-closed；因子评估按原始 bar index 交集对齐，Paper/Shadow 执行观测不再把预测方向复制为实际标签。
- 特征节点不再以 `0.0` 填补缺失/NaN 特征；OHLCV normalizer 对缺失、非有限、非正价格、负成交量和不一致高低点返回 `INVALID`，风险 R7/R8 对缺失持仓/清算/保护计数返回 `UNKNOWN`。执行上下文不再用中性 Sharpe、重复单零计数或无清算价假设替代权威事实，因此缺失证据会阻断新增风险。
- 新增 `migrations/003_execution_outbox.up.sql`、`004_runtime_records.up.sql` 与 `PostgresPersistentStore`/`PostgresIntentOutbox`：PostgreSQL URL 现在选择同一持久化存储和 Outbox；缺少 schema/连接/fencing 时仅建立显式阻断的诊断 SQLite，不会进入 READY。该接线只有 DB-API fake 契约证据，仍不构成 G1/G3 生产事实链证据。
- 风险审批 HMAC 现在额外绑定 `risk_intent_hash`（完整订单身份、数量、价格、TIF、客户端/幂等键和 reduce-only 语义）；最终发送和切片计划复核该摘要。启动发现的交易所挂单不再视为本进程所有，关机只撤销本轮已确认归属的订单；其余挂单触发 `NO_NEW_RISK`/UNKNOWN。该行为只有本地合同测试证据。
- R10 重复订单证据已接入持久化 Outbox：SQLite/PostgreSQL 仅在具备完整 `client_order_id` 与 `idempotency_key` 时返回近 24 小时重复计数；内存兼容队列、缺身份、schema/query/fencing 异常均保持 `UNKNOWN`，不再永久硬编码 `None`。该查询仍未在真实 PostgreSQL/交易所运行中验证。
- REST K 线解析现在拒绝结构不足、未来/逆序时间、NaN/无穷、非正价格、负成交量、非法 quote volume 和 OHLC 高低点矛盾；WS ticker 只有在显式有限且双边价满足 `0 < bid <= ask` 时才可用于实时价，失效时回退 REST 特征，不复制 `close` 为 bid/ask。该边界有本地失败优先测试，尚无真实断流/回补证据。
- 订单 ACK 与重复 client ID 恢复现在都经过 Adapter typed identity 校验；缺少或不匹配的 `orderId/clientOrderId/symbol/side/type/status/origQty/reduceOnly` 保持 `UNKNOWN`，引擎只能认领唯一且语义一致的 venue 订单。监督器新增权威三方对账事实门（必须是新鲜 `MATCHED`），监控层的 UNKNOWN/unsupported 对账和重复订单身份统一 P0 fail-closed。
- 可写引擎已接入 Adapter-owned user-stream listen-key 创建/续期和 WS 订单/账户事件解析；断线、过期、解析/连续性异常会进入 `NO_NEW_RISK`，READY/Liveness 需要新鲜且完整的用户事件事实。真实 Testnet listen-key、授权 replay/gap-fill 和重连恢复仍未验证。
- Supervisor monitoring cycle 现在持续验证交易所账户权限事实；可写模式要求新鲜 `canTrade=true/canWithdraw=false`，缺失/过期/非法、提现开启或交易关闭均为 P0 `NO_NEW_RISK`。只读模式明确标注写能力关闭。
- ProtectionManager 现在在自身边界拒绝非有限/非正仓位、零距离或反向止损，以及绕过止损的止盈；Adapter 余额解析拒绝 NaN/结构缺失并保留已验证的有符号余额。Testnet 还必须加载有效签名风险策略，缺失时 READY 和风险增加发送均保持阻断，退出路径仍需显式 reduce-only 治理。
- 保护重试严格复用批准的 trigger price，不再在运行时 widening；启动遇到 durable ACTIVE 保护时先核对 venue 语义并重建本地投影，禁止重复下发。幽灵/超量保护自动清理要求新鲜匹配对账与 `RESUME`，缺失订单年龄或持仓事实时不撤单。
- 研究配置现在完整绑定 policy version 与 fee/spread/slippage/funding/impact 成本字段；EvidenceBundle 的 artifact hash 覆盖 stability/cost-capacity/warnings，ACTIVE 因子晋级必须提供与 factor/dataset/policy 匹配的 sealed bundle。缺少这些绑定时仍为 `NOT_VERIFIABLE`，不把默认网格或字符串证据当生产授权。
- 独立 MarketDataFeed 默认传输已接入 Binance Adapter；启动器/引擎不再扩展固定默认 symbol pool，风险增加前必须显式提供 symbol universe。
- 本轮授权范围仅涵盖 Testnet 只读、PG/G5/G7 门禁与受控验证；仍未执行停机、重启、部署、交易所写入、真实资金或 Git 推送。

## 验收原则

1. 任何状态不能由单一来源自证：本地状态、交易所快照、用户流/REST gap-fill、账本必须可比较。
2. UNKNOWN 只能减少风险；不能猜测为 FILLED、CANCELED、ACTIVE、PASS 或盈利。
3. 代码测试通过不等于 G5/G7 通过；旧证书、健康端点和模拟成交不构成实盘证据。

## 交付顺序

`P0 取证/冻结 → 唯一执行事实链 → PIT/研究门 → Paper/Shadow 真实性 → G5/G7 实机窗口 → 生产运维恢复演练`。

## 本轮本地验证快照

| 门 | 结果 |
|---|---|
| 全量回归 | `.venv/bin/pytest tests/ -q -W error::ResourceWarning`：1264 passed, 1 skipped（1265 collected） |
| CI 覆盖率 | `.venv/bin/pytest tests/ -q --cov --cov-report=term --cov-fail-under=85`：**FAIL，66.53%（终端总计约 67%）< 85%** |
| Ruff lint/format | `.venv/bin/ruff check . && .venv/bin/ruff format --check .`：PASS（420 files） |
| CI 包范围 mypy | PASS |
| compileall | PASS |
| `git diff --check` | PASS |
| 测试质量/禁止模式/硬编码扫描 | PASS（硬编码扫描仅有非阻断 localhost/tmp WARN） |

| 关键包 mypy | `.venv/bin/mypy beidou_shared … beidou_core --no-error-summary`：PASS |
| 新鲜 Testnet preflight | 34 项：28 PASS、6 P0 FAIL；G5 与 G7 均阻断 |
| G7 starter | `scripts/certification/start_g7.py --preflight-port 0`：退出 1，未创建窗口 |
| 只读 Binance 核验 | server time/exchangeInfo/account/position-mode/open-orders/Algo 成功；`canWithdraw=true`，未发送写请求 |
| 质量/安全扫描 | 禁止模式、测试质量、硬编码扫描：PASS；仅非阻断 localhost/tmp WARN |

以上是本地代码与只读外部事实证据（E1/E2），不替代真实交易所写场景、Testnet、经过时间的 Paper/Shadow、PostgreSQL/PITR 或 G5/G7 证书；当前总决策仍为 `HOLD / NOT READY FOR UNATTENDED TRADING`。
