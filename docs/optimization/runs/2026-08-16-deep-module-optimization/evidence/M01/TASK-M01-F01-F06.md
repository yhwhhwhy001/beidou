# M01 行情与 Market Data — 修复证据（F01-F06）

## 修复矩阵（4 个 P0 + 2 个 P1）

| 任务 | 缺陷 | 修复 | 测试 |
|---|---|---|---|
| F01 (P0-04) | feed.py:412-414 缺失 x 标志时制造 `is_closed=True`；同步合并丢 is_closed | 缺失标志 → `is_closed=False`（与 normalizer 不变量一致）；抽取 `_generated_bar_to_dict`（3 个合并点共用，显式携带 is_closed） | test_parse_rest_kline_missing_close_flag_is_not_closed / test_sync_kline_merge_preserves_closed_evidence |
| F02 (P0-05) | KLineGenerator 迟到 tick 静默并入当前/已闭合 bar | `timestamp < current.open_time` 或早于最后一根闭合 bar → 拒绝（`rejected_ticks`+`last_rejection` 审计），绝不污染已消费事实 | test_klines_late_tick_guard.py 4 个测试（bar 内迟到/跨 bar 迟到/顺序正常/边界） |
| F03 (P0-06) | bar 桶用本地 `datetime.now()` 切分 | `_parse_event_time`（E 字段 ms → datetime；缺失/未来>5s/早于 24h → None+审计计数）；WS/REST 同步/异步 4 个调用点全部贯通 | test_parse_event_time_valid_and_invalid / test_async_update_features_buckets_bars_by_exchange_event_time |
| F04 (P0-07) | DQ gate 构造后从不求值 | CLOCK_SKEW 检查接线（此前全仓零调用）+ spread CONSISTENCY 三态 + `overall_tier()` 求值（FAIL → MarketDataUnknownError）；`features["dq_tier"]` + FeatureVector.data_quality_tier 赋值（同步/异步双路径） | bucketing 测试断言 dq_tier PASS + FeatureVector 持久化 |
| F05 (P1-08) | replay 脚本 4 处 API 失配不可运行；normalizer 未闭合→闭合演进误判 DUPLICATE | 脚本按真实契约重写（normalize → BarIntegrity → BarSequenceValidator → FeatureVector 真实字段；available_at=bar.close_time 保证确定性）；normalizer `_seen` 注册移到 is_closed 判定之后 | test_replay_fixture_tool.py 3 个测试（演进语义/重复检测/确定性） |
| F06 (P1) | spread 2.0bps 回退静默制造市场事实 | 每 symbol 一次 WARN + `spread_bps_source` 显式标记（消费者侧收紧登记 M09/M10） | 由 bucketing 测试间接覆盖 |

## 测试证据

| 命令 | 结果 |
|---|---|
| 目标测试（market_data/market_data_dq/klines/replay/ws/rest/orderbook/strategy_data） | 92 passed |
| 全量 `pytest tests/` | **2554 passed / 0 failed（63.1s）** |
| ruff（本次改动文件） | All checks passed（全库保持基线 38） |
| compileall | OK |

## 过程注记

- TDD 三轮红灯→绿灯；中途发现 async 路径事件时间编辑遗漏（变量未定义被测试捕获）——已修复并验证。
- 测试自身的两处修正：① 假数据 E 用了 9 分钟前时间戳 → 新时钟检查如实 FAIL（证明门禁有效）；② 假数据点差 198bps 触发新 spread 检查 → 收紧假数据。
- `test_replay_fixture_tool` 在全量套件失败暴露 CWD 污染（test_launcher_cli_contracts chdir 未恢复）→ 测试改用 `__file__` 绝对路径锚定；根因登记 M21。
- DQ gate 行为变化声明：spread≥100bps → CONDITIONAL（不阻断，走引擎点差惩罚）；CLOCK_SKEW≥5s 或事件时间缺失 → CONDITIONAL（缺失）或 FAIL（skew 超限 → 阻断信号，INV-004 执行点）。运行引擎为修复前代码，下次重启生效。

## 残余（登记）

- spread 回退值消费者侧收紧 → M09/M10（`spread_bps_source: fallback` 已可供消费）
- 合约孤岛接线决策（orderbook/canonical_bars/BarSequenceValidator 进生产链）→ 逐项评审，后续模块
- CWD 污染根因 → M21
