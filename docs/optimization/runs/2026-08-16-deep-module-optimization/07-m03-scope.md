# M03 Feature / Indicator — 范围定义

## Phase A 结论（已核实）

feed._compute_kline_features（生产近线特征唯一来源）的指标数学缺陷：

| 指标 | 现状 | 标准 | 判定 |
|---|---|---|---|
| RSI | `returns[-15:]` 取 15 个收益率、`gains[-14:]` 简单均值（非 Wilder 平滑）；窗口逻辑混乱 | Wilder 平滑 RSI(14) | **数学错误**；仓库已有两套正确实现（rsi.py compute_rsi_wilder、canonical_feature_engine）—— 三套并存，feed 用的是第三套错误版 |
| MACD signal | `ema_12*0.2 + ema_26*0.8 - (ema_12-ema_26)*0.2` —— 代数化简恒等于 **EMA26**（macd_signal 字段完全错误） | MACD 线（EMA12-EMA26 序列）的 EMA9 | **数学错误** |
| ATR | `range(1, min(15,len))` 取 14 根 TR 简单均值 | Wilder 平滑 ATR(14) | 近似非标准 |
| 年化波动率 | 仅 interval=="1h" 用 √(365×24)；其余全部 √365（4h/1d/1m 全部错误） | √(每年 bar 数)，按 interval 换算 | **数学错误**（1h 之外的 interval） |
| EMA | 自实现标准算法（seed SMA） | 标准 | ✅ |
| Bollinger/SMA/量比 | 标准 | 标准 | ✅ |
| warm-up | <20 bar 返回 {} → 调用方抛 UNKNOWN | — | ✅ fail-closed |

## 任务

- F01：RSI 收敛到权威实现 —— feed 导入 `beidou_research.factors.rsi.compute_rsi_wilder`（架构允许 core→research），NOT_VERIFIABLE 时保守 50 中性值并审计
- F02：MACD 序列化标准实现（EMA12/EMA26 序列 → MACD 线 → signal=EMA9(MACD线)最后值）
- F03：ATR Wilder 平滑序列化
- F04：年化波动率按 interval 换算 bars_per_year（1m/5m/15m/30m/1h/4h/1d/1w 表）
- F05：性质测试 —— feed RSI 与 rsi.py 交叉验证（同输入同输出，容差 0.01）；MACD signal 与独立手算 EMA9 对比；ATR 与 Wilder 参考对比；年化换算表测试
- F06：回归 + 对抗审查 + 证据

## 不做（M03 范围外）

- 三套特征引擎的全链路收敛（canonical_feature_engine 接线）—— 更大的重构，登记残余
- feed 之外消费指标的数学（strategy 层 z-score/half-life 等）→ 各模块
- 特征缓存/性能优化
