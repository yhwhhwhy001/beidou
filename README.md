# 北斗 (Beidou) V2.0

**生产级自主加密合约量化交易系统**

三层决策时钟架构，46 个任务包的完整实现。支持 Binance USDⓈ-M 合约交易，全链路风控，策略驱动自动化交易，止盈止损保护，实盘认证阶梯。

[![Python](https://img.shields.io/badge/python-3.11+-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-446%20passed-green)]()
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
| `beidou_observability` | — | TraceContext / Incident / 告警抑制(P0永不抑制) |
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
- **P0永不抑制**: AlertSuppressor 对 CRITICAL/LOCKDOWN 级别告警不抑制
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

### Binance Demo 连接测试

```bash
# 配置 API 凭据
cp config/env.template.yaml config/env.testnet.yaml
# 编辑 env.testnet.yaml 填入 demo-fapi.binance.com 的 API Key/Secret

# 运行真实策略下单测试
python tools/strategy_live_trade.py
```

## 测试

```bash
pytest tests/ -q          # 446 项单元+架构测试
python tools/e2e_real_demo.py   # 全流程端到端 (真实API)
python tools/strategy_live_trade.py  # 策略驱动 + 止盈止损 + 真实成交
```

## 项目结构

```
beidou/
├── beidou_*/              # 18 个业务包 (81 个 .py 文件)
├── apps/                  # 3 个入口 (safety / strategy / research)
├── tests/                 # 26 个测试文件
│   ├── architecture/      # 架构约束测试 (跨层依赖禁止)
│   └── unit/              # 单元测试
├── tools/                 # 集成测试 & 工具脚本
├── config/                # 性能预算 + 环境模板
└── pyproject.toml         # hatchling + pytest + ruff + mypy
```

## 许可

Proprietary — 保留所有权利。
