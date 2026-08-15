# 北斗全系统可执行优化方案

目标基线：main@e2819c4dfd7786d68674d85c4aad286eef8872c1
方案日期：2026-08-15（Asia/Shanghai）
方案性质：只读审查后的执行合同；不是开发完成、Testnet Ready、Mainnet Ready 或盈利证明

## 0. Decision Memo

| 项目 | 结论 |
|---|---|
| Interaction Mode | Yellow：可完成方案；任何运行时、资金、交易所、部署或 Git 变更为 Red |
| S/M/L | L：23 个功能域、核心资金事实链、自动决策、生产运行与不可逆风险 |
| 命中的分级条件 | 超过 6 个模块；多数据/API/状态链；资金/安全/合规；24×7 自动化；难回滚运行状态 |
| 外部动作授权 | 无；本轮只创建方案文档 |
| 当前 Gate 决策上限 | PLAN-GO；Implementation Need Evidence |
| 最大价值 | 把现有“大量能力 + 多条例外路径”收敛为唯一可证伪、可恢复、可审计的交易事实链 |
| 最大风险 | 通过 Testnet 特殊宽松语义、伪证书或读时刷新事实获得表面 READY，并把 UNKNOWN 误当安全 |
| 推荐方案 | 旧 BD-T00 至 BD-T19 仅作意图来源；新建绑定当前基线、无环、不可继承旧 PASS 的交付图，先做 M00-C 安全隔离，再按依赖单模块闭环 |
| 不推荐方案 | 直接沿用旧计划；一次性重写；先优化收益；在当前运行 checkout 上边运行边改 |
| 当前发布/交易结论 | Paper/Shadow/Testnet：HOLD；Mainnet/真实资金：PROHIBITED |
| 盈利结论 | UNKNOWN；本方案不承诺持续盈利 |
| Final Decision | PLAN-GO / SYSTEM-PIVOT：路线图通过复验；拒绝按旧图或整块 M00 开工，下一授权候选仅 TASK-M00-C00，后续任务与全部运行写入继续 HOLD |

### Early Kill

| Kill | 状态 | 结论 |
|---|---|---|
| K1 无真实问题 | 未命中 | 当前代码和门禁失败证明问题真实 |
| K2 核心结论只有低等级推理 | 命中部分 | 运行账户、PG、用户流和保护覆盖缺现场 E1；限制实施决策 |
| K4 成功不可衡量 | OPEN / PARTIAL | 安全 Gate 已定义；盈利阈值、资本上限、最大回撤、样本量、试验次数和成本/容量假设仍须在看数据前预注册 |
| K5 Scope 无法收敛 | 已处理 | 一次只允许一个模块；跨模块只做接口最小切片 |
| K7 风险不可接受 | 当前未致死，但阻断运行 | Testnet 语义分叉与伪 CERT-G5 必须先隔离 |
| K8 无法转成验收 | PARTIAL | M00 已拆为四个子 Gate；M01-M22 仍须在各自开工前展开到原子 REQ/Test/AC |

## 1. Problem、JTBD 与范围

### Problem Statement

对于北斗的单操作员、工程 Owner 与风险承担者，当他们尝试让系统长期自动运行并逐步验证净成本后 Alpha 时，当前实现中仍存在启动/证书旁路、环境语义分叉、多个兼容入口、事实新鲜度自刷、执行链静态错误和证据门失真，导致“代码存在或测试变绿”不能可靠证明订单、仓位、保护、账本、对账和恢复是正确的。如果不解决，系统可能错误增加风险、错误恢复 RESUME、错误宣称 READY，且任何盈利指标都不可信。

### JTBD

当系统准备进入 Paper、Shadow、Testnet 或无人值守阶段时，我需要从固定 commit、唯一事实链和独立证据确认每一步都满足安全与统计门禁，以便在任何 UNKNOWN 时停止新增风险，并能重放、对账、恢复和审计。

### In Scope

- M00 至 M22 的模块图、依赖顺序、任务合同、测试/证据/验收和 Gate；
- 当前 HEAD 的 P0/P1/P2 基线；
- 单一入口、Testnet/Production 语义一致、PG/Outbox/订单/仓位/保护/账本/对账主链；
- 研究、回测、Paper、Shadow、Testnet 与无人值守的分阶段证据；
- 回滚、前向迁移、监控、故障注入和独立对抗审查。

### Out of Scope

- 本轮代码实现、修复、测试覆盖率补齐、部署、重启和 Git 交付；
- 交易所写入、账户权限变更、提款、转账、仓位处置；
- Mainnet 或真实资金；
- 收益承诺、参数寻优或策略晋级；
- 未经授权读取 .env、密钥、账户和仓位明细。

## 2. Evidence 与 Claim Ledger

详细命令和限制见 00-context.md。

| Evidence ID | 核心事实 | 等级 | 支持/反证 |
|---|---|---|---|
| E-001 | 当前基线为 e2819c4，本地 main ahead 21，起始工作树干净 | E1/CODE | 支持固定版本；反证旧报告可直接继承 |
| E-002 | beidou_core 约 14,840 LOC，核心运行编排和近 21 个提交高度集中 | E1/CODE | 支持巨型编排与回归半径风险 |
| E-003 | start_beidou.sh 生成 CERT-G5 PASS + DEV_BYPASS，并可生成策略/启动基础设施 | E1/CODE | 直接反证证书独立性与只读启动 |
| E-004 | Supervisor/Engine/Monitoring 存在 Testnet 特殊安全语义 | E1/CODE | 反证 INV-006 |
| E-005 | Ruff/format/forbidden/hardcoded/test-quality 当前均 FAIL | E1/EXPERIMENT | 反证当前工程基础门可通过 |
| E-006 | Ruff 发现执行路径引用未定义 order_id | E1/CODE | 支持 M11 P0 |
| E-007 | mypy 与 package validator PASS，但关键模块有 ignore_errors 且 validator 未发现伪 CERT-G5 | E1/EXPERIMENT | 反证单一绿灯可证明安全 |
| E-008 | 2,486 tests 可收集；53 个关键测试通过并明确批准 Testnet 宽松语义 | E1/EXPERIMENT | 反证“测试绿=符合提示词” |
| E-009 | 本机存在 Testnet/200 symbols 进程 | E1/OBSERVATION | 证明实施需要运行 checkout 隔离；不证明账户状态 |
| E-010 | 旧 run 状态仍记录 coverage 未达门、Testnet 写生命周期被前置条件阻断 | E2/INTERNAL | 仅作缺口线索；旧 DAG、状态和 PASS 均不可消费 |
| E-011 | 当前 verify_g5_certificate 离线接受 16 个 scenario 均为 PASS、details=DEV_BYPASS 的证书，并返回 passed=true、failures=[] | E1/EXPERIMENT | 直接证明验证端也可接受语义假阳性；未触碰运行进程或交易所 |

| Claim ID | P级 | 命题 | Falsifier | Evidence | 状态 | 决策影响 |
|---|---:|---|---|---|---|---|
| C-001 | P0 | 当前 HEAD 不能被视为 Testnet-ready | 新鲜独立证据证明无旁路、所有门通过、事实链闭环 | E-003 至 E-009 | SUPPORTED | Testnet HOLD |
| C-002 | P0 | Testnet 与未来生产安全语义当前不等价 | 机器可读 parity manifest 和全路径测试证明差异只限 endpoint/credential/rate/limit | E-004、E-008 | SUPPORTED | M00 必须先隔离 |
| C-003 | P0 | 当前证据系统可产生或接受语义假阳性 | 独立验证器从不可变原始证据重算全部结论和 hash，且未知变异也不能产生 PASS | E-003、E-007、E-011 | SUPPORTED | ENG 证据门与 CERT-G5 重开 |
| C-004 | P0 | 执行路径至少有一个当前静态正确性错误 | 目标代码重验无未定义名称且故障路径测试覆盖 | E-005、E-006 | SUPPORTED | M11 阻断 |
| C-005 | P0 | 旧 BD-T00 至 BD-T19 及 delivery.yaml 不能作为当前权威 DAG 或状态源，只能提供需求意图 | 新图可证明与当前基线一致、无环、状态不可从旧图继承，且每项有新鲜证据 | E-001、E-010 | SUPPORTED | 新建 versioned delivery graph；旧状态不可消费 |
| C-006 | P0 | 持续盈利证据当前不足 | 净成本、容量、OOS、Paper/Shadow、Testnet 和经过时间证据达到预注册阈值 | E-010 | UNKNOWN | 禁止盈利声明 |

## 3. DA-G0–DA-G7 当前状态

本节只表示 Deep Analysis Gate。工程交付使用 ENG-G0 至 ENG-G8，运行证书使用 CERT-G5/CERT-G7/CERT-G8；不同命名空间之间不得继承或折算 PASS。

| Gate | 状态 | 证据 | 未通过项 | 决策上限 | 下一动作 |
|---|---|---|---|---|---|
| DA-G0 Interaction/Kill | PARTIAL | E-001、E-009 | 活跃运行事实、远端状态和实施权限未知 | 仅文档/离线方案 | M00-C00 权限与隔离检查点 |
| DA-G1 Problem/Axiom | PASS | E-003 至 E-008、E-011 | 无 | 可设计方案 | 冻结 Problem/INV |
| DA-G2 Evidence/Reality | PARTIAL | E-001 至 E-011 | 未做账户/PG/运行事实重验；全量测试未运行 | Need Evidence | 在获授权环境补 E1 |
| DA-G3 Relative Value | PASS | Option 比较 | 无 | 推荐增量收敛 | 新建当前基线交付图；旧合同只作意图输入 |
| DA-G4 Strategic/Economic | PARTIAL | 安全优先约束 | 人力、预算、机会成本、研究阈值和 Owner 未具名 | 不给排期或收益承诺 | Owner 在看结果前确认 |
| DA-G5 System/Solution | PARTIAL | 模块图、主链、P0 基线 | 当前调用链仍有旁路/重复事实源 | 不得进入 Testnet | 先完成 M00-C，再按子 Gate 推进 |
| DA-G6 Adversarial Survival | PASS | 首轮审查发现 10 P0/4 P1；修订后两轮定向复验关闭全部计划结构 Kill，新增 P0=0 | OPEN_IMPLEMENTATION/OPEN_DECISION 仍由硬阻断器和后续任务承接 | 仅路线图 GO | 下一授权候选 TASK-M00-C00 |
| DA-G7 Delivery/Learning | PARTIAL | 修订后的 REQ/Test/AC/Metric 设计 | 人类 Owner、运行环境、盈利预注册与外部门未确认 | 不得运行或宣称 ready | 逐任务授权，不接受整包授权 |

## 4. 当前真实入口、主链与旁路

### 当前权威声明链

pyproject console script
→ beidou_launcher.cli.main
→ BeidouSupervisor.run
→ run_preflight
→ AutonomousEngine
→ MarketDataFeed/BinanceUsdmAdapter
→ StrategyKernel
→ Risk/Approval
→ Intent/Outbox
→ Adapter
→ User Stream/REST
→ Order/Fill/Position/Protection/Ledger/Reconciliation
→ Control/Health/Certification

### 当前必须处理的旁路或重复实现

| ID | 路径 | 当前问题 | 处置 |
|---|---|---|---|
| BYP-001 | start_beidou.sh | 默认 Testnet、固定 symbols、自动启动基础设施、生成策略、伪造 CERT-G5 PASS/DEV_BYPASS | M00 退役为无副作用薄包装器或删除可执行权 |
| BYP-002 | apps.autopilot | 可直接创建 AutonomousEngine，绕过 Supervisor | Testnet 写模式硬阻断；兼容入口只允许委托 launcher |
| BYP-003 | apps.safety_executor / strategy_engine | 直接创建 Engine，并默认 BEIDOU_ENV=testnet/固定 symbols | 限定离线测试或委托 launcher；不得作为生产入口 |
| BYP-004 | in-memory TransactionalOutbox / SQLite IntentOutbox | 与 PG Outbox 并存 | 仅测试/零写诊断；写模式类型与运行门禁止实例化 |
| BYP-005 | legacy AlphaGraph + typed graph | 两套图仍同时接线 | M06 证明只有 typed graph 可执行，legacy 只读迁移后删除 |
| BYP-006 | Testnet semantic branches | 权限、对账、用户流、恢复、保护和事件处理放宽 | M00 全局写隔离；在对应模块逐项收敛 |
| BYP-007 | 保护恢复直接写 Algo | 启动阶段构造/提交保护，与统一 Intent/Approval/Outbox 边界关系不清 | M11/M12 收敛为治理命令 |
| BYP-008 | call-time truth refresh | build_truth_snapshot 刷新保护时间，初始 protection hash=ACTIVE | M00/M18 改为只读事实，不制造新鲜度 |
| BYP-009 | 本地兼容/开发 bootstrap | paper/research 由 Supervisor 激活 DEV_BYPASS patch | 移出生产包和生产入口；只允许显式测试 fixture |
| BYP-010 | cleanup_stale_exchange_orders.py、scripts/testnet/run_g5.py | 可直接调用 adapter 执行撤单/测试订单生命周期，未证明经过统一 intent/approval/outbox/ownership 门 | 纳入 Write Capability Registry；默认禁写，只能在单独授权的隔离账户与绑定 task 下启用 |
| BYP-011 | engine/supervisor domain recovery | Engine 与 Supervisor 内均存在直接 RESUME/重新授权路径 | 删除域内直接 RESUME；改为 recovery request，只有 fresh 三方对账和一次性具名授权可消费 |

## 5. Beidou Module Map（当前 HEAD）

状态词严格使用：FULLY_IMPLEMENTED、PARTIALLY_IMPLEMENTED、MOCKED、PLACEHOLDER、BROKEN、UNREACHABLE、UNVERIFIED、NOT_IMPLEMENTED。

| 模块 | 当前职责与代码 | Input → Output | 上下游与状态依赖 | 当前测试 | 主链位置 | 当前分类 / 风险 |
|---|---|---|---|---|---|---|
| M00 基线/架构治理 | beidou_launcher、beidou_core、guard、registry、start_beidou.sh、apps 入口 | CLI/config/commit → gated runtime | 上游 operator/config；下游全部模块；PG、证书、环境 | launcher、guard、architecture、v3 fail-closed | 入口与编排 | BROKEN / P0：伪 CERT-G5、多入口、默认 Testnet、巨型 engine |
| M01 行情 | beidou_exchange、core/feed.py、data/market.py、canonical_bars.py、orderbook.py、quality.py | REST/WS events → normalized closed bars/orderbook/DQ | Binance、时钟、reference data、feature | market_data、DQ、orderbook、REST/WS client | 实时主链 | PARTIALLY_IMPLEMENTED / P0：真实 gap-fill、断流、乱序和时间域未独立验证 |
| M02 Universe | data/trading_pool.py、trading_pool_lifecycle.py、universe_hysteresis.py、bootstrap、engine pool | market/liquidity/DQ → versioned tradable universe | M01、配置、生命周期、store | strategy_data、registry_dynamic_factors、universe contracts | 近线主链 | PARTIALLY_IMPLEMENTED / P1：当前进程显式 200 symbols；dev bootstrap/固定入口残留 |
| M03 Feature/Indicator | data/feature_store.py、research/features、strategy/components | closed PIT bars → versioned features | M01、dataset manifest、warm-up | RSI、aux granularity、BF01/BF02、strategy_data | 研究/近线 | PARTIALLY_IMPLEMENTED / P1：未完成全部算法参考对拍与 PIT 证明 |
| M04 因子 | research/factors、contracts、evidence_bridge、factor registry | features/labels → factor evidence/registry state | M03、M08、policy、store | factor_research、evidence_bridge、promotion_chain | 研究到策略桥 | PARTIALLY_IMPLEMENTED / P1：真实 sealed OOS/成本容量链未重验 |
| M05 因子挖掘 | research/mining、apps/factor_miner | dataset/policy → candidates/statistics/OOS bundle | M03/M04/M08、PG/Parquet、policy | BF03/BF05、mining、WFO/CPCV/PBO/DSR | 离线 | PARTIALLY_IMPLEMENTED / P1：异常吞噬；多重检验/样本充分性需独立对拍 |
| M06 策略 | strategy/alpha、kernel、components、core/expression_component.py | factor graph/market state → ACTION/NO_ACTION/VETO/DEGRADED | M03-M05、M07、M10 | alpha_graph、typed_graph、kernel parity、signal contracts | 近线主链 | PARTIALLY_IMPLEMENTED / P0：legacy 与 typed 图并存；唯一可执行图需证明 |
| M07 Portfolio | strategy/portfolio、core PortfolioOptimizerImpl/SignalFuser | proposals/covariance/cost → signed target allocation | M06、M09/M10、position truth | strategy_portfolio、S3 portfolio、capacity | 风控前主链 | PARTIALLY_IMPLEMENTED / P1：需证明 alpha/covariance/cost/capacity/turnover/CVaR 全绑定 |
| M08 回测/研究 | research/backtest、statistics、paper_shadow、datasets | PIT dataset + strategy kernel → gross/net PnL/OOS evidence | M01-M07、M16、fees/funding/slippage | WFO leakage/parity、statistics、paper_shadow、PnL kernel | 离线/Paper | UNVERIFIED / P0：尚无当前 HEAD 的 kernel parity、真实成本与经过时间证据 |
| M09 仓位/杠杆 | core adaptive sizing、strategy/risk、portfolio constraints | equity/margin/vol/liquidity/confidence/risk → target size/leverage | M07/M10/M13、venue rules | adaptive sizing、strategy risk、intent quantization | 风控主链 | PARTIALLY_IMPLEMENTED / P0：Testnet 推导/默认路径及事实来源需收敛 |
| M10 Risk Engine | safety/risk、core PreRisk/RiskEngine/RiskSnapshot/Approval | immutable facts + target → signed approve/reject | M07/M09/M13/M18、policy | risk engine/rules/snapshot/approval fail-closed | P0 主链 | BROKEN / P0：Testnet 提款豁免、周期 hash、post-risk 吞异常 |
| M11 Order Execution | safety/execution、infra/outbox.py、exchange adapter、engine send path | approved intent → command aggregate/ACK/fill/final state | M10/M16、Binance、user stream | execution、algorithms、aggregate、PG outbox、fencing | P0 主链 | BROKEN / P0：未定义 order_id；多 outbox/trackers；保护直写边界 |
| M12 Position/TP/SL | safety/position、safety/protection、strategy/protection、engine recovery | durable fills + independent venue snapshot → owned position/protection projection | M11/M16、venue Algo orders；输出供 M13 对账 | protection、adaptive、position lifecycle/truth | P0 主链 | BROKEN / P0：5%/10%默认保护、猜精度、UNKNOWN inventory→empty、未归属清理 |
| M13 账户事实/对账 | account_discovery、user_events、ledger、reconciliation、store projection | REST + user stream + ledger → MATCHED/DIFF/UNKNOWN | M11/M12/M16、PG、Binance | reconciliation、ledger、user events/replay | P0 主链 | BROKEN / P0：Testnet 1%/300s/事件漂移宽松；提款权限豁免 |
| M14 生命周期 | beidou_lifecycle、factor/pool/model states、certification state | evidence/events → legal state transition | 全模块、policy、store | lifecycle、promotion、certification | 横切 | PARTIALLY_IMPLEMENTED / P0：Testnet 自动 RESUME 破坏人工授权点 |
| M15 Evolution | beidou_autonomy、MAPE-K、model registry、chaos、promotion | observation → challenger/research/promote/rollback | M04-M08/M14/M18/M19 | mapek、model_control、risk_and_chaos | 离线/治理 | UNVERIFIED / P1：闭环存在但无长期证据；Champion 在线变更必须硬阻断 |
| M16 数据/存储 | infra/postgres_store.py、outbox.py、event_store.py、migrations 001-006、monitoring repository | domain events → append-only authority/replay | PG/SQLite/Redis、全部主链 | PG store/outbox/migrations/store adapter | 事实底座 | PARTIALLY_IMPLEMENTED / P0：PG authority/PITR/crash replay 未验；SQLite 与内存实现并存 |
| M17 API/Frontend | control/api.py、core/health.py、launcher status endpoints | authority facts → status/control API | M13/M18/M20 | control API/gate/health | 观测/控制 | PARTIALLY_IMPLEMENTED；Frontend NOT_IMPLEMENTED / P1：字段 provenance 与授权需逐项验证 |
| M18 Monitoring | observability/monitoring、telemetry、alerts、health、supervisor | facts/SLI/incidents → alert/control/evidence | 全模块、SQLite sidecar/webhook | monitoring、alerts、health、operational fact bus | 横切/P0 | BROKEN / P0：Testnet 账户权限不检查、call-time freshness、扫描/告警事实缺口 |
| M19 Self-Healing | autonomy/recovery_planner、safety/recovery、supervisor recovery/control | incident → isolate/reconcile/recover/verify/manual resume | M13/M14/M18 | recovery fail-closed、supervisor tests、user stream recovery | 横切/P0 | BROKEN / P0：Testnet auto RESUME；异常路径语义分叉 |
| M20 Security/Config | shared/config、security、policy loader、guard、.env loader | approved config/secret refs → typed immutable settings | Operator/secret provider、全部模块 | config、security、policy、guard | 入口横切 | BROKEN / P0：shell 默认 key/策略生成、CLI 直读 .env、默认 Testnet |
| M21 CI/Test/Supply | .github/workflows、pyproject、Makefile、tests、delivery scripts | source → reproducible gate/evidence/artifact | 全模块 | 150 files/2,486 tests collected | 发布门 | BROKEN / P0：多静态门 FAIL；关键 mypy 被忽略；95/90 分支覆盖未实际强制 |
| M22 Deployment/24h | docker-compose、deploy plist、launcher、certification/unattended、production ladder | verified artifact/config → supervised runtime/certification | M00-M21、PG/Redis/Binance/host | unattended、CERT-G7 integration、release | 最终门 | UNVERIFIED / P0：当前进程不构成认证；无新鲜重启/故障/长窗口证据 |

## 6. P0/P1/P2 风险基线

### P0：进入任何可写 Testnet 前必须清零

| Risk ID | 当前证据 | 失败模式 | 立即控制 | 关闭模块 |
|---|---|---|---|---|
| RISK-P0-001 | start_beidou.sh:249-290 | 无场景执行也生成 CERT-G5 PASS/DEV_BYPASS | 禁止使用该脚本和其证书作为授权 | M00/M21 |
| RISK-P0-002 | start_beidou.sh:16-18、150-245 | 默认 Testnet，自动改基础设施/策略，使用默认签名值和固定风险参数 | 启动入口只读、无副作用、显式模式 | M00/M20 |
| RISK-P0-003 | supervisor Testnet lock/auto-resume | 故障后无人批准即重新 RESUME | Testnet 写能力全局隔离，统一人工恢复合同 | M00/M14/M19 |
| RISK-P0-004 | engine/monitoring account permission branches | canWithdraw=true 在 Testnet 被改写为安全 | 所有可写环境统一阻断 | M00/M10/M13/M18 |
| RISK-P0-005 | Testnet 1%/300s/event drift branches | 陈旧或不一致事实被当 MATCHED/ready | 统一安全语义；环境差异仅 endpoint/credential/rate/limit | M00/M13 |
| RISK-P0-006 | engine initial ACTIVE/read-time refresh | 没有新观测仍刷新保护事实并获得 ELIGIBLE | 事实时间只由来源事件更新 | M00/M12/M18 |
| RISK-P0-007 | engine protection fallback | blocked config 变固定 5%/10%，精度按价格猜测 | UNKNOWN 时不创建/替换保护；使用 venue rule snapshot | M12 |
| RISK-P0-008 | Ruff F821 order_id | fill/ledger 分支运行时 NameError，事实链中断 | 在 M00 隔离写能力；M11 修复并故障回归 | M11 |
| RISK-P0-009 | Algo inventory failure→empty、unowned Testnet cleanup | 未知订单被当空或触碰非本系统订单 | UNKNOWN 不写；归属不明不得撤单 | M11/M12/M13 |
| RISK-P0-010 | 多入口/直接 Engine | 绕过 Supervisor、preflight 与运行授权 | 单一入口架构测试与运行 interlock | M00 |
| RISK-P0-011 | 当前运行进程 + 账户事实未知 | 修改/停止可能破坏已有保护；继续也不能证明安全 | 本轮不动；实施前具名 Owner 选择隔离或受控停机 | M00 checkpoint |
| RISK-P0-012 | CI/test semantics | 扫描器漏 shell，测试明确批准 INV-006 违规 | 语义测试先红后绿；证据生成/验证分权 | M00/M21 |
| RISK-P0-013 | interlock 对 CANCEL/REDUCE/DELETE 的宽松假设 | “减少风险”操作在 owner、qty、position、intent 为 UNKNOWN 时仍可能触碰他人订单或放大净风险 | 所有终端写分类为 INCREASE/CANCEL_OWNED/REDUCE_OWNED/EMERGENCY；任一归属或数量 UNKNOWN 时全部禁写 | M00-C/M11/M12/M13 |
| RISK-P0-014 | E-011 | 同一证书形状可携带 DEV_BYPASS 并被 verifier 判为 PASS | 生产者与验证者分权；从不可变原始证据独立重算；未知变异测试 | M00-V/M21 |
| RISK-P0-015 | delivery.yaml 与旧任务状态 | 有环、旧基线或旧 PASS 被自动当作当前依赖/完成状态 | 新建绑定当前 SHA 的 versioned acyclic graph；旧图只读且状态不可导入 | M00-V/M21 |

### P1

| Risk ID | 风险 | 关闭模块 |
|---|---|---|
| RISK-P1-001 | engine 约 14.8k LOC，21 个本地提交集中，回归半径过大 | M00 后逐模块抽离 |
| RISK-P1-002 | beidou_core.engine、execution、strategy、infra 等 mypy ignore_errors | M21，按触达模块收紧 |
| RISK-P1-003 | CI 只跑 Python 3.12，而当前运行/验证为 3.14 | M21/M22 |
| RISK-P1-004 | 关键包 Line 95% / Branch 90% 只是非标准 pyproject 表，CI 未执行 per-package/branch gate | M21 |
| RISK-P1-005 | 旧 docs/run 状态互相矛盾且基线过期 | M00/M21 |
| RISK-P1-006 | research/Paper/Shadow/成本/容量/OOS 无当前闭环 | M04-M09 |
| RISK-P1-007 | Frontend 不存在，API 字段 provenance 未完整验证 | M17 |
| RISK-P1-008 | 运行时 PG/PITR/user-stream/告警/恢复没有当前 HEAD 的独立证据 | M13/M16/M18/M19/M22 |

### P2

- 文档、命名、兼容包装和性能优化；只有在 P0/P1 关闭且不改变事实语义时进入。
- UI、报表美化和非关键自动化不得挤占安全与证据任务。

## 7. 方案选项与决策

| Option | 优点 | 缺点/风险 | 结论 |
|---|---|---|---|
| O0 不行动 | 零开发成本 | 当前 P0 保留，所有 readiness/盈利结论无效 | KILL |
| O1 原样执行旧 BD-T00-T19 | 有任务包和验收框架 | 旧 baseline，已被后续改动和新旁路穿透 | 不采用 |
| O2 一次性全量重写 | 理论上可消除遗留 | 资金系统回归面最大、证据断裂、无法逐步回滚 | 不采用 |
| O3 增量收敛：M00-C 隔离 + 新交付图 + 单模块 Gate；旧任务只作意图来源 | 保留领域知识而不继承旧状态；每步可证伪/回滚，适配当前事实 | 文档和 Gate 成本较高；需要严格 Owner 与独立证据边界 | 推荐（经复审修订） |
| O4 先优化策略收益 | 可快速得到漂亮指标 | 会在不可信执行/成本/事实链上过拟合 | KILL |

Decision D-001：选择修订后的 O3。为当前 HEAD 创建新版本、无环的交付图；旧 BD 任务和 delivery.yaml 保持只读，仅提取需求意图，旧 PASS/FAIL/依赖/证书均不得导入。当前 HEAD 使用新的 Evidence ID、状态和证书。

## 8. 统一模块执行协议

每次只允许一个主模块处于 IMPLEMENTING。跨模块改动必须是当前模块不可分割的接口切片，并在任务报告中列出影响和回归测试。每个模块都执行以下固定序列：

1. Reality Check：冻结 baseline、现状调用链、状态流、数据流、当前失败证据。
2. First-Principles Contract：定义 Input、Processing、Output、State Machine、Persistence、Failure、Observability、Recovery。
3. Algorithm Ledger：为当前模块每个算法建立 ALG-MXX-NNN，记录公式、参考实现、边界、复杂度、泄漏/过拟合/生产风险。
4. Red：先增加能复现缺陷或违反不变量的测试，保留原始失败输出。
5. Minimal Implementation：最小可逆修改；不同时清理无关代码。
6. Verification：unit → property/state-machine → integration/contract → failure injection → regression → coverage/security。
7. Independent Adversarial Review：冻结 diff 和证据，尝试证明该模块仍不能用于真实资金。
8. Evidence Archive：REQ → Code → Test → Evidence → AC；记录 commit、环境、退出码、hash 和限制。
9. Gate：只有无 P0、核心 P1 关闭、无新增旁路、回滚可验证，才进入下一模块。

模块验收状态只能是 PASS、CONDITIONAL_PASS、FAIL、NOT_VERIFIABLE；接口冻结状态另设 CONTRACT_READY。CONTRACT_READY 仅表示接口、失败语义和阻断器足以让下游做离线实现，不等于行为验收，不得解锁运行、写能力或证书。UNKNOWN 不能通过解释、评分或依赖模块的 PASS 升级。

## 9. 推荐依赖顺序

提示词编号保留，但执行顺序按资金事实链依赖调整。M21 是贯穿门，每个模块都要通过其适用子集。

| Wave | 严格顺序 | 原因 | 进入下一 Wave 的 Gate |
|---|---|---|---|
| W0 安全隔离 | M00-C → M00-E → M00-V01/V02 → M00-T01/T02 → M00-V03 | 先隔离已知共享写边界，再枚举全部入口/能力，建立验证基础，冻结真值/语义合同，最后独立验收整个 M00 | M00-V03 决策后 M00 总体最高 CONTRACT_READY；Testnet 仍 HOLD |
| W1 信任底座 | M20 → M21 → M16 | 配置/密钥、证据门和持久化 authority 是其余模块前提 | 无默认 secret；CI 基础门绿；PG contract 可验证 |
| W2 资金事实脊柱 | M10 → M11 → M12 → M13 | Risk→Intent/Order→仓位/保护投影→三方对账；M13 在 M12 产出后闭合一致性，不形成互相依赖 | 单一 durable chain；所有 UNKNOWN fail-closed |
| W3 运行治理 | M18 → M19 | 先观测真实事实，再做恢复；恢复不能自证 | 告警/控制/恢复证据独立；无自动 RESUME |
| W4 数据与研究 | M01 → M02 → M03 → TASK-M06-K00(CONTRACT_READY) → M08 → M04 → M05 | 先冻结 PIT 数据与策略内核接口，再构建独立模拟器，让因子/挖掘消费 sealed OOS | 数据/统计/成本证据闭环；合同节点不获得行为 PASS |
| W5 策略与资本 | TASK-M06-IMPLEMENT → M07 → M09 | 在 M08/M04/M05 证据后实现统一 kernel，再组合和 sizing | Paper/Shadow parity 与约束通过 |
| W6 生命周期/产品 | M14 → M15 → M17 | 有事实和策略后才能晋级、自进化和展示 | 晋级可证伪；API 字段全 provenance |
| W7 外部认证 | M22 | 所有本地门通过后才可单独授权 Testnet/长窗口 | CERT-G5→Shadow→CERT-G7；Mainnet 仍需人工 CERT-G8 |

不得为了按编号顺序而让研究优化先于资金事实链。M01-M09 可以在 W2 期间只读审查，但不能并行修改或验收。

## 10. M00 具体任务包

### M00 目标与边界

M00 不是一次性修完全部安全语义，而是建立可安全开发的控制面：先封住写能力，再证明所有入口均被登记，再建立不可自签的证据门，最后把环境与事实差异机器化暴露并保持 HOLD。M10/M11/M12/M13/M18/M19 负责各自领域的语义根治；只要其中仍有开放差异，M00 不得宣称环境 parity 或 Testnet Ready。

执行顺序严格为 M00-C（Containment）→ M00-E（Entry/Capability）→ M00-V01/V02（验证基础）→ M00-T（Truth/Parity）→ M00-V03（最终独立验收）。M00-V 只有在 V03 完成后才形成 Gate 决策，不得在 T 之前提前标记完成。每个任务需要独立 Task ID、文件范围、baseline、Owner、有效期和授权；前一任务完成不自动授权后一任务。

### M00-C00：实施前权限与运行 checkout 隔离

| 字段 | 内容 |
|---|---|
| 优先级 | P0 / 首个且唯一可建议授权的任务 |
| 修改范围 | 首先只更新本 run 文档；不得修改运行状态 |
| REQ-M00-001 | 固定 HEAD、branch、工作树、Python、依赖 lock hash 和现有进程身份 |
| REQ-M00-002 | 具名 Business/Engineering/Approval/Runtime/Test Owner |
| REQ-M00-003 | Owner 选择独立 worktree 或受控停机；没有选择则 BLOCKED |
| REQ-M00-004 | 若选受控停机，必须先完成账户/订单/仓位/保护/PG/WAL 只读取证和退出计划 |
| Test/Evidence | Git/进程元数据/依赖 hash；不读取或打印 secret |
| AC-M00-001 | 运行 checkout 与开发 checkout 的 commit、路径和权限可明确区分 |
| 失败动作 | 不进入任何代码修改；保持当前运行事实 UNKNOWN |

### M00-C01：全终端写隔离与对象归属

| 字段 | 内容 |
|---|---|
| 优先级 | P0 containment；不在此任务重构 OMS |
| 主要范围 | 当前已识别的共享 transport/adapter interlock、credential/capability boundary、最小合同测试；入口穷举归 M00-E |
| REQ-M00-005 | 在 Write Capability Registry 完整且领域 P0 关闭前，当前已识别的共享终端 choke-point 对 Testnet 全部终端写默认 HOLD，而非只阻断 risk-increase；不得声称已覆盖未知旁路 |
| REQ-M00-006 | 每个终端写必须分类为 INCREASE、CANCEL_OWNED、REDUCE_OWNED 或 EMERGENCY，并绑定 Task ID、调用入口、账户、对象 owner/generation/qty、审批和失效时间 |
| REQ-M00-007 | owner、qty、position、intent、账户专属关系任一 UNKNOWN/STALE/MISMATCHED 时，四类写全部拒绝；EMERGENCY 也不得靠猜测归属执行 |
| REQ-M00-008 | 在 M11 修复未定义 order_id 与 fill/ledger 链前，触发该分支只能得到 typed ERROR/UNKNOWN，不得 ACK 或更新仓位/账本为成功 |
| REQ-M00-009 | 后续任何 Testnet 写必须使用可证明由本项目独占的专用账户；共享账户、历史对象或不明保护不得靠放宽语义处置 |
| AC-M00-002 | 对当前已识别的共享 transport/adapter/engine/script 路径做离线合同测试，缺 capability 或对象证据时 send/cancel/reduce/delete 均为零；M00-E 未完成前状态只能是 scoped containment，不是 exhaustive PASS |
| AC-M00-010 | 当前 F821 路径的最小回归只产生 ERROR/UNKNOWN + NO_NEW_RISK |
| 失败动作 | 保持全局禁写；不停止、不重启当前进程，不触碰交易所对象 |

### M00-E01：入口与写能力全量登记

| 字段 | 内容 |
|---|---|
| 优先级 | P0 |
| 主要文件 | pyproject.toml、beidou_launcher、apps、scripts、shell、plist、adapter/engine 构造点、architecture tests |
| REQ-M00-010 | 唯一可申请 writable runtime 的入口为 beidou_launcher；其他入口只能委托或硬失败 |
| REQ-M00-011 | Registry 枚举 console/module/shell/plist/cron/script、直接 adapter、直接 Engine、Algo/撤单/减仓/恢复路径，并由静态扫描与运行 dry-run 双重证明 |
| REQ-M00-012 | apps.autopilot、safety_executor、strategy_engine、cleanup_stale_exchange_orders.py、scripts/testnet/run_g5.py 默认均不可写 |
| REQ-M00-013 | 任一工具不得默认 BEIDOU_ENV=testnet、固定 symbols 或隐式 writable mode |
| REQ-M00-014 | start_beidou.sh 若保留，只能参数校验后 exec launcher；不得写文件、启动依赖或改变环境 |
| AC-M00-003 | Registry 中每个入口均有 owner、capability、调用图和负向测试；未登记入口出现即 ENG Gate FAIL |
| 失败动作 | 任一未登记或可旁路写路径使 M00-E FAIL，M00-C 禁写继续有效 |

### M00-E02：移除启动副作用与证书生产旁路

| 字段 | 内容 |
|---|---|
| 优先级 | P0 |
| 主要文件 | start_beidou.sh、launcher/preflight、certificate producer、相应测试 |
| REQ-M00-015 | 启动路径不得创建、刷新或修改 CERT-G5/CERT-G7、Paper 或 Shadow 证书 |
| REQ-M00-016 | 启动不得生成风险策略、签名 key、数据库密码、固定账户事实或业务风险参数 |
| REQ-M00-017 | 启动不得自动执行 docker compose、migration 或任何交易所写；preflight 只报告事实 |
| REQ-M00-018 | writable mode 需要显式模式、绑定单个 Task ID 的 capability、具名审批和到期/撤销；默认 action 为零写 |
| AC-M00-004 | 缺任一配置/证据/PG/账户/权限时只得到 NOT_VERIFIABLE/HOLD，不产生证书或副作用 |
| 回滚 | 只能回到上一已验证零写入口；不得恢复 DEV_BYPASS 或自签路径 |

### M00-V01：独立证据验证器与扫描器

| 字段 | 内容 |
|---|---|
| 优先级 | P0 |
| 主要文件 | certification verifier、CI、delivery scripts、semantic scanners、fixtures |
| REQ-M00-019 | verifier 必须从不可变原始 evidence 重算 scenario 结果、baseline/config/dependency hash 和最终状态，不信任 producer 给出的 PASS/details |
| REQ-M00-020 | producer 不得修改 verifier；verifier 使用独立版本化 artifact，证书记录其 content digest、版本、policy digest 与原始 evidence manifest |
| REQ-M00-039 | 受保护 CI 环境只允许运行 Quality Owner 预批准 digest；verifier/schema/policy 升级需 Quality Owner + Approval Owner 双人批准，签名 rotation manifest 绑定 old/new digest、单调版本、生效时间和最低允许版本，禁止回退；producer PR 无权改 allowlist |
| REQ-M00-021 | DEV_BYPASS、固定 PASS、空/重复 scenario、旧 commit、篡改 evidence、canWithdraw=true、PG unavailable、Mainnet URL 必须失败 |
| REQ-M00-022 | 除已知恶意 fixture 外，property/mutation/fuzz 必须覆盖未知字段组合、证据删除/交换/重放和 schema 演化 |
| REQ-M00-023 | forbidden/hardcoded scan 覆盖 Python、shell、YAML、JSON、TOML、plist、Docker/CI 配置，且检测直接 network/engine/write 入口 |
| AC-M00-005 | E-011 反例由独立 verifier 拒绝；合法证书只能由原始 evidence 重算得到，producer 文本无法改变结论 |
| AC-M00-006 | CI 不允许 continue-on-error、软覆盖率、扩大 ignore/allowlist 或让 producer 与 verifier 在同一变更中互相放行；未知/旧 verifier digest 和版本降级均硬失败 |

### M00-V02：新交付图与 Source Trace

| 字段 | 内容 |
|---|---|
| 优先级 | P0/P1 |
| 产物 | current-baseline delivery graph、Module Map、Risk/Entry/Write Registry、Source Trace、evidence manifest |
| REQ-M00-024 | 新图绑定 current commit、schema/version 和唯一 run ID；拓扑检查必须证明无环、无缺失节点和无自依赖 |
| REQ-M00-025 | 旧 delivery.yaml、BD-T task、旧证书和状态只能只读引用；不得导入 PASS/FAIL、依赖边或完成百分比 |
| REQ-M00-026 | 每项当前结论绑定 file:line、command、commit、状态和证据限制；任一 P0 可追到 REQ/Test/AC/失败动作 |
| AC-M00-007 | clean-checkout validator 对循环、旧 baseline、孤儿 AC、复用 Evidence ID、旧状态注入均硬失败 |
| AC-M00-011 | P0 Source Trace 完整率 100%，且证据 ID 在 run 内唯一 |

### M00-T01：环境语义清单、直接 RESUME 与硬阻断

| 字段 | 内容 |
|---|---|
| 优先级 | P0 contract + containment；领域修复分别归 M10/M13/M18/M19 |
| 主要范围 | environment semantic manifest、AST/call-graph gate、control interlock、parity tests |
| REQ-M00-027 | Manifest 仅预先允许 endpoint、credential namespace、rate/limit 和零真实资金差异；所有其他差异逐项列 Owner/模块/测试/状态 |
| REQ-M00-028 | 权限、UNKNOWN、对账、用户流、保护、账本、告警、锁定和恢复分支必须被机器扫描；任一 OPEN 差异强制 Testnet HOLD |
| REQ-M00-029 | Engine、Supervisor 和其他域内直接 RESUME 全部登记并在 interlock 阻断；M19 只能以 recovery request → fresh three-way reconciliation → 一次性具名授权消费恢复 |
| REQ-M00-030 | canWithdraw=true 在所有可写环境均为 P0；不得改写风险输入；专用 Testnet 账户归属未证时不可写 |
| AC-M00-008 | 相同输入的参数化 parity test 暴露所有未批准差异；只要差异开放，预期结果是 HOLD 而非伪 PASS |
| Exit | 可达到 CONTRACT_READY；只有 M10/M13/M18/M19 各自 PASS 后才可重算环境行为 PASS |

### M00-T02：事实时间与初始状态合同

| 字段 | 内容 |
|---|---|
| 优先级 | P0 contract + containment；领域修复归 M12/M13/M18 |
| 主要范围 | truth schema、read-effect detector、eligibility interlock、clock/property tests |
| REQ-M00-031 | build/read/status 不得修改 source_timestamp、observed_at、received_at 或事实 hash；检测到读副作用即 HOLD |
| REQ-M00-032 | protection、risk、account、order、position、ledger、reconciliation 初始状态均为 UNKNOWN/NOT_OBSERVED |
| REQ-M00-033 | 只有权威来源事件或独立验证器可更新时间；每次更新携带 source ID/hash 和单调序列 |
| REQ-M00-034 | 读时刷新、无输入周期刷新、wall-clock jump、stale source、missing owner、empty inventory、observer failure 均有负向测试 |
| AC-M00-009 | 同一快照重复读取 hash/timestamps 不变；事实超时或 observer 失败时 eligibility 降级为 NO_NEW_RISK |
| Exit | 检测器与阻断器可 CONTRACT_READY；实际 producer 全部诚实后才 PASS |

### M00-V03：冻结后独立对抗验收

| 字段 | 内容 |
|---|---|
| 优先级 | P0 Gate |
| REQ-M00-035 | 冻结 diff、测试、verifier artifact、evidence manifest、Entry/Write Registry、semantic manifest 与新 DAG |
| REQ-M00-036 | 独立审查必须尝试旁路 launcher、伪造证书、自动 RESUME、刷新旧事实、从 UNKNOWN 写入和使 CI 假绿 |
| REQ-M00-037 | 任一 Kill 绑定 Claim/REQ/AC/Evidence、严重度、Owner、失败动作和复验方法 |
| REQ-M00-038 | 结构问题可由方案修订关闭；系统行为问题只能由实现与新鲜证据关闭 |
| AC-M00-012 | 进入下一主模块前，当前任务范围内开放 P0 Kill=0；跨模块 P0 必须由硬阻断器隔离并保持明确 Owner |
| Gate | 本步骤才可对整个 M00 给出 PASS、CONDITIONAL_PASS、CONTRACT_READY 或 FAIL；任何状态均不自动授权运行或 Testnet 写 |

### M00 完成定义

- M00-C 只能先获得 CONDITIONAL_PASS（scope=known shared choke-points）：当前开发与运行 checkout 隔离，已识别共享 choke-point 在缺 capability/归属事实时零写；不得声称覆盖未知入口；
- M00-E 完成后才可把 containment 升级为入口覆盖 PASS：Write Capability Registry 与唯一入口的静态/动态负向测试闭合；
- M00-V01/V02 建立验证基础但不形成最终 Gate；M00-V03 在 T 完成后才验证新无环交付图、独立 verifier、不可变 raw evidence 与抗未知变异测试；
- M00-T 在领域修复前最高 CONTRACT_READY：差异/读副作用能被发现并硬阻断，不能把 OPEN 伪装成 parity；
- Ruff、format、forbidden、hardcoded、test-quality、M00 touched-code strict mypy 与相关回归必须真实通过；
- 未执行重启、部署、交易所写或 Mainnet；代码级状态不证明当前运行进程已加载修复；
- M00 总体 CONTRACT_READY 只允许后续离线工程任务，不等于 ENG-G8、CERT-G5、Testnet Ready 或运行验收。

## 10A. 前置接口合同节点

### TASK-M06-K00：冻结策略内核接口，不实现策略行为

| 字段 | 内容 |
|---|---|
| 状态上限 | CONTRACT_READY；不是 M06 PASS |
| 依赖 | M03 的 PIT feature schema；不依赖 M04/M05/M08 结果 |
| Owner | Engineering Owner + Test/Quality Owner，均须具名 |
| 输入/输出 | versioned MarketFrame/FeatureFrame/StrategyContext → ACTION/NO_ACTION/VETO/DEGRADED typed result；只定义协议、状态和 hash |
| Scope | Protocol/schema、golden neutral fixtures、版本兼容与失败语义；禁止实现 alpha、参数或候选选择 |
| Exit Gate | 接口无业务默认值；NaN/Inf/stale/not-closed 均有确定失败语义；M08 可用独立 simulator adapter 消费；artifact/digest 固定 |
| 失败动作 | M08 BLOCKED；不得让 M08 依赖生产策略实现或为候选结果修改协议 |

## 11. M01–M22 执行任务包摘要

以下每行代表一个独立主任务包。实施时必须展开为 TASK-MXX-NNN，不得把整行一次提交。

| 模块 | 依赖 | 原子交付范围 | 核心 REQ | 必须测试/证据 | Exit Gate |
|---|---|---|---|---|---|
| M20 Security/Config | M00 | secret provider、typed config、signed policy、环境隔离 | 无默认/明文 secret；不直读 .env；配置 hash/版本；权限最小化 | config precedence、tamper、rotation、Mainnet URL rejection | 所有缺失/冲突配置 fail-closed |
| M21 CI/Test/Supply | M00,M20；贯穿 | reproducible CI、SBOM、lock、semantic scans、critical coverage | Python 3.12/实际运行版本矩阵；strict mypy；冻结 coverage denominator/omit/pragma；Line≥85；关键 Line≥95/Branch≥90；critical mutation≥90% | clean checkout CI、mutation/negative fixtures、anti-gaming config diff、pip-audit/bandit/SBOM | 所有门硬失败；无 skip/降断言/移出计量范围 |
| M16 Storage | M20,M21 | PG schema/migration/outbox/event/fill/position/ledger/recon authority | 单事务 intent/outbox；fencing；append-only；PITR；Redis 非事实源 | real PG integration、crash-after-send、dual worker、backup/restore | restart/replay 无重复/丢失/改写 |
| M10 Risk | M16 | immutable RiskSnapshot、single authority、signed approval | source/observed/received timestamps；approval binds full intent；UNKNOWN→NO_NEW_RISK | property/state machine、stale/NaN/permission/margin failures | 任何风险增加有唯一批准证据 |
| M11 Execution | M10,M16 | target-delta、parent/child aggregate、send/ACK/query/retry | stable IDs；write timeout→UNKNOWN；query-before-retry；no direct protection write | adapter contract、partial fill/cancel race/503/429/crash | durable command→venue→aggregate 闭环 |
| M12 Position/Protection | M11,M16 | 从 durable fills 与独立 venue snapshot 构建 position projection、native SL/TP、coverage/replacement | nonzero position≤valid SL coverage；owner/generation/qty/side/ACK；未知 inventory 不得变 empty | partial fill/increase/reduce/reverse/restart/unknown inventory | 无未保护/孤儿/超量/错向保护；向 M13 提供只读投影 |
| M13 Account/Recon | M11,M12,M16 | REST+user stream+ledger+position/protection 四类独立事实、double-entry 与三方对账 | independent sources；sequence/gap-fill；commission/funding/PnL/margin；不反向写 M12 | real PG replay、user-stream gaps、mismatch property | 仅 fresh MATCHED 可授权新增风险 |
| M18 Monitoring | M10-M13 | typed facts、SLI、alerts、audit、adaptive polling | health only from observation；P0 immediate；alert delivery durable | collector failure、webhook failure、stale facts、clock drift | 监控失败本身可见并 fail-closed |
| M19 Self-Healing | M13,M18 | Detect→Isolate→Preserve→Reconcile→Recover→Verify→Authorize | RETRYABLE/NON_RETRYABLE/UNKNOWN/CRITICAL；无自动 RESUME | process/DB/Redis/network/user-stream crash matrix | 恢复后 fresh facts + named approval |
| M01 Market Data | M16,M18 | raw event、sequence、closed bars、DQ、backfill | de-dup/out-of-order/gap/stale/revision/timeframe isolation | reference/property + recorded replay + WS disconnect | 数据异常不能产生有效 signal |
| M02 Universe | M01 | versioned dynamic tradable universe | liquidity/depth/spread/vol/funding/OI/slippage/status/DQ | listing/delisting/hysteresis/capacity/stale rules | 无硬编码 active universe |
| M03 Feature | M01,M02 | PIT features/indicators with manifests | formula/warmup/NaN/alignment/numeric stability | independent reference/property/metamorphic tests | 每个 ALG 有对拍和边界证据 |
| M08 Backtest | M01,M03,M16,TASK-M06-K00(CONTRACT_READY) | independent event/fill/cost simulator、Paper parity、small-build benchmark；不消费生产策略模块实现 | fee/funding/spread/slippage/latency/tick/step/min notional/partial fill；不得为候选策略改模拟器 | golden ledger、PIT replay、Paper/Shadow differential、替代 OMS/零策略基线 | gross→net PnL 可逐笔重放，模拟器与候选解耦 |
| M04 Factor | M03,M08 | factor registry、IC/RankIC/ICIR/decay/turnover/correlation | sealed provenance、cost/capacity/stability | resampling/regime/sensitivity/redundancy | 低证据只能 CHALLENGER |
| M05 Mining | M03,M04,M08 | generation→screen→OOS→promotion input | purge/embargo/CPCV/PBO/DSR/multiple-testing correction | deterministic replay、candidate identity、null experiments | 无 p-hacking/data leakage |
| M06 Strategy | M03-M05,M08 | single typed kernel，ACTION/NO_ACTION/VETO/DEGRADED | no legacy fallback；strategy version/provenance/failure conditions | same input across backtest/Paper/Testnet dry-run hashes | 唯一执行图和 parity |
| M07 Portfolio | M06,M13 | conflict resolution、covariance/risk budget/allocation | alpha/cov/risk contribution/turnover/cost/liquidity/capacity/leverage/concentration/CVaR | constraint/property/stress/regime | 不简单平均；目标可解释可签名 |
| M09 Sizing/Leverage | M07,M10,M13 | dynamic target sizing/leverage | equity/margin/vol/liquidity/confidence/DD/correlation/stop/liquidation | monotonic risk properties、venue readback、liquidation stress | 风险恶化不得增仓/加杠杆 |
| M14 Lifecycle | M04-M13,M19 | factor/strategy/pair/model/risk lifecycle | transitions only from sealed evidence；NaN/Inf block | state-machine/property/restart | 无固定分数晋级/旁路 |
| M15 Evolution | M14,M18,M19 | challenger loop、promotion/rollback | Champion immutable online；experiment lineage/version/rollback | shadow comparison、bad challenger、rollback rehearsal | 无在线自改 Champion |
| M17 API/Frontend | M13,M14,M15,M16,M18,M20,M21 | provenance-backed status/control API；按需再建 UI | field→authority mapping；RBAC/CSRF/origin/audit；UNKNOWN visible | contract/auth/error/stale/E2E | 不显示固定健康/PnL/leverage |
| M22 Deployment/24h | M00-M21 | immutable artifact、deploy/restart/DR、CERT-G5/CERT-G7 | no checkout deployment；graceful stop；duplicate process；host reboot；30-day evidence | Docker/host/DB/Redis/network/Binance/clock/disk chaos | CERT-G5 PASS→Shadow→CERT-G7；Mainnet仍人工 CERT-G8 |

## 12. 跨模块不变量验收矩阵

| Invariant / Acceptance | 必须客观成立 | Test 类型 | Evidence | 失败动作 |
|---|---|---|---|---|
| AC-INV-001 UNKNOWN 时终端零写 | send/cancel/reduce/delete 都需 capability 与 owned object truth；timeout/5xx/断流后先查询/对账，归属或数量 UNKNOWN 时不得写 | contract + property + fault + real Testnet later | Write Capability Registry、durable intent transitions、venue readback、recon hash | 全部终端写 HOLD；隔离 intent/object |
| AC-INV-002 非零仓位有保护 | 每个 symbol/side/generation 的有效 SL coverage ≥ position qty | property + integration + venue later | position/protection inventory snapshots | EXIT_ONLY；具名处置 |
| AC-INV-003 稳定幂等身份 | intent/client/exchange/signal/risk/plan IDs 全链一致 | property + restart | PG rows、event log、venue query | UNKNOWN；禁止重发 |
| AC-INV-004 异常行情无信号 | gap/乱序/stale/NaN/Inf/not-closed 均 VETO/NO_ACTION | property + replay | raw event→DQ→kernel trace | 数据隔离；停止该 symbol |
| AC-INV-005 NaN/Inf 不晋级 | factor/risk/portfolio/lifecycle 输入非有限即 reject | property + fuzz | typed validation result | NOT_VERIFIABLE |
| AC-INV-006 环境核心语义一致 | 同一事实输入在 Paper/Shadow/Testnet/Production safety layer 得同一结论 | parameterized parity + AST semantic scan | semantic manifest/diff report | 全局 Testnet HOLD |
| AC-INV-007 全链可追溯 | Dataset→Factor→Strategy→Signal→Risk→Intent→Order→Fill→Position→PnL | integration + replay | trace IDs、hashes、PG rows | 不得晋级 |
| AC-INV-008 恢复不绕过状态机 | 无具名授权不能 RECOVERY→NORMAL/RESUME | state-machine + fault | control audit/recon evidence | LOCK/NO_NEW_RISK |
| AC-INV-009 健康来自真实观测 | read/status 不刷新事实；collector failure 非 PASS | contract + clock fault | source/observed/received timestamps | readiness false |
| AC-INV-010 无法证明即 NO_NEW_RISK | 任一 critical fact UNKNOWN/STALE/MISMATCHED 时 authority 关闭 | property + integration | eligibility decision trace | NO_NEW_RISK |

## 13. TASK-MXX-NNN 标准合同

每个原子任务必须包含：

| 区块 | 必填内容 |
|---|---|
| 基本信息 | Task ID、名称、P0/P1/P2、模块、baseline SHA、Owner、依赖 Gate |
| 当前问题 | 可复现症状、代码/运行证据、分类、根因，不接受纯文档结论 |
| Scope | 文件、类、函数、schema/migration、API/config；明确 Out of Scope |
| REQ | 原子、可观察、不可用“优化/增强/完善”代替行为 |
| Algorithm | ALG ID、公式/参考、参数/边界、复杂度、leakage/overfit/production risk |
| State/Data | 输入、输出、合法状态转换、authority、幂等、事务、历史兼容 |
| Failure | timeout/429/5xx/disconnect/partial/UNKNOWN/crash 的预期控制动作 |
| Test | Unit、Property、Contract、Integration、Failure、Regression、E2E 的 Test ID |
| Acceptance | Preconditions、Action、Expected、Verification、Evidence path、Owner、Fail action |
| Migration | forward-only、双读/双写如适用、校验、不可逆点、补偿 |
| Rollback | code/config/data/external side effect 的回滚与回滚后验证 |
| Evidence | 原始 stdout/stderr、exit code、environment、dependency/commit hash、manifest |
| Adversarial | Kill items、反例、未关闭项、最终 Gate |
| 固定完成输出 | 模块现状、调用链、问题分级、算法/根因/目标设计、实际 diff、测试/故障/回归结果、未解决项、证据、客观 Before/After 评分、GO/CONDITIONAL/NO-GO 共 18 项；无实现时必须明确写“不适用”，不得伪造 After |

### 禁止事项

- 禁止 Mock/Fake/Stub/Placeholder 进入生产路径；
- 禁止硬编码账户、行情、symbol、精度、价格、仓位、杠杆、保护或健康/收益数据；
- 禁止吞异常、默认成功、UNKNOWN→empty/success；
- 禁止删除/skip/弱化测试和断言；
- 禁止 Testnet 特殊宽松路径；
- 禁止一次任务修改不相关模块；
- 禁止用文档、对象存在、HTTP 200、单次 demo 下单或历史证书作为完成证据；
- 禁止同一组件既生产证据又自签 PASS；
- 禁止在未授权情况下执行外部写入、重启、部署或 Git 交付。

## 14. 测试与证据策略

### 14.1 本地基础门（每个任务）

命令必须通过 evidence collector 执行并保存原始输出；下列只是合同，不是本轮已执行结果：

1. Python compile/collection gate，且不写入运行目录。
2. Ruff lint + format check，扫描 source、tests、scripts、tools、delivery、shell/YAML/JSON/plist 的专用语义门。
3. strict mypy：当前任务触达的关键模块不得依赖 module-wide ignore_errors。
4. targeted unit/property/state-machine tests。
5. module integration/contract/failure tests。
6. architecture/entry/environment-parity tests。
7. full repository regression。
8. coverage：总 Line≥85%；关键链 Line≥95%、Branch≥90%，必须实际启用 branch 计量和 per-package enforcement；在任务开始时冻结 source denominator、omit/exclude/pragma/no-cover/skip 列表和阈值，相关变更视为独立 P0 审批；changed-code Line/Branch 均≥95%，关键不变量 mutation score≥90% 且 P0 survivor=0。
9. bandit、pip-audit、secret scan、SBOM、lock hash、artifact signing。
10. diff check、package semantic validation、evidence manifest validation。

任何一步失败立即保留输出并停止 Gate；不得通过移动代码到未计量路径、扩大 allowlist/omit/pragma、删除或 skip 测试、降低阈值或改动测试任务范围来隐藏失败。覆盖率、mutation 和扫描器的配置 diff 本身必须进入证据清单。

### 14.2 实数据库门（M16 起）

需要独立授权和隔离环境，且不得指向当前运行库：

- 从空库执行 001→latest forward migrations；
- schema head/checksum/constraints/indexes；
- approval+intent+outbox 原子提交；
- crash before send / after send / before ACK / after ACK；
- dual worker + fencing；
- user-event/fill/ledger/position/reconciliation replay；
- backup、restore、PITR、corrupt/WAL interruption；
- 恢复后对账 hash 与原事实一致。

若没有真实 PG 证据，M16 最高 NOT_VERIFIABLE，后续可做代码工作但不得获得 Testnet Gate。

### 14.3 Testnet 写门（M22，单独授权）

Testnet 不自动继承本计划或代码测试权限。必须具名确认以下前置：

- 严格域名/环境身份，无 Mainnet；
- 使用与其他程序、人或历史 run 完全隔离、可证明由本项目独占的专用账户；共享账户不可授权写；
- 当前账户 canTrade=true、canWithdraw=false，账户环境与 endpoint 身份由独立读取证明；
- 从零或全部可归属的仓位/订单/保护状态；
- 只允许一个 symbol、最小名义金额、最大损失/时长/订单数；
- 稳定 client ID、PG journal、user stream、REST gap-fill、三方对账已就绪；
- timeout/5xx 后 query-before-retry；
- 任何 UNKNOWN、保护失败、对账差异、告警失败立即停止；
- 不自动撤销或平掉不归属本 run 的对象；
- 运行结束恢复到可证明的零风险或受保护状态。

### 14.4 故障矩阵

每个模块从下列全集选择适用项并写明为什么 N/A：timeout、429、500、502、503、connection reset、DNS、WS disconnect、packet loss、stale、duplicate、out-of-order、partial fill、cancel race、UNKNOWN order、DB failure/restart、Redis failure、process crash、host reboot、double instance、NaN、Inf、invalid response、clock drift、disk pressure、alert delivery failure。

共同预期：

- 不重复下单；
- 不错误增加风险；
- 不丢仓或伪造保护；
- 不把 UNKNOWN 当 SUCCESS；
- 不伪造账本或 PnL；
- 恢复必须重新建立独立事实。

## 15. Owner 与权限检查点

执行前必须填写具名人类，Agent 不得代替：

| Role | 当前状态 | 必须批准 |
|---|---|---|
| Business/Product Owner | TBD | 优先级、范围、成功指标、机会成本 |
| Engineering Owner | TBD | 架构、实现、迁移、回滚 |
| Test/Quality Owner | TBD | 测试有效性、证据和 Gate |
| Approval Owner | TBD | Testnet 写、账户权限、资金/风险、不可逆动作 |
| Runtime Owner | TBD | 当前进程、停机/重启、事故、恢复 |
| Learning Owner | TBD | OOS/Paper/Shadow 指标、停止条件、复盘 |

| Checkpoint | 需要的新授权 | 未授权默认 |
|---|---|---|
| CP-00 创建 branch/worktree 或 Git 状态变更 | Git scope/target | 只读当前 checkout |
| CP-01 修改源码/测试 | 只绑定一个 Task ID、明确文件/测试范围、baseline SHA、Owner、到期时间与撤销条件；禁止 blanket approval | 只交付计划 |
| CP-02 停止当前 Testnet 进程 | 进程、前置事实、退出影响 | 不停止 |
| CP-03 迁移/写入 PostgreSQL | 隔离 DSN、备份、回滚 | 不连接/不写 |
| CP-04 Testnet GET/account facts | 账户/字段/目的/保留 | 不读取 |
| CP-05 Testnet order lifecycle | 专用独占账户、单个 Task ID、symbol/名义/时长/订单数/最大损失/停止条件/对象前缀 | 不下单/撤单/平仓 |
| CP-06 restart/deploy | artifact/config/host/rollback | 不重启/部署 |
| CP-07 commit/push | scoped files/branch/remote | 不提交/推送 |
| CP-08 Mainnet/real funds | 独立 CERT-G8 人工批准 | 永久 PROHIBITED |

## 16. Migration 与 Rollback 原则

- 数据库只使用 forward-only migration；错误 migration 用新的补偿 migration，不在运行库逆向改 schema。
- 先影子写/校验，再切 authority；任何双写期必须定义冲突优先级和停止条件。
- 旧事实源只有在读写流量为零、重放一致、回滚窗口结束后才能删除。
- 运行 artifact 必须固定 commit/dependency/config/policy/schema hash；不得从可变 checkout 部署。
- 回滚不得把 UNKNOWN 改写为过去的 PASS，也不得复用旧 CERT-G5/CERT-G7 时间窗口。
- 外部副作用无法回滚时，必须通过幂等补偿和 fresh reconciliation 恢复，而不是“再发一次”。
- 当前 21 个本地提交属于用户状态；实施不得 reset、覆盖或重写。

## 17. 旧 BD-T00–T19 映射与处置

本表只用于提取需求意图，不是当前交付 DAG、完成状态或执行授权。所有 REOPEN/REVERIFY/RESET 都表示“在新图中创建新 Task ID 后从零取证”，绝不表示修改或继续消费旧节点；旧 delivery.yaml 的顶层 PASS、依赖边和百分比均不可导入。

| 旧任务 | 新模块 | 处置 | 原因 |
|---|---|---|---|
| BD-T00 baseline | M00 | REOPEN | 当前 SHA/门禁已变化 |
| BD-T01 key/approval | M20/M10 | REOPEN | start script 默认 key/策略与 Testnet权限分叉 |
| BD-T02 config | M20/M02 | REOPEN | 多入口仍默认环境/symbol |
| BD-T03 exchange gateway | M00/M11 | REOPEN | 入口与保护写路径仍需证明唯一 |
| BD-T04 ClosedBar/PIT | M01/M03 | REVERIFY | 本地代码存在，外部 gap/replay 未证 |
| BD-T05 typed kernel | M06 | REOPEN | legacy+typed 同时接线 |
| BD-T06 factor lifecycle | M04/M05/M14 | REVERIFY | 证据链需当前 HEAD 重验 |
| BD-T07 risk snapshot/approval | M10 | REOPEN | 事实时间自刷/环境权限输入 |
| BD-T08 PG outbox | M16/M11 | REOPEN | 与 SQLite/in-memory 并存，真实 PG 未证 |
| BD-T09 order aggregate | M11 | REOPEN | 当前 F821 与 21 commits 高风险变更 |
| BD-T10 fill/position | M11/M13 | REOPEN | partial/restart/ledger 链未当前验证 |
| BD-T11 protection | M12 | REOPEN | 默认保护/精度/UNKNOWN inventory |
| BD-T12 ledger | M13/M16 | REVERIFY | durable PG/restore 证据缺失 |
| BD-T13 reconciliation | M13 | REOPEN | Testnet 1%/300s/event drift |
| BD-T14 lifecycle/recovery | M14/M19 | REOPEN | Testnet auto RESUME |
| BD-T15 CI/evidence | M00/M21 | REOPEN | 当前多门 FAIL，scanner 漏 shell |
| BD-T16 Paper | M08 | REVERIFY | 当前 parity/net cost 未证 |
| BD-T17 factor/portfolio | M04/M05/M07/M09 | REVERIFY | 盈利/容量证据未知 |
| BD-T18 Testnet | M22 | RESET | 历史证书不继承；当前 HOLD |
| BD-T19 unattended | M22 | RESET | 必须从零开始真实经过时间 |

## 18. Source Trace Matrix（P0/P1）

| Delivery ID | Problem/Claim | Evidence | Decision | Scope | Test/AC | Metric | Owner |
|---|---|---|---|---|---|---|---|
| DL-001 单一入口/写能力 | C-001/C-003 | E-003/E-004/E-011 | D-001 | M00-C/M00-E | AC-M00-002/003/004 | M-001 bypass count=0 | Engineering TBD |
| DL-002 环境 parity | C-002 | E-004/E-008 | D-001 | M00-T/M10/M13/M19 | AC-M00-008、AC-INV-006/008 | M-002 unapproved diff=0 | Approval TBD |
| DL-003 事实时间 | C-001 | E-004/E-008 | D-001 | M00-T/M12/M13/M18 | AC-M00-009、AC-INV-009 | M-003 read mutations=0 | Test TBD |
| DL-004 真实证据门 | C-003 | E-003/E-005/E-007/E-011 | D-001 | M00-V/M21 | AC-M00-005/006/007 | M-004 false/unknown-mutant certificates rejected=100% | Test TBD |
| DL-005 执行修复 | C-004 | E-006 | D-001 | M11 | AC-M00-010、AC-INV-001/003/007 | M-005 ambiguous writes unresolved=0 before resume | Engineering TBD |
| DL-006 PG authority | C-001 | E-010 | D-001 | M16 | AC-INV-003/007 | M-006 replay divergence=0 | Runtime TBD |
| DL-007 盈利证据 | C-006 | E-010 | D-001 | M04-M09/M22 | Hypothesis AC | M-007 net OOS/realized risk metrics | Learning TBD |

## 19. Learning Contract

| Metric ID | Claim | 基线 | 成功阈值 | 护栏/窗口 | 停止条件 | 失败动作 |
|---|---|---|---|---|---|---|
| M-001 | 无启动/写旁路 | 当前至少 4 类入口 | 可写入口=1；旁路=0 | 每次 CI | 任一新入口可写 | HOLD/M00 |
| M-002 | 环境语义一致 | 当前多条 Testnet 分叉 | 未批准差异=0 | 每次 critical diff | safety branch 未在 allowlist | HOLD |
| M-003 | 事实时间诚实 | 当前保护读时刷新 | read side-effect=0 | clock/stale tests | stale 仍 eligible | NO_NEW_RISK |
| M-004 | 证据抗伪 | 当前 validator 接受 DEV_BYPASS 语义假阳性 | known malicious + property/mutation/fuzz 未知变异 100% rejected；合法结论从 raw evidence 重算 | 每次证书/verifier schema 或 artifact 变化 | producer 文本可改变结论、raw hash 不重算或未知变异获 PASS | ENG Gate FAIL |
| M-005 | 执行幂等/恢复 | 当前未独立证明 | duplicate risk-increase=0；UNKNOWN 未查询前重发=0 | fault matrix | orphan/duplicate/unattributed | LOCK |
| M-006 | 事实链重放 | 未有当前 PG 证据 | replay/hash divergence=0 | crash/restore/PITR | ledger/position/protection diff | HOLD |
| M-007 | 净成本后研究价值 | UNKNOWN | 在看候选/OOS 结果前冻结资本上限、最大回撤、最少样本/交易数/日历窗口/regime、总试验预算（含失败）、fee/funding/spread/slippage/latency、turnover/capacity、net 指标、DSR/PBO 和选择规则 | sealed OOS + 多 regime + Paper/Shadow；Testnet 仅在独立写授权后；同时比较 Small-Build、简化/替代 OMS、flat/no-trade 与长期 Paper-only | 未预注册、试验账本不全、成本/容量失真、复杂方案不优于简单基线或风险护栏失败 | 不晋级；选择更简单方案或保持 Paper-only |

盈利 Metric 的具体数值必须由 Learning Owner 在接触候选结果前签名预注册，并绑定 dataset manifest、代码/参数、资本上限、最大回撤、试验预算和成本/容量模型。预注册前 M-007 保持 OPEN，任何回测、Paper 或 Testnet 结果只能探索，不能用于晋级或盈利声明；复杂系统若不能显著优于 Small-Build/flat/Paper-only 备选，应 KILL 或缩减范围。

## 20. 独立对抗式审查结果

完整记录见 12-adversarial-review.md。

- 首轮冻结输入：初版 00-context、初版 task plan、用户提示词、main@e2819c4、E-001 至 E-011、C-001 至 C-006、Risk/Module/REQ/AC。
- 首轮不继承作者结论，得到 10 个 P0、4 个 P1，DA-G6=FAIL，决策 PIVOT。
- 修订吸收了依赖环、旧状态污染、Gate/Evidence 冲突、全终端写能力、专用 Testnet 账户、直接 RESUME、verifier trust、M00 分解、CONTRACT_READY、覆盖率防游戏化和经济预注册。
- 第二轮发现 1 个新增 P0 和 3 个新增 P1：V/T 顺序环、C/E 覆盖边界、缺失 kernel contract 节点、verifier trust root 不闭合。
- 再修订后，独立定向复验确认上述 4 项均 CLOSED_BY_PLAN_REVISION，新增 P0=0。
- 原 14 项中，7 项计划结构问题已关闭；4 项保持 OPEN_IMPLEMENTATION，3 项保持 OPEN_DECISION，并全部绑定硬阻断、Owner/授权点和后续任务。
- Revised-plan DA-G6=PASS；只表示路线图通过对抗复验，不关闭源码/运行/证书/盈利 Gate。

## 21. 质量评分与最终结论

| 维度 | 分数/5 | 说明 |
|---|---:|---|
| 问题真实性 | 5 | 当前代码与门禁失败直接证明 |
| 证据充分度 | 3 | 代码证据强；运行账户/PG/保护仍未知 |
| 根因清晰度 | 4 | 入口、语义分叉、事实/证据自证和巨型编排可定位 |
| 战略一致性 | 5 | 资金安全与唯一事实链优先 |
| 相对价值/经济 | 2 | 已定义预注册合同与简单备选；具体阈值、人力/预算/机会成本未确认 |
| 方案可行性 | 4 | 新建无环交付图、旧任务只作意图、逐模块可回滚 |
| 范围收敛 | 4 | M00 细化；后续需逐模块再展开 |
| 执行可交付性 | 4 | REQ/Test/AC/Evidence/Owner/rollback 已定义 |
| 上线可验证性 | 2 | Testnet 专用账户、PG、运行事实和长窗口仍需要新权限与真实环境 |
| 对抗生存 | 4 | 独立首审与两轮定向复验完成；共享上下文，不等同盲审 |
| 总分 | 37/50 | PLAN-GO_WITH_CONDITIONS；系统实施与运行仍 Need Evidence |

Final Decision：PLAN-GO / SYSTEM-PIVOT。

- 对方案文档：GO；依赖图无环，M00 已拆为可单独授权的任务，独立复验新增 P0=0。
- 对源码实施：HOLD；下一授权候选仅 TASK-M00-C00，之后每个 Task 重新经过 CP-01。
- 对当前 Testnet：HOLD；当前进程存在不等于 READY。
- 对 Mainnet/真实资金：PROHIBITED。
- 对持续盈利：UNKNOWN，不作承诺。

## 22. 执行激活记录

- 激活时间：2026-08-15 22:05+08:00。
- 用户目标：依本方案执行并交付，完成后提交推送 main。
- 已授权能力：本地源码/测试/文档修改、独立 worktree、验证、commit、push main。
- 未扩展能力：现有 Testnet 进程停止/重启、账户或交易所读写、运行数据库写入、部署、Mainnet/真实资金仍需独立授权。
- 首个活动任务：TASK-M00-C00；选择独立 worktree，不触碰 PID 15276 的现有 Testnet 进程。
- Gate 规则不变：任务级验证失败即停止扩展；计划 GO 不等于源码、运行、CERT 或盈利 PASS。
