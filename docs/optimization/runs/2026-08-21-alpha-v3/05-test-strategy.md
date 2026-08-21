# Alpha V3 测试策略与最终结果

## 测试分层

- **合同/负向测试**：验证有限值、schema/hash、成本/venue/account metadata 缺失时显式
  `UNKNOWN`/`NOT_VERIFIABLE`/reject，不转成零值或 magic default。
- **状态与 Alpha**：验证 canonical MarketState、五类 regime、Trend、RS、Breakout、Residual、
  Mean Reversion、calibration checksum 和未来字段不泄漏。
- **融合与组合**：验证全 entry 稳定排序、贡献/冲突、reliability/diversity、target beta/gross/
  net/vol、covariance、turnover、fee/slippage/funding、liquidity/capacity 和 stress 单调性。
- **归因与运行**：验证 attribution completeness、lineage/hash、只读 shadow probe、duplicate-path
  boundary 和既有 Risk/Approval/Execution/Protection 不被绕过。
- **经济门禁**：fixture 只能验证代码合同；真实 sealed OOS/Paper shadow 缺失时 A7 必须 FAIL。

## 覆盖范围

V3 专项 CI 明确列出 22 个核心模块，使用 `--cov-branch --cov-fail-under=100`。最终结果：

| 指标 | 结果 |
|---|---:|
| statements | 3497 |
| missed statements | 0 |
| branches | 1070 |
| partial branches | 0 |
| line coverage | 100% |
| branch coverage | 100% |
| full tests | 3532 passed |

完整模块清单和机器可读结果见 `evidence/V3-COVERAGE.json`。

## 最终门禁命令结果

- `python -m compileall ...`：PASS。
- `ruff format --check ...`、`ruff check ...`：PASS，549 files already formatted。
- 全仓 mypy：PASS。
- `scan_test_quality.py`、`scan_hardcoded.py`、`check_forbidden_patterns.py`：PASS。
- `validate_package.py`、`verify_write_registry.py --root .`：PASS。
- Bandit：0 issues；pip-audit：No known vulnerabilities（本地 editable `ueds` 无 PyPI 审计源）。
- CI 全局覆盖命令：3532 passed，85.020340...%，满足项目 `fail_under=85` gate。

## 运行验证

Paper worktree 只读重启验证见 `14-production-validation.md`：健康接口和算法探针通过，
write interlock 通过；真实保护归属/对账 UNKNOWN 使系统保持 `DEGRADED/NO_NEW_RISK`，
`/ready` 为 503。该证据不升级为交易或 Paper promotion 证据。
