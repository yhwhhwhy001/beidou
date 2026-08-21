# Alpha V3 需求追踪矩阵

审计日期：2026-08-21（Asia/Shanghai）
执行包：`BEIDOU_ALPHA_V3_EXECUTION_PACKAGE`
基线：`b156881710aeeec278bfbbe75973aa8dbc83b614`
工作树：`codex/alpha-v3-20260821`

## 状态语义

- `COMPLETE`：实现、测试和离线证据在当前 worktree 闭合；不表示运行激活。
- `BLOCKED`：需要真实数据、Paper 窗口、外部授权或 UNKNOWN 事实；不得伪造通过。
- 旧 V2 模块只作为显式 compatibility/baseline 路径保留。

## 任务到实现、测试、证据

| ID | 要求 | 实现 | 测试/证据 | 状态 |
|---|---|---|---|---|
| A0-001 | 基线、manifest、漂移分类 | `BASELINE_DRIFT_REPORT.md` | `G-A0` | COMPLETE |
| A0-002 | benchmark contract、版本/hash、非 BTC-only | `state/benchmark.py` | `test_benchmark.py` | COMPLETE |
| A0-003 | benchmark/portfolio/beta/gross/net/拖累/残差归因 | `beidou_reporting/pnl_attribution.py` | attribution unit/integration、`G-A5` | COMPLETE（离线） |
| A0-004 | V2 additive read-only decision trace | `beidou_launcher/runtime.py` | runtime probe、`G-A0` | COMPLETE（只读） |
| A1-001～004 | canonical state、五类 regime、DQ fail-closed、kernel hash | `state/market_state.py`, `typed_kernel.py`, `kernel_parity.py` | unit/property/architecture、`G-A1` | COMPLETE |
| A2-001～006 | AlphaForecast、Trend、RS、Breakout、Residual、唯一 MR math | `beidou_strategy/alpha/*` | unit/property/integration、`G-A2` | COMPLETE |
| A3-001～003 | calibration checksum、published runtime、N-entry fusion、贡献/冲突/reliability | `forecast.py`, `model_registry.py`, `signal_fusion.py`, `typed_graph.py` | unit/integration、`G-A3` | COMPLETE |
| A4-001～004 | ExposureGovernor、target validation、active optimizer、成本/流动性/容量/规则 | `portfolio/exposure_governor.py`, `optimizer.py`, `constraints.py` | unit/property/scenario、`G-A4` | COMPLETE |
| A5-001 | 完整 beta/alpha/timing/exposure/decision/constraint/execution/protection/residual | `pnl_attribution.py`, reporting engine | unit/integration、`G-A5` | COMPLETE（无真实 fill） |
| A6-001 | benchmark→state→forecast→target→PnL 只读 trace | `alpha/pipeline.py`, runtime probe | unit/architecture、`G-A6` | COMPLETE（只读 shadow） |
| A7-001 | 同数据/同成本 OOS、walk-forward、regime、Paper shadow | `alpha_v3_challenger.py` | fixture/integration、`G-A7` | BLOCKED |

## Gate 状态

- `G-A0`～`G-A6`：PASS，范围限于合同、静态链路、fixture/property/integration 和
  只读 shadow；没有经济收益或生产晋级含义。
- `G-A7`：FAIL/NOT_VERIFIABLE。当前没有 sealed real same-data/same-cost OOS 与完整
  Paper shadow 窗口；不能把 fixture PASS 解释为优于 V2 或允许 promotion。
- `GLOBAL-CI`：PASS。全仓 `3400 passed`；CI 原全局 coverage 命令 `80.59%`，高于
  保留的 78% gate；V3 专项 22 模块 `3497 statements / 1070 branches` 为 100%/100%。
- `RESTART`：PASS_WITH_FAIL_CLOSED_RUNTIME。隔离 Paper 进程启动两轮、健康接口通过、
  `/ready=503`、安全停止后端口关闭；原 PID 98924 未触碰。

## 环境与兼容边界

- `/Users/maguannan/ueds/.venv/bin/python` 3.14.6；lock 依赖、`pip check`、mypy、
  Ruff、Bandit、pip-audit 均已复核。
- 缺失运行目录 `.beidou/`、`evidence/bootstrap/` 已创建并验证可写。
- Paper 运行观察到 `SIGNED_POLICY_UNAVAILABLE`、保护归属 UNKNOWN 和 reconciliation
  UNKNOWN；这些事实触发 `DEGRADED/NO_NEW_RISK`，没有生成伪政策或伪账户事实。
- `components/mean_reversion_fixed.py` 为 compatibility re-export；
  `portfolio/simple_optimizer.py` 为 baseline/test-only，均有后续 removal gate。
