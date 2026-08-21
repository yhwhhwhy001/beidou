# Alpha V3 最终验收报告

验收结论：`CONDITIONAL_ACCEPTANCE_FOR_OFFLINE_SHADOW / NO-GO_FOR_A7_AND_UNATTENDED_TESTNET`

本轮已完成执行包要求的代码审查、合同、测试、静态门禁、运行时安全修复和用户授权的
Main Testnet 重启验证。验证同时确认了两个不可忽略的阻断：全仓 coverage 未达到项目
配置的 `fail_under=85`，且 G5/G-A7 的独立经济证据不可验证。因此不能把本轮结果表述为
“全部门禁通过”、Paper promotion、生产就绪或 Mainnet/live 授权。

## 基线与范围

- 执行包：`/Users/maguannan/Downloads/BEIDOU_ALPHA_V3_EXECUTION_PACKAGE.zip`
- 基线/HEAD：`77fbbc3cdf6ca8068063343c053749d9e156be61`
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
| G-A7 | FAIL | 缺 sealed real OOS 与完整 Paper shadow 窗口 |
| GLOBAL-CI | `FAIL_COVERAGE` | 全量 `3403 passed`，但 coverage `80.57%`，低于项目配置 `fail_under=85`；静态/包/质量扫描本身通过 |
| V3 coverage | `PARTIAL_EVIDENCE` | 执行包列出的 V3 核心实现文件在全量 line report 中达到 100% line；包含 compatibility/额外模块的 alpha+portfolio 分支覆盖运行合计为 86.44%，不能宣称整个 V3 或全仓 line/branch 100% |
| RESTART | `PASS_WITH_FAIL_CLOSED_RUNTIME / NO-GO` | 新提交启动并提供健康接口；active safety incidents 期间正确阻断 ready/恢复；后续测试网运行出现一次自动放置请求，随后进入 `NO_NEW_RISK`，验证结束已停机 |

## 已补齐内容

- canonical MarketState/Benchmark、Trend/RS/Breakout/Residual/MR、统一 AlphaForecast。
- checksum-bound calibration、N-entry fusion、贡献/冲突/reliability/diversity trace。
- ExposureGovernor、active optimizer、venue/cost/liquidity/capacity fail-closed。
- attribution completeness、runtime hash lineage、只读 V3 shadow probe。
- V3 核心实现、registry、扫描、格式、mypy、安全审计和 active-incident 恢复阻断逻辑已补齐；
  “V3 100% line/branch”仍不是本轮可据证据宣称的结果。
- 环境运行目录 `.beidou/` 与 `evidence/bootstrap/`；不伪造 signed policy、OOS 或账户保护事实。

## 运行状态与安全边界

Main Testnet 重启期间 `/health` 曾为 HTTP 200/`HEALTHY`，但 `/ready` 在 active critical
incident 时为 503、`trading_ready=false`，且新代码没有自动恢复风险权限。事件暂时清除后
`/ready` 曾短暂恢复 200；随后测试网运行自动产生过 1 条 `OrderIntent` 并形成 1 次放置请求。
在新的 active incidents 出现后，控制面切换为 `NO_NEW_RISK`，后续观测到的进入请求被拒绝，
最终 `/ready` 再次为 503。验证结束已停止 LaunchAgent 并确认进程/端口关闭；未执行人工下单、
撤单或未知订单清理，也未触碰 Mainnet/live。

## 不可替代的剩余条件

1. 修复并重新验证全仓 coverage，使项目配置的 `fail_under=85` 通过；同时按执行包要求补足
   V3 核心 branch gate 的独立可复核报告，不能用局部 line 结果代替。
2. 提供同数据、同成本、point-in-time sealed OOS 与有效 Paper shadow 窗口，并由独立流程审查
   signed policy、保护归属、reconciliation、alert delivery 和 lifecycle 事实。
3. 只有在 G5、G-A7、归因、回滚和运行授权全部满足后，才可另行评估 promotion；本轮不授权
   unattended Testnet 或 Mainnet/live。
