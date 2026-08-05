# BF-00 基线冻结与证据包 — 完成报告

- **任务**: BF-00
- **优先级**: P0
- **状态**: COMPLETE
- **基线 Commit**: `24b3c9c631597618408e21bc699bb16b7c9e1aac`
- **分析日期**: 2026-08-06
- **执行**: Claude Code（只读分析，未修改交易行为）

---

## 1. 基线冻结

### 1.1 代码状态

| 项目 | 值 |
|------|-----|
| Commit | `24b3c9c631597618408e21bc699bb16b7c9e1aac` |
| 分支 | `main` |
| 未提交变更 | `beidou_core/engine.py`, `beidou_core/feed.py`, `evidence/BD-00/startup_audit.json` |
| 配置文件 SHA-256 | 7 个文件均记录在 `baseline_metadata.json` |
| 测试结果 | **475 passed, 2 failed**（2个失败与证书门禁相关，非因子评估问题） |

### 1.2 依赖锁

Python 3.14.6，核心依赖版本记录在 `baseline_metadata.json`。
**注意：当前依赖不含 NumPy/SciPy/Pandas/Statsmodels/Scikit-learn 等研究库**。

### 1.3 因子注册状态

引擎在 `__init__` 中注册 8 个因子并**无证据**推进到 CHALLENGER：
- `meanrev_entry_v1`, `momentum_filter_v1`, `trend_entry_v1`, `breakout_entry_v1`
- `volatility_filter_v1`, `volume_filter_v1`, `trailing_exit_v1`, `time_exit_v1`

---

## 2. 时间错配 — 最小可复现实例

### 2.1 问题定位

**文件**: `beidou_core/engine.py`

**错误发生在 `_nearline_tick()` (行 1301-1309)**：
```python
# 先计算 past return（return from prev_close to close）
if prev_close > 0:
    fwd_return = (close - prev_close) / prev_close  # 这是 return(t-1, t)
    self._factor_returns.append(fwd_return)

# 然后生成预测（基于当前 close）
# ... DAG 执行 ...

# 在 _offline_tick() 中按位置配对：
preds_aligned = preds[-min_n:]
rets_aligned = returns[-min_n:]  # 实际是 past returns
ic_mean, ic_std = FactorEvaluator.compute_ic(preds_aligned, rets_aligned)
```

**正确关系应为**：
```text
prediction(symbol, t, horizon=h) ↔ forward_return(symbol, t → t+h)
```

**当前实际关系为**：
```text
prediction(symbol, t) ↔ realized_return(symbol, t-1 → t)
```

### 2.2 可复现测试

测试文件: `tests/unit/test_bf00_timing_mismatch.py` — **7 个测试全部通过**

| 测试 | 结论 |
|------|------|
| `test_prediction_paired_with_past_return_in_current_engine` | IC vs past=+0.37, IC vs forward=-0.06 — 符号和幅度均不同 |
| `test_current_engine_returns_are_past_not_forward` | 确定性 4 点序列证明配对的是 past return |
| `test_correct_pairing_yields_different_ic` | 1000 点序列 IC 差异 0.38 |
| `test_ic_std_is_return_std_not_ic_std` | `compute_ic()` 第二个返回值 = returns std，不是 IC std |
| `test_extended_window_icir_is_biased` | 扩展窗口 ICIR 高估 **13.29x** |
| `test_factor_predictions_not_isolated_by_symbol` | 多品种混合导致 IC 信号抵消 |
| `test_shared_close_price_creates_spurious_correlation` | 共享 close price 制造伪相关 |

### 2.3 运行命令

```bash
python -m pytest tests/unit/test_bf00_timing_mismatch.py -v -s
```

---

## 3. 当前因子结果标记

### 3.1 判决

```text
███████████████████████████████████████████████████████
█                                                     █
█  INVALID_FOR_PROMOTION                              █
█                                                     █
█  当前所有在线 IC/RankIC/ICIR 结果                    █
█  不可用于因子晋级决策。                              █
█                                                     █
█  原因:                                               █
█  1. 预测-标签时间错配 (F-P0-01)                      █
█  2. IC 标准差定义错误 (F-P0-04)                      █
█  3. ICIR 计算使用高度重叠扩展窗口 (~13x 高估)       █
█  4. 多品种预测/收益不隔离 (F-P0-02)                  █
█                                                     █
███████████████████████████████████████████████████████
```

### 3.2 已知缺陷确认

| 缺陷 ID | 描述 | 确认状态 | 证据 |
|---------|------|---------|------|
| F-P0-01 | 预测与标签时间错配 | ✅ CONFIRMED | 自动化测试 |
| F-P0-02 | 收益序列不隔离 | ✅ CONFIRMED | 自动化测试 |
| F-P0-03 | 生命周期证据被绕过 | ✅ CONFIRMED | 代码审查（8因子无证据推进到CHALLENGER） |
| F-P0-04 | compute_ic 统计定义错误 | ✅ CONFIRMED | 自动化测试 |
| F-P1-01 | VIF 非真正 VIF | ✅ CONFIRMED | 代码审查（最大两两相关近似） |
| F-P1-02 | 边际贡献使用常数倍数 | ✅ CONFIRMED | 代码审查（marginal_sharpe = icir × 0.1） |
| F-P1-04 | 状态仅内存存储 | ✅ CONFIRMED | 代码审查（FactorRegistry 纯内存 dict） |
| S-P0-02 | Filter 输出方向信号 | ✅ CONFIRMED | 代码审查（MomentumFilter 输出 LONG/SHORT） |
| S-P0-03 | 双实现（简化/目标算法） | ✅ CONFIRMED | 代码审查（engine.py 内的硬编码 SMA20/RSI） |
| R-P0-01 | Purged Walk-Forward 占位 | ✅ CONFIRMED | 需后续 BF-05 验证 |

---

## 4. 证据文件清单

| 文件 | 路径 | SHA-256 |
|------|------|---------|
| 基线元数据 | `evidence/BF-00/baseline_metadata.json` | (见 SHA256SUMS) |
| 完成报告 | `evidence/BF-00/TASK_COMPLETION_REPORT.md` | (见 SHA256SUMS) |
| 时间错配测试 | `tests/unit/test_bf00_timing_mismatch.py` | (见 SHA256SUMS) |

---

## 5. 验收标准

| AC | 标准 | 状态 |
|----|------|------|
| AC-00-01 | 能用自动化测试证明 prediction_t 与 return_{t-1,t} 被错误配对 | ✅ PASS |
| AC-00-02 | 基线包包含 SHA-256 | ✅ PASS |
| AC-00-03 | 不修改交易行为 | ✅ PASS（只读分析，仅新增测试文件） |

---

## 6. 修改文件

### 新增文件
| 文件 | 用途 |
|------|------|
| `tests/unit/test_bf00_timing_mismatch.py` | 时间错配最小可复现实例（7 tests） |
| `evidence/BF-00/baseline_metadata.json` | 基线元数据 JSON |
| `evidence/BF-00/TASK_COMPLETION_REPORT.md` | 本报告 |
| `evidence/BF-00/SHA256SUMS` | 文件完整性校验和 |

### 未修改已有文件
本任务为只读分析，未修改任何已有代码或配置。

---

## 7. 后续步骤

BF-00 完成后，建议执行顺序：
```
BF-00 ✅ → BF-01 (点时数据与标签合同) → BF-02 (因子统计评估器重写) + BF-03 (表达式AST)
```

**关键前置条件**：在 BF-01 之前，不允许使用当前 IC/ICIR 值进行任何因子晋级决策。
