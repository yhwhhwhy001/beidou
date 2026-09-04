"""M-002/M-005/M-006/M-008/M-010/M-014: the daily report measures what the plan said it measures."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from beidou_live.probe import ProbeParams
from beidou_live.reports import (
    daily_payload,
    drift_check,
    evidence_window,
    exit_and_pool_events,
    income_drift,
    leg_split,
    probe_correlation,
)
from beidou_live.state import StateStore

HOUR = 3_600_000
BASE = 1_788_000_000_000  # a round bar open time inside 2026-09


def _store(tmp_path: Path, cycles: list[dict], attributions: list[dict] | None = None) -> StateStore:
    store = StateStore(tmp_path / "live")
    for row in cycles:
        store.append_cycle(row)
    for row in attributions or []:
        store.append_attribution(row)
    return store


def _cycle(i: int, *, construction: str = "aaa", equity: float = 10_000.0, **extra: object) -> dict:
    return {"bar_open_ms": BASE + i * HOUR, "bar": f"bar-{i}", "equity": equity, "construction": construction, **extra}


def test_evidence_window_starts_at_the_current_construction(tmp_path: Path) -> None:
    """Changing the band or a signal parameter resets what the live record is evidence of."""
    cycles = [_cycle(i, construction="old") for i in range(5)] + [_cycle(i, construction="new") for i in range(5, 9)]
    window = evidence_window(_store(tmp_path, cycles))
    assert window["construction"] == "new" and window["bars"] == 4
    assert window["since_ms"] == BASE + 5 * HOUR
    assert window["changes_7d"] == 1, "one change inside the trailing week"


def test_evidence_window_counts_every_promotion_in_the_week(tmp_path: Path) -> None:
    """The plan allowed one promotion per week and nothing counted them; two landed on 2026-09-04."""
    cycles = [_cycle(0, construction="a"), _cycle(1, construction="b"), _cycle(2, construction="c")]
    assert evidence_window(_store(tmp_path, cycles))["changes_7d"] == 2


def test_evidence_window_is_empty_without_fingerprints(tmp_path: Path) -> None:
    window = evidence_window(_store(tmp_path, [{"bar_open_ms": BASE, "equity": 1.0}]))
    assert window["construction"] is None and window["bars"] == 0


def test_income_drift_is_per_strategy_and_uses_income_not_equity(tmp_path: Path) -> None:
    """M-010: equity here is multi-asset collateral and one number for two books; income is neither."""
    attributions = [{"bar_open_ms": BASE + i * HOUR, "by_strategy": {"tsmom": 1.0, "flow": -0.5}} for i in range(60)]
    store = _store(tmp_path, [_cycle(i) for i in range(60)], attributions)
    result = income_drift(store, {"tsmom": {"oos_sharpe": 1.5}}, equity=10_000.0, since_ms=None)
    rows = result["by_strategy"]
    assert set(rows) == {"tsmom", "flow"}, "each strategy is measured on its own"
    assert rows["tsmom"]["bars"] == 60 and rows["tsmom"]["pnl"] == pytest.approx(60.0)
    assert rows["flow"]["expected_sharpe"] is None, "a strategy without an expectation is reported, not judged"
    assert income_drift(store, {}, equity=None, since_ms=None)["status"] == "INSUFFICIENT_DATA"
    # a constant series has no dispersion, so no Sharpe: the metric refuses rather than inventing one
    assert rows["tsmom"]["realised_sharpe"] is None


def test_income_drift_alerts_when_a_strategy_falls_two_standard_errors_short(tmp_path: Path) -> None:
    rng = __import__("numpy").random.default_rng(3)
    values = list(rng.normal(-0.002, 0.01, 24 * 40))
    attributions = [
        {"bar_open_ms": BASE + i * HOUR, "by_strategy": {"tsmom": v * 10_000}} for i, v in enumerate(values)
    ]
    store = _store(tmp_path, [_cycle(i) for i in range(len(values))], attributions)
    result = income_drift(store, {"tsmom": {"oos_sharpe": 1.6}}, equity=10_000.0, since_ms=None)
    assert result["status"] == "ALERT"
    assert result["by_strategy"]["tsmom"]["z"] < -2.0


def test_income_drift_respects_the_evidence_window(tmp_path: Path) -> None:
    attributions = [{"bar_open_ms": BASE + i * HOUR, "by_strategy": {"tsmom": 1.0}} for i in range(100)]
    store = _store(tmp_path, [_cycle(i) for i in range(100)], attributions)
    assert income_drift(store, {}, equity=10_000.0, since_ms=BASE + 90 * HOUR)["by_strategy"]["tsmom"]["bars"] == 10


def test_leg_split_uses_the_sign_of_the_target_that_bar(tmp_path: Path) -> None:
    """M-008: the backtest earned more on the short leg; live has to be able to say whether it does too."""
    cycles = [_cycle(0, targets={"A": 0.05, "B": -0.03, "C": 0.0})]
    attributions = [
        {
            "bar_open_ms": BASE,
            "by_symbol": {"A": {"total": 2.0}, "B": {"total": 5.0}, "C": {"total": -1.0}},
        }
    ]
    result = leg_split(_store(tmp_path, cycles, attributions), since_ms=None, equity=10_000.0)
    assert result["pnl"] == {"long": 2.0, "short": 5.0, "flat": -1.0}
    assert result["symbol_bars"] == {"long": 1, "short": 1, "flat": 1}
    assert result["pnl_pct"]["short"] == pytest.approx(0.0005)


def test_probe_correlation_exposes_a_sleeve_that_is_really_a_tilt(tmp_path: Path) -> None:
    """91% of the flow sleeve's positions stacked on tsmom's shorts in the backtest (round 5)."""
    attributions = [
        {"bar_open_ms": BASE + i * HOUR, "by_strategy": {"tsmom": float(i % 7) - 3.0, "flow": float(i % 7) - 3.0}}
        for i in range(30)
    ]
    store = _store(tmp_path, [_cycle(i) for i in range(30)], attributions)
    probes = (ProbeParams(book="flow_short", strategy="flow"),)
    result = probe_correlation(store, probes, since_ms=None)
    assert result["flow~tsmom"]["correlation"] == pytest.approx(1.0), "a perfect tilt reads as correlation 1"
    assert result["flow~tsmom"]["bars"] == 30
    thin = probe_correlation(_store(tmp_path / "b", [_cycle(0)], attributions[:3]), probes, since_ms=None)
    assert thin["flow~tsmom"]["correlation"] is None


def test_exit_and_pool_events_reach_the_report(tmp_path: Path) -> None:
    """Both lived only in cycles.jsonl before; the daily report never mentioned either."""
    day = "2026-09-04"
    rows = [
        {
            "bar_open_ms": BASE,
            "at": f"{day}T01:00:00+00:00",
            "bar": "b0",
            "equity": 1.0,
            "exit_events": [{"symbol": "A", "rule": "TAKE_PROFIT"}, {"symbol": "B", "rule": "TAKE_PROFIT"}],
            "universe_update": {"entered": ["NEW"], "left": ["OLD"]},
        }
    ]
    result = exit_and_pool_events(_store(tmp_path, rows), _day_of_bar(BASE))
    assert result["exit_count"] == 2 and result["by_rule"] == {"TAKE_PROFIT": 2}
    assert result["pool_entered"] == ["NEW"] and result["pool_left"] == ["OLD"] and result["pool_changes"] == 2


def _day_of_bar(bar_ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(bar_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def test_drift_check_skips_bars_whose_clock_jumped(tmp_path: Path) -> None:
    """M-012 promised it; the bars were recorded but still counted as returns."""
    clean = [_cycle(i, equity=10_000.0 + i) for i in range(60)]
    jumped = [
        *clean[:30],
        _cycle(30, equity=99_999.0, clock={"jumped": True}),
        *[_cycle(i, equity=10_000.0 + i) for i in range(31, 60)],
    ]
    expectations = {"tsmom": {"oos_sharpe": 1.5, "full_sample_max_drawdown": -0.13}}
    without = drift_check(_store(tmp_path / "a", clean), expectations)
    with_jump = drift_check(_store(tmp_path / "b", jumped), expectations)
    assert without["status"] != "INSUFFICIENT_DATA"
    assert with_jump["trailing_drawdown"] == pytest.approx(without["trailing_drawdown"], abs=1e-9)


def test_daily_payload_carries_every_new_section(tmp_path: Path) -> None:
    day = _day_of_bar(BASE)
    cycles = [
        {**_cycle(i), "at": f"{day}T0{i}:00:00+00:00", "targets": {"A": 0.1}, "exit_events": [], "universe_update": {}}
        for i in range(3)
    ]
    attributions = [
        {
            "bar_open_ms": BASE + i * HOUR,
            "at": f"{day}T0{i}:00:00+00:00",
            "by_strategy": {"tsmom": 1.0},
            "by_symbol": {"A": {"total": 1.0}},
        }
        for i in range(3)
    ]
    payload = daily_payload(_store(tmp_path, cycles, attributions), day, {"tsmom": {"oos_sharpe": 1.6}})
    for key in ("evidence_window", "income_drift", "legs", "probe_correlation", "events"):
        assert key in payload, key
    assert payload["legs"]["pnl"]["long"] == pytest.approx(3.0)
    assert json.dumps(payload, default=str)
    assert math.isfinite(payload["legs"]["pnl"]["long"])
