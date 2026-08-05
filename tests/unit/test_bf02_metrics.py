"""BF-02 因子统计评估指标单元测试。

覆盖：
- IC / RankIC 与手工计算结果一致；
- ic_std 是 IC 序列标准差而非收益标准差（关键验证）；
- 分期 IC 序列非重叠、独立计算；
- ICIR 小样本返回 NOT_VERIFIABLE；
- Newey-West t 统计量与手工公式一致；
- VIF 检测已知共线性（多元回归，非两两相关近似）；
- 增量贡献在候选无增量时接近 0；
- 常数列、反向排序、缺失值等边界条件。
"""

import math
import random

import pytest

from beidou_research.mining.evaluation.metrics import (
    Verifiability,
    compute_block_bootstrap_ci,
    compute_cost_adjusted_metrics,
    compute_hit_rate,
    compute_ic,
    compute_icir,
    compute_incremental_contribution,
    compute_newey_west_tstat,
    compute_period_ic_series,
    compute_quantile_monotonicity,
    compute_rank_ic,
    compute_true_vif,
    compute_turnover,
)

# ---------------------------------------------------------------------------
# 手工参考实现（与生产实现独立，避免测试依赖被测代码的推导）
# ---------------------------------------------------------------------------


def _pearson_manual(xs, ys):
    """手工 Pearson 相关系数（不含缺失值处理）。"""
    xs = [float(x) for x in xs]
    ys = [float(y) for y in ys]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy)


def _ranks_manual(xs):
    """手工平均秩（1-based，处理并列）。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _sample_std_manual(vals):
    """手工样本标准差（ddof=1）。"""
    n = len(vals)
    mu = sum(vals) / n
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / (n - 1))


# ---------------------------------------------------------------------------
# IC / RankIC
# ---------------------------------------------------------------------------


def test_compute_ic_matches_manual_single_period():
    preds = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    rets = [0.1, -0.2, 0.3, 0.05, 0.4, 0.2, -0.1]
    out = compute_ic(preds, rets)
    assert out["ic_mean"] == pytest.approx(_pearson_manual(preds, rets))
    # 单期输入没有 IC 序列，标准差恒为 0
    assert out["ic_std"] == 0.0
    assert out["sample_count"] == 7


def test_compute_ic_multi_period_matches_manual():
    periods_p = [[1, 2, 3, 4], [2, 4, 6, 8], [1, 3, 5, 7]]
    periods_r = [[0.1, 0.2, 0.3, 0.4], [0.05, 0.1, 0.15, 0.2], [-0.1, 0.0, 0.1, 0.2]]
    out = compute_ic(periods_p, periods_r)
    ics = [_pearson_manual(p, r) for p, r in zip(periods_p, periods_r, strict=True)]
    assert out["ic_mean"] == pytest.approx(sum(ics) / 3)
    assert out["ic_std"] == pytest.approx(_sample_std_manual(ics))
    assert out["sample_count"] == 12


def test_ic_std_is_ic_series_std_not_returns_std():
    """关键验证：ic_std 必须是 IC 序列的标准差，而不是收益标准差。"""
    random.seed(7)
    periods_p = []
    periods_r = []
    for _ in range(20):
        p = [random.uniform(-1, 1) for _ in range(50)]
        r = [2.0 * v + random.uniform(-0.05, 0.05) for v in p]
        periods_p.append(p)
        periods_r.append(r)
    out = compute_ic(periods_p, periods_r)
    all_returns = [x for period in periods_r for x in period]
    returns_std = _sample_std_manual(all_returns)
    # 收益方差很大（±2 量级），但 IC 序列稳定（预测与收益强相关）
    assert out["ic_std"] != pytest.approx(returns_std, rel=1e-3)
    assert out["ic_std"] < returns_std * 0.5
    # IC 序列标准差等于各期 IC 的手工标准差
    ics = [_pearson_manual(p, r) for p, r in zip(periods_p, periods_r, strict=True)]
    assert out["ic_std"] == pytest.approx(_sample_std_manual(ics))


def test_rank_ic_matches_manual_spearman():
    # 预测含并列（两个 2.0），验证平均秩处理
    preds = [3.0, 1.0, 2.0, 2.0, 5.0, 4.0]
    rets = [0.5, 0.1, 0.2, 0.3, 0.9, 0.7]
    out = compute_rank_ic(preds, rets)
    expected = _pearson_manual(_ranks_manual(preds), _ranks_manual(rets))
    assert out["rank_ic_mean"] == pytest.approx(expected)
    assert out["rank_ic_std"] == 0.0
    assert out["sample_count"] == 6


def test_rank_ic_multi_period_matches_manual():
    periods_p = [[1, 3, 2, 4], [5, 1, 4, 2, 3]]
    periods_r = [[0.1, 0.3, 0.2, 0.4], [0.5, 0.1, 0.4, 0.2, 0.3]]
    out = compute_rank_ic(periods_p, periods_r)
    rank_ics = [_pearson_manual(_ranks_manual(p), _ranks_manual(r)) for p, r in zip(periods_p, periods_r, strict=True)]
    assert out["rank_ic_mean"] == pytest.approx(sum(rank_ics) / 2)
    assert out["rank_ic_std"] == pytest.approx(_sample_std_manual(rank_ics))
    assert out["sample_count"] == 9


# ---------------------------------------------------------------------------
# 分期 IC 序列
# ---------------------------------------------------------------------------


def test_period_ic_series_non_overlapping():
    """分期序列必须非重叠、每期独立计算（不用扩展窗口）。"""
    random.seed(3)
    preds = [random.uniform(0, 1) for _ in range(20)]
    rets = [random.uniform(-0.1, 0.1) for _ in range(20)]
    series = compute_period_ic_series(preds, rets, 5)
    assert len(series) == 4
    expected_bounds = [(0, 5), (5, 10), (10, 15), (15, 20)]
    for item, (s, e) in zip(series, expected_bounds, strict=True):
        assert item["period_start"] == s
        assert item["period_end"] == e
        # 每期 IC 只依赖该期样本，等于对该期切片的手工计算
        assert item["ic"] == pytest.approx(_pearson_manual(preds[s:e], rets[s:e]))
        assert item["rank_ic"] == pytest.approx(_pearson_manual(_ranks_manual(preds[s:e]), _ranks_manual(rets[s:e])))
        assert item["sample_count"] == 5


def test_period_ic_series_start_indices_form():
    random.seed(4)
    preds = [random.uniform(0, 1) for _ in range(20)]
    rets = [random.uniform(-0.1, 0.1) for _ in range(20)]
    series = compute_period_ic_series(preds, rets, [3, 9, 15])
    # 自动补 0，最后一期延伸到末尾
    assert [x["period_start"] for x in series] == [0, 3, 9, 15]
    assert [x["period_end"] for x in series] == [3, 9, 15, 20]
    # 与固定大小分期的第一期结果一致（独立计算验证）
    size_series = compute_period_ic_series(preds, rets, 3)
    assert series[0]["ic"] == pytest.approx(size_series[0]["ic"])


def test_period_ic_series_rejects_nested_input():
    with pytest.raises(ValueError):
        compute_period_ic_series([[1, 2], [3, 4]], [[1, 2], [3, 4]], 2)


# ---------------------------------------------------------------------------
# ICIR
# ---------------------------------------------------------------------------


def test_icir_small_sample_not_verifiable():
    """小样本（< 12 期）必须返回 NOT_VERIFIABLE，不得冒充有效值。"""
    out = compute_icir([0.1, 0.05, -0.02, 0.08, 0.03])
    assert out["status"] == Verifiability.NOT_VERIFIABLE.value
    assert out["icir"] == 0.0
    assert out["sample_count"] == 5

    # 恰好 11 期：仍不足
    out11 = compute_icir(list(range(11)))
    assert out11["status"] == Verifiability.NOT_VERIFIABLE.value


def test_icir_sufficient_sample_matches_manual():
    random.seed(21)
    ics = [random.uniform(-0.05, 0.05) for _ in range(15)]
    out = compute_icir(ics)
    assert out["status"] == Verifiability.VERIFIED.value
    assert out["ic_mean"] == pytest.approx(sum(ics) / 15)
    assert out["ic_std"] == pytest.approx(_sample_std_manual(ics))
    assert out["icir"] == pytest.approx(out["ic_mean"] / out["ic_std"])
    assert out["sample_count"] == 15


def test_icir_filters_missing_values():
    out = compute_icir([0.1, None, 0.2, float("nan"), 0.3] * 3)
    assert out["sample_count"] == 9
    assert out["status"] == Verifiability.NOT_VERIFIABLE.value


# ---------------------------------------------------------------------------
# Newey-West t 统计量
# ---------------------------------------------------------------------------


def test_newey_west_tstat_matches_manual_formula():
    ics = [0.05, 0.03, 0.07, 0.04, 0.06, 0.02, 0.08, 0.05, 0.01, 0.09]
    n = len(ics)
    max_lags = 2
    out = compute_newey_west_tstat(ics, max_lags=max_lags)
    assert out["max_lags"] == max_lags
    assert out["sample_count"] == n
    mu = sum(ics) / n
    # 手工 Newey-West 方差公式
    gamma = {}
    for k in range(max_lags + 1):
        gamma[k] = sum((ics[t] - mu) * (ics[t - k] - mu) for t in range(k, n)) / n
    var_nw = gamma[0] + 2.0 * sum((1.0 - k / (max_lags + 1.0)) * gamma[k] for k in range(1, max_lags + 1))
    se = math.sqrt(var_nw / n)
    assert out["se"] == pytest.approx(se)
    assert out["t_stat"] == pytest.approx(mu / se)
    assert out["p_value"] == pytest.approx(2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(mu / se) / math.sqrt(2.0)))))


def test_newey_west_tstat_auto_lags_reasonable():
    """自动滞后阶数 = floor(4 * (n/100)^(2/9))，统计量方向合理。"""
    random.seed(11)
    ics = [0.03 + 0.02 * math.sin(i / 3.0) + random.uniform(-0.01, 0.01) for i in range(60)]
    out = compute_newey_west_tstat(ics)
    assert out["sample_count"] == 60
    assert out["max_lags"] == math.floor(4.0 * (60 / 100.0) ** (2.0 / 9.0))
    assert out["se"] >= 0.0
    # 均值显著为正时 t 统计量为正
    assert out["t_stat"] > 0.0
    assert 0.0 <= out["p_value"] <= 1.0


# ---------------------------------------------------------------------------
# Block bootstrap
# ---------------------------------------------------------------------------


def test_block_bootstrap_ci_contains_mean():
    random.seed(17)
    ics = [random.uniform(-0.1, 0.1) for _ in range(60)]
    out = compute_block_bootstrap_ci(ics, n_bootstraps=500, block_size=6, seed=17)
    assert out["ic_mean"] == pytest.approx(sum(ics) / 60)
    assert out["ci_lower_95"] < out["ci_upper_95"]
    assert out["ci_lower_95"] <= out["ic_mean"] <= out["ci_upper_95"]
    assert out["ic_std_bootstrap"] > 0.0
    # 区间宽度应远小于序列本身的范围
    assert out["ci_upper_95"] - out["ci_lower_95"] < 0.05


def test_block_bootstrap_ci_small_sample():
    out = compute_block_bootstrap_ci([0.1], seed=1)
    assert out["ci_lower_95"] is None
    assert out["ci_upper_95"] is None


# ---------------------------------------------------------------------------
# 分位数单调性
# ---------------------------------------------------------------------------


def test_quantile_monotonicity_increasing():
    preds = [float(i) for i in range(1, 101)]
    rets = [2.0 * v for v in preds]
    out = compute_quantile_monotonicity(preds, rets, n_quantiles=5)
    assert out["is_monotonic"] is True
    assert out["monotonicity_score"] == 1.0
    # 最低分位均值 2*10.5，最高分位均值 2*90.5
    assert out["spread"] == pytest.approx(2.0 * (90.5 - 10.5))
    assert len(out["quantile_returns"]) == 5
    # 每组 20 个样本
    assert out["quantile_returns"] == pytest.approx([2.0 * sum(range(20 * q + 1, 20 * q + 21)) / 20 for q in range(5)])


def test_quantile_constant_predictions_boundary():
    """常数列预测：无可区分信息，判定为不单调，spread 记 0。"""
    random.seed(8)
    preds = [0.5] * 50
    rets = [random.uniform(-0.1, 0.1) for _ in range(50)]
    out = compute_quantile_monotonicity(preds, rets, n_quantiles=5)
    assert out["is_monotonic"] is False
    assert out["monotonicity_score"] == 0.0
    assert out["spread"] == pytest.approx(0.0)
    assert len(out["quantile_returns"]) == 5


# ---------------------------------------------------------------------------
# VIF（真正多元回归）
# ---------------------------------------------------------------------------


def test_vif_detects_known_collinearity():
    """完全线性相关必须给出极大 VIF，独立因子 VIF 接近 1。"""
    random.seed(5)
    f1 = [float(i) for i in range(1, 101)]
    f2 = [2.0 * v + 1.0 for v in f1]  # f2 = 2*f1 + 1：完全共线
    f3 = [random.uniform(-1, 1) for _ in range(100)]  # 独立因子
    f4 = [v + random.uniform(-0.5, 0.5) for v in f1]  # 高相关但非完全
    vifs = compute_true_vif({"f1": f1, "f2": f2, "f3": f3, "f4": f4})
    assert vifs["f1"] == float("inf")
    assert vifs["f2"] == float("inf")
    assert vifs["f3"] < 5.0  # 独立因子 VIF 接近 1
    assert vifs["f4"] > 50.0  # 高共线性显著放大 VIF


def test_vif_exact_duplicate_factor():
    f1 = [float(i) for i in range(1, 21)]
    vifs = compute_true_vif({"f1": f1, "dup": list(f1)})
    assert vifs["f1"] == float("inf")
    assert vifs["dup"] == float("inf")


def test_vif_single_factor_and_empty():
    assert compute_true_vif({"only": [1.0, 2.0, 3.0]}) == {"only": 1.0}
    assert compute_true_vif({}) == {}


def test_vif_requires_equal_lengths():
    with pytest.raises(ValueError):
        compute_true_vif({"a": [1.0, 2.0], "b": [1.0, 2.0, 3.0]})


# ---------------------------------------------------------------------------
# 增量贡献
# ---------------------------------------------------------------------------


def test_incremental_contribution_zero_when_no_increment():
    """候选因子与已有模型完全相同时，所有增量必须接近 0。"""
    random.seed(9)
    periods_p = []
    periods_r = []
    for _ in range(12):
        p = [random.uniform(-1, 1) for _ in range(40)]
        r = [0.5 * v + random.uniform(-0.2, 0.2) for v in p]
        periods_p.append(p)
        periods_r.append(r)
    out = compute_incremental_contribution(periods_p, periods_r, periods_p, periods_r)
    assert abs(out["delta_ic"]) < 1e-9
    assert abs(out["delta_sharpe"]) < 1e-9
    assert abs(out["delta_ir"]) < 1e-9
    assert out["sample_count"] == 12 * 40


def test_incremental_contribution_positive_when_candidate_helps():
    """候选因子含有真实信号而现有模型为噪声时，增量显著为正。"""
    random.seed(13)
    n_periods, n_stocks = 10, 50
    signal = [[random.uniform(-1, 1) for _ in range(n_stocks)] for _ in range(n_periods)]
    rets = [[0.5 * s + random.uniform(-0.1, 0.1) for s in period] for period in signal]
    noise = [[random.uniform(-1, 1) for _ in range(n_stocks)] for _ in range(n_periods)]
    out = compute_incremental_contribution(signal, rets, noise, rets)
    assert out["delta_ic"] > 0.3
    assert out["existing_ic"] < out["candidate_ic"]


def test_incremental_contribution_length_mismatch():
    # 扁平输入：样本数不一致
    with pytest.raises(ValueError):
        compute_incremental_contribution([1, 2], [1, 2], [1, 2, 3], [1, 2, 3])
    # 嵌套输入：期数不一致
    with pytest.raises(ValueError):
        compute_incremental_contribution(
            [[1, 2], [3, 4]],
            [[1, 2], [3, 4]],
            [[1, 2]],
            [[1, 2]],
        )
    # 同一期内样本数不一致
    with pytest.raises(ValueError):
        compute_incremental_contribution(
            [[1, 2, 3]],
            [[1, 2, 3]],
            [[1, 2]],
            [[1, 2]],
        )


# ---------------------------------------------------------------------------
# 换手率 / 命中率 / 成本调整
# ---------------------------------------------------------------------------


def test_turnover_weight_change():
    pos_t = [0.5, 0.5, 0.0]
    pos_t1 = [0.7, 0.3, 0.0]
    # 0.5 * (|0.2| + |0.2| + 0) = 0.2
    assert compute_turnover(pos_t, pos_t1) == pytest.approx(0.2)


def test_turnover_constant_positions():
    assert compute_turnover([0.3, 0.7], [0.3, 0.7]) == pytest.approx(0.0)


def test_turnover_length_mismatch():
    with pytest.raises(ValueError):
        compute_turnover([0.5, 0.5], [0.5])


def test_hit_rate():
    preds = [1.0, -1.0, 1.0, 0.0, 2.0]
    rets = [1.0, -0.5, -1.0, 0.5, 0.0]
    # 有效对: (1,1) 命中, (-1,-1) 命中, (1,-1) 未中, (0,0.5) 排除, (2,0) 排除
    assert compute_hit_rate(preds, rets) == pytest.approx(2.0 / 3.0)


def test_hit_rate_no_valid_samples():
    assert compute_hit_rate([0.0, 0.0], [0.1, -0.1]) == 0.0


def test_cost_adjusted_metrics_scalar_cost():
    rets = [0.01, -0.005, 0.02, 0.0]
    out = compute_cost_adjusted_metrics(rets, costs_bps=5)
    net = [r - 0.0005 for r in rets]
    assert out["net_mean"] == pytest.approx(sum(net) / 4)
    assert out["gross_mean"] == pytest.approx(sum(rets) / 4)
    assert out["cost_impact_bps"] == pytest.approx(5.0)
    assert out["total_costs_bps"] == pytest.approx(20.0)
    assert out["net_sharpe"] == pytest.approx((sum(net) / 4) / _sample_std_manual(net))
    assert out["sample_count"] == 4


def test_cost_adjusted_metrics_sequence_cost():
    rets = [0.01, 0.02, 0.03]
    out = compute_cost_adjusted_metrics(rets, costs_bps=[10, 20, 30])
    net = [0.01 - 0.001, 0.02 - 0.002, 0.03 - 0.003]
    assert out["net_mean"] == pytest.approx(sum(net) / 3)
    assert out["total_costs_bps"] == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# 边界条件
# ---------------------------------------------------------------------------


def test_constant_column_boundary():
    """常数列预测：相关性无定义，按约定返回 0。"""
    random.seed(2)
    preds = [1.0] * 10
    rets = [random.uniform(-0.1, 0.1) for _ in range(10)]
    out = compute_ic(preds, rets)
    assert out["ic_mean"] == 0.0
    assert out["sample_count"] == 10
    assert compute_rank_ic(preds, rets)["rank_ic_mean"] == 0.0


def test_reverse_sorted_boundary():
    """反向排序：IC 接近 -1，分位数单调性为假。"""
    rets = [float(i) for i in range(1, 11)]
    preds = list(reversed(rets))
    out = compute_ic(preds, rets)
    assert out["ic_mean"] < -0.99
    q = compute_quantile_monotonicity(preds, rets, n_quantiles=5)
    assert q["is_monotonic"] is False
    assert q["monotonicity_score"] == 0.0
    assert q["spread"] < 0.0


def test_missing_values_boundary():
    """缺失值成对剔除，剩余样本与手工计算结果一致。"""
    preds = [1.0, None, 3.0, float("nan"), 5.0, 6.0]
    rets = [0.1, 0.2, 0.3, 0.4, float("nan"), 0.6]
    out = compute_ic(preds, rets)
    valid_p = [1.0, 3.0, 6.0]
    valid_r = [0.1, 0.3, 0.6]
    assert out["sample_count"] == 3
    assert out["ic_mean"] == pytest.approx(_pearson_manual(valid_p, valid_r))
    assert compute_rank_ic(preds, rets)["sample_count"] == 3


def test_empty_input_boundary():
    out = compute_ic([], [])
    assert out == {"ic_mean": 0.0, "ic_std": 0.0, "sample_count": 0}
    assert compute_period_ic_series([], [], 5) == []
    assert compute_hit_rate([], []) == 0.0


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        compute_ic([1.0, 2.0], [1.0])
    with pytest.raises(ValueError):
        compute_period_ic_series([1.0, 2.0], [1.0], 1)
    with pytest.raises(ValueError):
        compute_period_ic_series([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], 0)
    with pytest.raises(ValueError):
        compute_quantile_monotonicity([1.0, 2.0], [1.0, 2.0], n_quantiles=1)
