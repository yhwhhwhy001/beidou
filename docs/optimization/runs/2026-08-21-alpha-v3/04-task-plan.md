# Alpha V3 补齐任务计划与完成记录

本计划严格按执行包的阶段依赖推进；所有完成项都区分离线代码/测试证据与运行、
经济和晋级证据。

## 已完成：A0～A2

1. 锁定基线、核对 manifest，记录并解决 MarketState/legacy runtime 语义漂移。
2. 增加 benchmark、完整 attribution skeleton 和 V2 additive read-only trace。
3. 实现 canonical MarketState、确定性 regime、质量 fail-closed、policy/hash lineage。
4. 增加 Trend、Relative Strength、Breakout、Residual Momentum 和统一 MR forecast。
5. 收敛 half-life/robust z-score 到 `alpha/mean_reversion_math.py`；旧模块仅兼容导出。

## 已完成：A3～A4

1. 增加 checksum-bound CalibrationArtifact、published registry 和 deterministic inference。
2. 让 SignalFuser/TypedGraph 使用同一 N-entry EnsembleFuser，记录贡献、冲突、顺序不变性。
3. 增加 regime reliability/diversity/correlation penalty。
4. 增加 ExposureGovernor、V3 target validator 和 active expected-return/covariance/turnover/
   cost/liquidity/capacity/venue optimizer。
5. 明确 `simple_optimizer.py` 为 baseline/test-only；缺事实直接拒绝，不使用 magic fallback。
6. 增加 bull/range/crisis/conflict/cost-shock/missing-metadata 场景与 property tests。

## 已完成：A5～A6

1. AttributionRecord 覆盖 beta、active alpha、timing、exposure、decision/veto、constraint、
   execution cost、funding、protection 和 unexplained residual，并要求 lineage/hash/correlation。
2. 只读 shadow engine 完成 benchmark→state→five forecasts→calibration→ensemble→exposure→portfolio。
3. runtime probe 传播 V3 health/trace hashes，不创建 OrderIntent，不绕过既有安全链。

## 已完成：全局门禁与覆盖

- 3400 个测试全部通过。
- V3 核心 22 模块 3497 statements、1070 branches，line/branch 均 100%。
- compileall、Ruff format/lint、mypy、test-quality、hardcoded、forbidden、package、registry
  oracle、Bandit、pip-audit、pip check 全部通过；CI 的全局 78% coverage 命令实测 80.59%。
- 官方 write-capability registry 已按当前源码重建，未降低任何阈值或删除安全门禁。

## 已完成：受控重启验证

- 补齐 `.beidou/` 与 `evidence/bootstrap/` 后，隔离 worktree Paper preflight PASS。
- 在独立 19090 端口启动两次，健康/算法探针/写互锁通过；`/ready` 因真实保护与对账 UNKNOWN
  保持 503，符合 fail-closed 要求。
- 两次安全停止均关闭端口并写入 `phase=STOPPED`；未触碰原 Testnet PID 98924。

## 未完成且必须保持阻断：A7

challenger、walk-forward、regime、active return/IR/DD/capture/beta/gross/net 和 leverage-only
rejection 已实现并有 fixture 测试；但没有可审计 sealed real OOS、同成本事实和完成的 Paper shadow
窗口，因此 `G-A7=FAIL/NOT_VERIFIABLE`，不得作经济收益、Paper promotion 或生产就绪结论。
