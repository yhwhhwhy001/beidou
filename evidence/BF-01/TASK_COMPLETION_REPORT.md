# BF-01 点时数据与标签合同 — 完成报告

- **任务**: BF-01
- **优先级**: P0
- **前置**: BF-00 ✅
- **状态**: COMPLETE
- **基线 Commit**: `24b3c9c631597618408e21bc699bb16b7c9e1aac`
- **新增测试**: 78 tests (all passing)
- **全量回归**: 560 passed, 2 failed (已知证书问题)

---

## 1. 交付文件

### 新增模块

| 文件 | 用途 |
|------|------|
| `beidou_research/mining/__init__.py` | 包初始化，导出所有公共类型 |
| `beidou_research/mining/contracts.py` | 核心数据合同：PredictionKey, LabelRecord, PredictionRecord, DatasetManifest, LabelSpec |
| `beidou_research/mining/point_in_time.py` | 点时连接引擎：PIT Join, ClosedBarEnforcer, FutureDataGuard, IsolationValidator |
| `beidou_research/mining/label_builder.py` | 标签构造器：forward return, triple-barrier, 成本调整 |

### 新增测试

| 文件 | 测试数 | 覆盖的验收标准 |
|------|--------|--------------|
| `tests/unit/test_bf01_contracts.py` | 30 | PredictionKey, LabelRecord, PredictionRecord, DatasetManifest, LabelSpec |
| `tests/unit/test_bf01_point_in_time.py` | 34 | PIT Join, revision rules, overlap detection, closed-bar, timeframe isolation |
| `tests/unit/test_bf01_label_builder.py` | 14 | LabelBuilder, log/simple return, cost adjustment, triple-barrier |

---

## 2. 实现摘要

### 2.1 PredictionKey（预测键）

```python
PredictionKey(
    venue, symbol, timeframe,           # 品种/周期隔离
    prediction_time, data_available_time,  # 点时约束
    horizon, horizon_unit,              # 持有期
    factor_id, factor_version,          # 版本追溯
)
```

**强制约束**: `data_available_time ≤ prediction_time`（构造时验证，注入未来数据直接拒绝）

### 2.2 LabelRecord（标签记录）

```python
LabelRecord(
    label_id, prediction_key,           # 唯一标识 + 关联预测
    label_start_time, label_end_time,   # 标签时间区间
    label_available_time,               # 标签变为可知的时间
    label_value, gross_return,          # 标签值与原始收益
    expected_cost_bps,                  # 预期成本
    quality_status,                     # VALID/FUTURE_LEAK/OVERLAPPING/...
    revision,                           # 修订版本号
)
```

**强制约束**:
- `label_start_time < label_end_time`
- `label_available_time ≥ label_end_time`
- `overlaps_with()` 检测时间重叠

### 2.3 PointInTimeJoin（点时连接引擎）

- 按 `(venue, symbol, timeframe)` 隔离预测和标签
- PredictionKey 层面拒绝 `data_available_time > prediction_time`
- ClosedBarEnforcer: K 线仅在 `is_closed=True` 且 `close_time` 已过后可用
- 重复写入按 revision 规则：相同 revision 幂等，高 revision 覆盖，低 revision 忽略
- 批量连接生成 JoinReport（matched/unmatched/future_data_blocked/overlapping）

### 2.4 LabelBuilder（标签构造器）

- 基础标签: `log(exit/entry) - expected_cost`
- 支持: log return, simple return, residual return
- Triple-barrier: 上限/下限/水平触及
- 成本模型: fee + spread + slippage + funding（持有期相关）
- Embargo bars 支持
- 确定性 label_id（基于 prediction_key + spec_hash）

---

## 3. 验收标准映射

| AC | 标准 | 证据 | 状态 |
|----|------|------|------|
| AC-01-01 | 注入未来数据时测试必须失败 | `PredictionKey` 构造时 `data_available_time > prediction_time` → ValueError | ✅ PASS |
| AC-01-02 | 相同 key 重复写入按 revision 规则处理 | `test_store_prediction_idempotent_same_revision`, `test_store_prediction_higher_revision_wins`, `test_store_prediction_lower_revision_ignored` | ✅ PASS |
| AC-01-03 | 任意预测可以追溯到原始事件范围 | `test_label_prediction_key_traceable` — venue/symbol/timeframe/time/horizon/factor_version 全部可追溯 | ✅ PASS |
| AC-01-04 | 1h 与 5m 数据不能交叉读取 | `test_cross_timeframe_join_rejected`, `test_validate_batch_timeframe_mismatch`, `IsolationValidator.group_by_scope` | ✅ PASS |
| AC-01-05 | closed-bar enforcement | `test_closed_bar_available`, `test_open_bar_not_available`, `test_closed_but_too_early_not_available` | ✅ PASS |
| AC-01-06 | 多品种隔离 | `test_symbol_mismatch_different_symbol`, `test_validate_batch_symbol_mismatch`, `test_group_by_scope_different_symbols` | ✅ PASS |
| AC-01-07 | 重叠标签检测 | `test_overlap_detection`, `test_no_overlap_for_disjoint_labels`, `test_overlaps_different_symbols_never_overlap` | ✅ PASS |

---

## 4. 禁止事项合规

- ✅ 未修改 `beidou_core/engine.py` 或其他已有生产代码
- ✅ 未导入/调用交易写 API
- ✅ 所有阈值由参数控制（未硬编码）
- ✅ 无 `eval`/`exec`/任意代码执行
- ✅ 未降低已有测试断言

---

## 5. 后续步骤

```
BF-00 ✅ → BF-01 ✅ → BF-02 (因子统计评估器重写) + BF-03 (表达式AST)
```

BF-02 需要依赖 `contracts.py` 中的 PredictionKey/LabelRecord 来替换当前错误的 IC 计算。
