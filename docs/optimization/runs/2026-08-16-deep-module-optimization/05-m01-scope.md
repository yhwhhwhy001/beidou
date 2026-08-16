# M01 行情与 Market Data — 范围定义

## Phase A 结论（已核实）

- P0-04 闭合证据伪造：feed.py:412-414 `include_closed=True` 且 x 缺失时制造 `is_closed=True`；同步合并（874-884/887-897）丢 is_closed 字段
- P0-05 迟到 tick：klines.py:111-133 对 `timestamp < current.open_time` 的迟到 tick 无任何防护，直接并入当前 bar
- P0-06 时钟：feed.py:194/543/586/827 用本地 `datetime.now()` 当 bar 时间；Binance ticker 提供 E（事件时间）未使用；quality.add_clock_skew_check 全仓零调用
- P0-07 DQ 门禁装饰化：feed.py:594-609/836-851 构造 gate 后从不求值
- P1-08：replay 脚本 4 处 API 失配（check_closed_bar/status=="CLOSED"/kline_gen.add/FeatureVector kwargs）；FeatureVector.data_quality_tier 从未赋值
- P2：market.py:331-338 `_seen` 注册先于 is_closed 判定 → 未闭合→闭合的正常演进被标 DUPLICATE

## 任务

- F01（P0-04）：_parse_rest_kline 缺失标志→is_closed=False（绝不伪造闭合）；同步合并补 is_closed
- F02（P0-05）：KLineGenerator 迟到 tick 拒绝（拒绝计数+原因审计）；测试覆盖顺序/迟到/跨 bar
- F03（P0-06）：ticker E 事件时间贯通 5m/1h 生成器（非法/缺失→本地回退+审计）；bar 桶切分基于交易所时间
- F04（P0-07）：DQ gate 真实检查（CLOCK_SKEW 接线、spread CONSISTENCY）+ overall_tier() 求值（FAIL→MarketDataUnknownError）；FeatureVector 赋值 data_quality_tier
- F05（P1-08）：replay 脚本按真实 API 重写；normalizer dup-before-closed 修复
- F06：spread 2.0 回退审计（CONDITIONAL 检查+日志）
- F07：回归 + 独立对抗审查 + 证据归档

## 不做（M01 范围外）

- RSI/MACD/ATR 数学修正 → M03；Universe 动态发现 → M02；spread 默认值的消费者侧处理 → M09/M10；合约孤岛接线决策（orderbook/canonical_bars 进生产链）→ 逐项评审后由 M01-R2 或后续模块决策
