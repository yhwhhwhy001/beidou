# Alpha V3 实施日志

## 基线与漂移

- 核对执行包 manifest 和基线 SHA；初始工作树 clean，当前 HEAD 仍为
  `b156881710aeeec278bfbbe75973aa8dbc83b614`。
- 发现 package-named MarketState skeleton、engine legacy estimator、kernel parity
  和缺失 benchmark/attribution 文件之间的语义漂移；记录于 `BASELINE_DRIFT_REPORT.md`。

## A0～A2

- 新增 benchmark、attribution、decision trace、canonical MarketState 和 hash lineage。
- 新增统一 AlphaForecast、Trend、Relative Strength、Breakout、Residual Momentum。
- 将 half-life/robust z-score 收敛到唯一 MR math；legacy fixed module 仅 re-export。

## A3～A4

- 新增 checksum-bound calibration registry 和 deterministic runtime inference。
- SignalFuser/TypedGraph 统一使用 N-entry fusion，记录 contributions/conflict/order-invariant hash。
- 新增 ExposureGovernor、V3 constraints 和 active portfolio optimizer；缺 venue/cost/liquidity
  facts 时 fail closed，未保留 V3 固定 1%/5 USDT/0.001 fallback。

## A5～A7

- 完成 full PnL attribution contract、完整性 gate 和只读 shadow pipeline/runtime probe。
- 完成 challenger fixture/integration contract；真实 sealed OOS/Paper window 仍缺失，A7 保持 FAIL。

## 质量与覆盖补齐

- 增加 `test_alpha_v3_core_coverage_complete.py` 及边界/负向测试；清除 MR、registry、fusion、
  typed graph、optimizer、constraints 和 state 的未覆盖分支。
- 官方 write-capability registry 重建；补齐安全扫描所需的逐行审查注释；未降低阈值。
- CI 增加 22 核心模块 100% line/branch gate，并将全局 gate 对齐执行包的 `85%` 要求。
- 补齐数据、交易所适配器、WebSocket、outbox、supervisor、monitoring、runtime health、
  certification 和 write-registry 边界测试；修复 slots OHLCV revision、Paper shadow fail-closed
  分支以及 mining runner 非有限统计/RSI 边界。
- 最终：`3532 passed`；V3 3497/3497 statements、1070/1070 branches；全局 coverage
  `85.02117828263381%`；独立 registry oracle `PASS`。

## 重启验证

- 补齐隔离 worktree 的 `.beidou/` 和 `evidence/bootstrap/` 运行目录。
- 先前隔离 Paper 运行已完成多轮验证；最终合并提交 `1a14e0908bc2376ef4ea585fb3f4f43a366e8d74`
  又在 `main` 工作树以 19090 端口启动验证。最终观测为 `/health=200 HEALTHY`、
  supervisor `DEGRADED`、`/ready=503`、`can_write=false`，保护/对账 UNKNOWN 时保持
  `NO_NEW_RISK`。
- 最终实例安全停止，端口关闭、`phase=STOPPED`，未取消或修改 unowned orders；原 PID 98924
  untouched，LaunchAgent 未启动。

## 最终边界

代码、测试、静态门禁、覆盖率和受控只读重启均已完成；A7 的真实经济证据、Paper promotion、
Mainnet/live activation 仍明确不通过，不用 fixture 或运行健康事实替代。

## Git handoff

- 代码/测试/CI/registry 主提交：`8bffe2014205bb7dfae1a5274d88c43e365fcd6b`，消息为
  `feat(alpha-v3): close coverage and execution gates`；最终 registry oracle 修正提交为
  `1a14e0908bc2376ef4ea585fb3f4f43a366e8d74`。
- 文档证据随后回填该代码 SHA；运行时 JSONL 告警文件继续保留在 worktree、未进入提交。
- 推送和 main fast-forward 合并已在文档证据提交后执行，并以远端 ref 复核为准。
