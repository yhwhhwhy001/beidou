# 北斗 Beidou 全项目深度审查、优化重构与生产化交付总提示词

你现在是“北斗”加密货币合约量化交易系统的首席系统架构师、量化研究负责人、交易执行负责人、风险负责人、SRE、测试负责人和独立对抗式审查委员。

目标仓库：

```text
https://github.com/yhwhhwhy001/beidou
```

请直接读取当前仓库 `main` 分支的最新代码、配置、测试、脚本、CI、提交历史和文档，对整个项目进行代码级深度审查、优化设计和分阶段实施。

本任务不是编写一份泛化建议，也不是根据 README 复述现有功能。你必须以实际代码、配置、测试执行结果、运行日志、数据库状态和交易所返回结果为依据，判断系统真实能力。

---

## 一、最终业务目标

将北斗建设为：

1. 只服务于加密货币 USDⓈ-M 永续合约的个人量化交易系统。
2. 支持本地单操作员运行，不引入企业式多人审批流程。
3. 支持全天候、无人值守、自动恢复和尽量少的人为干预。
4. 具备行情、因子、策略、交易池、组合、仓位、杠杆、风险、订单、保护单、账本、对账和恢复的完整闭环。
5. 所有关键模块具备明确生命周期、健康状态、降级策略和恢复条件。
6. 在经过充分 Paper、Shadow 和 Testnet 验证后，具备逐步进入真实资金运行的工程基础。
7. 最终追求长期、净交易成本后、风险调整后的正收益，但不得承诺或伪造“持续盈利”。

盈利只能作为长期验证结果，不能通过提高回测收益、降低风险门槛、忽略成本或选择性展示结果来证明。

优化目标优先级如下：

```text
资金与账户安全
> 事实链正确性
> 数据和订单一致性
> 风险调整后净收益
> 系统可靠性
> 自适应和自动化程度
> 性能
> 功能数量
```

工程建设成本不是首要限制，但手续费、资金费、点差、滑点、市场冲击和容量必须计入策略收益。

---

## 二、当前状态与必须验证的风险线索

不得直接认定以下内容已经正确实现，必须逐项验证。

### 2.1 当前项目状态

README 将系统标记为：

```text
PIVOT
Paper HOLD
Testnet HOLD
Mainnet PROHIBITED
```

在完成证据验证前，不得更改这一状态，不得开放 Mainnet，不得使用真实资金。

### 2.2 核心引擎问题线索

重点审查：

```text
beidou_core/engine.py
```

当前文件存在以下需要验证和收敛的迹象：

1. 文件说明中明确存在绕过 ExchangeAdapter、直接调用 Binance REST 的实现。
2. 行情、策略、因子、组合、风险、订单、保护、账本和监控逻辑集中在同一个大型引擎中。
3. `DEFAULT_UNIVERSE` 在代码中固定列出交易对。
4. 自适应杠杆使用固定波动率分段。
5. 仓位计算使用固定比例、固定惩罚参数和固定上限。
6. 策略阈值、RSI 阈值、SMA 阈值、波动率阈值和信号置信度存在代码内常量。
7. 某些信号缺失时使用默认 BTCUSDT、默认价格、默认指标或默认波动率。
8. 止损百分比、风险回报比和保护配置可能在执行路径内直接构造。
9. 策略组件直接定义在核心运行引擎中，可能造成策略、执行和基础设施耦合。
10. 交易关闭、保护单成交和账本记账近期仍有大量修复，必须进行完整回归和状态机审查。

### 2.3 工程门禁问题线索

重点审查：

```text
pyproject.toml
.github/workflows/
scripts/
Makefile
tests/
```

必须验证：

1. README 的 Python 版本说明与 `pyproject.toml` 是否一致。
2. `mypy strict=true` 是否被大量 `ignore_errors=true` 实际架空。
3. `beidou_core`、交易所适配器、风险、订单状态机等关键模块为何被类型检查豁免。
4. 大量 Ruff `per-file-ignores` 是否掩盖真实缺陷。
5. 自定义的关键包覆盖率配置是否被实际执行，而不是仅存在于配置文件。
6. CI 是否执行 E2E、Testnet、性能、故障注入、恢复和数据库迁移测试。
7. 提交说明中的“769 tests passed”等结论是否有对应 CI 日志和可重现证据。
8. 扫描器 allowlist 是否可能把真实硬编码、假实现或危险行为排除。
9. 是否存在为了通过检查而删除测试、降低断言、增加 ignore、增加 allowlist 或跳过测试的行为。

---

## 三、强制工作原则

### 3.1 证据规则

严格执行：

```text
没有证据，不算完成。
代码存在，不等于功能可用。
测试通过，不等于测试有效。
能够下单，不等于交易链正确。
Testnet 成交，不等于可以实盘。
回测盈利，不等于存在可交易 Alpha。
README、提交说明和开发报告不得作为独立完成证据。
```

每个重要结论必须关联：

* 文件路径和代码行；
* 配置项；
* Git commit；
* 测试名称和测试结果；
* API 请求与响应；
* 数据库查询；
* 日志或指标；
* 故障注入结果；
* 可复现执行命令。

所有结论必须分类为：

```text
完整实现
部分实现
模拟实现
占位实现
未实现
存在实现但未经验证
无法验证
```

### 3.2 禁止事项

严禁通过以下方式完成任务：

1. 不得用 Mock、Fake、Stub、Placeholder 替代生产实现。
2. 不得将随机生成或人工构造的数据作为策略有效性证据。
3. 不得把异常转换为零余额、空仓位、空订单或健康状态。
4. 不得吞异常后继续交易。
5. 不得使用 `except Exception: pass`。
6. 不得在生产代码中留下 TODO、FIXME、NotImplemented 或无操作 `pass`。
7. 不得直接在策略、控制面或研究模块调用交易所。
8. 不得保留两条或多条相互独立的订单执行链。
9. 不得通过删除测试、增加 skip/xfail、降低覆盖率、降低断言或扩大 lint/type ignore 使 CI 通过。
10. 不得把交易参数、交易池、风险阈值、杠杆、仓位和保护参数散落硬编码在业务代码中。
11. 不得在缺少行情、账户、持仓、订单或交易所能力信息时使用“合理默认值”继续增加风险。
12. 不得启用 Mainnet 或使用真实资金完成测试。
13. 不得以“后续优化”“暂不考虑”“MVP”规避已确认的生产要求。
14. 不得为了提高回测结果删除亏损样本、缩短样本、选择最优时间段或忽略交易成本。
15. 不得声称可以保证盈利。

测试代码允许使用明确标识的 deterministic fixture、property-based generator 和故障模拟，但单元测试中的 Mock 不能替代真实数据库、真实 API、Testnet 和真实经过时间的验收证据。

---

## 四、执行流程

必须按以下顺序执行，不得直接开始大规模改代码。

# Phase 0：仓库基线冻结

记录：

```bash
git status --short
git branch --show-current
git rev-parse HEAD
git log --oneline -20
git ls-files
python --version
```

生成：

```text
docs/optimization/00_BASELINE.md
```

至少包含：

* 当前 commit SHA；
* 仓库目录树；
* 包、应用入口、工具、配置和测试清单；
* 当前依赖；
* 当前运行模式；
* 当前数据库和外部服务；
* 当前 CI 状态；
* 当前可重复执行的测试结果；
* 当前 Paper/Testnet/Mainnet 准入状态。

所有优化在独立分支执行，不直接修改 `main`。

建议分支：

```text
refactor/full-system-convergence-v3
```

# Phase 1：代码级全量审查

逐文件检查全部生产代码、配置、测试、脚本和工作流。

不要只使用关键词扫描。必须追踪真实调用链、状态流、数据流和错误流。

输出：

```text
docs/optimization/01_FULL_AUDIT_REPORT.md
docs/optimization/02_GAP_MATRIX.md
docs/optimization/03_SOURCE_TRACE.md
```

每个问题至少包含：

* 问题编号；
* P0/P1/P2 等级；
* 涉及文件；
* 代码证据；
* 触发条件；
* 失败模式；
* 对资金、收益、数据和运行稳定性的影响；
* 当前测试为什么没有发现；
* 推荐修复方案；
* 验收证据；
* Falsifier：什么证据可以证明该判断错误。

# Phase 2：第一性原理目标架构

先回答：

1. 系统为什么需要这个模块？
2. 该模块是否是最小必要复杂度？
3. 是否与其他模块职责重复？
4. 是否存在第二事实源？
5. 是否能在进程崩溃、网络超时、重复消息和交易所状态未知时恢复？
6. 模块失败后系统是 fail-closed 还是 fail-open？
7. 模块输出是否能被测试、审计和证伪？

生成唯一目标架构，不得在旧架构旁边再新增一套平行架构。

目标链必须收敛为：

```text
Market Data
→ Normalization and Data Quality Gate
→ Point-in-Time Feature Store
→ Strategy Kernel
→ Signal and Filter Contracts
→ Portfolio Decision
→ Strategy Risk
→ Pre-Trade Risk
→ Persisted Order Intent
→ Transactional Outbox
→ Exchange Adapter
→ Order State Machine
→ Position State
→ Protection Orders
→ Immutable Ledger
→ Exchange Reconciliation
→ Authoritative Projections
→ Control Plane and Observability
```

研究链必须收敛为：

```text
Raw Dataset
→ Dataset Manifest
→ Point-in-Time Processing
→ Factor Mining
→ Statistical Evaluation
→ Cost and Capacity Evaluation
→ Purged Validation
→ Paper Shadow
→ Testnet Challenger
→ Promotion Gate
→ Champion / Suspended / Retired
```

输出：

```text
docs/optimization/04_TARGET_ARCHITECTURE.md
docs/optimization/adr/
```

---

## 五、必须审查和优化的模块

### 5.1 项目架构和模块边界

逐包审查：

```text
beidou_shared
beidou_data
beidou_strategy
beidou_research
beidou_safety
beidou_exchange
beidou_policy
beidou_security
beidou_observability
beidou_lifecycle
beidou_autonomy
beidou_infra
beidou_delivery
beidou_control
beidou_chaos
beidou_production
beidou_certification
beidou_reporting
beidou_core
apps
tools
```

必须检查：

* 职责是否明确；
* 依赖方向；
* 循环依赖；
* 跨层调用；
* 重复类型和重复状态；
* 领域模型是否被基础设施污染；
* 核心引擎是否过度集中；
* 入口程序是否绕过领域服务；
* 是否存在多个风险入口、执行入口、账本或事实源。

需要将 `beidou_core/engine.py` 中的策略、配置、交易所访问、保护、记账和恢复职责拆分到对应领域模块。

核心引擎只负责：

```text
编排
生命周期协调
时钟调度
控制命令
依赖注入
健康状态聚合
```

不得继续承担算法实现和交易所协议细节。

### 5.2 行情与数据系统

检查：

* WebSocket 与 REST 回补；
* closed-bar 语义；
* symbol、venue、timeframe、open time、close time；
* sequence、revision、source；
* 重复、乱序、丢失和延迟；
* 订单簿 snapshot + diff；
* 交易所时间同步；
* 数据可用时间；
* 数据质量评分；
* 数据版本和 lineage；
* 数据缓存、持久化和恢复；
* survivorship bias；
* delisted symbol；
* funding、mark price、index price；
* 合约规格变化。

任何 UNKNOWN、STALE、GAP、OUT_OF_ORDER 数据不得触发风险增加订单。

### 5.3 交易池生命周期

删除代码中的固定交易池业务决策。

实现配置化、数据驱动的交易池生命周期：

```text
DISCOVERED
→ WARMING
→ ELIGIBLE
→ ACTIVE
→ DEGRADED
→ SUSPENDED
→ RETIRED
```

评分至少考虑：

* 成交额；
* 订单簿深度；
* 点差；
* 滑点；
* 波动率；
* funding；
* 数据完整性；
* 上市时间；
* 异常跳价；
* API 稳定性；
* 策略适配度；
* 组合相关性；
* 可交易容量。

交易池变化必须有冷却时间、滞回机制、版本和审计记录，防止频繁抖动。

### 5.4 因子挖掘和生命周期

逐个审查现有因子算法，不得因为存在类或测试就判定有效。

每个因子必须记录：

* 经济或市场微观结构假设；
* 数据输入；
* 数据可用时刻；
* 计算公式；
* 参数；
* 适用周期；
* 适用市场状态；
* 预期方向；
* 失效模式；
* 成本敏感度；
* 容量；
* 与已有因子的相关性；
* 统计证据；
* 版本；
* 生命周期；
* Kill 条件。

验证至少包括：

* IC、Rank IC、ICIR；
* Newey-West 修正；
* Bootstrap 区间；
* 稳定性；
* 单调性；
* 多重检验修正；
* Deflated Sharpe Ratio；
* Probability of Backtest Overfitting；
* Purged Walk-Forward；
* embargo；
* CPCV；
* 参数邻域；
* regime 分层；
* symbol 分层；
* 成本压力；
* 容量压力；
* 时间衰减。

生命周期必须持久化并可恢复。不得只存在于内存。

### 5.5 策略系统

将 ENTRY、FILTER、EXIT、SIZING 和 PROTECTION 语义严格分离。

FILTER 不得生成新的 LONG 或 SHORT 方向，只允许：

```text
ACCEPT
VETO
DEGRADE
```

EXIT 不得被信号融合误认为新入场信号。

逐项审查：

* 均值回归；
* 趋势跟踪；
* 突破；
* 波动率过滤；
* 成交量过滤；
* 移动退出；
* 时间退出；
* 信号融合；
* Champion-Challenger；
* 漂移检测。

所有阈值必须来自版本化 Policy 或模型参数，禁止散落于核心代码。

Backtest、Paper 和 Testnet 必须调用同一个 StrategyKernel，不得分别实现策略逻辑。

### 5.6 市场状态和自适应算法

市场状态至少覆盖：

* 趋势方向和强度；
* 波动率状态；
* 流动性状态；
* 相关性和系统性风险；
* funding 状态；
* jump/crash 状态；
* 数据质量状态。

自适应不是简单使用若干 `if volatility < threshold`。

必须建立：

```text
观测
→ 状态估计
→ 参数候选
→ 安全约束
→ Shadow 验证
→ 受控激活
→ 持续监控
→ 回滚
```

禁止运行时未经验证地自动修改影响资金风险的参数。

### 5.7 组合、仓位和杠杆

仓位决策至少考虑：

* 账户权益；
* 可用保证金；
* 当前总风险；
* 单策略风险预算；
* 单交易对风险；
* 波动率；
* 信号不确定性；
* 相关性；
* 组合集中度；
* 点差和市场冲击；
* 清算距离；
* funding；
* 最大可成交容量；
* 已有持仓和挂单；
* 尾部风险。

杠杆不是收益放大参数，而是保证金和清算风险控制参数。

不得仅根据波动率分段决定杠杆。

输出必须同时满足：

```text
目标名义敞口
目标保证金占用
允许杠杆上限
清算距离下限
单笔风险
组合风险
交易所可用杠杆
风险降级结果
```

资金费只能降低仓位或否决交易，不得机械增加仓位。

### 5.8 风控系统

审查 R0-R10 是否真实进入唯一订单链。

至少覆盖：

* 账户状态未知；
* 行情陈旧；
* 交易所异常；
* 数据缺口；
* 单笔风险；
* 单标的风险；
* 组合集中；
* 相关性；
* 最大回撤；
* 单日亏损；
* 连续亏损；
* 波动率突变；
* 流动性枯竭；
* 清算距离；
* funding 极端；
* 订单频率；
* 重复订单；
* 系统降级；
* 账本和交易所不一致。

Post-Risk 只能监控、降级、退出和锁定，不能批准风险增加订单。

### 5.9 订单、执行和幂等

建立唯一执行入口。

每笔订单必须具备：

```text
strategy_version
factor_version
dataset_version
signal_id
portfolio_decision_id
risk_snapshot_id
approval_id
intent_id
stable_client_order_id
exchange_order_id
correlation_id
causation_id
reconciliation_status
```

顺序必须是：

```text
持久化 Intent
→ 事务 Outbox
→ 发送交易所
→ 处理 ACK
→ 状态迁移
→ 成交处理
→ 仓位更新
→ 账本分录
→ 对账
```

超时后必须先用相同 `clientOrderId` 查询交易所，禁止换 ID 直接重发。

状态机至少覆盖：

```text
CREATED
PERSISTED
SENT
ACKNOWLEDGED
PARTIALLY_FILLED
FILLED
CANCEL_PENDING
CANCELED
REJECTED
EXPIRED
UNKNOWN
```

必须测试重复消息、乱序事件、部分成交、取消与成交竞争、超时、重启和网络分区。

### 5.10 保护单与退出

逐项审查：

* 固定止损；
* ATR 止损；
* 波动率止损；
* 移动止损；
* 结构止损；
* 固定 RR；
* 分批止盈；
* 移动止盈；
* 时间退出；
* 紧急退出。

保护单必须：

* 使用真实持仓方向和数量；
* 使用交易所 filters 修正精度；
* 使用 reduce-only 或 close-position 正确语义；
* 与 position ID 绑定；
* 能在重启后恢复；
* 避免重复提交；
* 处理保护单被拒绝、取消和失联；
* 在交易所原生保护不可用时进入明确降级状态。

禁止以任意固定 5% 回退后继续声称保护完整。回退策略必须配置化、可审计，并经过风险审批。

### 5.11 账本、仓位和对账

重新验证当前 debit/credit 模型是否是真正的复式记账，而不是简单现金流累计。

至少定义：

* 资产账户；
* 保证金账户；
* 已实现 PnL；
* 未实现 PnL；
* 手续费；
* funding；
* 应收应付；
* 转账；
* 调整；
* 对账差异。

交易所账户不能直接作为系统账本值，也不能为了消除 mismatch 将双方设置为同源数据。

系统事实和交易所事实必须独立获取，再进行对账。

任何关键差异必须触发：

```text
NO_NEW_RISK
EXIT_ONLY
LOCK
```

并生成结构化 Incident。

### 5.12 交易所适配器

所有 Binance 调用必须通过：

```text
ExchangeAdapter
→ BinanceUSDMAdapter
→ BinanceRESTClient / WebSocketClient
```

核心和策略模块不得直接调用 REST。

适配器必须处理：

* `/exchangeInfo` filters；
* 请求权重；
* 订单限频；
* server time；
* recvWindow；
* Binance 业务错误码；
* HTTP 错误；
* 网络超时；
* 重试和退避；
* 熔断；
* API Key 权限；
* Hedge/One-way mode；
* isolated/cross margin；
* leverage；
* reduce-only；
* conditional orders；
* Testnet 能力差异。

错误结果不得返回 `{}` 或 `[]` 冒充真实空状态。

### 5.13 生命周期、自愈和无人值守

每个模块至少具备：

```text
CREATED
INITIALIZING
VALIDATING
ACTIVE
DEGRADED
SUSPENDED
RECOVERING
FAILED
STOPPING
STOPPED
```

恢复流程必须区分：

```text
进程重启
状态恢复
事实重建
对账完成
业务恢复
```

“重启成功”不等于“恢复成功”。

自愈必须有：

* 故障指纹；
* 有界重试；
* 指数退避；
* 熔断；
* 恢复验证；
* 最大恢复次数；
* 升级告警；
* 不可自动恢复事项；
* 回滚点；
* Checkpoint。

任何自动 RESUME 都必须依赖完成对账、状态恢复和安全门禁，不得只依赖固定等待时间。

### 5.14 数据库、配置和密钥

生产状态必须使用 PostgreSQL 等持久化数据库，不得依赖进程内字典作为唯一事实源。

检查：

* schema；
* migrations；
* forward-only 迁移；
* rollback；
* transaction；
* locking；
* unique constraint；
* event version；
* outbox；
* dead-letter queue；
* backup；
* restore；
* retention；
* corruption recovery。

所有业务参数进入版本化 Policy Registry：

```text
policy_id
version
effective_at
checksum
signature
created_by
reason
rollback_version
```

密钥不得写入代码、配置模板、日志或 Git 历史。

### 5.15 控制面、安全和可观测性

本项目为本地单操作员，不需要多人审批，但必须保留机器可验证的安全门禁和不可变审计。

控制面至少提供：

```text
READ_ONLY
NO_NEW_RISK
EXIT_ONLY
EMERGENCY_FLATTEN
LOCK
```

检查：

* OIDC 或本地等价强身份方案；
* token 验证；
* API 权限；
* CSRF/CORS；
* 密钥轮换；
* 日志脱敏；
* 安全审计；
* 控制命令签名；
* 重放防护。

可观测性至少包含：

* metrics；
* structured logs；
* traces；
* incidents；
* data quality；
* market latency；
* order latency；
* rejection rate；
* reconciliation lag；
* exposure；
* leverage；
* liquidation distance；
* PnL；
* drawdown；
* factor health；
* strategy health；
* module lifecycle；
* recovery attempts。

P0 和 LOCKDOWN 告警不得被抑制。

### 5.16 测试、CI 和质量门禁

建立分层测试：

```text
T0 静态检查、Schema、Secret、硬编码和依赖
T1 单元、属性、算法不变量和状态机
T2 数据库、Outbox、Replay、API 和集成
T3 Binance public API，只读验证
T4 Binance Testnet 订单和异常场景
T5 长时间 Paper/Shadow 和无人值守证据
```

必须补充：

* property-based testing；
* deterministic replay；
* contract testing；
* mutation testing；
* state-machine testing；
* concurrency testing；
* fuzz testing；
* chaos testing；
* performance testing；
* security testing；
* migration testing；
* recovery testing。

质量门禁至少要求：

```text
Ruff：关键代码不得使用大范围忽略
Mypy：核心交易链不得 ignore_errors
Global line coverage ≥ 85%
Critical line coverage ≥ 95%
Critical branch coverage ≥ 90%
所有阈值必须由真实 CI 命令强制执行
```

关键包包括：

```text
beidou_safety
beidou_exchange
beidou_core
订单状态机
Outbox
账本
对账
保护单
风险审批
恢复引擎
```

---

## 六、算法审查输出要求

对每个因子、策略、组合算法、杠杆算法、仓位算法、风控算法和止盈止损算法分别建立评审卡。

每张评审卡必须包含：

```text
算法编号
算法名称
所在文件
业务目的
数学定义
数据输入
数据可用时刻
参数来源
默认参数
输出合同
适用市场状态
失效市场状态
计算复杂度
数值稳定性
前视偏差风险
数据泄漏风险
过拟合风险
交易成本敏感性
容量限制
当前测试
缺失测试
当前评分
推荐保留 / 重写 / 合并 / 删除
优化方案
验收条件
```

不得只描述算法概念，必须读取实际实现和实际调用路径。

---

## 七、分阶段实施要求

完成审查后生成任务依赖图，按以下顺序实施。

### P0：事实链与资金安全

优先修复：

* 多执行路径；
* Adapter 绕过；
* 状态未知时 fail-open；
* 订单幂等；
* 账本错误；
* 对账同源；
* 保护单错误；
* 仓位恢复；
* 生命周期误恢复；
* Mainnet 门禁；
* 数据时序和 closed-bar；
* 策略退出被误认为入场；
* 测试和 CI 假阳性。

P0 未全部通过，不得优化收益参数，不得进入 Testnet 晋级。

### P1：研究和交易能力

包括：

* 因子挖掘；
* 策略语义；
* 成本模型；
* 组合优化；
* 自适应仓位和杠杆；
* 交易池生命周期；
* Backtest/Paper/Testnet parity；
* 漂移检测；
* Champion-Challenger；
* 控制面和可观测性。

### P2：无人值守和长期优化

包括：

* MAPE-K；
* 自动恢复；
* 灾备；
* 性能优化；
* 多日 Shadow；
* 自动报告；
* 证据导出；
* G0-G8 晋级；
* 30 天无人值守认证。

---

## 八、任务包格式

每个任务必须能够由一个工程代理独立执行。

每个任务包含：

```text
任务编号
任务名称
优先级
目标
问题证据
涉及模块
预计修改文件
前置依赖
禁止修改范围
必须实现行为
异常和边界
数据迁移
安全要求
测试要求
验收标准
回滚方案
交付证据
```

禁止使用“按照方案实现”“完善相关测试”等模糊表述。

每项任务必须给出：

* 精确文件范围；
* 精确接口或状态变更；
* 测试文件；
* 执行命令；
* 预期结果；
* 失败判定；
* 回滚步骤。

---

## 九、强制验收矩阵

每项需求建立映射：

```text
需求
→ 设计决策
→ 代码文件
→ 测试
→ 执行结果
→ 运行证据
→ Gate
```

验收状态只能是：

```text
PASS
CONDITIONAL PASS
FAIL
NOT VERIFIABLE
```

不得使用“基本完成”“大致通过”“看起来正常”。

---

## 十、三轮独立对抗式审查

全部实施完成后执行三轮相互独立的审查。

### 第一轮：代码和架构审查

重点寻找：

* 绕过；
* 双事实源；
* 状态机漏洞；
* 事务漏洞；
* 类型漏洞；
* 默认值；
* 硬编码；
* 静默失败；
* 模块耦合。

### 第二轮：量化和算法审查

重点寻找：

* 前视偏差；
* 数据泄漏；
* 幸存者偏差；
* 多重检验；
* 过拟合；
* 成本遗漏；
* 容量遗漏；
* 参数脆弱；
* regime 依赖；
* 收益指标错误。

### 第三轮：生产和故障审查

至少模拟：

* 行情中断；
* 数据乱序；
* WebSocket 重连；
* REST 超时；
* 订单 ACK 丢失；
* 重复发送；
* 部分成交；
* 撤单与成交竞争；
* 保护单被拒；
* 账户查询失败；
* 数据库重启；
* Redis 重启；
* 进程崩溃；
* 双实例；
* 磁盘满；
* 时间漂移；
* API 限频；
* 凭据失效；
* 对账差异；
* 极端行情。

如工具支持多 Agent 或独立上下文，三轮审查必须由不同 Agent 执行；否则必须在每一轮重新建立假设，不得复制上一轮结论。

发现问题后修复，并重新执行全量回归。不得只在报告中记录问题而不处理 P0/P1。

---

## 十一、最终交付物

必须生成：

```text
docs/optimization/
├── 00_BASELINE.md
├── 01_FULL_AUDIT_REPORT.md
├── 02_GAP_MATRIX.md
├── 03_SOURCE_TRACE.md
├── 04_TARGET_ARCHITECTURE.md
├── 05_ALGORITHM_REVIEW.md
├── 06_OPTIMIZATION_PRD.md
├── 07_TASK_DEPENDENCY_GRAPH.md
├── 08_ACCEPTANCE_MATRIX.md
├── 09_MIGRATION_AND_ROLLBACK.md
├── 10_ADVERSARIAL_REVIEW_ROUND_1.md
├── 11_ADVERSARIAL_REVIEW_ROUND_2.md
├── 12_ADVERSARIAL_REVIEW_ROUND_3.md
├── 13_FINAL_ACCEPTANCE_REPORT.md
├── 14_REMAINING_RISKS.md
└── adr/
```

任务包：

```text
delivery/task-packages/
```

执行证据：

```text
artifacts/evidence/
├── commands/
├── tests/
├── coverage/
├── api/
├── database/
├── testnet/
├── chaos/
├── performance/
└── recovery/
```

最终报告必须说明：

1. 实际修改了什么；
2. 删除、合并和重写了什么；
3. 哪些能力已经完整实现；
4. 哪些仍是部分实现；
5. 哪些无法验证；
6. 所有测试和门禁结果；
7. 当前是否允许 Paper；
8. 当前是否允许 Testnet；
9. 当前是否允许 Mainnet；
10. 剩余风险；
11. 下一步需要积累的真实经过时间证据。

---

## 十二、准入标准

### Paper 准入

至少满足：

* 唯一权威交易链；
* 数据合同通过；
* 策略内核同构；
* 成本模型有效；
* 状态可恢复；
* 无 P0；
* 全量 CI 通过。

### Testnet 准入

除 Paper 条件外，还必须满足：

* 真实 Binance Testnet；
* 重复、超时、部分成交和撤单测试；
* 原生保护单验证；
* 重启恢复；
* 交易所对账；
* 故障注入；
* 连续运行证据。

### Mainnet 准入

不得由本次代码修改自动开放。

必须在独立验收中满足：

* G0-G8 全部门禁；
* 不存在 P0/P1；
* 多日 Paper 和 Testnet 证据；
* 30 天无人值守证据；
* 无未解释账本差异；
* 无重复订单；
* 无未保护持仓；
* 风险调整后净收益具备统计可信度；
* 尾部和压力测试通过；
* 明确的资金阶梯、退出和回滚计划。

在此之前：

```text
Mainnet = PROHIBITED
```

---

## 十三、开始执行

现在执行以下操作：

1. 读取整个仓库，不要只读取 README。
2. 冻结当前基线。
3. 运行现有质量门禁和测试。
4. 生成完整能力清单和调用链。
5. 识别 P0/P1/P2。
6. 先输出基线、审查报告和目标架构。
7. 按依赖顺序修复 P0，再处理 P1、P2。
8. 每完成一个任务立即运行相关测试。
9. 每完成一个阶段运行全量回归。
10. 完成三轮独立对抗式审查。
11. 生成最终验收报告和全部证据。

不要因为工作量大而省略模块，也不要以文档数量或测试数量代替真实交付。

最终决策必须明确给出以下之一：

```text
CONTINUE RESEARCH
PAPER READY
TESTNET READY
MAINNET CANDIDATE
HOLD
PIVOT
STOP
```

任何无法验证的关键能力必须判定为 `NOT VERIFIABLE`，不得推测通过。
