# 执行交付方案（2026-08-10 00:24）

## 交付结论

当前交付是“安全收敛切片 + 证据驱动的后续方案”，不是 24 小时无人值守或持续盈利的上线批准。

决策：

- Paper/Shadow：HOLD
- Testnet：HOLD
- Mainnet：PROHIBITED
- 24×7 持续盈利：不作保证；只能在真实净成本、容量、OOS、事实链和经过时间窗口证据齐全后重新评估

## 本轮已执行切片

1. 订单簿 diff 只接受严格连续的 `prev_sequence → sequence`，缺口/错序立即要求 resync 且不修改快照。
2. Paper/Shadow 成本证据独立计数；成本证据缺失或偏差超过门限阻断 Testnet Gate；Paper 撮合成本含独立实现的手续费与价格冲击。
3. Supervisor 启动阶段在任何 P0/P1 blocker 存在时不得授权 `RESUME`；运行时阻断仍进入 `NO_NEW_RISK`/`DEGRADED`。
4. 市场特征不足时不再伪造止损/止盈默认值；保护配置 UNKNOWN 会记录 CRITICAL、关闭新增风险并拒绝创建保护。
5. 执行算法、切片或硬约束不可证明时拒绝 Intent，不回退裸 MARKET/LIMIT 单切片。
6. 因子和交易池禁止 Testnet 启动即 ACTIVE；ACTIVE 因子必须绑定 sealed OOS、成本容量、Paper/Shadow 与具名批准证据。
7. 风险签名新增 `issue_for_approved_risk` 边界，签名不能独立充当风险批准。
8. 监控静态组件默认不宣称健康；测试质量扫描、硬编码扫描和禁止模式扫描恢复为 PASS。
9. `StartupReport.passed` 只有在 `trading_ready=true` 且 supervisor=`RUNNING` 时才为真；启动中/无检查不再生成“通过”假证书。
10. 执行器在事实后端未接入时拒绝风险增加；交易所健康 UNKNOWN 时仅放行显式 reduce-only/close-position/撤单路径。
11. G5 runner 在构造网络客户端前执行 P0 preflight；G7 producer 要求至少 200 个 SLI 样本、全类别覆盖、100% SLI 通过且无活动事故；恢复 SLI 无上下文不再默认 PASS。
12. Durable protection gate 按交易所持仓逐符号验证 owner、generation、reduce-only 方向和完整止损数量；孤立保护、旧 generation、错方向及部分止损均保持 UNKNOWN。监督循环心跳统一使用 monotonic 时钟，避免健康证书的时钟域假阳性。
13. `ClosedBar` 与 normalizer 在缺失 `is_closed`/`x` 时默认 `NOT_CLOSED`，拒绝将未证实闭合的 K 线送入策略/标签链。
14. 新增 PostgreSQL v3 execution-outbox 与 runtime-records 合同：Approval、Intent、Outbox 初始事件和运行投影写入均有事务边界；PostgreSQL URL 下引擎选择 `PostgresPersistentStore` + `PostgresIntentOutbox`，Worker 使用 `FOR UPDATE SKIP LOCKED`、lease/fencing、ACK/UNKNOWN、重试/死信和旧代恢复；连接/schema/fencing 缺失时明确保持 blocked 并只建立诊断 SQLite。真实 PG、迁移、crash/chaos 和 replay 证据仍是硬门禁。
15. 紧急平仓模块删除直接 REST 旁路，统一生成带短期签名 Approval 的 reduce-only Intent，经同一 Outbox/Executor/Adapter 链提交；缺少治理执行器或签名时保持未执行并返回失败。
16. G7 窗口 evidence state 使用 schema v2 原子写入完整 SLI/事故/日报，进程重启可恢复；旧 counter-only 状态与 fast-forward 模拟样本只能产生 `NOT_VERIFIABLE`。
17. G7 实时追踪器与认证计划统一使用 `cost_and_pnl_reporting`；没有权威成本/PnL 检查时记录 0，不再把运行错误率冒充财务证据。Webhook 失败写入持久 delivery sidecar，重试、死信和 UNKNOWN 会进入运行时 P0/P1 检查。
18. Supervisor 只向已人工启动且处于 RUNNING 的 G7 窗口写入真实监控周期；日报按日期幂等，P0 会生成持久事故并重置窗口，绝不自动创建或签发证书。
19. 运行时算法探针只调用 typed `StrategyKernel`，typed graph 的 VETO/NO_ACTION 不回退 legacy AlphaGraph，并保留图哈希和提案哈希证据。
20. 行情特征显式传递 `bar_is_closed`、`bar_open_time`、`bar_close_time`、`bar_available_at`；缺失或未来可用时间保持 nearline 阻断。
21. 因子挖掘在 warm-up/缺失值清洗后按原始 bar index 交集对齐因子与标签，WFO、交互、残差和候选映射复用同一有效样本身份。
22. Paper/Shadow 成交记录降级为独立执行/成本观测；只有外部未来窗口标签才能计入方向误差和 `total_executed`，禁止预测方向自标。
23. 风险审批 HMAC 额外绑定完整 `risk_intent_hash`；最终发送与执行切片复核数量、符号、方向、订单语义、价格/TIF 和确定性 client ID。启动恢复挂单不自动认领，关机仅撤销当前 worker 已确认归属的挂单；未归属项保持未触碰并触发 `NO_NEW_RISK`。
24. 因子图重建改用不可变 `factor_id`，避免真实 ACTIVE 晋级后因 `FactorRecord` 可变且不可哈希导致重建崩溃；CHALLENGER 只进入零写 Paper/Shadow/Research，Testnet 写路径不再接入未批准因子，并增加回归合同。
25. 引擎自身 READY/Liveness 以及监督器 runtime checks 均显式要求实时循环运行；可写模式下 `RESUME + stopped` 或仅凭旧时间戳的健康状态现在进入 `UNHEALTHY`/P0，不能形成独立健康端点假证书。
26. realtime 心跳年龄和调度改用 monotonic 时钟，墙钟只保留为审计显示；运行时证据明确记录时钟来源，防止 NTP/人工校时制造假健康或假卡死。
27. 因子挖掘流水线只接受规范化外部 SHA-256 dataset manifest；空值或任意占位字符串统一降为 `UNKNOWN` 并阻断证据晋级。
28. 退役的旧研究内核不再返回零值 Purged-WFO 假折；调用该兼容入口明确 fail-closed，必须迁移到带真实标签区间、成本和 provenance 的 `MiningRunner`。
29. 退役的 timer-only `scripts/certify_72h.py` 不再把“经过 72 小时 + pytest”伪装成 G7 证书；启动/结束入口明确阻断，必须使用带权威成本、保护、对账和事故证据的 G7 producer。
30. 特征节点、OHLCV 规范化和 R7/R8 风险规则均移除“缺失即零/无持仓”的隐含安全假设；执行上下文对持仓、清算价、Sharpe 与重复订单计数采用显式事实或 `UNKNOWN`，避免在证据缺口时继续新增风险。
31. R10 重复订单证据接入持久化 Outbox：近 24 小时仅统计带完整 `client_order_id` 与 `idempotency_key` 的 durable intents；内存兼容队列、缺身份、无 fencing 或查询失败均返回 `UNKNOWN` 并阻断新增风险，真实 venue duplicate audit 仍未完成。
32. REST K 线边界拒绝短行、未来/逆序时间、NaN/无穷、非正价格、负成交量、非法 quote volume 与 OHLC 高低点矛盾；WS ticker 仅接受有限且 `0 < bid <= ask` 的显式报价，失效时回退 REST 特征，不再以 `close` 伪造 bid/ask。
33. Adapter 订单 ACK 与重复 client ID 恢复均绑定完整 venue 身份和订单语义；typed `query_order_by_client_id` 失败或返回不匹配数据时保持 UNKNOWN，禁止盲目换 client ID 重发。
34. ProtectionManager 与 StopLoss/TakeProfitCalculator 在自身边界拒绝非有限/非正仓位、零距离或反向止损、缺失 ATR/波动率/结构、无止损止盈和非法止盈目标，避免调用方绕过引擎治理门构造裸仓保护。
35. Binance Adapter 余额解析对响应结构、asset、有限 Decimal 和有符号余额做完整校验；NaN/Infinity/缺失行返回 UNKNOWN，不把异常回包投影为空账户。
36. Testnet preflight 与引擎 READY/最终发送均要求有效签名风险策略；缺失/过期/无效策略阻断风险增加，显式 reduce-only/close-position 仍须经过控制面和事实链治理。
37. Supervisor runtime checks 新增权威三方对账事实门：没有新鲜 `MATCHED` 结果、结果过期或状态/差异异常时直接 P0 阻断；监控层对账 `UNKNOWN/unsupported` 与重复订单身份也不再降级为 WARN。
38. PostgreSQL Outbox 启动恢复新增陈旧代际/过期租约隔离：`SENDING/SENT` 不再在重启后被当作可安全重发，而是在同一事务中转为持久 `UNKNOWN`，同步订单意图投影并写入 `FENCED_RECOVERY_UNKNOWN` 事件；恢复必须经过独立 client-order 查询与三方对账。
39. 启动保护恢复不再只比较本地 Algo ID：对交易所保护库存逐条比较 Algo ID、symbol、方向、订单类型、数量、触发价和 `reduceOnly`；缺失或语义不一致均保持 `NO_NEW_RISK`，不自动撤单或重建。
40. Binance Algo 回包的 `reduceOnly`/`closePosition` 改用严格布尔解析；非法字符串不再被 Python truthiness 误判为真，而是将回包置为 `UNKNOWN` 并阻断后续风险增加。
41. PostgreSQL 启动校验要求全部 forward-only migration head（001–005）出现在 `schema_migrations`；仅有同名表但迁移链不完整的数据库直接保持 blocked，不能被当作可用事实后端。
42. PostgreSQL Outbox 恢复按 `lease_owner IS DISTINCT FROM 当前进程代际` 识别旧 Worker；即使 fencing token 相同且旧 lease 尚未过期，也会在事务中转为 `UNKNOWN`，当前代际重复调用不会接管自己的在途命令。
43. 退役的 `_sync_exchange_state` 自愈入口现在显式抛出 `EXCHANGE_STATE_MUTATION_RECOVERY_DISABLED`；对账路径不能再以单一交易所快照删除幽灵状态、取消保护或重建本地持仓，必须走三方事实与授权恢复状态机。
44. 行情 Feed 的 WS 停止路径遵循客户端真实 `close()` 契约并保留受控兼容回退；可写模式的监控对账还必须同时看到新鲜 `engine._last_reconciliation_result=MATCHED`，单独 REST/本地投影一致不再形成 PASS。
45. 可写引擎启动真实 user-stream 生命周期：通过 Adapter 创建并续期 listen-key，使用 Binance user-data WS 解析 `ORDER_TRADE_UPDATE`/`ACCOUNT_UPDATE`，断线、过期、解析失败或续期失败均降级并撤销新增风险；没有 REST-only 可写回退。
46. READY、Liveness 和 Supervisor runtime safety 均要求新鲜 user-stream 事件、连续性投影无 GAP/UNKNOWN 且事件事实完整；未完成授权 replay/gap-fill 时保持 `NOT_READY/NO_NEW_RISK`，不把“已连接”当作已同步。
47. REST listen-key keepalive 现在绑定明确的 `listenKey` 参数并拒绝空 key；引擎 shutdown 会关闭 user-stream 任务/客户端并清理本进程 key，不触碰未确认归属的 venue 订单；mypy 增加 `explicit_package_bases`，apps 多个 `__main__` 的类型检查边界可复现。
48. Binance Testnet 的大响应读取从易受 `IncompleteRead` 影响的 `urllib` 同步读取切换到已依赖的 `httpx` framing；HTTP 错误重新映射回原有错误分类。只读 server-time、exchangeInfo、account、position-mode、open-orders 和 Algo 查询均成功，未发送订单/撤单请求。
49. 账户能力不再把 `can_withdraw=False` 当作本地默认事实；Adapter、引擎启动门和 G5 观察器均读取 venue `canTrade/canWithdraw`，缺失/非法保持 UNKNOWN，`canWithdraw=true` 直接阻断 writable readiness。当前 Testnet 只读快照实际为 `canTrade=true/canWithdraw=true`，因此能力门禁 FAIL。
50. 在用户授权范围内完成首次受控外部预检：Testnet 只读网络证据已落盘；当时含 API 凭据的 preflight 为 32 项、28 PASS、4 P0 FAIL（脏工作区、签名密钥、签名策略、fencing token）。PostgreSQL 5432 可达但配置 DSN 未注入密码，认证失败；未执行 G5 写场景、user-stream listen-key、重启、部署或 Git 推送。后续切片已将该连接失败显式提升为独立 P0。
51. 账户权限事实已接入每次 Supervisor monitoring cycle：可写模式必须持续看到新鲜且完整的 venue `canTrade=true/canWithdraw=false`；快照缺失、过期、未来时间戳、字段非法、提现开启或交易关闭均为 P0 `NO_NEW_RISK`。只读模式显式标记写能力关闭，不把权限检查降级为健康 PASS。
52. Testnet preflight 新增只读 PostgreSQL authority probe：在构造可写引擎前，以 5 秒连接上限验证必需表、001–005 migration head 和 SHA-256 checksum；连接失败、表缺失、迁移缺失或 checksum drift 均生成 P0，不创建表、不执行迁移、不打印 DSN。
53. G7 starter 不再写入“Initial DQ/Initial reconciliation/No incident”伪造 PASS SLI；窗口创建前复用 Testnet preflight，并从独立 `config/g5-testnet-plan.yaml` 验证 G5 场景，避免 G7 误用空的 G7 `scenarios` 字段或把启动前提当作经过时间证据。显式 `--window-id` 现在经过路径安全校验并真正绑定窗口。
54. G7 fast-forward 评估器改用公开 `create_window(..., window_id=...)` 绑定模拟窗口，不再先创建随机孤儿状态再修改私有映射；模拟证据继续显式 `NOT_VERIFIABLE`，并由合同测试防止绕过路径/重复 ID 校验。
55. 保护单重试严格复用已批准的 stop-loss/take-profit trigger price；删除运行时 widening，交易所拒绝保持 `UNKNOWN`，不能借重试改变风险合同。
56. 独立 MarketDataFeed 的默认 REST 传输也经 `BinanceUsdmAdapter`，消除研究/运行两条交易所旁路；启动器和引擎移除固定默认 symbol pool，交易 universe 必须显式提供。
57. 启动恢复对 durable ACTIVE 保护逐条校验 owner、generation、venue 语义后重建本地 `PositionProtection`；已 ACK 的保护不会再次创建/下发，inventory/持仓/语义任一未知即保持 `NO_NEW_RISK`；旧 raw Algo 旁路恢复已删除。
58. 幽灵仓位和超量条件单自动清理现在要求 `RESUME` 与新鲜匹配三方对账；没有完整创建时间、实时持仓或对账事实时不执行撤单，并由合同测试锁定该边界。
59. Purged-WFO 的标签 horizon 不再把全部 OOS 折叠为空；每个候选必须形成至少 5 个有效、按时间顺序隔离的折叠，否则证据保持 `NOT_VERIFIABLE`。
60. CPCV 评估要求至少 20 条有效路径、每个测试块单独 purge/embargo，并跳过非有限统计；q05 下界与路径一致性同时满足才允许进入候选证据。
61. 多重检验把 PBO 方向与候选身份绑定，并以候选专属 Benjamini–Hochberg 结果判定；缺失独立候选统计或样本不足时明确保持 `NOT_VERIFIABLE`。
62. YAML 研究策略现在强制绑定 policy version、generation/fast-screen/WFO/cost 全字段；禁止缺项时静默使用 Python 默认成本。`EvidenceBundle` 的哈希覆盖稳定性、成本/容量和警告字段，篡改后不可继续复用。
63. Testnet preflight 在构造引擎前独立验证 G5 证书/计划；当前新鲜结果为 34 项、28 PASS、6 个 P0 FAIL，G7 starter 因这些阻断退出 1，未创建无人值守窗口。
64. 可执行图只接受具名、sealed 且与 factor/dataset/policy 完整绑定的 EvidenceBundle；仅伪造 `ACTIVE` 字符串或缺少 artifact hash 的记录不再进入真实 alpha graph。

## 当前证据

- HEAD（最近观测）：`6b335d3246dd8597d7d636d1067e971b94813450`；工作树有 116 个 Git status 条目（95 个已跟踪、21 个未跟踪），未部署。
- 全量回归：`.venv/bin/pytest tests/ -q -W error::ResourceWarning` → **1264 passed, 1 skipped**（1265 collected）。
- 覆盖率门：`.venv/bin/pytest tests/ -q -W error::ResourceWarning --cov --cov-report=term-missing --cov-report=json` → **FAIL，66.53% < 85%**；资源警告门已清零。
- 全仓库 Ruff/format、关键包 mypy、compileall、`git diff --check`：PASS（format 420 files）。
- `scan_test_quality.py`、`check_forbidden_patterns.py`、`scan_hardcoded.py`：PASS（硬编码扫描输出非阻断 localhost/tmp WARN）。
- 最新 preflight（2026-08-10 00:24 CST，凭据仅检查存在性）：34 项中 28 PASS、6 个 P0 FAIL（工作区脏、G5 certificate、signing key、签名风险策略、PostgreSQL authority connection、fencing token）；Testnet 账户权限另有独立 P0：venue `canWithdraw=true`，修复后的 account capability FAIL。PostgreSQL 连接探针未注入密码而失败，未形成可采信的运行就绪证书。未执行订单、撤单、仓位修改、user-stream listen-key、重启、部署或 Git 推送。
- G7 starter（复用上述 preflight）退出码为 1，未创建真实窗口；未执行交易所写入。
- 该 preflight 的脱敏证据：[preflight-20260809T145003Z.json](/Users/maguannan/beidou/artifacts/evidence/testnet/preflight-20260809T145003Z.json)；不包含 DSN、错误原文或任何密钥值。

## G0–G7 判定

| Gate | 当前判定 | 不能宣称的内容 |
|---|---|---|
| G0 数据因果 | CONDITIONAL | 未完成全量 PIT/闭合 bar/数据内容哈希的独立重放 |
| G1 统计 Alpha | FAIL/NEED EVIDENCE | 不能把回测、IC 或历史收益当成未来盈利 |
| G2 组合/成本/容量 | BLOCKED | 未完成真实盘口容量、funding、impact 和跨 regime OOS |
| G3 执行事实链 | CONDITIONAL / NOT_VERIFIABLE | PostgreSQL Store/Outbox 已进入引擎后端选择路径，但未完成真实数据库、migration head、gap-fill、崩溃/混沌重放 |
| G4 代码/覆盖率 | FAIL | 总覆盖率 66.53%，低于 85% 发布门；资源警告门已通过 |
| G5 Testnet | HOLD | 未执行新的真实 16 场景矩阵；历史证书不继承 |
| G6 对抗性生产审查 | FAIL | SRE/执行/量化红队 P0 尚未全部关闭 |
| G7 无人值守 | NOT_VERIFIABLE | 没有真实 30 日/200 闭环、无 P0、无证据缺口窗口 |

## 后续依赖顺序

`P0 取证与隔离 → 唯一 PostgreSQL 事实链 → user-stream/replay/gap-fill → venue-backed protection/三方对账 → Paper/Shadow parity → 真实 G5 → 从零开始 G7 → 独立发布审批`。

任何阶段出现 UNKNOWN、未保护仓位、重复/孤儿单、账本差异、PIT/Parity 缺口或告警送达失败，立即停止新增风险并重置相应经过时间窗口。

## 回滚与权限边界

- 本轮未执行停机、重启、部署、撤单、平仓、余额/密钥变更或 Git 推送；外部运行进程不属于本轮授权动作。
- 代码回滚只能回到 schema 兼容的已验证制品；数据库采用前向补偿，不做未经验证的逆向迁移。
- 先前外部自动生成物清理提交删除了部分 tracked evidence artifacts；在恢复历史证据前必须先核对提交、哈希和来源，本轮不把缺失文件补造为新证据。
- 要继续真实 Testnet，必须先取得具名授权：保存当前 DB/WAL/监督证据、取得交易所只读快照、确认活跃订单归属与保护覆盖，然后再按治理停机流程处置。
