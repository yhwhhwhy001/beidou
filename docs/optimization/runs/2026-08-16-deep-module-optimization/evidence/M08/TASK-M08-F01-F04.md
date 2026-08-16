# M08 回测与研究系统 — 修复证据（F01-F04）

## 修复矩阵

| 任务 | 缺陷 | 修复 | 测试 |
|---|---|---|---|
| F01 | `StrategyPnL.total_return = log(1+Σr)` 数学错误（对数收益应累加 Σlog(1+r)；Σr<-1 时域错误） | `sum(math.log1p(r))` | test_total_return_is_compound_log_sum / test_total_return_handles_large_drawdown |
| F02 | Sharpe per-bar 与 Sortino/Calmar/年化波动率量纲分裂（同一报告内 sharpe 未年化） | evaluate/compute_sharpe_from_pnl/compute_sortino_from_pnl 年化口径统一 + `periods_per_year` 参数化（默认 252 兼容） | test_sharpe_is_annualized_and_consistent_with_volatility / test_sharpe_from_pnl_annualized（8760 vs 252 比例） |
| F03 | 最大回撤用算术累计（大波动偏离真实权益曲线、负 peak 分母失真） | 复利权益曲线 equity=∏(1+r)，dd=(peak-equity)/peak | test_drawdown_is_compounding（+50%/-40% → 0.4 而非旧公式 0.8） |
| F04 | simulate_paper_window 硬编码 √24（仅 1h 正确）、challenger_icir 用 signal×return 乘积冒充 IC 序列 IR、runner cost_bps=8.0 硬编码 | bars_per_year 参数化；challenger_icir 语义修正为 paper PnL 信息比率（=sharpe，字段名保留兼容）；成本从 config.cost_model.avg_spread_bps 绑定 + timeframe 年化 | 回归（simulate_paper_window/挖掘流水线） |

## 测试证据

- 新增 5 个数学性质测试 + 现有 pnl_kernel 12 个 + paper 模拟/挖掘流水线回归
- 全量套件结果：见本轮输出

## 残余（登记）

- Profit Factor 未实现 → 指标清单扩展（与 M08-R2 或研究演进）
- 订单级撮合/partial fill 无实现（contracts 声称 paper 可产生 partial fill）→ 登记
- pnl_kernel 的年化 252 默认仍存在于 annualized_return 属性（文档化兼容，方法化已提供）
- R-M03-1 阈值重标定：M08 修正后的真实成本/收益口径是重标定证据基础，重标定动作在 M06 联动执行
