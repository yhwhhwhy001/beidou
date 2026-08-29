# 北斗 (Beidou) V2.0

**加密合约量化交易系统 — PIVOT 重构阶段**

三层决策时钟架构。Testnet 验证阶段通过独立的 closed-bar verifier 复用策略、交易池、自适应 sizing 和 Binance Adapter。

**当前状态: PIVOT — Testnet HOLD（历史 demo 执行证据已保留；2026-08-29 无限重启事故已隔离，当前代码/CI 正在重新验收） / Alpha NOT_EVALUATED（E0–E6 门禁已实现未评估） / Mainnet PROHIBITED**

> 完整证据见 `docs/execution/beidou-testnet-alpha-first-v4/12-acceptance-report.md`。
> 当前专用 Testnet 账户已只读对账为空仓、无挂单、0 unresolved；这不替代当前 HEAD 全量验证和 GitHub CI。`Testnet READY / Completed / Alpha VERIFIED` 均未声明。

[![Python](https://img.shields.io/badge/python-3.12+-blue)](https://python.org)
[![License](https://img.shields.io/badge/license-Proprietary-red)]()

---

## 架构概览

```
┌─────────────────────────────────────────────────────┐
│  REALTIME  (实时安全/执行)                            │
│  MarketData → PreRisk → RiskEngine → Approval        │
│  → Intent → Outbox → Executor → OrderState → Ledger  │
│  → Reconciliation → Protection(SL/TP)                 │
├─────────────────────────────────────────────────────┤
│  NEARLINE  (近线策略)                                 │
│  MarketState → CostModel → AlphaSDK → SignalFusion    │
│  → PortfolioOpt → StrategyRisk → DriftDetect          │
├─────────────────────────────────────────────────────┤
│  OFFLINE   (离线研究)                                 │
│  Datasets → FactorResearch → ModelRegistry →          │
│  Backtest → ReplayValidator → Certification           │
└─────────────────────────────────────────────────────┘
```

## 模块矩阵

| 包 | 时钟域 | 功能 |
|------|-------------|------|
| `beidou_shared` | — | 共享类型、EventEnvelope、DomainError、ResultStatus |
| `beidou_safety` | REALTIME | PreRisk / R0-R10 / RiskApproval / IntentOutbox / 订单状态机 / 双重记账 / 对账 / 止盈止损 |
| `beidou_strategy` | NEARLINE | 三维市场状态 / 成本模型 / AlphaGraph / 信号融合 / 组合优化 / 策略风险管理 |
| `beidou_research` | OFFLINE | 因子研究(IC/ICIR/VIF) / 模型注册(Champion-Challenger) / 反作弊回测 |
| `beidou_policy` | — | Policy Registry / 签名验证 / 原子激活 / Fail-Closed |
| `beidou_security` | — | 服务身份 / 权限矩阵 / 脱敏 / 密钥轮换 |
| `beidou_observability` | — | TraceContext / 深度监控检查 / 本地证据持久化 |
| `beidou_exchange` | REALTIME | ExchangeAdapter / CapabilityMatrix / 账户发现 / BinanceUSDM |
| `beidou_lifecycle` | — | 10态模块FSM / 能力协商 / 健康探针 |
| `beidou_autonomy` | — | MAPE-K / 故障指纹 / 自愈 / Checkpoint |
| `beidou_infra` | — | 高可用 / 事实源权威 / 灾备 / Startup-Liveness-Readiness探针 |
| `beidou_delivery` | — | CI/CD / 制品签名 / 发布晋级 / 回滚 |
| `beidou_control` | — | P0控制面 / NO_NEW_RISK / EXIT_ONLY / EMERGENCY_FLATTEN / LOCK |
| `beidou_chaos` | — | KillRegister(10场景) / 组合故障注入 |
| `beidou_production` | — | L0-L5实盘阶梯 / Gate独立发证 |
| `beidou_certification` | — | G5 Testnet → G8 30天无人值守认证框架 |
| `beidou_reporting` | — | 日报/周报/事故报告 / 证据导出 / NOT_VERIFIABLE语义 |
| `beidou_data` | — | 实时行情 / 订单簿 / K线 / 质量门禁 / 数据集 / 交易池 / 特征仓 |

## 核心不变量

- **风险无旁路**: 所有风险增加订单必须通过 PreRisk → RiskEngine → Approval → Executor 完整链路
- **Fail-Closed**: UNKNOWN/ERROR 输入必须阻止风险增加操作，安全退出路径按独立策略执行
- **账户查询失败 ≠ 空仓**: 绝不把查询失败转换为零余额/空仓位
- **Post-Risk 只能监控**: PostRiskMonitor.cannot_approve() 永远返回 True
- **资金费不是机械加仓信号**: funding_rate_aware_position_size 只能减仓不能加仓
- **订单幂等**: IntentOutbox 通过 SHA256 哈希 + idempotency_key 防止重复
- **双主防护**: LeaseManager + FencingProtection 通过 generation 门控旧实例
- **不可变账本**: ImmutableLedger 所有分录只追加不可修改
- **重启≠恢复**: 模块重启后必须先 VALIDATING 才能 ACTIVE

## 止盈止损保护

| 止损类型 | 止盈类型 |
|---------|---------|
| FIXED_PERCENT — 固定百分比 | FIXED_RR — 固定风险回报比 |
| ATR_BASED — ATR波动率 | MULTI_TARGET — 多目标分批 |
| VOLATILITY_BASED — 历史波动率 | TRAILING_TAKE_PROFIT — 移动止盈 |
| TRAILING — 移动止损(跟踪价格极值) | |
| SWING_STRUCTURE — 支撑/阻力结构 | |

策略风险管理: 回撤监控 / 连续亏损熔断 / 单日亏损限制 / Sharpe衰减检测 / 风险预算仓位计算

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -q
```

### Binance Testnet Verification

默认流程只读，不会发送风险增加请求。先运行本地验证：

```bash
python -m apps.testnet_verify --help
pytest tests/unit/test_testnet_binance_contracts.py tests/integration/test_testnet_verification_runtime.py -q
```

只有在使用专用 Testnet/Demo 账户、确认凭据由外部安全环境注入，并获得本地 Testnet 写入授权后，才可显式开启有界验证单：

```bash
export BEIDOU_TESTNET_REST_URL=https://demo-fapi.binance.com
export BEIDOU_TESTNET_API_KEY='<provided-out-of-band>'
export BEIDOU_TESTNET_API_SECRET='<provided-out-of-band>'
export BEIDOU_TESTNET_ACCOUNT_ID='<dedicated-testnet-account>'
python -m apps.testnet_verify --once --confirm-testnet --close-after-verify \
  --max-notional 25 --max-leverage 3 --max-instruments 1
```

`apps.testnet_verify` 是本阶段唯一 Testnet 验证入口。它强制 HTTPS/host allowlist、Mainnet hard deny、绝对 notional/leverage/账户总暴露上限、stable clientOrderId、query-before-retry、ACK/持仓对账和可恢复 DecisionTrace。不要把凭据写入仓库、命令历史或聊天；`--confirm-testnet` 只代表本地 Testnet 写入确认，不代表生产授权。

需要停止新增风险时，可在不启动 runtime 的情况下持久化 kill switch；该文件由最终写权限边界实时检查，reduce-only 收敛路径仍可使用：

```bash
python -m apps.testnet_verify --engage-kill-switch
```

只有在明确排除 UNKNOWN、确认专用 Testnet 账户已平仓并完成独立授权后，才可人工删除配置的 kill-switch 文件。CLI 不提供自动解除命令。

Testnet VERIFIED 只证明决策与执行事实链闭合；它不证明策略盈利，也不等于 E0–E6 Economic Truth 或 `ALPHA VERIFIED`。真实 Testnet 证据缺失时状态必须保持 HOLD/NOT_VERIFIABLE。

## 测试

```bash
pytest tests/ -q
python -m apps.testnet_verify --help
```

仓库中的旧 safety/certification/production 模块与历史工具保留用于审计和显式的未来流程，但不属于本阶段 Testnet verifier 快速开始路径。

## 项目结构

```
beidou/
├── beidou_*/              # 18 个业务包 (81 个 .py 文件)
├── apps/                  # 包含唯一 Testnet 验证入口 apps.testnet_verify
├── tests/                 # 26 个测试文件
│   ├── architecture/      # 架构约束测试 (跨层依赖禁止)
│   └── unit/              # 单元测试
├── tools/                 # 集成测试 & 工具脚本
├── config/                # 性能预算 + 环境模板
└── pyproject.toml         # hatchling + pytest + ruff + mypy
```

## 许可

Proprietary — 保留所有权利。
