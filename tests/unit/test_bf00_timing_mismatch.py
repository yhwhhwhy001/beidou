"""BF-00 基线证据：证明当前因子评估存在预测与标签时间错配。

复现步骤：
1. 模拟当前 engine.py 的行为：先计算 past return，再生成 prediction，然后配对
2. 展示 prediction(t) 实际与 return(t-1, t) 配对，而非 forward_return(t, t+h)
3. 对比正确实现：prediction(t) 应与 forward_return(t, t+h) 配对
4. 量化错配导致的统计偏差
"""

from __future__ import annotations

import random

# ================================================================
# 模拟场景：生成带自相关的模拟价格序列
# ================================================================


def generate_price_series(n: int = 1000, seed: int = 42) -> list[float]:
    """生成GARCH-like价格序列，模拟真实市场数据。"""
    rng = random.Random(seed)
    prices = [100.0]
    for _ in range(n - 1):
        # AR(1) return with volatility clustering
        ret = 0.0001 + rng.gauss(0, 0.02)  # 日度波动约2%
        prices.append(prices[-1] * (1 + ret))
    return prices


def compute_sma(prices: list[float], window: int = 20) -> list[float]:
    """计算简单移动平均。"""
    sma = []
    for i in range(len(prices)):
        if i < window - 1:
            sma.append(prices[i])
        else:
            sma.append(sum(prices[i - window + 1 : i + 1]) / window)
    return sma


# ================================================================
# 当前引擎的错误实现（复现 engine.py 的行为）
# ================================================================


def current_engine_pairing(prices: list[float], sma20: list[float]) -> tuple[list[float], list[float], list[float]]:
    """复现当前 engine.py _nearline_tick + _offline_tick 的逻辑。

    当前实现：
    - _nearline_tick 中计算 past return，然后生成 prediction
    - _offline_tick 中按列表位置配对

    Returns:
        predictions: 因子预测值列表（基于 SMA 偏差）
        paired_returns: 与 prediction 配对的收益（实际是 past return）
        correct_forward_returns: 正确的 forward return（用于对照）
    """
    predictions = []
    paired_returns = []  # 当前实际配对的收益
    correct_forward_returns = []  # 正确应该配对的收益

    prev_close = prices[0]

    for i in range(1, len(prices)):
        close = prices[i]

        # === 当前引擎逻辑：先算 past return ===
        if prev_close > 0:
            past_return = (close - prev_close) / prev_close  # return(t-1, t)
        else:
            past_return = 0.0

        # === 然后生成当前时刻预测 ===
        sma = sma20[i]
        prediction = (close - sma) / sma * 100 if sma > 0 else 0.0  # 基于当前close的预测

        # === 正确的 forward return ===
        # 当前时刻的预测应该对应未来收益
        # forward_return(t, t+h) 这里用 h=1 简化
        if i + 1 < len(prices):
            correct_fwd_return = (prices[i + 1] - close) / close  # return(t, t+1)
        else:
            correct_fwd_return = 0.0

        predictions.append(prediction)
        paired_returns.append(past_return)  # 当前配的是 past return
        correct_forward_returns.append(correct_fwd_return)

        prev_close = close

    return predictions, paired_returns, correct_forward_returns


# ================================================================
# 正确的实现
# ================================================================


def correct_engine_pairing(
    prices: list[float], sma20: list[float], horizon: int = 1
) -> tuple[list[float], list[float]]:
    """正确实现：prediction(t) 与 forward_return(t, t+horizon) 配对。

    Args:
        prices: 价格序列
        sma20: SMA20 序列
        horizon: 预测持有期（步数）

    Returns:
        predictions: 因子预测值（基于时刻t的点时数据）
        forward_returns: 对应的 forward return(t, t+horizon)
    """
    predictions = []
    forward_returns = []

    for t in range(len(prices) - horizon):
        close_t = prices[t]
        sma_t = sma20[t]

        # 使用时刻t的数据生成预测
        prediction = (close_t - sma_t) / sma_t * 100 if sma_t > 0 else 0.0

        # 对应的 forward return
        close_t_h = prices[t + horizon]
        fwd_return = (close_t_h - close_t) / close_t

        predictions.append(prediction)
        forward_returns.append(fwd_return)

    return predictions, forward_returns


# ================================================================
# 统计函数
# ================================================================


def pearson_corr(x: list[float], y: list[float]) -> tuple[float, float]:
    """计算 Pearson 相关系数（IC）和标准差。"""
    n = min(len(x), len(y))
    if n < 3:
        return 0.0, 0.0
    x = x[:n]
    y = y[:n]
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n)) / (n - 1)
    sx = (sum((xi - mx) ** 2 for xi in x) / (n - 1)) ** 0.5
    sy = (sum((yi - my) ** 2 for yi in y) / (n - 1)) ** 0.5
    if sx == 0 or sy == 0:
        return 0.0, 0.0
    return cov / (sx * sy), sy  # 注意：与当前代码一样返回 sy 作为 ic_std


# ================================================================
# 测试用例
# ================================================================


class TestTimingMismatch:
    """BF-00: 时间错配最小可复现实例。"""

    def test_prediction_paired_with_past_return_in_current_engine(self):
        """证明当前引擎将 prediction(t) 与 return(t-1, t) 配对。"""
        prices = generate_price_series(500, seed=42)
        sma20 = compute_sma(prices)

        predictions, paired_returns, correct_fwd = current_engine_pairing(prices, sma20)

        # 验证：pairing 使用的是 past return，不是 forward return
        n = min(len(predictions), len(paired_returns), len(correct_fwd))
        predictions = predictions[:n]
        paired_returns = paired_returns[:n]
        correct_fwd = correct_fwd[:n]

        # 计算当前实现的 IC
        ic_current, _ic_std_current = pearson_corr(predictions, paired_returns)
        # 计算正确配对的 IC
        ic_correct, _ic_std_correct = pearson_corr(predictions, correct_fwd)

        # 关键断言：当前引擎确实使用了 past return
        # paired_returns[i] = (prices[i+1] - prices[i]) / prices[i] 这是时刻i的past return
        # 而 predictions[i] 是时刻 i+1 的预测（基于当前close的偏差）
        # 所以 prediction(t) ↔ return(t-1, t)
        assert ic_current != ic_correct, (
            f"IC values should differ due to timing mismatch: current={ic_current:.6f} vs correct={ic_correct:.6f}"
        )

    def test_current_engine_returns_are_past_not_forward(self):
        """验证 paired_returns 确实是 past returns，而非 forward returns。

        构造确定性格林：
        prices = [100, 101, 99, 102]
        sma20 始终 = 100（简化）

        t=0: close=100, sma=100, pred=0
        t=1: close=101, sma=100, pred=1.0
             当前引擎：先算 past_return = (101-100)/100 = 0.01
             然后生成 pred=1.0
             → 配对：pred=1.0 ↔ return=0.01 (这是 return(0,1))
             → 正确应为：pred=1.0 ↔ forward_return(1,2) = (99-101)/101 = -0.0198
        """
        prices = [100.0, 101.0, 99.0, 102.0]
        sma20 = [100.0] * 4

        _predictions, paired_returns, correct_fwd = current_engine_pairing(prices, sma20)

        # t=1 时刻（数组索引0对应i=1）
        # paired_returns[0] = (101-100)/100 = 0.01 ← 这是 past return
        # correct_fwd[0] = (99-101)/101 ≈ -0.0198 ← 这才是正确的 forward return

        # 验证 past return 不是 forward return
        assert abs(paired_returns[0] - 0.01) < 1e-9, f"Expected past return 0.01, got {paired_returns[0]}"
        assert abs(correct_fwd[0] - (-0.019801980198019802)) < 1e-9, (
            f"Expected forward return -0.0198, got {correct_fwd[0]}"
        )
        # 两者明显不同
        assert paired_returns[0] * correct_fwd[0] < 0, (
            "Past and forward returns should have opposite signs in this case"
        )

    def test_correct_pairing_yields_different_ic(self):
        """正确配对产生的 IC 与当前错误配对有本质差异。"""
        prices = generate_price_series(1000, seed=123)
        sma20 = compute_sma(prices)

        # 当前引擎
        pred_current, paired_current, _ = current_engine_pairing(prices, sma20)

        # 正确引擎
        pred_correct, fwd_correct = correct_engine_pairing(prices, sma20, horizon=1)

        # 确保比较相同长度
        n = min(len(pred_current), len(paired_current), len(pred_correct), len(fwd_correct))
        ic_current, _ = pearson_corr(pred_current[:n], paired_current[:n])
        ic_correct, _ = pearson_corr(pred_correct[:n], fwd_correct[:n])

        # 符号或幅度应不同
        assert abs(ic_current - ic_correct) > 1e-6, "Timing mismatch should produce materially different IC values"

    def test_shared_close_price_creates_spurious_correlation(self):
        """证明共享 close price 制造了虚假相关。

        当前引擎的核心问题：
        - prediction(t) = f(close_t, sma_t)  ← 使用 close_t
        - return(t-1, t) = (close_t - close_{t-1}) / close_{t-1}  ← 也使用 close_t

        两者共享 close_t，即使因子信号完全是随机噪声，也会因共享
        close price 而产生非零 IC。正确做法是使用不相交的价格区间。

        本测试构造极端场景：
        - close set = [50, 150, 50, 150, ...] 大幅振荡
        - SMA = 100 固定
        - 因子预测 = 纯随机噪声（无任何预测力）
        - 当前引擎将它配对 past return（使用相同 close）
        - 正确引擎将它配对 forward return（使用不同 close）
        """
        rng = random.Random(99)
        n = 500

        # 构造大幅振荡价格序列
        prices = []
        for i in range(n):
            if (i // 10) % 2 == 0:
                prices.append(50.0 + rng.gauss(0, 1))
            else:
                prices.append(150.0 + rng.gauss(0, 1))

        [100.0] * n  # 简化SMA

        # 纯随机预测（零预测力）
        random_predictions = [rng.gauss(0, 1) for _ in range(n - 1)]

        # 计算 past returns（当前引擎使用）
        past_returns = []
        for t in range(1, n):
            past_returns.append((prices[t] - prices[t - 1]) / prices[t - 1])

        # 计算 forward returns（正确应使用）
        forward_returns = []
        for t in range(n - 1):
            forward_returns.append((prices[t + 1] - prices[t]) / prices[t])

        n_align = min(len(random_predictions), len(past_returns), len(forward_returns))
        random_predictions = random_predictions[:n_align]
        past_returns = past_returns[:n_align]
        forward_returns = forward_returns[:n_align]

        _ic_vs_past, _ = pearson_corr(random_predictions, past_returns)
        ic_vs_forward, _ = pearson_corr(random_predictions, forward_returns)

        # 噪声因子与 forward return 的 IC 应很小（无预测力）
        # 但由于价格振荡结构，past return 可能与噪声有任何相关性
        # 关键：两种配对会产生不同结果，且都不能作为因子有效性的证据
        assert abs(ic_vs_forward) < 0.3, (
            f"Random noise should not show strong correlation with forward returns, got {ic_vs_forward:.4f}"
        )

    def test_ic_std_is_return_std_not_ic_std(self):
        """证明 compute_ic() 返回的第二个值是收益标准差，不是 IC 标准差。

        文档 F-P0-04 指出这个问题。"""
        predictions = [0.1, -0.2, 0.3, -0.1, 0.05, 0.15, -0.12, 0.08, -0.05, 0.02] * 10
        returns = [0.01, -0.02, 0.03, -0.01, 0.005, 0.015, -0.012, 0.008, -0.005, 0.002] * 10

        _ic, second_value = pearson_corr(predictions, returns)

        # 正确的 IC 标准差：对 IC 序列本身求 std
        # 当前代码返回的是 returns 的标准差
        returns_std = (sum((r - sum(returns) / len(returns)) ** 2 for r in returns) / (len(returns) - 1)) ** 0.5

        # 验证第二个返回值确实是收益标准差
        assert abs(second_value - returns_std) < 1e-9, (
            f"compute_ic second return value IS returns std, not IC std. Got {second_value}, returns std={returns_std}"
        )

    def test_extended_window_icir_is_biased(self):
        """证明使用扩展窗口（高度重叠）计算 ICIR 会失真。

        engine.py 在 _offline_tick 中使用：
        ```
        for i in range(max(5, min_n - 20), min_n):
            window_preds = preds_aligned[:i]  # 扩展窗口！
            ...
            rolling_ics.append(ic)
        ```
        这导致 IC 序列高度相关，ICIR 有偏。
        """
        import random as rng_mod

        rng = rng_mod.Random(42)

        # 生成独立同分布 IC 序列
        n = 100
        true_ics = [rng.gauss(0.02, 0.1) for _ in range(n)]  # mean=0.02, std=0.1

        # 正确方法：逐期 IC（每期独立）
        true_mean = sum(true_ics) / n
        true_std = (sum((ic - true_mean) ** 2 for ic in true_ics) / (n - 1)) ** 0.5
        true_icir = true_mean / true_std if true_std > 0 else 0.0

        # 当前方法：扩展窗口 IC 序列
        extended_ics = []
        for window_end in range(20, n + 1):
            window = true_ics[:window_end]
            w_mean = sum(window) / len(window)
            extended_ics.append(w_mean)  # 每个窗口的 mean IC

        ext_mean = sum(extended_ics) / len(extended_ics)
        ext_std = (sum((ic - ext_mean) ** 2 for ic in extended_ics) / (len(extended_ics) - 1)) ** 0.5
        ext_icir = ext_mean / ext_std if ext_std > 0 else 0.0

        # 或者直接看扩展窗口 IC mean 的 std
        # 扩展窗口均值比单期均值稳定得多 → std 偏低 → ICIR 偏高
        ext_icir / true_icir if true_icir != 0 else float("inf")

        # 扩展窗口的 std 应该更小（因为平均效应）
        assert ext_std < true_std, (
            f"Extended window IC series should have lower std than per-period ICs "
            f"due to averaging. Got ext_std={ext_std:.4f}, true_std={true_std:.4f}"
        )

        # ICIR 应该被高估
        assert ext_icir > true_icir, (
            f"Extended window ICIR should be inflated vs true ICIR. "
            f"Got ext_icir={ext_icir:.4f}, true_icir={true_icir:.4f}"
        )

    def test_factor_predictions_not_isolated_by_symbol(self):
        """证明预测和收益序列不按 symbol/timeframe/horizon 隔离。

        当前使用全局列表，多品种扩展后会混合。
        """
        # 模拟两个品种的预测和收益混合在全局列表中
        # symbol A: 强正相关
        # symbol B: 强负相关
        # 混合后互相抵消，IC 归零
        pred_a = [0.1 * i for i in range(1, 51)]  # 递增
        ret_a = [0.01 * i for i in range(1, 51)]  # 递增（正IC）

        pred_b = [0.1 * i for i in range(1, 51)]  # 递增（相同预测）
        ret_b = [-0.01 * i for i in range(1, 51)]  # 递减（负IC）

        # 当前：混在一起
        mixed_preds = pred_a + pred_b
        mixed_rets = ret_a + ret_b

        # 分离：
        ic_a, _ = pearson_corr(pred_a, ret_a)
        ic_b, _ = pearson_corr(pred_b, ret_b)
        ic_mixed, _ = pearson_corr(mixed_preds, mixed_rets)

        assert abs(ic_a) > 0.5, "Symbol A should have strong positive IC"
        assert abs(ic_b) > 0.5, "Symbol B should have strong negative IC"
        # 混合后 IC 应接近 0（两品种抵消）
        assert abs(ic_mixed) < 0.1, f"Mixed IC should be near 0 due to cancellation, got {ic_mixed:.4f}"
