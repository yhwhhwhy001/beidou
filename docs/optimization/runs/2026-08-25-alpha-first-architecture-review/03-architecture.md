# 北斗 Alpha-First 架构与 G2 审查

## 执行控制

- Interaction Mode：`Yellow`。产品方向和 Testnet/Mainnet 边界明确，但资源基线、团队容量、迁移窗口、经济阈值与具名 Owner 缺失。
- S/M/L：`L`。命中核心交易链路、跨 6 个以上功能/3 个以上模块、资金/安全、自动化、数据迁移和高回滚成本。
- 当前决策上限：原方案不得直接进入开发；允许 `PIVOT` 为受控、可逆的 Alpha-first 垂直切片方案。
- 外部动作授权：无。未提交、推送、部署、启动 Testnet、下单、修改凭据、创建 Git tag/branch 或删除旧代码。
- 审查时间与环境：2026-08-25T04:02:59+08:00；macOS；Python 3.14.7；仓库 HEAD `d21a9676df24737252c3019d332b41746723977f`。
- 工作区限制：审查开始时已有 67 个非本轮未提交变更，`333 insertions / 3764 deletions`；本审查保留这些改动，不把它们当作已验收基线。
- 独立性：原方案在审查前冻结为用户附件 `pasted-text.txt`；本次为同一 Agent 的独立反方复审，存在锚定限制，因此不得自评为高风险架构 `APPROVED`。

## Proposal

冻结方案的核心方向是把北斗从 Launcher/Safety-centric 平台转为 Alpha-first 系统：

1. 将默认控制面改为 Alpha Orchestrator。
2. 把工程投入目标设为 Alpha Value Chain 92%、Execution Truth 6%、Minimal Safety 2%。
3. 收敛为 `beidou_alpha / beidou_execution / beidou_platform / beidou_safety_min` 四域。
4. 将 R2/R3/R4/R5 等绩效判断从全局交易权限移入 Alpha 生命周期。
5. 补齐 ExperimentStore、checkpoint/resume、candidate-vs-champion、lineage、economic gates、paper/testnet attribution。
6. 逐步冻结、移出默认 wheel 并最终删除 Production/Certification/Chaos/旧 Control。

### 仓库事实与 Claim 判定

| Claim | 判定 | 证据与影响 |
|---|---|---|
| `C-001` 默认 CLI 是 Launcher-centric | `SUPPORTED` | `pyproject.toml:50-53` 三个脚本均指向 `beidou_launcher.cli:main`；`beidou_launcher/cli.py:56-61,104-107,158-179` 证明无参数等价于 `start`，并构造 ReadinessGate 与 Supervisor。控制面切换问题真实。 |
| `C-002` 离线 Research 当前受 Safety 直接依赖控制 | `PARTIAL/OVERSTATED` | AST 只读扫描显示 `beidou_research` 的跨包依赖仅为 `beidou_shared`；`tests/architecture/test_architecture.py:72-93` 已禁止 Research→Safety/Strategy。应用 worker 仍通过 `beidou_core.feed` 获取数据，但并不等于 Safety 会停止整个离线内核。不能用此 Claim 证明大爆炸重命名的必要性。 |
| `C-003` Alpha 运行生命周期未闭环 | `SUPPORTED` | `apps/factor_miner/__main__.py:251-257` 的 resume 与 `310-317` 的 compare 均显式 `NOT_IMPLEMENTED`。`MiningRunner` 有候选级证据持久化，但没有运行级 checkpoint/resume 协议。 |
| `C-004` Alpha 科学内核基本缺失 | `REFUTED` | `beidou_research/mining/runner.py:246-269` 已接入 WFO、CPCV、multiple testing、stability、cost/capacity 和 evidence；仓库已有 PIT、dataset manifest、marginal contribution、Alpha V3 challenger。127 个相关架构/Alpha 测试于本轮 fresh run 全部通过。缺口是运行闭环、语义统一和现实经济证据，而非从零重建全部算法。 |
| `C-005` 92/6/2 是最优资源分配 | `UNKNOWN` | 没有工时、算力成本、维护负担、故障成本、候选产出率或团队容量基线。最近 30 次提交明显偏向 launcher/safety/observability，只能证明近期 churn，不能证明最优比例。 |
| `C-006` R2-R5 不应锁死离线研究 | `SUPPORTED` | 当前 `RiskRuleRegistry.is_approved` 要求所有 R0-R10 为 PASS；Sharpe 缆入同一注册表。应把候选/策略绩效失败限制在 Alpha/portfolio 生命周期，而不是停止无交易写入的 Research。 |
| `C-007` Testnet 的 R8 Protection 可降为 best-effort | `REFUTED` | 对有持仓的 Testnet 实验，缺保护/退出政策会改变持仓时长、尾部损失和 PnL 归因，既是执行完整性问题也是实验污染。除非有经验证的等价 deterministic exit policy，否则 R8 对新增风险必须保持 hard gate。 |
| `C-008` Alpha-First 构建可做到 Mainnet capability absent | `SUPPORTED AS TARGET / FALSE AS-IS` | 当前 `Environment` 仍包含 MAINNET，WebSocket 客户端默认 `FSTREAM_PRODUCTION_URL`，REST 接受任意 URL。目标合理，但必须通过独立 distribution allowlist、构造器约束和负向扫描证明，不能靠“不提供 MainnetVenue 类名”宣称完成。 |
| `C-009` 当前已有可支持 Champion 的经济证据 | `REFUTED` | `BASELINE_DRIFT_REPORT.md:60-69` 明确 sealed real OOS、同成本 Paper shadow 和真实经济窗口仍 `FAIL/NOT_VERIFIABLE`。代码与 fixture PASS 不是 Alpha 有效性证据。 |

## Interfaces and data

### 原方案的接口缺陷

原依赖规则包含 `beidou_alpha -> beidou_execution`，同时又要求 Safety/Execution 故障不得影响离线 Research。这两条不能同时作为强不变量：一旦 Alpha 直接依赖具体 Execution package，离线导入、发布、故障和升级就会重新耦合。

另一个风险是统一 `AlphaStore` 容易成为新的 God Object。Dataset、Feature、Factor、Experiment、Validation、Portfolio、Execution 和 Attribution 的一致性来自不可变 ID、schema、lineage 与事务边界，而不是来自一个拥有所有写权限的大仓库对象。

### 优化后的依赖模型

```text
beidou_cli                         # 稳定、安全、无默认交易副作用的 facade
├── alpha_app      -> alpha + platform + contracts
├── paper_app      -> alpha + simulator + platform + contracts
└── testnet_app    -> alpha + execution + safety_min + platform + contracts

alpha       -> contracts + platform abstractions
execution   -> contracts + platform abstractions
safety_min  -> contracts + platform abstractions
simulator   -> contracts + platform abstractions

禁止：
alpha       -> concrete execution / safety / launcher / certification / production
execution   -> alpha implementation
safety_min  -> alpha lifecycle implementation
domain package -> CLI/composition root
```

`ExecutionAuthorizationPort`、`OrderIntent`、`ExecutionFact`、`DatasetRef` 等稳定合同放在中立 contracts 边界；Testnet composition root 注入 safety policy 与 venue adapter。Alpha 只产生目标仓位/意图，不知道订单传输实现。

### 双状态机，而不是互相覆盖

```text
Candidate lifecycle:
IDEA -> GENERATED -> RESEARCH_VALIDATED -> OOS_VERIFIED
     -> COST_VERIFIED -> PAPER -> CHALLENGER -> CHAMPION
     -> DEGRADED -> RETIRED

Runtime integrity:
STARTING -> RUNNING -> WRITE_PAUSED -> STOPPING -> STOPPED
```

- Candidate gate 的 `FAIL` 只淘汰 Candidate/Experiment。
- Runtime integrity 的 `UNKNOWN` 阻止风险增加，但不阻止纯离线 Research。
- 晋级必须同时满足 Candidate evidence 与该次运行的完整性；不能用“候选好”覆盖执行事实缺失。

### 数据与 lineage 合同

不要先建全能 `AlphaStore`。先定义 append-only 运行日志和不可变实体：

- `DatasetVersion`：来源、PIT 语义、时间范围、universe、schema/hash、修订政策。
- `FeatureSetVersion`：计算实现/hash、availability lag、依赖 DatasetVersion。
- `ExperimentRun`：policy、seed/search state、代码/环境、阶段、checkpoint sequence。
- `CandidateVersion`：表达式/模型、方向、父候选、生成器、评估状态。
- `ValidationRun`：sealed split、WFO/CPCV/multiple-testing/cost/capacity 证据。
- `PortfolioDecision`：candidate vs champion、置信区间、增量与 guardrails。
- `ExecutionRun` 与 `AttributionRun`：intent/order/fill/fee/funding/slippage/PnL lineage。

Checkpoint 必须保存“已生成、已筛除、待评估、已评估、RNG/搜索器状态、证据提交序号”，不能只保存当前 candidate index。

## Failure and security model

### 环境化安全矩阵

| 边界 | Offline/Replay | Paper | Testnet Alpha-First | Mainnet |
|---|---|---|---|---|
| Venue write | 不存在 | 模拟器 | allowlisted Demo endpoint | 构建中不存在 |
| 凭据 | 不读取 | 不读取 | scope/host/account 绑定 | 不适用 |
| 幂等与 UNKNOWN | 模拟故障注入 | 必测 | hard gate；先查 venue fact 再决定重试 | 不适用 |
| 绝对敞口/集中度 | 研究指标 | 仿真 guardrail | hard cap/constraint | 不适用 |
| 保证金/清算距离 | 模型输入 | 仿真 guardrail | hard gate/cap | 不适用 |
| Protection/退出政策 | 场景模型 | 必须可重放 | ACKed protection 或经验证的等价退出政策；否则不加风险 | 不适用 |
| Kill switch/退出 | 模拟 | 必测 | hard gate，reduce-only 路径独立 | 不适用 |
| Sharpe/回撤/日亏/连亏 | Candidate/portfolio evidence | lifecycle | 降级对应 Alpha，不锁死 Research | 不适用 |

官方 Binance USDⓈ-M 文档将 Testnet REST/WS 与生产端点明确分开，并明确超时/503 可能代表 execution status UNKNOWN，需先通过流或订单查询确认、避免重复。因此 `UNKNOWN resolution` 不是可压缩的“重风控”，而是 Execution Truth 的硬不变量。

### Open Kill Register

| Kill ID | 严重度 | 攻击命题 | 状态 | 必须动作 |
|---|---|---|---|---|
| `KILL-001` | P0 | 以 2% Safety 上限为由删除 R6/R7/R8 或 UNKNOWN/reconciliation，造成 Testnet 运行与归因不可信 | OPEN | 资源政策改为 outcome target + non-negotiable safety floor；按环境建立 hard gate 矩阵。 |
| `KILL-002` | P0 | Alpha 直接依赖具体 Execution，导致离线隔离不成立 | OPEN | 改用 neutral contracts + composition root；用 network-disabled/import-deny 架构测试证明。 |
| `KILL-003` | P0 | Alpha-first wheel 仍能通过默认值、任意 URL 或旧脚本构造生产端点 | OPEN | 独立 wheel allowlist；删除 production default；host allowlist；对 wheel 内容、字符串、AST/call graph 和运行时构造做负向测试。 |
| `KILL-004` | P0 | 在已有 67 文件脏改动上做包迁移，无法归因、回归和回滚 | OPEN | 开发前由人类 Owner 选择提交/隔离 worktree/可恢复快照；建立 clean immutable baseline。 |
| `KILL-005` | P0 | E0-E6 无阈值、窗口和现实数据，功能完成被误报为 Economic Truth | OPEN | 先形成 Metric/Experiment Contract；G-A7 当前保持 NOT_VERIFIABLE。 |
| `KILL-006` | P1 | `AlphaChangeRatio`/PR 文件数成为资源代理，引发 Goodhart、阻止必要安全修复 | OPEN | 仅作诊断；用工时、compute spend、valid candidates/compute-hour、time-to-evidence 和 P0 interrupt budget 评估。 |
| `KILL-007` | P1 | Research 与 runtime 出现两套因子/成本/组合数学 | OPEN | 单一 canonical kernel；同输入 hash、same-data/same-cost parity test；旧实现只能作为 adapter。 |
| `KILL-008` | P1 | resume 跳过或重复候选，改变多重检验总体并制造选择偏差 | OPEN | 故障注入覆盖每阶段；恢复结果与 uninterrupted run 的候选集合、序列、证据 hash 满足明确确定性合同。 |
| `KILL-009` | P1 | 根 CLI 一次性切换破坏脚本、运维和回滚 | OPEN | 先建稳定 facade 与显式子命令；兼容期 telemetry；最后切 entrypoint，不把 package ownership 等同于 CLI path。 |
| `KILL-010` | P1 | 单一 AlphaStore 成为新 God Object 与并发写热点 | OPEN | 分离 repositories；append-only run ledger；明确事务/幂等/迁移/备份恢复合同。 |

## Migration and rollback

### 原 R0-R11 的主要问题

1. 以“横向层迁移”为主，直到后期才形成 Alpha→Paper→Testnet 闭环，价值和风险反馈过晚。
2. 在 persistence contract 稳定前先承诺大量 package 搬迁，会把语义变更、路径变更和安全删减混在一起。
3. CI pivot 排到 R10 太晚；新边界从第一片代码起就必须被 architecture/negative tests 强制。
4. 仅靠 Git tag 归档旧生产能力不足以证明可恢复；还需要 signed/annotated ref、artifact hash、lock/SBOM、恢复说明和演练证据。

### 优化后的阶段方案：Vertical-Slice Strangler

| Phase | 交付切片 | 进入条件 | 客观退出条件 | 回滚 |
|---|---|---|---|---|
| `P0 Baseline & Quarantine` | 隔离当前 67 文件改动；记录 clean HEAD、依赖图、CLI、wheel、测试和经济证据现状 | 人类选择如何保留现有改动 | clean immutable baseline；无未解释 diff；G-A7 仍明确 NOT_VERIFIABLE | 回到 baseline ref/worktree；不删任何代码 |
| `P1 Safe Facade & Contracts` | 新增无默认交易副作用的 `beidou_cli` facade、neutral contracts、架构负向测试；旧 launcher 仍在 `execution` 子命令下 | P0 PASS | `beidou` 只显示 help/status；显式 `execution start` 才可启动；Alpha offline 在禁网且 safety/launcher 不可导入时运行 | entrypoint feature flag/旧 console script |
| `P2 Resumable Experiment Slice` | 只选现有 Template Grid + 单 Dataset/Universe/Timeframe，贯通 run ledger、checkpoint/resume、report | P1 PASS，checkpoint schema 冻结 | 每个阶段 kill/restart 后无丢失/重复；uninterrupted 与 resumed 结果满足确定性合同；corrupt/stale checkpoint fail closed | 旧 runner 只读保留；新 resume flag 可关闭 |
| `P3 Economic Evidence Slice` | sealed OOS、PIT/lineage、WFO/CPCV、multiple testing、after-cost/capacity、candidate-vs-champion | P2 PASS，Metric Owner/阈值已签字 | 无 manifest/泄漏/未封存 OOS 为 NOT_VERIFIABLE；增量组合价值带置信区间与 guardrails；现有科学模块通过 semantic negative fixtures | 不晋级候选；保留证据，不重写历史 |
| `P4 Paper Parity & Attribution` | canonical alpha kernel 同时供 research/paper；完整 PnL decomposition | P3 PASS | same-data/same-cost parity；fee/funding/slippage/position/PnL lineage 100% 绑定；残差在预设 tolerance 内 | Paper-only；禁止 Testnet writes |
| `P5 Execution Truth Extraction` | 以 ports/adapters 从 `beidou_safety`/engine 抽出 intent/order/fill/ledger/reconciliation；旧新双读影子比较 | P4 PASS | UNKNOWN 查询收敛；intent→orders→fills 唯一可解释；重启可重建；旧新 journal 在 tolerance 内一致 | 路由切回旧实现；新实现只读保留证据 |
| `P6 Testnet Safety Cutover` | 独立 Alpha-first wheel；Demo host allowlist；minimal safety hard matrix；授权的 Testnet 证据窗口 | P5 PASS；具名 Approval/Runtime Owner；显式运行授权 | 构建无 Mainnet capability；write-registry/负向扫描 PASS；明确授权的 Testnet window 内 reconciliation/attribution/unknown/protection 全部满足合同 | kill switch；禁止新风险；旧 wheel/adapter 回切 |
| `P7 CLI & Wheel Cutover` | facade 成为稳定入口；默认 wheel 移除未使用 legacy packages | P6 PASS，兼容 telemetry 无未知调用者 | 旧入口 0 调用、0 imports、rollback drill PASS、安装/隔离启动验证 PASS | 恢复上一个 signed artifact |
| `P8 Legacy Removal` | 删除 active main 中已无调用者的旧代码 | 连续验证窗口与两次 release candidate 均无 fallback；Owner 批准 | source trace、tag/artifact/lock/SBOM/restore doc 完整；全量回归和独立验收 PASS | 从可验证 artifact/ref 恢复 |

CI 不应等到后期才改。每个 Phase 都先加自己的 architecture/contract/negative gate；全仓 coverage 阈值只能在风险测试组合已定义、并以 mutation/semantic fixtures 证明没有降级后调整。

## Alternatives

| Option | 价值 | 成本/风险 | 可逆性 | 结论 |
|---|---|---|---|---|
| `O0 No-Build`：继续当前安全瘦身 | 快，减少局部代码 | 不补 resume/compare/economic evidence；可能误删关键执行真相 | 中 | 不推荐，仅停止新增 legacy 功能 |
| `O1 原方案 Big-Bang 四域迁移` | 终态叙事清晰 | 长时间无端到端证据；路径/语义/安全同时变化；当前脏基线放大风险 | 低 | `REJECTED AS WRITTEN` |
| `O2 Vertical-Slice Strangler` | 每一步关闭一个真实 Alpha 闭环，复用已有内核，同时逐步收紧边界 | 需要兼容层和双跑期 | 高 | `RECOMMENDED` |
| `O3 独立 greenfield Alpha 仓库` | 研究隔离最快 | 两套数学、数据和成本语义；Paper/Testnet parity 风险最高 | 中 | 只可作短期 sandbox，不可作为正式 runtime |
| `O4 只改资源政策/CI 比例` | 成本低 | 指标可游戏，不能改变架构或经济证据 | 高 | 仅作辅助，不是方案 |

## Independent findings

1. **方向成立、规模不成立。** 根 CLI 的确有危险默认副作用，Engine 也仍是 15k+ 行 God Object；但 Research 包本身已具备较干净边界和大量 Alpha 科学模块。最短路径是补闭环并以 strangler 抽取，而不是先把 20 个包改名为 4 个包。
2. **真正 P0 是 ExperimentRun，而不是目录结构。** 当前候选证据可保存，运行本身不可精确恢复；resume/compare 的缺失直接浪费算力并可能引入选择偏差。
3. **R2-R5 应迁移，R6-R10 不能按原表整体弱化。** Sharpe/回撤等策略绩效属于 lifecycle；margin、liquidation、protection、account capability、duplicate/UNKNOWN/reconciliation 属于 Testnet 运行与实验可信度硬边界。
4. **Mainnet absent 必须是 artifact property。** 当前源码仍含 Mainnet enum、生产 WebSocket 默认值和可注入任意 REST URL。目标应由独立 wheel 与 negative oracle 证明，而非靠配置默认关闭。
5. **Economic Truth 目前仍是目标，不是事实。** 相关代码/fixture 通过只证明实现存在；仓库自己记录 G-A7 未通过。任何 Champion、盈利或 Testnet-ready 表述都必须继续 fail closed。
6. **92% 是 North Star，不是初始硬 Gate。** 迁移期本就需要 contracts、execution extraction 和 safety proof；固定比例会诱导错误分类和隐藏必要工作。先测 4 周滚动工时、compute spend、valid-candidate yield、time-to-evidence 和故障成本，再设阶段性目标。PR diff ratio 只报告，不阻断 P0 修复。
7. **现有未提交删减需要单独验收。** 本轮 127 个相关测试通过不覆盖这 67 个改动的完整回归、coverage、告警可达性或运行行为；不得把 targeted PASS 当成工作区整体 PASS。

## Decision

### Architecture gate

- 对冻结原方案：`REJECTED`。
- Gate 结果：`FAIL`。
- 对战略方向：保留 Alpha-first、Mainnet-absent、resume/compare、economic evidence、portfolio incremental value 和 legacy freeze。
- 对执行方案：`PIVOT` 到 `O2 Vertical-Slice Strangler`。

### G0-G7 Summary

| Gate | 状态 | 原因与决策上限 |
|---|---|---|
| G0 Interaction/Kill | `PARTIAL` | 范围清晰，但脏基线、Owner、资源与窗口未冻结；无 FATAL，可继续审查，不能开工。 |
| G1 Problem/Axiom | `PARTIAL` | Launcher 默认启动、God Object、resume/compare 缺失真实；“Safety 阻断所有 Research”被夸大，损失规模未量化。 |
| G2 Evidence/Reality | `PARTIAL` | 代码与 127 fresh tests 支持部分 Claim；经济证据、投入基线、现实 OOS/Paper/Testnet 仍缺失。 |
| G3 Relative Value | `FAIL` | 原文未认真比较 strangler/greenfield/no-build；大爆炸不具相对最优证据。 |
| G4 Strategic/Economic | `UNKNOWN` | 战略匹配高，但 92/6/2、ROI、机会成本和维护容量无一手基线。 |
| G5 System/Solution | `FAIL` | 原依赖方向矛盾；R8 best-effort、Mainnet absence proof、迁移事务/回滚不充分。 |
| G6 Adversarial Survival | `FAIL` | 5 个 P0 Kill、5 个 P1 Kill 均未关闭；同一 Agent 审查限制未消除。 |
| G7 Delivery/Learning | `FAIL` | 原验收多为 PASS/ABSENT，无基线、阈值、窗口、Owner、失败动作和 Source Trace。 |

### 恢复为可开发状态的最小条件

1. 人类 Owner 隔离并处置当前 67 文件工作区变更，形成 clean immutable baseline。
2. 接受或修改 `O2` 的依赖模型、安全矩阵与 Phase P0-P8。
3. 为 P2/P3 定义 ExperimentRun/checkpoint schema、经济 Metric、阈值、窗口和 Owner。
4. 为 P6 单独定义 Testnet 写入授权、Demo host allowlist、停止条件、清理与 Runtime Owner；该分析不构成运行授权。
5. 关闭 `KILL-001` 至 `KILL-005` 后再进入正式 Development Contract。

### Analysis Quality Score（原方案）

问题真实性 4、证据充分度 3、根因清晰度 4、战略一致性 4、相对价值/经济 2、方案可行性 2、范围收敛 2、执行可交付 3、上线可验证 3、对抗/风险 2，合计 **29/50**。硬 Gate 优先，最终决策为 **PIVOT**。

## Evidence log

| ID | 观察/命令 | 结果 | 限制 |
|---|---|---|---|
| `E-001` | 用户附件 `pasted-text.txt`，2026-08-25 审阅 | 1066 行冻结方案 | 方案主张，不是现实证据 |
| `E-002` | `git rev-parse HEAD` / `git diff --shortstat` | HEAD 如上；67 files，333+/3764- | 脏改动归属用户；未改写 |
| `E-003` | pyproject、CLI、factor miner、risk rules、architecture tests 静态审阅 | 支持 C-001/C-003/C-006/C-008 | 静态代码不等于运行结果 |
| `E-004` | AST 跨包依赖扫描 | `beidou_research -> beidou_shared`，未发现 Research→Safety | 扫描 imports，不证明所有动态导入/运行行为 |
| `E-005` | `.venv/bin/python -m pytest` 9 个相关文件 | 最新复核 `127 passed in 4.35s` | targeted tests；不是全仓/coverage/runtime/economic PASS |
| `E-006` | `BASELINE_DRIFT_REPORT.md` 与 Alpha V3 final summary | 既有实现测试较强，但 G-A7 仍 NOT_VERIFIABLE | 旧运行证据；当前 HEAD/工作区已漂移 |
| `E-007` | Binance 官方 [USDⓈ-M Quick Start](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/quick-start) / [General Info](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info)，2026-08-25 查询 | Testnet 独立 Demo URL；UNKNOWN execution 需查询确认并防重复 | 官方接口事实；未连接账户、未发请求到交易端点 |
