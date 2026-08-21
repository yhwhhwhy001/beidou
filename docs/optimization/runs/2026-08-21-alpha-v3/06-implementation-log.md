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
- CI 增加 22 核心模块 100% line/branch gate，保留原有全局 78% gate。
- 最终：3400 passed；V3 3497/3497 statements、1070/1070 branches；全局 coverage 80.59%。

## 重启验证

- 补齐隔离 worktree 的 `.beidou/` 和 `evidence/bootstrap/` 运行目录。
- Paper mode 在 19090 端口启动两轮；`/health=HEALTHY`、liveness/market-data/algorithm probe/
  write interlock PASS，`/ready=503`，因为真实保护/对账 UNKNOWN 而保持 NO_NEW_RISK。
- 两轮均安全停止，端口关闭、`phase=STOPPED`，未取消或修改 unowned orders；原 PID 98924 untouched。

## 最终边界

代码、测试、静态门禁、覆盖率和受控只读重启均已完成；A7 的真实经济证据、Paper promotion、
Mainnet/live activation 仍明确不通过，不用 fixture 或运行健康事实替代。
