"""因子统计评估指标（BF-02）。

重写要点（与旧版 factor.py 中的手写简化版对比）：
- IC 标准差是「各期独立 IC 值」序列的标准差，而不是收益标准差；
- ICIR 基于非重叠分期 IC 序列计算，至少 12 期独立 IC 才返回有效值，
  样本不足时返回 NOT_VERIFIABLE 状态，不返回 0.0 冒充有效值；
- VIF 是真正多元回归（OLS + 极小岭正则保证数值稳定），
  不是两两相关系数的近似；
- 增量贡献基于 Leave-one-factor-out 组合差分，
  不使用常数倍数（如 icir × 0.1）近似边际贡献；
- 所有统计量（Pearson/Spearman、Newey-West、block bootstrap）均以
  纯 Python 实现，不依赖 numpy/scipy（研究依赖将在后续补充）。

输入约定：
- 本模块以「纯数值序列」为输入；上层（数据层）负责将
  PredictionKey / LabelRecord 记录转换为数值序列后再调用本模块。
- 多期数据使用嵌套列表形式：每期一个列表。
- 所有返回 dict 的键值均可被 json.dumps 序列化（除完美共线性时
  VIF 为 float("inf")，见 compute_true_vif 的说明）。

所有函数返回 dict，便于 JSON 序列化。
"""

from __future__ import annotations

import itertools
import math
import random
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

# 与数据层契约类型保持引用关系（本模块输入为纯数值列表，
# 上层负责将 LabelRecord / PredictionKey 记录转换为数值序列）
from beidou_research.mining.contracts import LabelRecord, PredictionKey  # noqa: F401

__all__ = [
    "Verifiability",
    "compute_block_bootstrap_ci",
    "compute_cost_adjusted_metrics",
    "compute_hit_rate",
    "compute_ic",
    "compute_icir",
    "compute_incremental_contribution",
    "compute_newey_west_tstat",
    "compute_period_ic_series",
    "compute_quantile_monotonicity",
    "compute_rank_ic",
    "compute_true_vif",
    "compute_turnover",
]

#: ICIR 有效所需的最少独立分期数（少于该值返回 NOT_VERIFIABLE）
MIN_IC_PERIODS_FOR_ICIR = 12

#: 判断数值恒定的容差
_EPS = 1e-12


class Verifiability(str, Enum):
    """统计指标的可验证状态。"""

    VERIFIED = "VERIFIED"
    """样本量充足，指标可信。"""

    NOT_VERIFIABLE = "NOT_VERIFIABLE"
    """样本量不足，指标不可验证（不得当作有效值使用）。"""


# ---------------------------------------------------------------------------
# 类型别名
# ---------------------------------------------------------------------------

#: 数值（含缺失值）
Value = Optional[float]

#: 信号输入：扁平列表（单期）或嵌套列表（多期）
SignalInput = Union[Sequence[Value], Sequence[Sequence[Value]]]


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------

def _mean(vals: Sequence[float]) -> float:
    """算术均值；空序列返回 0.0。"""
    return sum(vals) / len(vals) if vals else 0.0


def _std(vals: Sequence[float]) -> float:
    """样本标准差（ddof=1）；不足 2 个样本返回 0.0。

    与 scipy.stats.tstd 一致。IC 序列的标准差统一使用样本标准差。
    """
    n = len(vals)
    if n < 2:
        return 0.0
    mu = sum(vals) / n
    var = sum((v - mu) ** 2 for v in vals) / (n - 1)
    return math.sqrt(var)


def _clean_series(vals: Sequence[Value]) -> List[float]:
    """过滤 None / NaN / Inf 及不可转换的值。"""
    out: List[float] = []
    for v in vals:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or math.isinf(f):
            continue
        out.append(f)
    return out


def _clean_pairs(
    xs: Sequence[Value], ys: Sequence[Value]
) -> List[Tuple[float, float]]:
    """成对过滤缺失值：任一序列在该位置无效则整对剔除。"""
    if len(xs) != len(ys):
        raise ValueError(f"两个序列长度不一致: {len(xs)} vs {len(ys)}")
    out: List[Tuple[float, float]] = []
    for x, y in zip(xs, ys, strict=True):
        if x is None or y is None:
            continue
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            continue
        if math.isnan(fx) or math.isinf(fx) or math.isnan(fy) or math.isinf(fy):
            continue
        out.append((fx, fy))
    return out


def _is_nested(seq: Sequence[Any]) -> bool:
    """是否为「每期一个列表」的嵌套输入。"""
    return len(seq) > 0 and isinstance(seq[0], (list, tuple))


def _check_lengths(a: Sequence[Any], b: Sequence[Any]) -> None:
    if len(a) != len(b):
        raise ValueError(f"两个序列长度不一致: {len(a)} vs {len(b)}")


def _as_periods(
    predictions: SignalInput, returns: SignalInput
) -> List[Tuple[Sequence[Value], Sequence[Value]]]:
    """归一化为「每期一个 (预测, 收益) 对」的列表。

    扁平输入视为单期；嵌套输入视为多期。
    """
    p_nested = _is_nested(predictions)
    r_nested = _is_nested(returns)
    if p_nested != r_nested:
        raise ValueError("predictions 与 returns 必须同为扁平或嵌套列表")
    if p_nested:
        if len(predictions) != len(returns):
            raise ValueError(f"期数不一致: {len(predictions)} vs {len(returns)}")
        periods = [
            (list(p), list(r))
            for p, r in zip(predictions, returns, strict=True)  # type: ignore[arg-type]
        ]
        for p, r in periods:
            _check_lengths(p, r)
        return periods
    _check_lengths(predictions, returns)
    return [(list(predictions), list(returns))]  # type: ignore[arg-type]


def _period_starts(n: int, period_indices_or_size: Union[int, Sequence[int]]) -> List[int]:
    """把分期参数展开为「每期起始索引」列表。

    - int: 每期固定样本数（最后一期可能不足）；
    - Sequence[int]: 各期起始索引（自动补 0，超出范围的索引被丢弃）。
    """
    if isinstance(period_indices_or_size, int):
        size = period_indices_or_size
        if size <= 0:
            raise ValueError("分期大小必须为正整数")
        return list(range(0, n, size)) or [0]
    starts = sorted({int(s) for s in period_indices_or_size if s is not None})
    starts = [s for s in starts if 0 <= s < n]
    if 0 not in starts:
        starts.insert(0, 0)
    return starts or [0]


def _pearson(xs: Sequence[Value], ys: Sequence[Value]) -> Tuple[float, int]:
    """Pearson 相关系数（成对剔除缺失值后计算）。

    任一序列方差为 0（常数列）时相关系数无定义，按约定返回 0.0。
    返回 (相关系数, 有效样本数)。
    """
    pairs = _clean_pairs(xs, ys)
    n = len(pairs)
    if n < 2:
        return 0.0, n
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxy = sx = sy = 0.0
    for x, y in pairs:
        dx = x - mx
        dy = y - my
        sxy += dx * dy
        sx += dx * dx
        sy += dy * dy
    if sx <= _EPS or sy <= _EPS:
        return 0.0, n
    r = sxy / math.sqrt(sx * sy)
    # 浮点误差保护：|r| 不得超过 1
    return max(-1.0, min(1.0, r)), n


def _ranks(xs: Sequence[float]) -> List[float]:
    """平均秩（1-based，处理并列）。"""
    n = len(xs)
    order = sorted(range(n), key=lambda i: xs[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _spearman(xs: Sequence[Value], ys: Sequence[Value]) -> Tuple[float, int]:
    """Spearman 秩相关系数（平均秩 + Pearson）。"""
    pairs = _clean_pairs(xs, ys)
    n = len(pairs)
    if n < 2:
        return 0.0, n
    rx = _ranks([x for x, _ in pairs])
    ry = _ranks([y for _, y in pairs])
    return _pearson(rx, ry)


def _zscore(xs: Sequence[float]) -> List[float]:
    """标准化（总体标准差）；常数列返回全 0。"""
    n = len(xs)
    if n == 0:
        return []
    mu = sum(xs) / n
    var = sum((x - mu) ** 2 for x in xs) / n
    if var <= _EPS:
        return [0.0] * n
    sd = math.sqrt(var)
    return [(x - mu) / sd for x in xs]


def _sharpe(period_returns: Sequence[float]) -> Optional[float]:
    """夏普比率 = 均值 / 样本标准差；不足 2 期或零方差返回 None。"""
    n = len(period_returns)
    if n < 2:
        return None
    sd = _std(period_returns)
    if sd <= _EPS:
        return None
    return _mean(period_returns) / sd


def _normal_tail_p(t: float) -> float:
    """标准正态双侧尾部概率 P(|Z| > |t|)。"""
    return math.erfc(abs(t) / math.sqrt(2.0))


def _long_short_spread(preds: Sequence[Value], rets: Sequence[Value]) -> float:
    """多空价差：按预测升序排序后，最高 20% 平均收益 - 最低 20% 平均收益。"""
    pairs = _clean_pairs(preds, rets)
    pairs.sort(key=lambda t: t[0])
    n = len(pairs)
    if n < 2:
        return 0.0
    k = max(1, round(n * 0.2))
    k = min(k, n // 2)
    if k == 0:
        return 0.0
    top = [v for _, v in pairs[-k:]]
    bottom = [v for _, v in pairs[:k]]
    return _mean(top) - _mean(bottom)


def _solve_linear_system(
    mat: List[List[float]], rhs: List[float]
) -> Optional[List[float]]:
    """高斯消元（部分主元）解 mat·x = rhs；奇异矩阵返回 None。"""
    n = len(mat)
    aug = [[*row, rhs[i]] for i, row in enumerate(mat)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-14:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for r in range(col + 1, n):
            factor = aug[r][col] / aug[col][col]
            for c in range(col, n + 1):
                aug[r][c] -= factor * aug[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = aug[r][n] - sum(aug[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / aug[r][r]
    return x


def _ols_r2(design: List[List[float]], y: List[float]) -> float:
    """OLS 拟合优度 R²。

    加入极小岭正则（λ 约为 designᵀ·design 对角均值的 1e-10 倍）仅为保证
    设计矩阵病态/共线时数值稳定，不改变 R² 的统计含义；
    目标为常数列（被截距完全解释）时按约定返回 1.0。
    """
    n = len(design)
    p = len(design[0])
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    for row, yi in zip(design, y, strict=True):
        for i in range(p):
            xty[i] += row[i] * yi
            for j in range(p):
                xtx[i][j] += row[i] * row[j]
    trace = sum(xtx[i][i] for i in range(p))
    ridge = 1e-10 * max(1.0, trace / p)
    for i in range(p):
        xtx[i][i] += ridge
    beta = _solve_linear_system(xtx, xty)
    if beta is None:
        return 1.0  # 设计矩阵病态，视为完全共线
    mu = sum(y) / n
    sst = sum((yi - mu) ** 2 for yi in y)
    if sst <= _EPS:
        return 1.0  # 目标为常数列，被截距完全解释
    sse = 0.0
    for row, yi in zip(design, y, strict=True):
        yhat = sum(b * x for b, x in zip(beta, row, strict=True))
        sse += (yi - yhat) ** 2
    r2 = 1.0 - sse / sst
    return max(0.0, min(1.0, r2))


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def compute_ic(predictions: SignalInput, returns: SignalInput) -> Dict[str, float]:
    """Pearson IC。

    支持两种输入：
    - 扁平列表：单个截面（单期），此时 ic_std 恒为 0.0；
    - 嵌套列表（每期一个列表）：逐期计算 IC 后取序列的均值与样本标准差，
      ic_std 是「IC 序列」的标准差，不是收益序列的标准差。

    Returns:
        dict: ``{ic_mean, ic_std, sample_count}``
    """
    periods = _as_periods(predictions, returns)
    ics: List[float] = []
    total_samples = 0
    for p, r in periods:
        ic, n = _pearson(p, r)
        ics.append(ic)
        total_samples += n
    if not ics:
        return {"ic_mean": 0.0, "ic_std": 0.0, "sample_count": 0}
    return {
        "ic_mean": _mean(ics),
        "ic_std": _std(ics),
        "sample_count": total_samples,
    }


def compute_rank_ic(predictions: SignalInput, returns: SignalInput) -> Dict[str, float]:
    """Spearman RankIC。

    与 compute_ic 相同的输入约定；秩采用平均秩（处理并列）。
    rank_ic_std 为各期 RankIC 序列的样本标准差。

    Returns:
        dict: ``{rank_ic_mean, rank_ic_std, sample_count}``
    """
    periods = _as_periods(predictions, returns)
    rank_ics: List[float] = []
    total_samples = 0
    for p, r in periods:
        ric, n = _spearman(p, r)
        rank_ics.append(ric)
        total_samples += n
    if not rank_ics:
        return {"rank_ic_mean": 0.0, "rank_ic_std": 0.0, "sample_count": 0}
    return {
        "rank_ic_mean": _mean(rank_ics),
        "rank_ic_std": _std(rank_ics),
        "sample_count": total_samples,
    }


def compute_period_ic_series(
    predictions: Sequence[Value],
    returns: Sequence[Value],
    period_indices_or_size: Union[int, Sequence[int]],
) -> List[Dict[str, Any]]:
    """按时间分期计算 IC 序列（非重叠、独立分期，不用扩展窗口）。

    Args:
        predictions: 扁平预测序列（跨期拼接）。
        returns: 与 predictions 对齐的收益序列。
        period_indices_or_size:
            - int: 每期固定样本数（最后一期可能不足）；
            - Sequence[int]: 各期起始索引（自动补 0，最后一期延伸到末尾）。

    Returns:
        list[dict]: 每项 ``{period_start, period_end, ic, rank_ic, sample_count}``
    """
    if _is_nested(predictions) or _is_nested(returns):
        raise ValueError(
            "compute_period_ic_series 接受扁平列表；"
            "多期数据请使用 compute_ic / compute_rank_ic 的嵌套形式"
        )
    _check_lengths(predictions, returns)
    n = len(predictions)
    if n == 0:
        return []
    starts = _period_starts(n, period_indices_or_size)
    series: List[Dict[str, Any]] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else n
        p = predictions[start:end]
        r = returns[start:end]
        ic, ic_n = _pearson(p, r)
        rank_ic, _ = _spearman(p, r)
        series.append(
            {
                "period_start": start,
                "period_end": end,
                "ic": ic,
                "rank_ic": rank_ic,
                "sample_count": ic_n,
            }
        )
    return series


def compute_icir(ic_series: Sequence[Value]) -> Dict[str, Any]:
    """基于独立分期 IC 序列计算 ICIR = mean(IC) / std(IC)。

    少于 MIN_IC_PERIODS_FOR_ICIR（12）期独立 IC 时返回
    status=NOT_VERIFIABLE，此时 icir 置 0.0，调用方必须检查 status，
    不得当作有效值使用。

    Returns:
        dict: ``{icir, ic_mean, ic_std, sample_count, status}``
        status 为 Verifiability 枚举的字符串值。
    """
    ics = _clean_series(ic_series)
    n = len(ics)
    mean_ic = _mean(ics)
    std_ic = _std(ics)
    if n < MIN_IC_PERIODS_FOR_ICIR:
        return {
            "icir": 0.0,
            "ic_mean": mean_ic,
            "ic_std": std_ic,
            "sample_count": n,
            "status": Verifiability.NOT_VERIFIABLE.value,
        }
    ir = 0.0 if std_ic <= _EPS else mean_ic / std_ic
    return {
        "icir": ir,
        "ic_mean": mean_ic,
        "ic_std": std_ic,
        "sample_count": n,
        "status": Verifiability.VERIFIED.value,
    }


def compute_newey_west_tstat(
    ic_series: Sequence[Value], max_lags: Optional[int] = None
) -> Dict[str, Any]:
    """Newey-West 调整的 t 统计量（对 IC 序列的时序自相关稳健）。

    自动选择滞后阶数 ``max_lags = floor(4 * (n/100)^(2/9))``
    （Newey & West, 1994 建议）。
    t = mean(IC) / NW_SE，其中
    NW_SE² = (1/n) * [γ₀ + 2 * Σ_{k=1}^{L} (1 - k/(L+1)) * γ_k]，
    γ_k 为滞后 k 的自协方差（1/n 归一）。
    p 值基于标准正态双侧近似。

    Returns:
        dict: ``{t_stat, p_value, se, max_lags, sample_count}``
    """
    ics = _clean_series(ic_series)
    n = len(ics)
    if n < 2:
        return {
            "t_stat": 0.0,
            "p_value": 1.0,
            "se": 0.0,
            "max_lags": 0,
            "sample_count": n,
        }
    if max_lags is None:
        max_lags = math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0))
    max_lags = max(0, min(max_lags, n - 1))
    mu = _mean(ics)
    var_nw = 0.0
    for k in range(max_lags + 1):
        # 滞后 k 自协方差（1/n 归一，与 Newey-West 标准形式一致）
        acov = sum(
            (ics[t] - mu) * (ics[t - k] - mu) for t in range(k, n)
        ) / n
        if k == 0:
            var_nw += acov
        else:
            var_nw += 2.0 * (1.0 - k / (max_lags + 1.0)) * acov
    se = math.sqrt(var_nw / n) if var_nw > 0.0 else 0.0
    t_stat = 0.0 if se <= _EPS else mu / se
    return {
        "t_stat": t_stat,
        "p_value": _normal_tail_p(t_stat),
        "se": se,
        "max_lags": max_lags,
        "sample_count": n,
    }


def compute_block_bootstrap_ci(
    ic_series: Sequence[Value],
    n_bootstraps: int = 2000,
    block_size: Optional[int] = None,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """移动分块（Moving Block）bootstrap 的 IC 均值 95% 置信区间。

    block_size 默认 ``floor(sqrt(n))``；每轮从序列中随机抽取若干
    block_size 长度的连续块拼接至原长度，计算重抽样均值，
    取 2.5% / 97.5% 分位数作为置信区间。分块法保留了 IC 序列的
    时序自相关结构，比 iid 重抽样更保守。

    Args:
        seed: 可选随机种子，保证结果可复现。

    Returns:
        dict: ``{ci_lower_95, ci_upper_95, ic_mean, ic_std_bootstrap}``
        样本不足时置信区间为 None。
    """
    ics = _clean_series(ic_series)
    n = len(ics)
    if n < 2 or n_bootstraps < 2:
        return {
            "ci_lower_95": None,
            "ci_upper_95": None,
            "ic_mean": _mean(ics),
            "ic_std_bootstrap": 0.0,
        }
    if block_size is None:
        block_size = max(1, math.floor(math.sqrt(n)))
    block_size = min(block_size, n)
    # S311 例外：分块重抽样为统计抽样用途，非安全随机
    rng = random.Random(seed)  # noqa: S311
    n_blocks = math.ceil(n / block_size)
    means: List[float] = []
    for _ in range(n_bootstraps):
        sample: List[float] = []
        for _ in range(n_blocks):
            start = rng.randrange(n - block_size + 1)
            sample.extend(ics[start : start + block_size])
        means.append(_mean(sample[:n]))
    means.sort()
    lo_idx = max(0, round(0.025 * len(means)) - 1)
    hi_idx = min(len(means) - 1, round(0.975 * len(means)) - 1)
    return {
        "ci_lower_95": means[lo_idx],
        "ci_upper_95": means[hi_idx],
        "ic_mean": _mean(ics),
        "ic_std_bootstrap": _std(means),
    }


def compute_quantile_monotonicity(
    predictions: Sequence[Value],
    returns: Sequence[Value],
    n_quantiles: int = 5,
) -> Dict[str, Any]:
    """分位数单调性检验。

    按预测值升序将样本等分为 n_quantiles 个分位组，计算每组平均收益；
    检查各组平均收益是否随预测值单调递增。

    monotonicity_score = 满足「非降」的相邻分位对比例（0~1）。
    spread = 最高分位平均收益 - 最低分位平均收益。

    Returns:
        dict: ``{quantile_returns, is_monotonic, monotonicity_score, spread}``
    """
    if n_quantiles < 2:
        raise ValueError("n_quantiles 至少为 2")
    pairs = _clean_pairs(predictions, returns)
    n = len(pairs)
    if n < 2:
        return {
            "quantile_returns": [],
            "is_monotonic": False,
            "monotonicity_score": 0.0,
            "spread": 0.0,
        }
    # 常数列预测：无可区分/排序信息，判定为不单调，spread 记 0.0
    pred_min = min(p for p, _ in pairs)
    pred_max = max(p for p, _ in pairs)
    if pred_max - pred_min <= _EPS:
        group_means = [_mean([v for _, v in pairs])] * n_quantiles
        return {
            "quantile_returns": group_means,
            "is_monotonic": False,
            "monotonicity_score": 0.0,
            "spread": 0.0,
        }
    pairs.sort(key=lambda t: t[0])
    quantile_returns: List[float] = []
    size = n / n_quantiles
    for q in range(n_quantiles):
        lo = int(q * size)
        hi = n if q == n_quantiles - 1 else int((q + 1) * size)
        chunk = pairs[lo:hi]
        if chunk:
            quantile_returns.append(_mean([v for _, v in chunk]))
        else:
            quantile_returns.append(0.0)
    # 单调性：允许 1e-9 量级的浮点容差
    monotonic_pairs = 0.0
    for a, b in itertools.pairwise(quantile_returns):
        if b >= a - 1e-9:
            monotonic_pairs += 1.0
    score = monotonic_pairs / (n_quantiles - 1)
    return {
        "quantile_returns": quantile_returns,
        "is_monotonic": score >= 1.0,
        "monotonicity_score": score,
        "spread": quantile_returns[-1] - quantile_returns[0],
    }


def compute_true_vif(
    factor_values: Dict[str, Sequence[Value]],
) -> Dict[str, float]:
    """真正多元回归 VIF（方差膨胀因子）。

    对每个因子 y，用其余所有因子（含截距）做 OLS 回归，
    VIF(y) = 1 / (1 - R²)。两两相关矩阵的近似不在此实现——
    那会把「与多个因子同时共线」的真实 VIF 系统性低估。

    R² = 1（完全共线，例如 y = a*x + b）时 VIF 为 float("inf")，
    调用方在 JSON 序列化前需自行处理（例如替换为大数或 None）。
    样本数不足以回归（n < 3 或 n <= 因子数）时返回 1.0 并保守对待。

    Returns:
        dict[str, float]: 因子名 -> VIF
    """
    names = list(factor_values.keys())
    if not names:
        return {}
    data = {k: _clean_series(v) for k, v in factor_values.items()}
    n = len(data[names[0]])
    for k in names:
        if len(data[k]) != n:
            raise ValueError(f"因子 {k} 的样本数不一致（期望 {n}，实际 {len(data[k])}）")
    vifs: Dict[str, float] = {}
    for target in names:
        others = [k for k in names if k != target]
        y = data[target]
        if not others:
            # 单因子场景：没有其他因子可回归，VIF 恒为 1
            vifs[target] = 1.0
            continue
        if n < 3 or n <= len(others) + 1:
            # 样本不足以支持回归（含截距共 p+1 个参数）
            vifs[target] = 1.0
            continue
        design = [[1.0] + [data[k][i] for k in others] for i in range(n)]
        r2 = _ols_r2(design, y)
        if r2 >= 1.0 - 1e-10:
            vifs[target] = float("inf")
        else:
            vifs[target] = 1.0 / (1.0 - r2)
    return vifs


def compute_incremental_contribution(
    candidate_predictions: SignalInput,
    candidate_returns: SignalInput,
    existing_predictions: SignalInput,
    existing_returns: SignalInput,
) -> Dict[str, Any]:
    """Leave-one-factor-out 增量贡献评估。

    组合信号 = 0.5 * z(candidate) + 0.5 * z(existing)（等权混合，
    两信号先各自标准化到均值 0 方差 1）。四组序列在相同样本上比较：
    - 缺失值按「四序列同位置全部有效」的规则成对剔除，
      保证所有指标在同一组样本上计算（OOS 差分口径一致）；
    - delta_ic = IC(组合) - IC(已有)；
    - delta_ir = IR(组合) - IR(已有)，IR 为各期 IC 的 mean/std；
    - delta_sharpe = Sharpe(组合) - Sharpe(已有)，Sharpe 基于各期
      多空价差（最高 20% - 最低 20% 平均收益）序列计算。

    Returns:
        dict: ``{delta_sharpe, delta_ic, delta_ir, sample_count, ...}``
        含/不含候选因子的具体指标值一并在返回中给出，便于审计。
        不足 2 期或价差零方差时 Sharpe/IR 为 None，delta 置 0.0。
    """
    cand_periods = _as_periods(candidate_predictions, candidate_returns)
    exist_periods = _as_periods(existing_predictions, existing_returns)
    if len(cand_periods) != len(exist_periods):
        raise ValueError(f"候选与已有模型的期数不一致: {len(cand_periods)} vs {len(exist_periods)}")
    ic_existing: List[float] = []
    ic_combined: List[float] = []
    ic_candidate: List[float] = []
    sharpe_existing: List[float] = []
    sharpe_combined: List[float] = []
    sample_count = 0
    for (cp, cr), (ep, er) in zip(cand_periods, exist_periods, strict=True):
        if not (len(cp) == len(cr) == len(ep) == len(er)):
            raise ValueError(
                "同一期内候选与已有模型的样本数不一致"
                f"({len(cp)}, {len(cr)}, {len(ep)}, {len(er)})"
            )
        # 四序列同位置全部有效才保留（相同样本比较）
        triples = [
            (float(cp_), float(cr_), float(ep_), float(er_))
            for cp_, cr_, ep_, er_ in zip(cp, cr, ep, er, strict=True)
            if cp_ is not None and cr_ is not None
            and ep_ is not None and er_ is not None
        ]
        valid = [(a, b, c, d) for a, b, c, d in triples
                 if all(not (math.isnan(v) or math.isinf(v)) for v in (a, b, c, d))]
        sample_count += len(valid)
        if not valid:
            continue
        cp_v = [t[0] for t in valid]
        cr_v = [t[1] for t in valid]
        ep_v = [t[2] for t in valid]
        er_v = [t[3] for t in valid]
        zc = _zscore(cp_v)
        ze = _zscore(ep_v)
        combined = [(a + b) / 2.0 for a, b in zip(zc, ze, strict=True)]
        ic_candidate.append(_pearson(cp_v, cr_v)[0])
        ic_existing.append(_pearson(ep_v, er_v)[0])
        ic_combined.append(_pearson(combined, er_v)[0])
        sharpe_existing.append(_long_short_spread(ep_v, er_v))
        sharpe_combined.append(_long_short_spread(combined, er_v))
    n_periods = len(ic_existing)
    existing_sharpe = _sharpe(sharpe_existing)
    combined_sharpe = _sharpe(sharpe_combined)
    existing_ir = _sharpe(ic_existing) if n_periods >= 2 else None
    combined_ir = _sharpe(ic_combined) if n_periods >= 2 else None

    def _delta(new: Optional[float], old: Optional[float]) -> float:
        if new is None or old is None:
            return 0.0
        return new - old

    return {
        "delta_sharpe": _delta(combined_sharpe, existing_sharpe),
        "delta_ic": _delta(_mean(ic_combined), _mean(ic_existing)),
        "delta_ir": _delta(combined_ir, existing_ir),
        "sample_count": sample_count,
        "candidate_ic": _mean(ic_candidate),
        "existing_ic": _mean(ic_existing),
        "combined_ic": _mean(ic_combined),
        "existing_sharpe": existing_sharpe,
        "combined_sharpe": combined_sharpe,
        "existing_ir": existing_ir,
        "combined_ir": combined_ir,
    }


def compute_turnover(
    positions_t: Sequence[Value], positions_t_plus_1: Sequence[Value]
) -> float:
    """换手率（单边）= 0.5 * Σ|w_{t+1} - w_t|。

    若输入为权重向量（各期权重和为 1），结果落在 [0, 1]，
    即 t 到 t+1 期间实际调仓的资产权重比例；
    若输入为股数/份额，结果需调用方自行解释。

    Raises:
        ValueError: 两期长度不一致。
    """
    if len(positions_t) != len(positions_t_plus_1):
        raise ValueError(
            f"两期持仓长度不一致: {len(positions_t)} vs {len(positions_t_plus_1)}"
        )
    total = 0.0
    for a, b in zip(positions_t, positions_t_plus_1, strict=True):
        if a is None or b is None:
            continue
        total += abs(float(a) - float(b))
    return 0.5 * total


def compute_hit_rate(predictions: Sequence[Value], returns: Sequence[Value]) -> float:
    """方向命中率：sign(pred) == sign(ret) 的样本占比。

    预测或收益为 0 的样本无方向可比，从分母中排除；
    无有效样本时返回 0.0。
    """
    hits = 0
    valid = 0
    for p, r in _clean_pairs(predictions, returns):
        if p == 0.0 or r == 0.0:
            continue
        valid += 1
        if (p > 0) == (r > 0):
            hits += 1
    return hits / valid if valid else 0.0


def compute_cost_adjusted_metrics(
    returns: Sequence[Value], costs_bps: Union[Value, Sequence[Value]]
) -> Dict[str, Any]:
    """成本调整后指标。

    costs_bps 为每期交易成本，单位是基点（1bp = 1e-4），
    可为标量（每期相同）或与 returns 等长的序列。
    净收益 = 毛收益 - 成本（折算为小数）。

    Returns:
        dict: ``{gross_mean, net_mean, gross_sharpe, net_sharpe,
        cost_impact_bps, total_costs_bps, sample_count}``
        方差为 0 时 Sharpe 按 0.0 处理。
    """
    raw_returns = list(returns)
    if isinstance(costs_bps, (int, float)):
        cost_series = [float(costs_bps) / 10000.0] * len(raw_returns)
    else:
        if len(costs_bps) != len(raw_returns):
            raise ValueError(
                f"成本序列长度不一致: {len(costs_bps)} vs {len(raw_returns)}"
            )
        # 缺失成本按 0 处理，与收益成对清洗
        cost_series = [
            0.0 if c is None else float(c) / 10000.0 for c in costs_bps
        ]
    pairs = _clean_pairs(raw_returns, cost_series)
    clean_rets = [r for r, _ in pairs]
    costs = [c for _, c in pairs]
    net = [g - c for g, c in zip(clean_rets, costs, strict=True)]
    gross_sharpe = _sharpe(clean_rets)
    net_sharpe = _sharpe(net)
    gross_mean = _mean(clean_rets)
    net_mean = _mean(net)
    return {
        "gross_mean": gross_mean,
        "net_mean": net_mean,
        "gross_sharpe": gross_sharpe if gross_sharpe is not None else 0.0,
        "net_sharpe": net_sharpe if net_sharpe is not None else 0.0,
        "cost_impact_bps": (gross_mean - net_mean) * 10000.0,
        "total_costs_bps": sum(costs) * 10000.0,
        "sample_count": len(clean_rets),
    }
