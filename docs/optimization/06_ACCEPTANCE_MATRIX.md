# 全量验收矩阵

| 任务 | 名称 | 核心验收 | 证据 | Gate | 级别 |
|---|---|---|---|---|---|
| BD-T00 | 冻结基线并修复构建门 | 所有生产 Python 文件可编译，compileall 退出码为 0。；pytest 收集成功，收集数量写入证据；不得有 collection error。 | 命令/测试/数据库/API/证据 manifest | G0 | P0 |
| BD-T01 | 密钥与审批 Fail-Closed | 源码及模板中不存在默认签名密钥。；缺少密钥时 Testnet 风险增加请求确定性拒绝，错误码为 SIGNING_UNAVAILABLE。 | 命令/测试/数据库/API/证据 manifest | G0 | P0 |
| BD-T02 | 统一配置提供器 | 核心模块不直接 open config/env.testnet.yaml。；UNKNOWN/缺失环境启动为 SAFETY_ONLY 且写交易能力为 false。 | 命令/测试/数据库/API/证据 manifest | G0 | P0 |
| BD-T03 | 单一 Exchange Gateway | Adapter 外无 Binance 网络库、URL 和原始端点字符串。；账户查询失败返回 UNKNOWN/ERROR，绝不返回 EMPTY。 | 命令/测试/数据库/API/证据 manifest | G1 | P0 |
| BD-T04 | ClosedBar 与点时特征链 | 未闭合 bar 永不进入 StrategyKernel。；重复、乱序、缺口、陈旧、revision 均有明确状态和审计。 | 命令/测试/数据库/API/证据 manifest | G2 | P0 |
| BD-T05 | Typed Strategy Kernel 切换 | Filter contract 无 LONG/SHORT 字段。；Exit 不能增加绝对风险；属性测试覆盖所有方向组合。 | 命令/测试/数据库/API/证据 manifest | G3 | P0 |
| BD-T06 | 证据驱动因子生命周期 | 缺少任一强制证据时晋级返回 FAIL，状态不变。；重启后生命周期、版本和证据链一致。 | 命令/测试/数据库/API/证据 manifest | G3 | P0/P1 |
| BD-T07 | 持久化 Risk Snapshot 与 Approval | R0-R10 均有至少一个通过、拒绝和边界测试。；未知账户/DQ/交易所健康/对账差异均拒绝增加风险。 | 命令/测试/数据库/API/证据 manifest | G3 | P0 |
| BD-T08 | 持久化 Intent 与 PostgreSQL Transactional Outbox | 业务 Intent 与 Outbox 不会出现单边提交。；crash-before-send 不丢消息；crash-after-send 不重复订单。 | 命令/测试/数据库/API/证据 manifest | G1 | P0 |
| BD-T09 | 统一订单聚合与 UNKNOWN 恢复 | 状态机模型测试覆盖所有合法/非法转换。；重复用户流事件不改变累计成交。 | 命令/测试/数据库/API/证据 manifest | G1 | P0 |
| BD-T10 | Fill 与 Position 权威链 | 重复 fill 不重复更新仓位/PnL。；任意 fill 序列 replay 结果确定性一致。 | 命令/测试/数据库/API/证据 manifest | G1 | P0 |
| BD-T11 | 交易所原生保护与安全监督 | 每个风险仓位在 SLO 内有 ACKED 原生保护证书。；保护缺失/拒绝/失联自动进入 EXIT_ONLY 或 LOCK。 | 命令/测试/数据库/API/证据 manifest | G4 | P0 |
| BD-T12 | 真正的复式账本 | 每个 transaction 的 postings 按币种和方向求和为 0。；重复 fill/source_event 不重复记账。 | 命令/测试/数据库/API/证据 manifest | G1 | P0 |
| BD-T13 | 独立对账与差异门禁 | 注入余额/仓位/订单/保护差异均被发现。；所有非 MATCHED 状态阻断新风险。 | 命令/测试/数据库/API/证据 manifest | G1 | P0 |
| BD-T14 | 生命周期、恢复和控制面 | 未完成事实重建、对账、保护验证时 trading-ready=false。；固定等待不会自动 ACTIVE。 | 命令/测试/数据库/API/证据 manifest | G4 | P0/P1 |
| BD-T15 | CI 与证据系统加固 | 核心链无 mypy ignore_errors。；覆盖率阈值由 CI 失败机制验证，而非仅配置。 | 命令/测试/数据库/API/证据 manifest | G0 | P0 |
| BD-T16 | 真实 Paper 撮合与成本模型 | Paper 不直接把所有订单标记 FILLED。；手续费、spread、slippage、funding 全部进入账本。 | 命令/测试/数据库/API/证据 manifest | G4 | P1 |
| BD-T17 | 因子统计与组合优化 | 任何晋级报告可由冻结数据和 commit 复现。；训练、验证、测试区间无泄漏且有 embargo。 | 命令/测试/数据库/API/证据 manifest | G3 | P1 |
| BD-T18 | Binance Testnet 认证 | G5 全部强制场景 PASS；无 P0/P1 未闭合。；每个订单可从 Intent 追溯至 ACK/Fill/Position/Protection/Ledger/Reconciliation。 | 命令/测试/数据库/API/证据 manifest | G5 | P1 |
| BD-T19 | 30 天无人值守认证 | 连续 30 天真实经过时间完整，证据无缺口。；无重复订单、无超 SLO 未保护仓位、无未解释关键账本差异。 | 命令/测试/数据库/API/证据 manifest | G6 | P2 |
