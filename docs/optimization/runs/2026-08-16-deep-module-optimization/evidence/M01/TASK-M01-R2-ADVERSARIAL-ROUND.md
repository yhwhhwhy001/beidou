# M01 第二轮对抗审查与修复证据（F10-R2）

## 审查结论摘要

独立审查代理以 **live Binance API 实证**构造反例，判定：F01/F03/F04 三个 CONFIRMED_BUG、F02/F05/F06/replay 四个 RISK。核心实锤：

1. **REST kline index 11 不是 x 标志** —— 是 "Ignore" 字段（恒为 "0" 字符串），`bool("0")`=True 使每行（含形成中 bar）被制造为闭合。M01-F01 的分支对真实数据不可达（no-op），配套测试假绿。
2. 5m 生成器 venue key 不匹配（写 `BINANCE_USDM:*` 读 `BINANCE:*`）→ 事件时间接线落黑洞。
3. 分钟桶不 floor 到 interval 边界（10:04:30 → 10:04-10:09 错位桶）。
4. DQ 门禁只接保护参数路径，信号路径（近线特征）无门禁；dq_tier 零消费者（风险快照硬编码 PASS）；FAIL→raise 会中止整个 realtime tick。
5. 时钟偏差与数据年龄语义混用（now-E 是年龄不是偏差）；normalizer 的 sequence 消耗使 payload_hash 随摄取历史漂移；validator 只认 1m/5m/1h。

## 修复矩阵（R2）

| 反例 | 修复 | 测试 |
|---|---|---|
| index 11 误读 | `_parse_rest_kline` 不采信非 bool 字符串标志；缺失/不可信 → **close_time <= now 时间推导**（Binance 原生语义）；`include_closed=False` 调用方丢弃形成中 bar | 3 个新测试（形成中→False、已闭合→True、"0" 字符串不采信） |
| 5m venue key 黑洞 | `KLineGenerator.update` 默认 venue_id → "BINANCE"（与 feed 读取 key 一致） | test_update_writes_to_same_key_as_feed_reads |
| 分钟桶错位 | process_tick 对 m 级 interval floor 分钟到小时对齐边界（Binance 语义 :00/:05/:10…） | test_minute_interval_buckets_floor_to_boundary（含 15m/30m 非零分钟起点） |
| DQ 门禁接错点 | ① 语义修正：`_clock_offset_ms()`（适配器服务端时间偏移）与数据年龄分离；`_build_live_dq_gate` 共享构建器（FRESHNESS=年龄、CLOCK_SKEW=偏移、COMPLETENESS=点差）② 信号路径 `async_get_kline_features` 接入门禁（FAIL→raise）③ 引擎 realtime 批循环+启动保护重建循环**按符号分隔异常**（单标的 DQ FAIL 不再中止整 tick/整恢复） | bucketing 测试升级 + 假客户端时钟偏移；编译验证 |
| seq 漂移 | normalizer sequence "peek 不消耗"：NOT_CLOSED 不推进计数器 → 闭合 bar 的 payload_hash 与摄取历史无关 | test_payload_hash_stable_across_ingestion_histories |
| validator 间隔失真 | `_interval_seconds` 通用解析（m/h/d/w）；replay 以 bar.close_time 为参照 now（确定性+PIT 语义） | test_sequence_validator_supports_all_intervals（15m/2h/4h 连续 bar 不再误报 GAP） |

## 测试证据

| 命令 | 结果 |
|---|---|
| 目标测试（market_data/market_data_dq/klines/replay） | 53 passed |
| 全量 `pytest tests/` | **2559 passed / 0 failed** |
| ruff（改动文件逐项） | 0 新增 |
| compileall | OK |

## 残余（登记）

- dq_tier/`spread_bps_source` 消费者侧接线（风险快照硬编码 PASS、adaptive sizing/cost 未消费 fallback 标记）→ **M10/M09**（字段已可供消费）
- E 缺失时本地时钟回退的迟到守卫绕过（仅异常/重放传输可达；live Binance E 恒在）→ 文档化决策，登记 M19 观察
- 时钟超前场景的最终防线：is_closed 时间推导 + 信号路径 CLOCK_SKEW 门禁（偏移来自服务端同步）已闭合 TOP1 反例；`_clock_offset_ms` 的同步新鲜度（无时间戳）登记 M11 观察

## 对抗审查方法论注记

本轮审查员使用 live API 抓取推翻"文档级推断"——**外部协议事实必须实测，不能只信字段名**。教训已固化到测试：`test_parse_rest_kline_ignore_field_string_is_not_trusted` 用真实 Binance 行形态（12 字段、index 11="0"）构造。
