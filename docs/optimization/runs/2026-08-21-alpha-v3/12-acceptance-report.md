# Alpha V3 最终验收报告

验收结论：`CONDITIONAL_ACCEPTANCE_FOR_OFFLINE_SHADOW / NO-GO_FOR_A7_AND_UNATTENDED_TESTNET`

本轮已完成执行包要求的代码审查、合同、测试、静态门禁、覆盖率补齐、运行时安全修复和
受控隔离 Paper/Shadow 重启验证。全局 coverage 已达到项目配置的 `fail_under=85`，V3 精确
作用域 line/branch 均为 100%；G5/G-A7 的独立真实经济证据仍不可验证。因此不能把本轮
结果表述为 Paper promotion、生产就绪或 Mainnet/live 授权。

## 基线与范围

- 执行包：`/Users/maguannan/Downloads/BEIDOU_ALPHA_V3_EXECUTION_PACKAGE.zip`
- 基线/代码验证 HEAD：`8bffe2014205bb7dfae1a5274d88c43e365fcd6b`
- 工作树：`/Users/maguannan/beidou-worktrees/alpha-v3-20260821`
- 分支：`codex/alpha-v3-20260821`
- 主服务：`/Users/maguannan/beidou:9090`，由 `gui/501/com.beidou.autopilot` 管理。
- 已在用户授权下停止旧实例、部署并启动新提交；旧 PID `98924` 已退出，新的历史观测
  PID 为 wrapper `11005`、Python `11009`。验证结束后因运行时重新进入安全事件状态而安全停机，
  当前没有监听 `9090` 的服务。

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
| G5 | `FAIL/NOT_VERIFIABLE` | 缺少独立授权的真实 Testnet create/cancel/partial-fill/race 证据、完整 readback/cleanup 和可审计 sealed 运行包；离线场景 PASS 不替代真实证据 |
| G-A7 | `FAIL/NOT_VERIFIABLE` | 缺 sealed real OOS、同成本 walk-forward/regime 经济窗口与完整 Paper shadow 窗口 |
| GLOBAL-CI | `PASS` | 全量 `3532 passed`，coverage `85.020340...%`，达到项目配置 `fail_under=85`；静态/包/质量/安全扫描通过 |
| V3 coverage | `PASS` | 执行包精确 22 模块 `3497 statements / 1070 branches`，`0 missed / 0 partial`，line/branch 均 100% |
| RESTART | `PASS_WITH_FAIL_CLOSED_RUNTIME / NO-GO` | 最终合并提交上的 Paper 进程启动并提供 HTTP 200 健康端点，但状态为 `DEGRADED`；active safety incidents/保护缺失正确阻断 ready/恢复，验证结束已停机 |

## 已补齐内容

- canonical MarketState/Benchmark、Trend/RS/Breakout/Residual/MR、统一 AlphaForecast。
- checksum-bound calibration、N-entry fusion、贡献/冲突/reliability/diversity trace。
- ExposureGovernor、active optimizer、venue/cost/liquidity/capacity fail-closed。
- attribution completeness、runtime hash lineage、只读 V3 shadow probe。
- V3 核心实现、registry、扫描、格式、mypy、安全审计和 active-incident 恢复阻断逻辑已补齐；
  V3 100% line/branch 仅限执行包列出的精确 22 模块，不扩大为全仓 100%。
- 环境运行目录 `.beidou/` 与 `evidence/bootstrap/`；不伪造 signed policy、OOS 或账户保护事实。

## 运行状态与安全边界

最终合并提交的隔离 Paper 重启期间 `/health` 为 HTTP 200、`status=DEGRADED`，
`/ready` 为 503、`trading_ready=false`，`can_write=false`，pending/unacked outbox intent
为 0。保护归属/reconciliation UNKNOWN、signed policy 缺失、active critical incidents
和 protection coverage 缺失均保持阻断；保护放置、清理和未知订单处理均跳过。验证结束确认
Paper 进程和 `19090` 端口关闭；未启动历史 Testnet LaunchAgent，未执行人工下单、撤单或未知
订单清理，也未触碰 Mainnet/live。

## 不可替代的剩余条件

1. 提供执行包要求的真实 Testnet G5 证据包：受控授权、create/query/cancel、partial-fill、
   race/unknown、持久化 journal/readback、cleanup 和环境一致性，且不把离线 fixture 当作实测。
2. 提供同数据、同成本、point-in-time sealed OOS 与有效 Paper shadow 窗口，并由独立流程审查
   signed policy、保护归属、reconciliation、alert delivery 和 lifecycle 事实。
3. 只有在 G5、G-A7、归因、回滚和运行授权全部满足后，才可另行评估 promotion；本轮不授权
   unattended Testnet 或 Mainnet/live。
