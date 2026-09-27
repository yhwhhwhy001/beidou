"""#14 / #39：资金费对冲书按「价差」一列定价，整条 validate 验证链不必再长一条两腿的路径。

2026-09-27 操作者具名裁定重开（卡片「直接立项」），同日预登记（`docs/RESEARCH_LOG.md`「预登记：资金费
对冲书 carry_hedged」）。钉住的是预登记第 3 项写下的协议，每条对应一句：

1. **价差收益 = 现货收益 − 永续收益**，两种收益口径都成立；空头腿收资金费。`run_backtest` 在价差面板
   上算出来的，就是两条腿分开算再相加的结果。
2. **现货缺 bar 时永续腿是裸的**：现货按最后成交价计价，缺掉的那段涨跌落在它重新成交的那根 bar 上。
   记 0 是偏乐观的读法，这里不做。
3. **每天只在 00:00 UTC 开盘那根 bar 上决策**，进出场的 bar 里没有结算（D-034 的归属规则）。严格大于
   门槛；决策 bar 上没有现货价就不进；预热期是 NaN，回测从第一个决策开始。
4. **入选标的等名义，名义固定为每条腿权益的 2/3**；成员表与上市时长只在决策 bar 上读。
5. **因果**：改动决策时刻之后的数据，不改之前的任何分数与权重。
6. **只有 validate 能给它定价**：`AlphaModel` 拒绝它，validate 要求 `--no-exits`。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from click.testing import CliRunner

from beidou_alpha.backtest import CostModel, asset_returns, run_backtest
from beidou_alpha.hedged import HEDGED_NOTIONAL, hedged_weights, spread_panel
from beidou_alpha.model import AlphaModel
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams
from beidou_alpha.registry import StrategyEntry
from beidou_alpha.signals.carry_hedged import CarryHedgedParams, carry_hedged_scores
from beidou_cli import main
from beidou_data.spot import SpotMapping, write_spot_map
from beidou_data.store import SPOT_KLINE_KIND, FundingStore, KlineStore

ROOT = Path(__file__).resolve().parents[2]
BARS = pd.date_range("2024-01-01", periods=24 * 12, freq="h", tz="UTC")
SYMBOLS = ("AAAUSDT", "BBBUSDT", "CCCUSDT")


def _panel(funding: pd.DataFrame | None = None, spot_close: pd.DataFrame | None = None, seed: int = 7) -> Panel:
    rng = np.random.default_rng(seed)
    frames, spot_open, spot_closes = {}, {}, {}
    for symbol in SYMBOLS:
        close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, len(BARS))))
        opened = close * np.exp(rng.normal(0, 0.002, len(BARS)))
        frames[symbol] = pd.DataFrame(
            {"open": opened, "high": np.maximum(opened, close), "low": np.minimum(opened, close), "close": close,
             "volume": 1_000.0},
            index=BARS,
        )  # fmt: skip
        spot_closes[symbol] = close * np.exp(rng.normal(0, 0.001, len(BARS)))
        spot_open[symbol] = opened * np.exp(rng.normal(0, 0.001, len(BARS)))
    spot = {
        "open": pd.DataFrame(spot_open, index=BARS),
        "close": pd.DataFrame(spot_closes, index=BARS) if spot_close is None else spot_close,
    }
    return Panel.from_frames(frames, "1h", funding=funding, spot=spot)


def _funding(rate_per_settlement: dict[str, float]) -> pd.DataFrame:
    settlements = BARS[BARS.hour % 8 == 0]
    return pd.DataFrame({s: rate_per_settlement[s] for s in SYMBOLS}, index=settlements)


# --- 1. the spread ----------------------------------------------------------------------------------


@pytest.mark.parametrize("execution", ["close_to_close", "open_to_close"])
def test_the_spread_return_is_spot_minus_perpetual_under_both_conventions(execution: str) -> None:
    panel = _panel()
    spread = spread_panel(panel)
    spot_close, spot_open = panel.spot_field("close"), panel.spot_field("open")
    assert spot_close is not None and spot_open is not None
    if execution == "close_to_close":
        expected = spot_close.pct_change() - panel.close.pct_change()
    else:
        expected = (spot_close / spot_open - 1.0) - (panel.close / panel.open - 1.0)
    got = asset_returns(spread, execution)  # type: ignore[arg-type]
    np.testing.assert_allclose(got.iloc[1:].to_numpy(), expected.iloc[1:].to_numpy(), rtol=0, atol=1e-12)


def test_pricing_the_spread_equals_pricing_the_two_legs_and_the_short_leg_is_paid() -> None:
    funding = _funding({"AAAUSDT": 0.0001, "BBBUSDT": -0.0002, "CCCUSDT": 0.0})
    panel = _panel(funding=funding)
    weights = pd.DataFrame(0.2, index=BARS, columns=list(SYMBOLS))
    cost = CostModel(turnover_bps=19.0, use_funding=True)
    hedged = run_backtest(spread_panel(panel), weights, cost, execution="close_to_close").portfolio_net

    executed = weights.shift(1).iloc[1:]
    spot_close = panel.spot_field("close")
    assert spot_close is not None and panel.funding is not None
    legs = executed * (spot_close.pct_change() - panel.close.pct_change()).iloc[1:]
    paid = executed * panel.funding.iloc[1:]  # the short perpetual RECEIVES positive funding
    turnover = executed.diff().abs()
    turnover.iloc[0] = executed.iloc[0].abs()
    by_hand = (legs + paid - turnover * 19.0 / 10_000.0).sum(axis=1)
    np.testing.assert_allclose(hedged.to_numpy(), by_hand.to_numpy(), rtol=0, atol=1e-12)
    assert (paid["AAAUSDT"] > 0).sum() == len(BARS[BARS.hour % 8 == 0]) - 1


# --- 2. a spot bar goes missing while the hedge is on ------------------------------------------------


def test_a_missing_spot_bar_leaves_the_perpetual_leg_naked_until_spot_trades_again() -> None:
    base = _panel()
    spot_close = base.spot_field("close")
    assert spot_close is not None
    holed = spot_close.copy()
    holed.loc[BARS[100:103], "AAAUSDT"] = np.nan
    panel = _panel(spot_close=holed)
    got = asset_returns(spread_panel(panel), "close_to_close")["AAAUSDT"]
    perp = panel.close["AAAUSDT"].pct_change()
    for bar in BARS[100:103]:
        assert got[bar] == pytest.approx(-perp[bar], abs=1e-12), "the spot leg is marked at its last trade"
    jump = holed["AAAUSDT"][BARS[103]] / holed["AAAUSDT"][BARS[99]] - 1.0
    assert got[BARS[103]] == pytest.approx(jump - perp[BARS[103]], abs=1e-12)


# --- 3. the signal ---------------------------------------------------------------------------------


def test_the_signal_decides_once_a_day_after_the_midnight_settlement_and_strictly_above_the_line() -> None:
    funding = _funding({"AAAUSDT": 0.0001, "BBBUSDT": 0.0001, "CCCUSDT": -0.0001})
    params = CarryHedgedParams(lookback_days=2, threshold_per_day=0.0003)
    scores = carry_hedged_scores(_panel(funding=funding), params)

    assert scores.iloc[: 2 * 24 - 1].isna().all().all(), "warm-up is NaN, so the backtest starts at a decision"
    noisy = funding + np.random.default_rng(5).normal(0, 0.0003, size=funding.shape)
    moving = carry_hedged_scores(_panel(funding=noisy), CarryHedgedParams(lookback_days=1))
    changes = moving.ne(moving.shift(1)) & moving.notna() & moving.shift(1).notna()
    assert changes.to_numpy().sum() > 0 and set(pd.DatetimeIndex(moving.index[changes.any(axis=1)]).hour) == {0}
    decided = scores.loc[BARS[BARS.hour == 0][3]]
    # 0.0001 x 3 settlements a day is exactly 0.0003: not strictly above the line, so not held
    assert decided.to_dict() == {"AAAUSDT": 0.0, "BBBUSDT": 0.0, "CCCUSDT": 0.0}

    looser = carry_hedged_scores(_panel(funding=funding), CarryHedgedParams(lookback_days=2))
    assert looser.loc[BARS[BARS.hour == 0][3]].to_dict() == {"AAAUSDT": 1.0, "BBBUSDT": 1.0, "CCCUSDT": 0.0}


def test_no_price_on_either_leg_on_the_decision_bar_means_no_entry() -> None:
    """A spot hole, or a perpetual with no bar - delisted, halted - and the symbol is not held.

    Without the perpetual half a delisted symbol would keep qualifying for L days on the funding it paid
    before it stopped trading, and the book would hold a leg that cannot be priced or closed.
    """
    funding = _funding({"AAAUSDT": 0.0002, "BBBUSDT": 0.0002, "CCCUSDT": 0.0002})
    spot_close = _panel().spot_field("close")
    assert spot_close is not None
    holed = spot_close.copy()
    day = BARS[BARS.hour == 0][4]
    holed.loc[day, "BBBUSDT"] = np.nan
    panel = _panel(funding=funding, spot_close=holed)
    panel.close.loc[day, "CCCUSDT"] = np.nan
    scores = carry_hedged_scores(panel, CarryHedgedParams(lookback_days=2))
    assert scores.loc[day].to_dict() == {"AAAUSDT": 1.0, "BBBUSDT": 0.0, "CCCUSDT": 0.0}

    held = pd.DataFrame(1.0, index=BARS, columns=list(SYMBOLS))
    weights = hedged_weights(held, panel.close, None, min_history_bars=0, decision_hour_utc=0)
    assert weights.loc[day].to_dict() == pytest.approx(
        {"AAAUSDT": HEDGED_NOTIONAL / 2, "BBBUSDT": HEDGED_NOTIONAL / 2, "CCCUSDT": 0.0}
    )


# --- 4. the weights --------------------------------------------------------------------------------


def test_the_book_is_equal_notional_and_reads_membership_only_on_the_decision_bar() -> None:
    funding = _funding({"AAAUSDT": 0.0002, "BBBUSDT": 0.0002, "CCCUSDT": 0.0002})
    panel = _panel(funding=funding)
    scores = carry_hedged_scores(panel, CarryHedgedParams(lookback_days=2))
    membership = pd.DataFrame(True, index=BARS, columns=list(SYMBOLS))
    membership.loc[BARS[BARS.hour >= 5], "CCCUSDT"] = False  # leaves the pool mid-day, every day
    weights = hedged_weights(scores, panel.close, membership, min_history_bars=0, decision_hour_utc=0)

    decision = BARS[BARS.hour == 0][4]
    assert weights.loc[decision].to_dict() == pytest.approx(dict.fromkeys(SYMBOLS, HEDGED_NOTIONAL / 3))
    later = decision + pd.Timedelta(hours=10)
    assert weights.loc[later].to_dict() == weights.loc[decision].to_dict(), "held until the next decision"
    assert weights.iloc[: 2 * 24 - 1].isna().all().all()
    assert weights.dropna().sum(axis=1).max() == pytest.approx(HEDGED_NOTIONAL)


# --- 5. causality -----------------------------------------------------------------------------------


def test_nothing_after_a_decision_changes_it() -> None:
    """Everything after a decision bar is replaced by something that would flip it, and it does not flip.

    Funding is replaced at EVERY bar, not only at the next settlement: that one is eight bars on, so a
    signal reading one bar ahead would see nothing different and pass.  The spot leg goes missing too,
    because the signal refuses to enter without it.
    """
    funding = _funding({"AAAUSDT": 0.0002, "BBBUSDT": 0.00005, "CCCUSDT": 0.0001}).reindex(BARS, fill_value=0.0)
    cut = BARS[BARS.hour == 0][6]
    later_funding = funding.copy()
    later_funding.loc[later_funding.index > cut] = -0.01
    spot_close = _panel().spot_field("close")
    assert spot_close is not None
    later_spot = spot_close.copy()
    later_spot.loc[later_spot.index > cut] = np.nan
    params = CarryHedgedParams(lookback_days=2)
    before = carry_hedged_scores(_panel(funding=funding), params)
    after = carry_hedged_scores(_panel(funding=later_funding, spot_close=later_spot), params)
    assert before.loc[cut].eq(1.0).all(), "the decision on the cut holds every symbol, so a flip would show"
    assert after.loc[cut + pd.Timedelta(days=1) :].eq(0.0).all().all(), "and it does flip the next decision"
    pd.testing.assert_frame_equal(before.loc[:cut], after.loc[:cut])


# --- 6. only validate prices it ---------------------------------------------------------------------


def test_a_model_holding_one_weight_per_perpetual_refuses_it() -> None:
    with pytest.raises(ValueError, match="two-leg book"):
        AlphaModel(entries=(StrategyEntry(id="carry_hedged", params={}),), portfolio=PortfolioParams(), interval="1h")


def _hedged_root(august_dir: Path, root: Path) -> list[str]:
    symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
    perp, spot = KlineStore(root), KlineStore(root, kind=SPOT_KLINE_KIND)
    for index, symbol in enumerate(symbols):
        frame = pd.read_parquet(august_dir / symbol / "1h.parquet")
        frame["close_time"] = frame["open_time"] + 3_600_000 - 1
        perp.append(symbol, "1h", frame)
        discounted = frame.copy()
        for column in ("open", "high", "low", "close"):
            discounted[column] = discounted[column] * (1.0 - 0.0005 * (index + 1))
        spot.append(symbol, "1h", discounted)
        times = frame["open_time"][frame["open_time"] % (8 * 3_600_000) == 0].to_numpy() + 3
        FundingStore(root).append(
            symbol,
            pd.DataFrame(
                {"funding_time": times, "funding_rate": 0.0001 * (index - 1), "mark_price": frame["close"].iloc[0]}
            ),
        )
    write_spot_map(root, {symbol: SpotMapping(symbol, symbol, 1.0) for symbol in symbols})
    return symbols


def _validate(root: Path, out: Path, symbols: list[str], *extra: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        main,
        [
            "research", "validate", "--strategy", "carry_hedged",
            "--root", str(root), "--symbols", ",".join(symbols), "--out", str(out),
            "--costs", str(ROOT / "config" / "costs.carry_hedged.yaml"),
            "--grid", json.dumps({"lookback_days": [1, 2], "threshold_per_day": [0.0, 0.0003]}), "--charge", "4",
            "--folds", "2", "--min-train", "300", "--purge", "5", "--cpcv-groups", "3", "--min-history", "0",
            *extra,
        ],
    )  # fmt: skip
    return result.exit_code, str(result.output) + str(result.exception)


def test_validate_prices_the_spread_and_charges_its_own_bucket(
    tmp_path: Path, august_dir: Path, isolated_trials_ledger: Path
) -> None:
    root, out = tmp_path / "data", tmp_path / "reports"
    symbols = _hedged_root(august_dir, root)

    code, output = _validate(root, out, symbols)
    assert code != 0 and "pass --no-exits" in output, output

    code, output = _validate(root, out, symbols, "--no-exits")
    assert code == 0, output
    report = json.loads(next(out.glob("carry_hedged-validation-*.json")).read_text(encoding="utf-8"))
    assert report["portfolio"]["book"] == "hedged_spread"
    assert report["portfolio"]["notional_per_leg"] == pytest.approx(HEDGED_NOTIONAL)
    assert report["layers"]["band"] == "none" and report["exits"] is None
    assert report["costs"]["turnover_bps"] == pytest.approx(19.0)
    assert report["grid_size"] == 4
    rows = [json.loads(line) for line in isolated_trials_ledger.read_text(encoding="utf-8").splitlines()]
    assert [row["strategy"] for row in rows] == ["carry_hedged"] * 4
