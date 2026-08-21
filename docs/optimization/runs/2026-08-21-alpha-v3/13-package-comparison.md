# 执行包逐项对比与遗漏审查

对照文件：`03_FILE_CHANGE_MATRIX.md`、`04_CONTRACTS_AND_INTERFACES.md`、
`06_TEST_AND_ACCEPTANCE.md`、`07_MIGRATION_AND_ROLLBACK.md`、`machine/GATES.yaml`、
`machine/TASK_BACKLOG.yaml`。

## 文件矩阵

| 执行包要求 | 当前文件/内容 | 结果 |
|---|---|---|
| MarketState canonical | `beidou_strategy/state/market_state.py` | COMPLETE |
| Benchmark | `beidou_strategy/state/benchmark.py` | COMPLETE |
| Alpha contracts/Trend/Breakout/RS/Residual/Forecast/MR | `beidou_strategy/alpha/` | COMPLETE |
| MR compatibility boundary | `beidou_strategy/components/mean_reversion_fixed.py` | COMPLETE；仅 re-export |
| Fusion/TypedGraph/Registry/Extensions | `beidou_strategy/alpha/` | COMPLETE |
| Typed kernel/runtime trace | `beidou_strategy/kernel/typed_kernel.py`, `kernel_parity.py`, `beidou_launcher/runtime.py` | COMPLETE；只读 |
| ExposureGovernor/constraints/active optimizer | `beidou_strategy/portfolio/` | COMPLETE |
| simple optimizer demotion | `portfolio/simple_optimizer.py` | COMPLETE；baseline/test-only |
| Factor/attribution/reporting | `beidou_research/factors/factor.py`, `beidou_reporting/` | COMPLETE |
| Challenger/economic gate | `beidou_research/backtest/alpha_v3_challenger.py` | IMPLEMENTED；A7 BLOCKED |
| CI/coverage | `.github/workflows/ci.yml` | COMPLETE；V3 100% line/branch |
| Registry/security inventory | `config/write-capability-registry.json` | COMPLETE；oracle PASS |
| Environment runtime dirs | `.beidou/`, `evidence/bootstrap/` | SUPPLEMENTED and preflight PASS |

## Gate/验收对照

| 执行包门禁 | 结果 | 证据 |
|---|---|---|
| A0 baseline/benchmark/attribution/trace | PASS | `evidence/G-A0.json` |
| A1 canonical state/DQ/hash | PASS | `evidence/G-A1.json` |
| A2 formal alpha/MR convergence | PASS | `evidence/G-A2.json` |
| A3 calibration/fusion/reliability | PASS | `evidence/G-A3.json` |
| A4 exposure/optimizer/constraints | PASS | `evidence/G-A4.json` |
| A5 full attribution | PASS（合同） | `evidence/G-A5.json` |
| A6 shadow/runtime read-only trace | PASS（只读） | `evidence/G-A6.json` |
| A7 real OOS/Paper economic acceptance | FAIL/NOT_VERIFIABLE | `evidence/G-A7.json` |
| Global CI/static/security/package | PASS | `evidence/GLOBAL-CI.json` |
| User override: V3 100% line/branch | PASS | `evidence/V3-COVERAGE.json` |
| Authorized restart/rollback | PASS_WITH_FAIL_CLOSED_RUNTIME | `evidence/RESTART.json` |

## Non-negotiable review

- UNKNOWN remains fail-closed；未将缺失成本、venue rules、账户事实、保护归属、对账或 signed
  policy 转成零值、默认规则或伪造证据。
- 未增加绕过 Risk/Approval/Execution/Protection 的调用；V3 runtime 仅是 read-only shadow。
- 未把 alpha improvement 转换为 leverage increase；optimizer 使用显式 exposure target 和风险约束。
- 未降低 CI 阈值：既有全局 78% gate 保留，另增 V3 核心 100% line/branch gate。
- 未把 fixture/OOS contract/health 结果解释为收益、生产就绪、Paper promotion 或 Mainnet 授权。
- `reference/contract_skeletons.py` 未被直接复制为生产实现。

## 结论

执行包要求的代码、接口、测试、静态门禁、注册表、覆盖率、运行目录和受控重启条件已逐项
闭合；唯一仍不可由本地代码补齐的执行包事实是 A7 所需的真实 sealed OOS/Paper 经济窗口，
因此该门禁必须保持 FAIL，不能用虚构数据“补齐”。
