from beidou_research.backtest.replay import simulate_paper_window


def _trend_data(n: int = 600) -> tuple[list[float], list[float]]:
    closes = [100.0]
    for _i in range(1, n):
        closes.append(closes[-1] * (1 + 0.001))  # 单调上涨
    factor = [1.0] * n  # 恒定做多
    return factor, closes


def test_insufficient_window_returns_none() -> None:
    assert simulate_paper_window([1.0] * 100, [100.0] * 100, cost_bps=5.0) is None


def test_trend_following_profitable_and_consistency_high() -> None:
    factor, closes = _trend_data()
    result = simulate_paper_window(factor, closes, cost_bps=5.0)
    assert result is not None
    assert result.paper_sharpe > 0
    assert result.paper_drawdown_pct <= 0.0
    assert result.signal_consistency > 0.9
    assert result.evidence_source == "historical_replay"
    assert result.window_bars == 600


def test_costs_reduce_returns() -> None:
    factor, closes = _trend_data()
    cheap = simulate_paper_window(factor, closes, cost_bps=0.0)
    expensive = simulate_paper_window(factor, closes, cost_bps=200.0)
    assert cheap is not None and expensive is not None
    assert expensive.paper_sharpe < cheap.paper_sharpe


def test_flat_signal_is_zero_position() -> None:
    factor = [0.0] * 600
    _, closes = _trend_data()
    result = simulate_paper_window(factor, closes, cost_bps=5.0)
    assert result is not None and result.paper_sharpe == 0.0 and result.n_trades == 0
