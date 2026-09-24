"""#6.4 / #6.9: the multi-factor loadings read only what each bar knew, and read the book D-045 reads.

Four properties, each one a way the reading could be wrong while still printing plausible numbers:

- **No look-ahead in the sorts.**  Everything after a cutoff bar is shuffled - klines, funding and the
  universe the later cycles recorded - and every sort key up to and including the cutoff is compared
  bit for bit, the ranking with it.  A loader that hands the sort one bar of the future is caught at
  the cutoff, which is where a one-bar leak is visible and nowhere else.
- **Each sort points the way its label says.**  A sign flip would invert a loading's meaning silently.
- **The regression reads the book D-045 reads.**  With the dummy switched off, the market-only fit is
  `report beta`'s conditional beta on the same state, to the last bit the tolerance allows.
- **Planted loadings come back.**  A book built as a known mix of the factors, net exposure on the
  market and BTC, gross on the sorts, and an alpha only on bars where the signal held a short.

The cycle rows copy their field names and nesting from the live record, not from memory: the row with
`bar_open_ms` 1790269200000 (`at` 2026-09-24T18:00:30+00:00) for a traded cycle, the ERROR shape of
the one with `bar_open_ms` 1788843600000 for a failed one, and the attribution row for the operator's
2026-09-10T04:00Z flatten (`foreign` with `by_symbol`, `reconciled`, `rows`, `total`).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from click.testing import CliRunner

import beidou_live.report_beta as report_beta
from beidou_cli import main
from beidou_data.store import FundingStore, KlineStore
from beidou_live.factor_loadings import (
    FACTORS,
    _thirds,
    archive_funding,
    archive_history,
    factor_returns,
    sort_keys,
)
from beidou_live.reports import daily_alerts, daily_markdown, daily_payload, market_beta
from beidou_live.state import StateStore
from tests.alpha.test_causality import _bit_for_bit

HOUR = 3_600_000
START = 1_786_000_000_000 - 1_786_000_000_000 % HOUR
HISTORY = 720  # thirty days before the window, so every key has its whole window from the first bar
WINDOW = 260  # above 4 x NW_LAGS, so no regression runs a clamped kernel
SYMBOLS = ("BTCUSDT", "ETHUSDT", "AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT", "FFFUSDT", "GGGUSDT")
JOINS, JOINS_AT = "GGGUSDT", 60  # enters the universe here: before it, it is in no sort
LEAVES, LEAVES_AT = "FFFUSDT", 150  # leaves the universe here
FAILED_AT = 40  # a failed cycle: no usdt_equity, so its bar is not in the record and a step spans two hours
FOREIGN_AT = 90  # the operator's own fills land on this bar (D-032)
CLOSES_FROM = 120  # cycle rows carry `closes` from here on, as the live record does
EXPOSURE_STEP_AT = 130  # the loop's exposure rises here (a `vol_target` move)
ALL_LONG_FROM = 170  # from here the signal holds no short
PLANTED = {"market": 1.0, "btc": 0.3, "size": -0.4, "low_vol": 0.6, "funding": 0.2}
ALPHA = 2e-4  # per hour, on bars where the signal held a short
DAY = "2026-09-16"  # the window's last UTC day (it runs 2026-09-05T07:00Z to 09-16T02:00Z)


def _archive_frames(seed: int = 11) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Klines and funding for nine names with a common market, three different orderings and noise on each.

    Turnover, volatility and funding rank the names in three different orders, so the three sorts pick
    different legs and the factors are not copies of each other.
    """
    rng = np.random.default_rng(seed)
    hours = HISTORY + WINDOW
    stamps = START + HOUR * np.arange(hours, dtype="int64")
    shock = rng.normal(0.0, 0.004, hours)
    size_order, funding_order = (2, 0, 5, 1, 7, 3, 8, 4, 6), (6, 3, 0, 8, 1, 5, 2, 7, 4)
    klines, funding = {}, {}
    for i, symbol in enumerate(SYMBOLS):
        rets = (0.6 + 0.15 * i) * shock + rng.normal(0.0, 0.001 + 0.0008 * i, hours)
        close = 100.0 * np.exp(np.cumsum(rets))
        turnover = 1e9 / (1 + size_order[i]) * rng.uniform(0.6, 1.4, hours)
        klines[symbol] = pd.DataFrame(
            {
                "open_time": stamps,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": turnover / close,
                "close_time": stamps + HOUR - 1,
                "quote_volume": turnover,
                "trades": np.zeros(hours, dtype="int64"),
                "taker_buy_base": np.zeros(hours),
                "taker_buy_quote": np.zeros(hours),
            }
        )
        settled = stamps[::8] + 5  # a few ms after the hour, as the venue stamps them
        funding[symbol] = pd.DataFrame(
            {
                "funding_time": settled,
                "funding_rate": rng.normal(1e-4 * (funding_order[i] - 4), 6e-5, len(settled)),
                "mark_price": close[::8],
            }
        )
    return klines, funding


def _universe_at(i: int) -> list[str]:
    return [s for s in SYMBOLS if not (s == JOINS and i < JOINS_AT) and not (s == LEAVES and i >= LEAVES_AT)]


def _bars() -> list[int]:
    return [START + (HISTORY + i) * HOUR for i in range(WINDOW)]


def _closes(klines: Mapping[str, pd.DataFrame]) -> dict[str, dict[int, float]]:
    return {s: dict(zip(f["open_time"].astype(int), f["close"].astype(float), strict=True)) for s, f in klines.items()}


def _planted_factors(klines: Mapping[str, pd.DataFrame], funding: Mapping[str, pd.DataFrame]) -> dict[str, Any]:
    """The factor returns the reading will build, over the bars it will see (the failed one is not one)."""
    bars = [bar for i, bar in enumerate(_bars()) if i != FAILED_AT]
    universe = {bar: _universe_at(i) for i, bar in enumerate(_bars()) if i != FAILED_AT}
    keys = sort_keys(bars, universe, klines.__getitem__, funding.__getitem__)
    return factor_returns(bars, universe, _closes(klines), keys)["series"]


def _row(
    i: int, bar: int, usdt: float, exposure: tuple[float, float], short_until: float, klines: Mapping[str, pd.DataFrame]
) -> dict[str, Any]:
    """A traded cycle, named and nested as the live record's 2026-09-24T17:00Z row is.

    `exposure` is (net, gross) on the USDT line.  `targets` are fractions of TOTAL equity, which is
    1.25x the USDT line here, so they are divided by 1.25 and `series_from_cycles` multiplies it back.
    """
    net, gross = exposure
    held = _universe_at(i)
    long_each, short = (net + gross) / 4.0, (gross - net) / 2.0  # two longs and one short
    row: dict[str, Any] = {
        "as_of_ms": bar,
        "at": datetime.fromtimestamp((bar + HOUR + 30_000) / 1000, UTC).isoformat(),
        "bar": datetime.fromtimestamp(bar / 1000, UTC).isoformat(),
        "bar_open_ms": bar,
        # total equity carries collateral, so exposure is rescaled onto the USDT line (A-GB01)
        "collateral": {"collateral": usdt * 0.25, "equity": usdt * 1.25, "share": 0.2, "usdt_equity": usdt},
        "contributions": {
            "flow": {},
            "tsmom": {s: (-1.0 if s == "CCCUSDT" and i < short_until else 1.0) for s in held},
        },
        "dry_run": False,
        "equity": usdt * 1.25,
        "exit_events": [],
        "guard_reasons": [],
        "orders": [],
        "skip": False,
        "targets": {"BTCUSDT": long_each / 1.25, "ETHUSDT": long_each / 1.25, "CCCUSDT": -short / 1.25},
        "universe": held,
    }
    if i >= CLOSES_FROM:
        row["closes"] = {s: float(klines[s]["close"].iloc[HISTORY + i]) for s in held}
    return row


def _state(directory: Path, *, all_long_from: int | None = ALL_LONG_FROM, alpha: float = ALPHA) -> StateStore:
    """A book whose USDT line moves by the planted loadings on the reading's own factors, plus a little noise."""
    klines, funding = _archive_frames()
    factors = _planted_factors(klines, funding)
    rng = np.random.default_rng(5)
    store = StateStore(directory)
    usdt, step = 10_000.0, 0
    previous: tuple[float, float, bool] | None = None
    for i, bar in enumerate(_bars()):
        net, gross = (0.9, 1.1) if i < EXPOSURE_STEP_AT else (1.6, 2.0)
        if i == FAILED_AT:
            # The live record's ERROR shape: no collateral, so the bar is not a bar that happened.
            store.append_cycle(
                {
                    "at": datetime.fromtimestamp((bar + HOUR + 47_000) / 1000, UTC).isoformat(),
                    "bar": datetime.fromtimestamp(bar / 1000, UTC).isoformat(),
                    "bar_open_ms": bar,
                    "consecutive_errors": 1,
                    "dry_run": False,
                    "error": "ProxyError: 503 Service Unavailable",
                    "orders": [],
                    "phase": "ERROR",
                    "targets": {},
                }
            )
            continue
        if previous is not None:
            carried_net, carried_gross, distinct = previous
            planted = sum(
                PLANTED[name]
                * (carried_net if name in ("market", "btc") else carried_gross)
                * float(factors[name][step])
                for name in FACTORS
            )
            usdt *= math.exp(planted + (alpha if distinct else 0.0) + rng.normal(0.0, 1e-4))
            step += 1
        short_until = math.inf if all_long_from is None else all_long_from
        store.append_cycle(_row(i, bar, usdt, (net, gross), short_until, klines))
        previous = (net, gross, i < short_until)
    store.append_attribution(
        {
            "at": datetime.fromtimestamp((_bars()[FOREIGN_AT] + 2 * HOUR) / 1000, UTC).isoformat(),
            "bar_open_ms": _bars()[FOREIGN_AT],
            "by_strategy": {},
            "by_symbol": {},
            "foreign": {"by_symbol": {}, "reconciled": True, "rows": 3, "total": 268.82565908},
        }
    )
    return store


def _data_root(root: Path) -> Path:
    klines, funding = _archive_frames()
    for symbol in SYMBOLS:
        KlineStore(root).append(symbol, "1h", klines[symbol])
        FundingStore(root).append(symbol, funding[symbol])
    return root


def test_shuffling_everything_after_t_leaves_every_key_and_ranking_up_to_t_bit_for_bit() -> None:
    """Klines, funding and later cycles' universes all shuffled past the cutoff; nothing up to it moves."""
    klines, funding = _archive_frames()
    bars = _bars()
    universe = {bar: _universe_at(i) for i, bar in enumerate(bars)}
    cutoff = bars[100]
    before = sort_keys(bars, universe, klines.__getitem__, funding.__getitem__)

    rng = np.random.default_rng(3)
    shuffled_klines, shuffled_funding = {}, {}
    for symbol in SYMBOLS:
        frame = klines[symbol].copy()
        later = frame["open_time"] > cutoff  # kline cutoff + 1h is the first one that closes after bar t does
        for column in ("close", "quote_volume"):
            values = frame.loc[later, column].to_numpy().copy()
            rng.shuffle(values)
            frame.loc[later, column] = values * rng.uniform(0.5, 1.5, len(values))
        shuffled_klines[symbol] = frame
        events = funding[symbol].copy()
        unsettled = events["funding_time"] >= cutoff + HOUR  # not settled by bar t's close
        rates = events.loc[unsettled, "funding_rate"].to_numpy().copy()
        rng.shuffle(rates)
        events.loc[unsettled, "funding_rate"] = rates * rng.uniform(-3.0, 3.0, len(rates))
        shuffled_funding[symbol] = events
    reshuffled = {
        bar: (names if bar <= cutoff else list(rng.permutation(SYMBOLS)[:5])) for bar, names in universe.items()
    }
    after = sort_keys(bars, reshuffled, shuffled_klines.__getitem__, shuffled_funding.__getitem__)

    for key in ("turnover", "volatility", "funding"):
        _bit_for_bit(before[key].loc[:cutoff], after[key].loc[:cutoff])
        # Not a comparison of two things the shuffle never reached: every key moves after the cutoff.
        assert not before[key].loc[cutoff + HOUR :].equals(after[key].loc[cutoff + HOUR :]), key
        for bar in (b for b in bars if b <= cutoff):
            row_before = {s: float(v) for s, v in before[key].loc[bar].items()}
            row_after = {s: float(v) for s, v in after[key].loc[bar].items()}
            assert _thirds(row_before) == _thirds(row_after), (key, bar)
    # The late entrant is in no sort before it joins: a panel's symbols would have ranked it all along.
    assert before["turnover"][JOINS].loc[: bars[JOINS_AT - 1]].isna().all()
    assert before["turnover"][JOINS].loc[bars[JOINS_AT] :].notna().all()
    assert before["volatility"][LEAVES].loc[bars[LEAVES_AT] :].isna().all()


def test_a_loader_that_hands_the_sort_one_bar_of_the_future_is_caught_at_the_cutoff() -> None:
    """The control: kline t+1 relabelled as t.  Only the cutoff bar can see it, and the comparison does."""
    klines, funding = _archive_frames()
    bars = _bars()
    universe = {bar: _universe_at(i) for i, bar in enumerate(bars)}
    cutoff = bars[100]

    def leaky(frames: Mapping[str, pd.DataFrame]) -> Any:
        return lambda symbol: frames[symbol].assign(open_time=frames[symbol]["open_time"] - HOUR)

    shuffled = {}
    for symbol in SYMBOLS:
        frame = klines[symbol].copy()
        later = frame["open_time"] > cutoff
        frame.loc[later, "quote_volume"] = frame.loc[later, "quote_volume"].to_numpy()[::-1] * 3.0
        shuffled[symbol] = frame
    before = sort_keys(bars, universe, leaky(klines), funding.__getitem__)["turnover"]
    after = sort_keys(bars, universe, leaky(shuffled), funding.__getitem__)["turnover"]

    _bit_for_bit(before.loc[: cutoff - HOUR], after.loc[: cutoff - HOUR])
    with pytest.raises(AssertionError):
        _bit_for_bit(before.loc[:cutoff], after.loc[:cutoff])


def test_each_sort_points_the_way_its_label_says() -> None:
    """Six names, keys set by hand: size is big minus small, low_vol low minus high, funding high minus low."""
    bars = [START, START + HOUR]
    names = [f"N{i}USDT" for i in range(6)]
    universe = dict.fromkeys(bars, names)
    rising = {s: {bars[0]: 100.0, bars[1]: 100.0 * (1.0 + 0.01 * i)} for i, s in enumerate(names)}  # N5 +5%
    ascending = pd.DataFrame([list(range(6)), list(range(6))], index=bars, columns=names, dtype=float)
    keys = {"turnover": ascending, "volatility": ascending, "funding": ascending}

    series = factor_returns(bars, universe, rising, keys)["series"]

    top, bottom = (0.05 + 0.04) / 2, (0.00 + 0.01) / 2  # N5, N4 against N0, N1
    assert series["size"][0] == pytest.approx(math.log1p(top - bottom), rel=1e-12), "high turnover long"
    assert series["low_vol"][0] == pytest.approx(math.log1p(bottom - top), rel=1e-12), "low volatility long"
    assert series["funding"][0] == pytest.approx(math.log1p(top - bottom), rel=1e-12), "high funding long"
    assert series["market"][0] == pytest.approx(math.log1p(sum(0.01 * i for i in range(6)) / 6), rel=1e-12)
    assert _thirds({"A": 1.0, "B": 1.0, "C": 1.0}) == (["C"], ["A"]), "ties break by symbol, deterministically"


def test_with_the_dummy_off_the_market_only_fit_is_report_betas_conditional_beta(
    tmp_path: Path,
) -> None:
    """The same book, the same basket, the same exposure, the same NW: D-045's number, not a neighbour of it."""
    store = _state(tmp_path / "live", all_long_from=None)
    data_root = _data_root(tmp_path / "data")

    block = report_beta.factor_loadings(store, root=data_root)
    d045 = market_beta(store, root=data_root)["decomposition"]

    regression = block["regression"]
    assert regression["all_long_bars"] == 0, "every bar held a short, so there is no dummy"
    assert regression["bars"] == d045["bars"] == WINDOW - 3, "one failed bar, one D-032 bar, and the first"
    assert regression["market_only"]["loading"] == pytest.approx(d045["conditional"]["beta"], rel=1e-12)
    assert regression["market_only"]["t"] == pytest.approx(d045["conditional"]["beta_t"], rel=1e-12)
    assert regression["market_only"]["r2"] == pytest.approx(d045["conditional"]["r2"], rel=1e-12)


def test_planted_loadings_come_back_and_alpha_is_read_only_where_the_signal_held_a_short(tmp_path: Path) -> None:
    store = _state(tmp_path / "live")
    data_root = _data_root(tmp_path / "data")

    block = report_beta.factor_loadings(store, root=data_root)

    regression = block["regression"]
    assert regression["measured"] is True
    assert regression["excluded_bars"] == 1 and regression["bars"] == WINDOW - 3
    assert regression["factor_missing_bars"] == dict.fromkeys(FACTORS, 0) and regression["short_of_factors_bars"] == 0
    fit = regression["conditional"]
    for name, planted in PLANTED.items():
        assert fit["loadings"][name]["loading"] == pytest.approx(planted, abs=0.02), name
    # Steps whose entry bar held a short: the window's first ALL_LONG_FROM bars, less the failed bar
    # (not in the record) and the step into the D-032 bar (not the book's).
    assert fit["alpha_bars"] == ALL_LONG_FROM - 2
    assert regression["all_long_bars"] == regression["bars"] - fit["alpha_bars"]
    assert fit["alpha_bps_per_hour"] == pytest.approx(ALPHA * 1e4, abs=0.5)
    assert fit["nw_lags"] == 48 and fit["nw_covers_intended_horizon"] is True
    assert fit["r2"] > 0.99 > regression["market_only"]["r2"], "the sorts explain what the market alone cannot"
    # The unconditioned fit cannot see the exposure step, so it misses the plant by more.
    constant = regression["constant"]["loadings"]
    assert abs(constant["market"]["loading"] - 1.0) > abs(fit["loadings"]["market"]["loading"] - 1.0)
    assert set(fit["collinearity"]["vif"]) == set(FACTORS) and all(
        v >= 1.0 for v in fit["collinearity"]["vif"].values()
    )


def test_every_bar_all_long_means_no_alpha_is_printed(tmp_path: Path) -> None:
    store = _state(tmp_path / "live", all_long_from=0)
    block = report_beta.factor_loadings(store, root=_data_root(tmp_path / "data"))

    fit = block["regression"]["conditional"]
    assert fit["alpha_bps_per_hour"] is None and fit["alpha_t"] is None and fit["alpha_annualised"] is None
    assert "constant_long" in fit["alpha_why"]
    assert "不谈" in report_beta._factor_loadings_lines(block)["alpha"]


def _profile(tmp_path: Path) -> Path:
    (tmp_path / "registry.yaml").write_text("strategies: []\n", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"paths:\n  state_dir: {tmp_path / 'live'}\n  reports_dir: {tmp_path / 'reports'}\n"
        f"registry: {tmp_path / 'registry.yaml'}\n",
        encoding="utf-8",
    )
    return profile


def test_the_daily_block_is_report_betas_factor_page_to_the_bit(tmp_path: Path) -> None:
    """Both commands end to end on one state: one computation, so one JSON."""
    _state(tmp_path / "live")
    data_root = _data_root(tmp_path / "data")
    common = ["--profile", str(_profile(tmp_path)), "--data-root", str(data_root)]
    runner = CliRunner()

    page_run = runner.invoke(main, ["report", "beta", *common, "--out", str(tmp_path / "beta")])
    assert page_run.exit_code == 0, page_run.output
    daily_run = runner.invoke(main, ["report", "daily", *common, "--date", DAY, "--out", str(tmp_path / "daily")])
    assert daily_run.exit_code == 0, daily_run.output

    (page_path,) = (tmp_path / "beta").glob("factors-*.json")
    page = json.loads(page_path.read_text(encoding="utf-8"))
    daily = json.loads((tmp_path / "daily" / f"{DAY}.json").read_text(encoding="utf-8"))
    assert json.dumps(daily["factor_loadings"], sort_keys=True) == json.dumps(page, sort_keys=True)
    assert page["regression"]["measured"] is True
    (beta_path,) = (tmp_path / "beta").glob("beta-*.json")
    beta_page = json.loads(beta_path.read_text(encoding="utf-8"))
    assert json.dumps(beta_page, sort_keys=True) == json.dumps(daily["beta"], sort_keys=True), (
        "beta's own file is untouched"
    )

    markdown = (tmp_path / "daily" / f"{DAY}.md").read_text(encoding="utf-8")
    section = markdown.split("## Factor loadings")[1].split("\n## ")[0]
    loading = page["regression"]["conditional"]["loadings"]["low_vol"]
    assert f"| 低波（低减高） | {loading['loading']:.2f}（t {loading['t']:.2f}） |" in section
    assert markdown.index("## Market beta") < markdown.index("## Factor loadings") < markdown.index("## Probe")
    assert "# Factor loadings (#6.4 / #6.9)" in page_run.output


def test_a_block_that_cannot_be_computed_says_why_and_the_rest_of_the_report_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _state(tmp_path / "live")
    data_root = _data_root(tmp_path / "data")
    healthy = daily_payload(store, DAY, data_root=data_root)

    def broken(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("the funding archive answered with something that is not a rate")

    monkeypatch.setattr(report_beta, "factor_reading", broken)  # where the block calls it
    payload = daily_payload(StateStore(tmp_path / "live"), DAY, data_root=data_root)

    assert payload["factor_loadings"] == {
        "measured": False,
        "reason": "RuntimeError: the funding archive answered with something that is not a rate",
    }
    assert healthy["factor_loadings"]["regression"]["measured"] is True, "the control half read a number"
    assert set(payload) == set(healthy)
    text, control = daily_markdown(payload), daily_markdown(healthy)
    assert [h for h in text.splitlines() if h.startswith("## ")] == [
        h for h in control.splitlines() if h.startswith("## ")
    ]
    assert "RuntimeError: the funding archive answered" in text
    assert daily_alerts(payload) == daily_alerts(healthy), "the block reports; it never pages"


def test_the_archive_adapters_read_the_stores_layout(tmp_path: Path) -> None:
    """Three kline columns and two funding columns, from where the stores put them; a missing file raises."""
    data_root = _data_root(tmp_path / "data")
    frame = archive_history(data_root, "1h")("BTCUSDT")
    events = archive_funding(data_root)("BTCUSDT")

    assert list(frame.columns) == ["open_time", "close", "quote_volume"] and len(frame) == HISTORY + WINDOW
    assert list(events.columns) == ["funding_time", "funding_rate"]
    with pytest.raises(FileNotFoundError):
        archive_funding(data_root)("NOTLISTEDUSDT")
