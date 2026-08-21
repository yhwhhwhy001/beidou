# Alpha V3 最终验收报告

验收结论：`CONDITIONAL_ACCEPTANCE_FOR_OFFLINE_SHADOW / NO-GO_FOR_A7_AND_PROMOTION`

当前工作树已完成执行包要求的代码、合同、测试、静态门禁、V3 100% 覆盖和受控只读重启验证；
真实经济验收和 Paper promotion 因 A7 证据缺失仍必须保持阻断。

## 基线与范围

- 执行包：`/Users/maguannan/Downloads/BEIDOU_ALPHA_V3_EXECUTION_PACKAGE.zip`
- 基线/HEAD：`b156881710aeeec278bfbbe75973aa8dbc83b614`
- 工作树：`/Users/maguannan/beidou-worktrees/alpha-v3-20260821`
- 分支：`codex/alpha-v3-20260821`
- 原始 Testnet PID 98924（`/Users/maguannan/beidou:9090`）仅作只读边界确认，未修改、未重启。
- 修改 worktree 在 `paper:19090` 两次启动/停止，独立证据见 `14-production-validation.md`。

## Gate 结果

| Gate | 结果 | 解释 |
|---|---|---|
| G-A0 | PASS | baseline/benchmark/trace 离线合同 |
| G-A1 | PASS | canonical state、DQ、regime、kernel hash 离线验证 |
| G-A2 | PASS | 五类 Alpha 与 MR consolidation 离线验证 |
| G-A3 | PASS | calibration/fusion/reliability/conflict 合同 |
| G-A4 | PASS | exposure/active optimizer/math/scenario/property |
| G-A5 | PASS | 完整归因合同和 completeness gate；无真实 fill |
| G-A6 | PASS | 只读 shadow pipeline/probe/lineage |
| G-A7 | FAIL | 缺 sealed real OOS 与完整 Paper shadow 窗口 |
| GLOBAL-CI | PASS | 3400 tests、80.59% 全局 coverage、所有静态/安全/包门禁 |
| V3 coverage | PASS | 3497 statements/1070 branches，line/branch 100% |
| RESTART | PASS_WITH_FAIL_CLOSED_RUNTIME | 启动健康、ready 503、停止可回滚 |

## 已补齐内容

- canonical MarketState/Benchmark、Trend/RS/Breakout/Residual/MR、统一 AlphaForecast。
- checksum-bound calibration、N-entry fusion、贡献/冲突/reliability/diversity trace。
- ExposureGovernor、active optimizer、venue/cost/liquidity/capacity fail-closed。
- attribution completeness、runtime hash lineage、只读 V3 shadow probe。
- V3 22 模块的 100% line/branch CI gate；registry、扫描、格式、mypy、安全审计闭合。
- 环境运行目录 `.beidou/` 与 `evidence/bootstrap/`；不伪造 signed policy、OOS 或账户保护事实。

## 运行状态与安全边界

Paper 重启期间 `/health` 为 HEALTHY，市场数据与算法探针通过，write interlock 为 PASS；
由于观察到 `SIGNED_POLICY_UNAVAILABLE`、保护归属 UNKNOWN 和 reconciliation UNKNOWN，
`/status` 为 `DEGRADED/NO_NEW_RISK`，`/ready` 为 503。这是预期的 fail-closed 结果，
不是生产就绪或收益证据。停止时端口 19090 关闭并持久化 `phase=STOPPED`。

## 不可替代的剩余条件

1. 提供同数据、同成本、point-in-time sealed OOS 与有效 Paper shadow 窗口。
2. 由独立流程审查真实 signed policy、保护归属和 reconciliation 事实。
3. 只有在 A7、归因、回滚和运行授权全部满足后，才可另行评估 promotion；本轮不授权 Mainnet/live。
