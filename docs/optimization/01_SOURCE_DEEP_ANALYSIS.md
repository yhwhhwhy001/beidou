# 北斗 Beidou 全项目深度分析与受控收敛优化方案 V1.0

> 仓库：`yhwhhwhy001/beidou`  
> 审查基线：`main@6b0d95dfa67465be421d9a4f5eaa5a406e7c3341`  
> 生成日期：2026-08-06  
> 审查方式：只读代码级静态审查；未修改仓库、未执行交易、未使用真实资金  
> 方法：Evidence → Claim → Gate → Falsifier → 独立对抗式复核

---

## 0. 执行结论

| 项目 | 结论 |
|---|---|
| 最终决策 | **PIVOT：受控收敛，不继续横向扩张** |
| Paper | **HOLD** |
| Shadow | **HOLD**，待统一事实链和成本仿真完成 |
| Testnet | **HOLD** |
| Mainnet | **PROHIBITED** |
| 生产就绪度预评估 | **24 / 100**，仅用于排序，不构成验收证据 |
| 运行证据可信度 | **低**：当前 HEAD 无可见 CI 状态或工作流运行证据 |
| 静态代码证据可信度 | **高**：关键问题可定位到当前主分支文件和提交差异 |
| 盈利能力 | **UNKNOWN / NOT VERIFIABLE** |

北斗当前已经具备较丰富的模块骨架、类型、状态枚举和安全理念，但真实运行链并未收敛。系统同时存在：

1. `beidou_core/engine.py` 中的集中式运行链；
2. `BinanceUsdmAdapter` 的契约式但未真实执行的适配器链；
3. `BinanceRESTClient` 的另一条真实 HTTP 链；
4. 旧 `AlphaGraph` 与新 `TypedAlphaGraph` 两套策略链；
5. `IntentOutbox` 与 `TransactionalOutbox` 两套内存 Outbox；
6. `OrderStateTracker` 与 `OrderStateMachine` 两套订单状态机；
7. 内存账本、SQLite 状态库、配置中 PostgreSQL 三套事实载体。

这些并行实现使“代码存在”与“运行时真实使用”之间产生严重断裂。当前最重要的工作不是增加策略、因子或自动进化功能，而是建立一条唯一、持久化、可恢复、可审计、可证伪的交易事实链。

---

## 1. 分析边界与证据规则

### 1.1 已读取范围

本次至少检查了：

- `README.md`
- `pyproject.toml`
- `.github/workflows/ci.yml`
- `Makefile`
- `apps/autopilot/__main__.py`
- `beidou_core/engine.py`
- `beidou_core/feed.py`
- `beidou_core/store.py`
- `beidou_core/guard.py`
- `beidou_data/market.py`
- `beidou_strategy/alpha/typed_graph.py`
- `beidou_research/factors/factor.py`
- `beidou_exchange/binance_usdm/rest_client.py`
- `beidou_exchange/binance_usdm/adapter.py`
- `beidou_safety/execution/intent.py`
- `beidou_infra/outbox.py`
- `beidou_safety/execution/order_state.py`
- `beidou_safety/execution/order_machine.py`
- `beidou_safety/execution/ledger.py`
- `beidou_safety/execution/reconciliation.py`
- `beidou_safety/protection/engine.py`
- `beidou_safety/risk/engine.py`
- `scripts/scan_hardcoded.py`
- `scripts/scan_test_quality.py`
- 最近 20 个提交和当前 HEAD 状态

### 1.2 尚未获得的证据

以下结论目前必须标记为 `NOT VERIFIABLE`：

- 当前 HEAD 的 `pytest` 实际执行结果；
- 当前 HEAD 的 Ruff、Mypy、Bandit、pip-audit 结果；
- 当前 HEAD 的全局和关键包覆盖率；
- Binance Testnet 的真实订单、撤单、部分成交、保护单和重启恢复结果；
- PostgreSQL 迁移、备份、恢复和一致性结果；
- 24 小时 Paper/Shadow；
- 30 天无人值守；
- 净成本后 Alpha；
- 主网候选资格。

### 1.3 证据等级

| 等级 | 说明 |
|---|---|
| E0 | README、提交说明、口头声明 |
| E1 | 当前代码、配置、接口和状态机静态证据 |
| E2 | 可复现测试命令及原始输出 |
| E3 | 集成环境、真实数据库、真实公共 API 证据 |
| E4 | Binance Testnet 真实订单链证据 |
| E5 | 多日真实经过时间的 Paper/Shadow/Testnet 证据 |

当前结论主要为 **E1**。提交消息中“817/818 tests passed”仅属于 E0，除非能关联当前 commit、工作流运行、日志哈希和可复现命令。

---

## 2. 当前基线

### 2.1 仓库状态

```text
Repository: yhwhhwhy001/beidou
Default branch: main
HEAD: 6b0d95dfa67465be421d9a4f5eaa5a406e7c3341
Decision in README: PIVOT
Paper: HOLD
Testnet: HOLD
Mainnet: PROHIBITED
```

### 2.2 明显配置不一致

1. README 徽章声明 Python `3.11+`；`pyproject.toml` 要求 `>=3.12,<4.0`。
2. Autopilot 默认将 `BEIDOU_ENV` 设置为 `testnet`，但仓库不存在 `config/env.testnet.yaml`；核心引擎和行情 Feed 又无条件打开该文件。
3. 配置模板声明 PostgreSQL，运行时核心状态仍写入本地 SQLite `beidou_state.db`。
4. 配置声明完整 Testnet/G5/Shadow/Chaos 场景，但 CI 未执行这些层级。
5. README 描述唯一 Adapter 和全链路闭环，实际运行代码仍存在多个旁路和存根实现。

### 2.3 当前质量门禁真实性

当前 CI 包含 Ruff、Mypy、单元/集成/架构测试、基础扫描、全局覆盖率、Bandit 和依赖审计，方向正确；但仍存在以下不足：

- Ruff 未检查格式；
- 关键模块存在大量 `per-file-ignores`；
- Mypy `strict=true`，但核心交易链被 `ignore_errors=true` 豁免；
- 自定义关键包覆盖率配置并不会由 coverage.py 自动执行；
- CI 未显式启用 branch coverage；
- Makefile 的 `verify` 不执行覆盖率、安全扫描、E2E、Testnet、Chaos、恢复和迁移；
- 硬编码扫描器遇到生产代码 `SyntaxError` 时直接忽略；
- 测试质量扫描器并未实现其文档所称的 Mock 生产路径检查；
- 当前 HEAD 无可见 combined status，也无可见工作流运行证据。

结论：**当前“质量门禁全绿”不可作为生产就绪证据。**

---

## 3. P0 阻断项

### BD-P0-01：当前主分支疑似存在语法回归，并重新引入默认审批密钥

**分类：部分实现 / 高概率缺陷 / 未运行验证**

#### 证据

- 最近提交移除了 `if cli_mode == "full":` 条件，但保留了其下方缩进的 G5 证书代码块；当前 `guard.py` 显示异常缩进。
- 最新 HEAD 将缺失的 `BEIDOU_SIGNING_KEY` 回退为 `beidou-testnet-default-key`。
- `verify()` 在未提供签名时，只要 Approval ID 已存在于进程内集合即可通过。

#### 影响

- 当前主分支可能无法导入或编译；
- 默认密钥破坏 fail-closed，任何共享代码环境都可生成相同签名；
- Paper/Testnet 与生产的密钥边界混乱；
- “当前测试全部通过”的声明失去可信度。

#### 修复

1. 将 `python -m compileall` 和 `python -m py_compile` 置于 CI 第一门；
2. 删除任何默认审批密钥；
3. 将无密钥状态表示为 `SIGNING_UNAVAILABLE`，风险增加请求必须拒绝；
4. Paper 模式使用明确的 `PaperApprovalPort`，不得复用真实 HMAC 签名器；
5. Testnet 必须通过短期测试密钥环境变量注入；
6. 禁止提交运行生成的审批证据和测试事件到源代码分支。

#### Falsifier

在当前 HEAD 上执行以下命令全部返回 0，且源码中不存在默认密钥：

```bash
python -m py_compile beidou_core/guard.py
python -m compileall -q beidou_* apps
rg -n "default-key|testnet-default|beidou-default" beidou_* apps config
```

---

### BD-P0-02：真实交易写链在异步上下文中被同步包装器阻断

**分类：存在实现但不可用**

#### 证据

- `_api()` 检测到正在运行的事件循环后直接返回 `{"error": -2}`；
- `_place_order()`、`_execute_protection_order()` 和 `_reconcile()` 均为异步方法，却调用同步 `_api()`；
- 因而真实下单、保护单和对账路径会收到本地错误对象，而不是交易所响应。

#### 影响

- Testnet 模式不能证明订单闭环；
- 保护单可能无法提交；
- 对账会提前返回；
- 上层仍可能继续显示进程健康。

#### 修复

- 删除核心运行路径中的同步 `_api()`；
- 所有交易所调用只允许通过 `TradingExchangePort` 的异步命名方法；
- 禁止业务层传原始 `/fapi/...` 路径；
- 在架构测试中扫描所有网络库、Binance URL 和原始端点字符串；
- 对 `Result` 进行完整模式匹配，错误不得转换为空字典。

#### Falsifier

Testnet 真实执行证据证明：订单 Intent、HTTP 请求、ACK、用户流事件、订单状态、成交、仓位、账本和对账具备同一 correlation ID，并且代码扫描显示业务层不存在 `_api` 或 `/fapi/` 字符串。

---

### BD-P0-03：存在两条交易所执行路径，其中 Adapter 仍是存根

**分类：模拟实现 + 双路径**

#### 证据

`BinanceUsdmAdapter` 当前行为包括：

- `get_account_info()` 固定 `can_trade=True`；
- 健康允许后，余额和仓位返回 `EMPTY`，但不查询交易所；
- `create_order()` 只创建局部内存 Outbox 并返回 `NEW`；
- `cancel_order()` 在本地构造 `CANCELED`；
- `get_order_status()` 新建一个空状态机并返回 `NEW`。

另一方面，`engine.py` 直接依赖 `BinanceRESTClient`。

#### 影响

- 契约测试可能通过，但生产行为未实现；
- 上层调用不同对象会得到完全不同的事实；
- 无法证明唯一执行入口；
- 容易产生“假下单、假撤单、假空仓”。

#### 修复

- `BinanceUsdmAdapter` 成为唯一实现；
- `BinanceRESTClient` 仅作为 Adapter 内部传输层；
- 删除 Adapter 内所有构造性返回；
- 所有账户、订单和仓位结果必须带 `ResultStatus`、错误分类、时间戳和来源；
- 契约测试必须对真实 Testnet 适配器运行一组只读和写入测试。

---

### BD-P0-04：行情链绕过 Adapter，且未使用已定义的 ClosedBar 合同

**分类：部分实现 / 平行架构**

#### 证据

- `beidou_core/feed.py` 自己使用 `urllib`、HMAC 和 Binance URL；
- 请求失败转换为 `{}` 或 `[]`；
- `is_healthy()` 只统计累计错误数；
- `update_features()` 将 freshness 无条件标记为 PASS；
- K 线未检查交易所闭合标志；
- `beidou_data.market.ClosedBar` 已定义，但运行引擎未使用；
- 最新 K 线可能包含尚未闭合的 bar。

#### 影响

- 前视偏差；
- 数据陈旧、缺口、乱序和重放不可区分；
- 研究、Paper 和 Testnet 不同构；
- 数据质量门禁可以被假 PASS 绕过。

#### 修复

建立唯一数据链：

```text
ExchangeMarketDataPort
→ RawEventStore
→ Sequence/Gap Validator
→ ClosedBarNormalizer
→ DataQualityGate
→ PointInTimeFeatureStore
→ StrategyKernel
```

`ClosedBar` 必须增加或明确：

```text
venue
symbol
interval
open_time
close_time
available_at
is_closed
source
sequence
revision
payload_hash
DQ tier
```

`UNKNOWN / STALE / GAP / OUT_OF_ORDER / BLOCK` 不得产生风险增加 Proposal。

---

### BD-P0-05：旧 AlphaGraph 仍是实际运行链，新 TypedAlphaGraph 未接管

**分类：平行实现 / 语义不一致**

#### 证据

- `engine.py` 实例化旧 `AlphaGraph`；
- FILTER 组件仍输出 LONG/SHORT/NO_ACTION；
- 新 `TypedAlphaGraph` 定义 ACCEPT/VETO/DEGRADE 和 Exit 约束，但没有进入 Autopilot；
- `FeatureNode` 对缺失特征填 0 并只标记 DEGRADED；
- `ExitNode` 的异常分支疑似向 `TypedNodeOutput` 传入不存在的字段。

#### 影响

- Filter 可能改变方向或成为独立入场来源；
- Exit 与 Entry 语义依赖运行时代码筛选，而非类型约束；
- Backtest、Paper、Testnet 无法保证同一策略内核；
- 新架构测试通过不代表主链使用新架构。

#### 修复

- 选择 Typed Strategy Kernel 作为唯一实现；
- 迁移完成后删除旧 `AlphaGraph`；
- 缺失输入默认 `BLOCK`，不是数值 0；
- Entry、Filter、Exit、Sizing、Protection 使用不同输出类型；
- 同一冻结输入必须在 Backtest/Paper/Shadow/Testnet 产生相同 Proposal hash。

---

### BD-P0-06：因子在启动时被无证据自动晋级为 Challenger

**分类：占位式生命周期**

#### 证据

- 引擎初始化时注册八个因子；
- 随后自动经过 `RESEARCH → BACKTEST → PAPER_TRADING → CHALLENGER`；
- 没有关联 dataset hash、统计检验、成本容量结果、OOS 证据或 Paper 经过时间；
- 生命周期状态主要保存在内存。

#### 影响

- 生命周期只是枚举变化，不是晋级门禁；
- 未验证因子可进入运行策略图；
- 系统可能把随机噪声当作 Alpha。

#### 修复

每次转移必须由不可变 `PromotionDecision` 驱动，并验证：

```text
factor_version
code_commit
policy_version
dataset_manifest_hash
feature_lineage_hash
statistical_evidence_id
cost_capacity_evidence_id
paper_evidence_id
reviewer_agent_id
decision
falsifier
```

禁止初始化代码直接晋级。

---

### BD-P0-07：两套 Outbox 均为内存实现，不具备事务性

**分类：模拟实现**

#### 证据

- `IntentOutbox` 使用 list/dict/set；
- 虽定义状态枚举，但提交、发送、ACK 并未完整驱动状态；
- ACK 使用 `intent_id`，提交去重使用 `idempotency_key`，身份语义不一致；
- `TransactionalOutbox` 同样是内存 dict，不与业务状态共享数据库事务；
- 重启后消息和幂等记录全部丢失。

#### 影响

- crash-before-send 和 crash-after-send-before-ack 无法可靠恢复；
- 重复订单风险；
- 无法实现至少一次投递 + 幂等消费；
- 订单意图不是持久化事实。

#### 修复

PostgreSQL 中建立：

```text
order_intent
outbox_message
outbox_attempt
inbox_dedup
order_command
```

约束：

- `idempotency_key UNIQUE`；
- 业务状态和 Outbox 在同一事务提交；
- Worker 使用 `FOR UPDATE SKIP LOCKED`；
- 发送前从数据库读取；
- ACK、UNKNOWN、FAILED、DEAD_LETTER 都是持久化状态；
- 重启后可继续；
- 不允许第二套 Outbox。

---

### BD-P0-08：存在两套订单状态机，且运行链使用较弱版本

**分类：重复事实源**

#### 证据

- `OrderStateTracker` 默认状态为 NEW，SENT/ACKED 仍映射为 NEW；
- `OrderStateMachine` 有更完整的 CREATED/READY/SENT/ACKED/PARTIAL 等状态；
- Engine 实际使用 `OrderStateTracker`；
- 两者均以内存 dict 保存；
- 恢复后无法重建完整事件历史。

#### 修复

- 保留一个 `OrderAggregate`；
- 以 `order_event` 追加表为事实源；
- 投影表只用于查询；
- 每个交易所事件需 `exchange_event_id` 幂等；
- UNKNOWN 恢复必须按稳定 `clientOrderId` 查询；
- 同 symbol 存在 UNKNOWN 风险增加订单时，冻结新风险；
- 取消/成交竞争以交易所最终事实和事件版本解决。

---

### BD-P0-09：当前账本不是真正的复式记账

**分类：错误实现**

#### 证据

- 单个 `JournalEntry` 同时包含 debit 和 credit；
- `is_balanced()` 要求同一条分录 debit 等于 credit；
- 余额按 `debit-credit` 计算，因此“平衡”分录对余额影响为 0；
- 没有交易事务和多账户 posting；
- SQLite 表同样只有一行 debit/credit，而不是至少两条账户分录。

#### 影响

- 无法正确表示现金、保证金、仓位成本、手续费、资金费、已实现 PnL 和应收应付；
- `is_balanced()` 可产生假阳性；
- 交易所对账缺少独立系统事实。

#### 修复

采用：

```text
ledger_transaction
ledger_posting
chart_of_accounts
account_balance_projection
```

每个 transaction 至少两条 posting，满足：

```text
sum(posting.signed_amount) == 0
```

账户至少包括：

- Cash/Wallet；
- Margin；
- Position Cost；
- Realized PnL；
- Unrealized PnL Projection；
- Fee Expense；
- Funding Expense/Income；
- Transfer；
- Suspense/Reconciliation Difference。

---

### BD-P0-10：对账使用同源余额并过滤余额差异

**分类：错误实现 / 假对账**

#### 证据

- Engine 从交易所读取 balance；
- 随后把同一个 balance 写入 exchange facts 和 system facts；
- 对账之后又过滤 `Balance mismatch`；
- `ReconciliationResult.should_block_new_risk` 未覆盖 `MISMATCHED` 和 `ONE_SIDE_MISSING`；
- `repair_strategy()` 对余额差异返回 `SYSTEM_IS_AUTHORITATIVE`，但没有权威性证据。

#### 影响

- 余额对账永远容易匹配；
- 真实账本错误被隐藏；
- 一侧缺失或差异时仍可能继续增加风险。

#### 修复

- 系统事实只从账本和订单/仓位投影读取；
- 交易所事实只从独立 Exchange Adapter 读取；
- `MISMATCHED / ONE_SIDE_MISSING / BOTH_SIDES_MISSING / ERROR` 全部阻断新风险；
- 自动修复只能创建审计调整提案，不得直接覆盖任一侧；
- 差异触发 `NO_NEW_RISK`，严重差异触发 `EXIT_ONLY` 或 `LOCK`。

---

### BD-P0-11：保护单是本地轮询触发，缺少交易所原生保护闭环

**分类：部分实现**

#### 证据

- ProtectionManager 主要在内存管理；
- 实时循环检查价格后再发送市价退出；
- swing 数据缺失时固定回退 5%；
- 未识别止损类型也固定回退 5%；
- 保护执行仍走上述失效的同步 `_api()`；
- 没有证明原生条件单 ACK、重启恢复、重复提交保护和失联修复。

#### 修复

保护分两层：

1. **交易所原生保护**：STOP_MARKET / TAKE_PROFIT_MARKET / closePosition/reduceOnly；
2. **本地安全监督**：只用于检测原生保护缺失、异常或触发紧急退出。

必须保存：

```text
position_id
protection_policy_version
client_order_id
exchange_order_id
ack_status
trigger_status
quantity_binding
reduce_only
close_position
last_verified_at
reconciliation_status
```

禁止用固定 5% 作为无数据时的隐式回退。缺数据应进入 `PROTECTION_UNAVAILABLE` 并阻断风险增加。

---

### BD-P0-12：交易池和风险参数仍由核心代码硬编码并跳过观察期

**分类：硬编码业务决策**

#### 证据

- 25 个 `DEFAULT_UNIVERSE` 固定在 `engine.py`；
- 当 CLI symbols 数量不大于 2 时自动扩展为默认交易池；
- 初始评分固定为 0.7/0.8；
- `min_observation_hours=0`；
- 启动时直接 promote + activate；
- 杠杆、仓位、阈值、RSI、SMA、时间退出和止损上下限散落在核心代码。

#### 影响

- 交易池生命周期失真；
- 已下架或流动性恶化标的可能继续使用；
- 参数版本和回滚不可追踪；
- 扫描器难以区分算法常数与业务策略参数。

#### 修复

所有业务参数进入 Policy Registry；交易池通过数据驱动状态机管理：

```text
DISCOVERED → WARMING → ELIGIBLE → ACTIVE
→ DEGRADED → SUSPENDED → RETIRED
```

启动种子只能进入 `DISCOVERED/WARMING`，不得直接 ACTIVE。

---

## 4. P1 重要问题

| 编号 | 问题 | 优化方向 |
|---|---|---|
| BD-P1-01 | REST Client 名义异步，内部使用阻塞 urllib | 改为单例 `httpx.AsyncClient`，连接池、超时分类、取消安全 |
| BD-P1-02 | RateLimitState 基本未读取响应头 | 解析权重、订单限频、Retry-After，统一预算器 |
| BD-P1-03 | FeatureStore/RawLayer/FactorRegistry 多为内存 | PostgreSQL/对象存储持久化，版本和 lineage |
| BD-P1-04 | 因子 IC 统计定义不严谨 | Newey-West、Bootstrap、Purged WFO、CPCV、多重检验、DSR、PBO |
| BD-P1-05 | `compute_marginal_contribution` 使用启发式常数 | 使用真实组合增量回测和成本容量结果 |
| BD-P1-06 | 仓位主要由单信号强度、波动、点差决定 | 账户、组合、相关性、尾部、容量、清算距离、资金费联合约束 |
| BD-P1-07 | 杠杆失败时返回目标值继续 | UNKNOWN/FAILED 时降低风险或拒绝，绝不假设生效 |
| BD-P1-08 | Paper 零写模式直接 ACKED/FILLED | 建立独立撮合与成本仿真器，模拟延迟、滑点、部分成交、拒绝 |
| BD-P1-09 | 健康状态过于粗糙 | liveness/readiness/trading-ready/exit-ready 四层健康 |
| BD-P1-10 | 运行证据被写入源代码目录并提交 | 外部 evidence bundle、commit/config hash、签名和保留策略 |

---

## 5. 第一性原理目标架构

### 5.1 唯一交易链

```text
Binance Market Data Adapter
→ Raw Market Event Store
→ Sequence / Gap / Clock Validation
→ ClosedBar / OrderBook Normalizer
→ Data Quality Gate
→ Point-in-Time Feature Store
→ Typed Strategy Kernel
→ Strategy Proposal
→ Portfolio Decision
→ Strategy Risk
→ Pre-Trade Risk
→ Signed Approval
→ Persisted Order Intent
→ Transactional Outbox
→ Single-Writer Executor
→ Binance USD-M Adapter
→ ACK / User Data Stream
→ Authoritative Order Aggregate
→ Fill Aggregate
→ Position Aggregate
→ Exchange-Native Protection
→ Double-Entry Ledger
→ Independent Reconciliation
→ Projections / Control Plane / Observability
```

### 5.2 唯一研究链

```text
Raw Dataset
→ Dataset Manifest
→ Point-in-Time Transform
→ Feature / Factor Candidate
→ Leakage & Sanity Gate
→ Statistical Evaluation
→ Cost & Capacity Evaluation
→ Purged WFO / CPCV
→ Paper Challenger
→ Shadow Challenger
→ Testnet Challenger
→ Promotion Decision
→ Champion / Suspended / Retired
```

### 5.3 事实源优先级

| 领域 | 唯一事实源 | 派生投影 |
|---|---|---|
| 原始行情 | Raw Market Event Store | Closed bars、特征、指标 |
| 策略决策 | Strategy Proposal + Portfolio Decision | UI 信号视图 |
| 风险 | Risk Snapshot + Approval | 风险仪表盘 |
| 订单 | Order Event Store | 当前订单状态 |
| 成交 | Exchange Fill Event | 成交查询 |
| 仓位 | Fill 驱动的 Position Aggregate | 当前仓位 |
| 账务 | Ledger Transaction/Postings | 余额、PnL、费用 |
| 交易所事实 | Adapter Snapshot | 对账差异 |
| 控制状态 | Versioned Control State | 控制面展示 |
| 生命周期 | Promotion/Transition Event | 当前生命周期状态 |

### 5.4 必须删除或退役的平行实现

迁移完成后，不允许长期保留“新旧并行”：

- 退役旧 `AlphaGraph`；
- 合并 `IntentOutbox` 和 `TransactionalOutbox`；
- 合并 `OrderStateTracker` 和 `OrderStateMachine`；
- 删除 Adapter 中的假账户、假下单、假撤单；
- 删除 Feed 中的直接 HTTP；
- 删除 `engine.py` 中的策略算法、交易所端点、精度逻辑、账本逻辑和保护实现；
- SQLite 只可用于明确隔离的开发测试，不得作为 Paper/Testnet 权威事实源；
- 删除自动因子晋级；
- 删除任何默认密钥、默认余额、默认仓位、默认交易精度和默认风险放行值。

---

## 6. 模块边界

### `beidou_core`

仅保留：

- 调度；
- 生命周期协调；
- 依赖注入；
- 控制命令；
- 时钟域协调；
- 健康聚合。

不得包含：策略公式、Binance 端点、订单状态机、账本、对账、止盈止损算法、交易池评分。

### `beidou_exchange`

拥有：

- Binance REST/WebSocket；
- 时间校准；
- 限频；
- filters；
- 错误分类；
- 账户模式；
- 原生条件单；
- 用户数据流；
- clientOrderId 查询和恢复。

### `beidou_data`

拥有：

- 原始事件；
- closed bar；
- order book snapshot+diff；
- DQ；
- 数据集 manifest；
- point-in-time 特征；
- 交易池。

### `beidou_strategy`

拥有：

- Typed Strategy Kernel；
- Entry/Filter/Exit/Sizing contracts；
- 市场状态；
- 组合决策；
- 策略风险。

### `beidou_safety`

拥有：

- Pre-Trade Risk；
- Approval；
- Intent；
- Outbox；
- Order Aggregate；
- Position；
- Protection；
- Ledger；
- Reconciliation。

### `beidou_research`

拥有：

- 因子挖掘；
- 统计验证；
- 回测；
- 成本容量；
- Promotion Evidence。

---

## 7. 目标数据模型

### 7.1 必需表

```text
policy_version
control_state_event
module_lifecycle_event
market_raw_event
closed_bar
feature_vector
feature_lineage
trading_pool_member
factor_definition
factor_version
factor_evaluation
promotion_decision
strategy_version
strategy_proposal
portfolio_decision
risk_snapshot
risk_approval
order_intent
outbox_message
outbox_attempt
order_event
order_projection
fill_event
position_event
position_projection
protection_order
ledger_transaction
ledger_posting
reconciliation_run
reconciliation_difference
incident
checkpoint
evidence_manifest
```

### 7.2 核心唯一约束

```text
order_intent.idempotency_key UNIQUE
order_intent.client_order_id UNIQUE
order_event(exchange, exchange_event_id) UNIQUE
fill_event(exchange, trade_id) UNIQUE
outbox_message.idempotency_key UNIQUE
policy_version(policy_id, version) UNIQUE
factor_version(factor_id, version) UNIQUE
strategy_version(strategy_id, version) UNIQUE
closed_bar(venue, symbol, interval, open_time, revision) UNIQUE
```

### 7.3 追溯字段

每个风险增加型订单必须绑定：

```text
dataset_version
feature_lineage_hash
factor_versions
strategy_version
signal_id
strategy_proposal_id
portfolio_decision_id
risk_snapshot_id
approval_id
policy_versions
intent_id
client_order_id
correlation_id
causation_id
code_commit
config_hash
```

---

## 8. 分阶段实施方案

## Phase 0：Stop-the-Line 与真实基线

目标：证明当前 HEAD 能否构建和运行，冻结所有写路径。

必须执行：

```bash
git status --short
git branch --show-current
git rev-parse HEAD
git log --oneline -20
python --version
python -m compileall -q beidou_* apps scripts
ruff check beidou_* apps tests scripts
ruff format --check beidou_* apps tests scripts
mypy beidou_* apps
pytest --collect-only -q
pytest tests -q
pytest tests -q --cov=beidou_shared --cov=beidou_safety --cov=beidou_strategy --cov=beidou_research --cov=beidou_exchange --cov=beidou_data --cov=beidou_core --cov-branch --cov-report=xml
```

规则：

- 任一语法错误立即停止；
- 不允许先改测试来适配错误实现；
- 将所有输出保存到 `artifacts/evidence/baseline/`；
- 报告必须记录失败，不得覆盖；
- 创建 `refactor/full-system-convergence-v3` 分支；
- Mainnet 和 Testnet 写权限均保持关闭，直至对应 Gate。

交付：

```text
docs/optimization/00_BASELINE.md
artifacts/evidence/baseline/manifest.json
artifacts/evidence/baseline/commands/*.txt
```

## Phase 1：P0 事实链收敛

按依赖顺序：

```text
Guard/Build
→ Config/Secrets
→ Exchange Port
→ Market Data Contract
→ Typed Strategy Kernel
→ Risk Approval
→ Intent/Outbox
→ Order Aggregate
→ Fill/Position
→ Protection
→ Ledger
→ Reconciliation
→ Recovery/Control
→ CI Evidence
```

完成标准：无 P0，唯一运行链，Paper 仍保持 HOLD，等待 Phase 2。

## Phase 2：研究与交易能力

- 因子挖掘和证据生命周期；
- 真实成本和容量模型；
- Purged WFO/CPCV；
- 交易池生命周期；
- 组合风险与自适应仓位；
- Backtest/Paper/Shadow/Testnet parity；
- 漂移和 Champion-Challenger。

## Phase 3：Paper/Shadow

- 独立撮合模拟；
- spread/slippage/funding/fee/latency/partial fill；
- deterministic replay；
- 7 天 Shadow；
- 事故和恢复演练。

## Phase 4：Testnet

- 真实 Testnet Adapter；
- 用户数据流；
- 原生保护；
- 重启恢复；
- 重复/超时/部分成交/撤单竞争；
- 对账；
- Chaos；
- 形成 G5 证书候选。

## Phase 5：无人值守认证

- 30 天运行；
- 自动恢复有界；
- 无重复订单；
- 无未保护仓位；
- 无未解释账本差异；
- 所有 P0/P1 事件有闭环；
- 仍不自动开放 Mainnet。

---

## 9. 可执行任务包

## BD-T00：冻结基线并修复构建门

- **优先级**：P0
- **目标**：当前 HEAD 可编译、可收集测试、失败证据不可覆盖。
- **预计修改**：`beidou_core/guard.py`、CI、Makefile、baseline scripts。
- **禁止修改**：不得降低测试、不得 skip、不得新增 ignore。
- **验收**：compileall、pytest collect、Ruff、Mypy 可重复；每条命令有原始日志和 exit code。
- **回滚**：单独 commit，`git revert`。

## BD-T01：密钥与审批 Fail-Closed

- **优先级**：P0
- **依赖**：BD-T00
- **预计修改**：`beidou_safety/risk/engine.py`、`beidou_security/*`、配置和测试。
- **必须实现**：无密钥不能签名；Paper 使用独立端口；Testnet 通过 env/Vault 注入；签名绑定完整 Approval payload，而非仅 ID。
- **验收**：默认密钥扫描为 0；篡改任一字段验证失败；密钥缺失拒绝风险增加。

## BD-T02：统一配置提供器

- **优先级**：P0
- **依赖**：BD-T00
- **预计修改**：新增 `beidou_shared/config/`；修改 Autopilot、Engine、Feed。
- **必须实现**：配置从 `BEIDOU_ENV` 和 schema 验证加载；缺文件显式失败；禁止核心模块自行打开固定 testnet 文件。
- **验收**：research/paper 在无交易凭据下可启动零写模式；testnet 缺凭据失败；production URL 永久阻断。

## BD-T03：单一 Exchange Gateway

- **优先级**：P0
- **依赖**：BD-T01、BD-T02
- **预计修改**：`beidou_exchange/*`、`beidou_core/engine.py`、`beidou_core/feed.py`。
- **必须实现**：所有 Binance 调用经 Adapter；REST Client 只在 Adapter 内；异步 httpx；命名方法；错误 Result 不转换为空集合。
- **验收**：业务层 `urllib/requests/httpx/binance URL/fapi endpoint` 扫描为 0；Adapter contract + Testnet contract 通过。

## BD-T04：ClosedBar 与点时特征链

- **优先级**：P0
- **依赖**：BD-T03
- **预计修改**：`beidou_data/*`、Feed、Feature Store、Strategy inputs。
- **必须实现**：closed-bar、sequence、revision、available_at、DQ；乱序/重复/缺口/陈旧处理。
- **验收**：未闭合 bar 永不进入 Kernel；确定性 replay hash 一致；DQ BLOCK 不产生风险增加 Proposal。

## BD-T05：Typed Strategy Kernel 切换

- **优先级**：P0
- **依赖**：BD-T04
- **预计修改**：`beidou_strategy/alpha/*`、`engine.py`。
- **必须实现**：Entry/Filter/Exit/Sizing 分离；修复 TypedGraph 缺陷；删除旧 Graph 运行依赖。
- **验收**：Filter 不存在 LONG/SHORT 字段；Exit 不能增加绝对风险；同输入 Proposal hash 一致。

## BD-T06：证据驱动因子生命周期

- **优先级**：P0/P1
- **依赖**：BD-T04、BD-T05
- **预计修改**：`beidou_research/factors/*`、数据库 schema、Promotion service。
- **必须实现**：每次转移校验证据；删除启动自动晋级；状态持久化；退役不可恢复为原版本。
- **验收**：缺任一证据无法晋级；重启后状态一致；所有转移可追溯。

## BD-T07：持久化 Risk Snapshot 与 Approval

- **优先级**：P0
- **依赖**：BD-T01、BD-T05
- **预计修改**：`beidou_safety/risk/*`。
- **必须实现**：R0-R10 真实接入唯一链；Approval 绑定 proposal、账户、仓位、价格、policy、过期时间和签名。
- **验收**：未知账户、DQ、交易所健康、对账差异均拒绝增加风险；Post-Risk 无审批接口。

## BD-T08：持久化 Intent + PostgreSQL Transactional Outbox

- **优先级**：P0
- **依赖**：BD-T07
- **预计修改**：`beidou_safety/execution/*`、`beidou_infra/outbox.py`、migrations。
- **必须实现**：单 Outbox；同事务；唯一幂等键；worker lease；重试、UNKNOWN、DLQ。
- **验收**：crash-before-send 不丢；crash-after-send 不重复；双 worker 只有一个发送者。

## BD-T09：统一订单聚合与 UNKNOWN 恢复

- **优先级**：P0
- **依赖**：BD-T03、BD-T08
- **预计修改**：订单状态机和 Adapter query methods。
- **必须实现**：一个状态机；event append；clientOrderId 查询；用户流幂等；cancel/fill race。
- **验收**：状态机 property test、model-based test、重放一致；UNKNOWN 未闭合阻断同标的新风险。

## BD-T10：Fill/Position 权威链

- **优先级**：P0
- **依赖**：BD-T09
- **必须实现**：仓位只由成交事件构建；支持部分成交、反向、reduce-only、hedge/one-way；重启回放。
- **验收**：数据库重建仓位与 Testnet 独立快照一致。

## BD-T11：交易所原生保护与安全监督

- **优先级**：P0
- **依赖**：BD-T03、BD-T10
- **必须实现**：原生 stop/take-profit ACK；position 绑定；精度规则；重启恢复；拒绝/失联处置；本地监督。
- **验收**：任何风险仓位在保护 SLO 内有已确认保护；否则系统自动进入 EXIT_ONLY/LOCK。

## BD-T12：真正的复式账本

- **优先级**：P0
- **依赖**：BD-T10
- **必须实现**：transaction + postings；账户科目；fee/funding/PnL；不可变追加；幂等。
- **验收**：每个 transaction postings 求和为 0；重放余额一致；重复 fill 不重复记账。

## BD-T13：独立对账与差异门禁

- **优先级**：P0
- **依赖**：BD-T03、BD-T10、BD-T12
- **必须实现**：系统事实来自账本/投影；交易所事实来自 Adapter；所有非 MATCHED 状态阻断新风险。
- **验收**：人为注入余额、仓位、订单差异均触发 NO_NEW_RISK；严重差异触发 LOCK；不得自动覆盖事实。

## BD-T14：生命周期、恢复和控制面

- **优先级**：P0/P1
- **依赖**：BD-T08 至 BD-T13
- **必须实现**：恢复分为进程、状态、事实、对账、业务五阶段；无固定等待自动 RESUME；有界重试和熔断。
- **验收**：数据库/网络/进程故障后，未完成对账不得 ACTIVE。

## BD-T15：CI 和证据系统加固

- **优先级**：P0
- **依赖**：贯穿全部任务
- **必须实现**：compile、format、strict type、global/critical coverage、mutation、architecture、security、migration、recovery、chaos 分层；证据与 commit/config hash 绑定。
- **验收**：核心链无 mypy ignore；关键包 95% line/90% branch；扫描器语法错误必须阻断。

## BD-T16：真实 Paper 撮合和成本模型

- **优先级**：P1
- **依赖**：BD-T05、BD-T09、BD-T10
- **必须实现**：延迟、排队、部分成交、拒绝、spread、slippage、fee、funding、capacity；与 Testnet 同一 Order/Position/Ledger contracts。
- **验收**：Paper 不再直接 ACKED/FILLED；同一事件序列可 replay。

## BD-T17：因子统计与组合优化

- **优先级**：P1
- **依赖**：BD-T04、BD-T06、BD-T16
- **必须实现**：Newey-West、Bootstrap、多重检验、DSR、PBO、Purged WFO、CPCV、成本容量、相关性、尾部风险。
- **验收**：任何晋级报告可由冻结数据和 commit 重现。

## BD-T18：Testnet 认证

- **优先级**：P1
- **依赖**：BD-T00 至 BD-T17
- **必须实现**：重复、超时、ACK 丢失、部分成交、撤单竞争、原生保护、重启、对账、限频、时钟漂移、凭据失效。
- **验收**：G5 全部 PASS；任一 P0 为 FAIL；不能使用 NOT_VERIFIABLE 代替通过。

## BD-T19：30 天无人值守认证

- **优先级**：P2
- **依赖**：BD-T18
- **验收**：连续经过时间、无重复订单、无未保护仓位、无未解释账本差异、自动恢复有界、所有事故有闭环。

---

## 10. 任务依赖图

```text
T00
├─ T01 ─┐
├─ T02 ─┼─ T03 ─ T04 ─ T05 ─ T06
│       │                 └─ T07 ─ T08 ─ T09 ─ T10 ─ T11
│       │                                  │       └─ T12 ─ T13
│       └──────────────────────────────────┴─────────────── T14
└─ T15（全程门禁）

T05 + T09 + T10 → T16
T04 + T06 + T16 → T17
T00..T17 → T18 → T19
```

---

## 11. 测试和验收矩阵

### T0：静态与供应链

- compileall / py_compile；
- Ruff check + format；
- Mypy strict；
- secret scan；
- hardcoded scan；
- architecture scan；
- dependency audit；
- SBOM；
- migration lint。

### T1：单元和算法不变量

- property-based；
- state-machine；
- numerical stability；
- deterministic hash；
- risk invariants；
- ledger balance；
- Filter/Exit type invariants。

### T2：数据库与集成

- PostgreSQL transaction；
- outbox/inbox；
- replay；
- migrations；
- API；
- control state；
- restart recovery；
- double worker/fencing。

### T3：Binance 公共 API

- server time；
- exchangeInfo；
- filters；
- ticker/depth/klines；
- closed-bar；
- rate-limit headers。

### T4：Binance Testnet

- create/cancel/query；
- stable clientOrderId；
- partial fill；
- cancel/fill race；
- user data stream；
- native protection；
- restart/reconciliation；
- credential failure；
- time skew；
- rate limit。

### T5：真实经过时间

- 7 天 Shadow；
- 多日 Testnet；
- 30 天无人值守；
- 自动恢复和事故闭环；
- 净成本后策略稳定性。

### 强制覆盖率

```text
Global line coverage >= 85%
Critical line coverage >= 95%
Critical branch coverage >= 90%
Mutation score for critical pure logic >= 75%
```

关键范围：

```text
risk approval
intent/outbox
order state
fill/position
protection
ledger
reconciliation
control state
recovery
exchange error handling
```

---

## 12. Gate 体系

| Gate | 名称 | 通过条件 |
|---|---|---|
| G0 | Baseline Integrity | 当前 commit 可编译，测试可收集，证据可复现 |
| G1 | Single Truth Chain | 无平行执行链、Outbox、状态机、账本和交易所旁路 |
| G2 | Data Integrity | closed-bar、DQ、PIT、replay、lineage 全通过 |
| G3 | Strategy/Research Integrity | Typed Kernel parity；因子证据门禁通过 |
| G4 | Paper Ready | 持久化订单/仓位/账本/对账；真实成本仿真；无 P0 |
| G5 | Testnet Ready | 真实 Testnet 异常矩阵和恢复全部通过 |
| G6 | Shadow Stability | 至少 7 天经过时间，漂移、成本、容量和运行稳定 |
| G7 | Unattended | 30 天无人值守证据，无未解释关键差异 |
| G8 | Mainnet Candidate | 独立验收、资金阶梯、退出和回滚计划；仍需人工明确批准 |

当前状态：

```text
G0: FAIL / NOT VERIFIABLE
G1: FAIL
G2: FAIL
G3: FAIL
G4: HOLD
G5: HOLD
G6: NOT RUN
G7: NOT RUN
G8: PROHIBITED
```

---

## 13. 三轮独立对抗式审查

### Round 1：代码与架构

寻找：

- 网络旁路；
- 第二事实源；
- 默认值；
- 静默失败；
- 类型绕过；
- 状态机非法转换；
- 事务边界；
- 旧实现仍被导入。

### Round 2：量化与算法

寻找：

- 未闭合 bar；
- 前视和泄漏；
- 幸存者偏差；
- 参数选择偏差；
- 多重检验；
- 成本/容量遗漏；
- regime 脆弱；
- 组合集中；
- 收益归因错误。

### Round 3：生产与故障

注入：

- 行情中断/乱序/缺口；
- REST 超时、ACK 丢失；
- 重复请求；
- 部分成交；
- 撤单竞争；
- 保护单拒绝；
- 用户流断开；
- DB 重启、锁等待、磁盘满；
- 双实例；
- 时钟漂移；
- 限频；
- 凭据失效；
- 对账差异；
- 极端行情。

每轮必须由独立上下文执行，输出独立 Falsifier；发现 P0/P1 后修复并重跑全量回归。

---

## 14. 最终交付结构

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

delivery/task-packages/
├── BD-T00/
├── BD-T01/
└── ...

artifacts/evidence/
├── manifest.json
├── baseline/
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

所有证据必须带：

```text
commit SHA
branch
config hash
policy versions
command
exit code
started_at / finished_at
stdout/stderr hash
artifact hash
operator/agent ID
```

---

## 15. Claude Code 首批执行指令

首批只执行 **T00–T03**，不要开始策略收益优化。

```text
1. 从 main@6b0d95dfa67465be421d9a4f5eaa5a406e7c3341 创建：
   refactor/full-system-convergence-v3

2. 冻结基线并执行 compileall、Ruff、Mypy、pytest collect、全量测试。

3. 如 guard.py 当前无法编译，先提交独立修复，不同时修改功能。

4. 删除默认签名密钥，恢复无密钥 fail-closed。

5. 建立统一配置提供器，禁止核心模块固定读取 env.testnet.yaml。

6. 将 Testnet 写权限继续保持关闭，直到 T03 完成并通过独立验收。

7. 将 BinanceRESTClient 收入 BinanceUsdmAdapter 内部；业务模块不得调用原始 endpoint。

8. 每个任务独立 commit；每个 commit 生成 evidence manifest；不得把测试运行生成物混入生产代码提交。

9. 不得删除测试、增加 skip/xfail、扩大 ignore、降低覆盖率或修改断言来获得绿色结果。

10. 首批完成后停止，输出：
    - 修改文件
    - 失败与修复
    - 测试原始结果
    - 未完成项
    - 回滚命令
    - 当前 Gate 状态
```

---

## 16. 最终判断

北斗当前不是“再优化几个算法即可进入实盘”的状态，而是需要完成一次**受控架构收敛**：

1. 先证明当前源码可构建；
2. 再消除默认密钥和写链错误；
3. 建立唯一 Exchange、Data、Strategy、Risk、Order、Position、Protection、Ledger、Reconciliation 链；
4. 再重建真实因子与策略证据；
5. 最后通过 Paper、Shadow、Testnet 和真实经过时间 Gate。

因此最终决策为：

# **PIVOT**

```text
Development: CONTINUE — 仅限收敛和证据修复
Paper: HOLD
Shadow: HOLD
Testnet: HOLD
Mainnet: PROHIBITED
Profitability: UNKNOWN / NOT VERIFIABLE
```

任何关键能力在没有 E2–E5 证据前，均不得标记为完成、生产可用或可持续盈利。
