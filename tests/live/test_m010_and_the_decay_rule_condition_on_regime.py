"""D-049（操作者 2026-10-01「现在就条件化」）：M-010 与衰减规则按 regime 取期望。

backtest-guard 09-30（Ⅲ.1）：在位 tsmom 证据的样本外 Sharpe 按篮子 30 天波动分三段，读数是 2.97 / 1.44 / 0.91。
而 M-010 与衰减规则都拿不分段的数比，于是波动大的一个月读成衰减，平静的一个月读成健康。这里钉住四件事：

1. **实盘的 regime 状态就是研究的那个量**：同一个归档，同两个函数（`benchmark_returns` + `trailing_benchmark_vol`），
   成员是循环每根 bar 记下的 universe。这里按定义逐根独立重算来比。
2. **M-010 的期望是证据三段按实盘 bar 占比的混合**。Sharpe 不能按占比相加，均值与二阶矩可以。
3. **衰减规则的每个整窗取起点那根 bar 所在段的 q10**，与证据给窗口归档的方式相同。
4. **条件化不了的行说清原因**，并回到不分段的数，读数与改动前相同。
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from beidou_data.store import KlineStore
from beidou_live.composition import load_registry
from beidou_live.reports import (
    _decay_alerts,
    _decay_lines,
    _regime_note,
    daily_alerts,
    decay_watch,
    expectations_from_evidence,
    income_drift,
    regime_state,
)
from beidou_live.state import StateStore

ROOT = Path(__file__).resolve().parents[2]
HOUR = 3_600_000
BASE = int(datetime(2026, 10, 3, 6, tzinfo=UTC).timestamp() * 1000)
EQUITY = 5_000.0
LOW, MID, HIGH = 0.55, 0.80, 1.40  # one state inside each tercile below
SPLIT: dict[str, Any] = {
    "low": {"bars": 10, "sharpe": 3.0, "vol_from": 0.40, "vol_to": 0.70, "mean_annual": 0.60, "vol_annual": 0.20,
            "windows": 21, "window_sharpe_q10": 0.50},
    "mid": {"bars": 10, "sharpe": 1.5, "vol_from": 0.71, "vol_to": 0.88, "mean_annual": 0.33, "vol_annual": 0.22,
            "windows": 21, "window_sharpe_q10": -1.00},
    "high": {"bars": 10, "sharpe": 0.8, "vol_from": 0.89, "vol_to": 2.20, "mean_annual": 0.20, "vol_annual": 0.25,
             "windows": 21, "window_sharpe_q10": -3.00},
}  # fmt: skip
PROMISED = {
    "oos_sharpe": 1.83,
    "oos_window_sharpe_q10": -1.85,
    "regime_split": SPLIT,
    "regime_execution": "open_to_close",
}
#: The shape every validate report had before D-049: Sharpe, bars and edges, no moments and no windows.
OLD_SPLIT = {name: {key: row[key] for key in ("bars", "sharpe", "vol_from", "vol_to")} for name, row in SPLIT.items()}


def _store(tmp_path: Path, returns: list[float]) -> StateStore:
    store = StateStore(tmp_path / "live")
    cycles, attributions = [], []
    for i, value in enumerate(returns):
        bar = BASE + i * HOUR
        at = datetime.fromtimestamp((bar + HOUR) / 1000 + 25, tz=UTC).isoformat()
        cycles.append({"at": at, "bar_open_ms": bar, "equity": EQUITY, "construction": "0a82ff40868d"})
        attributions.append({"at": at, "bar_open_ms": bar, "by_strategy": {"tsmom": value * EQUITY}})
    store.cycles_path.write_text("".join(json.dumps(row) + "\n" for row in cycles), encoding="utf-8")
    store.attribution_path.write_text("".join(json.dumps(row) + "\n" for row in attributions), encoding="utf-8")
    return store


def _state(states: list[float], *, execution: str = "open_to_close") -> dict[str, Any]:
    by_bar = {BASE + i * HOUR: value for i, value in enumerate(states)}
    return {"execution": execution, "by_bar": by_bar, "through_ms": max(by_bar), "latest": states[-1]}


def _window(sharpe: float, bars: int) -> list[float]:
    """`bars` returns whose annualised Sharpe is exactly `sharpe` (+-s around a mean, alternating)."""
    spread = 1e-3
    mean = sharpe * spread * math.sqrt(bars / (bars - 1)) / math.sqrt(8760.0)
    return [mean + spread * (1 if i % 2 == 0 else -1) for i in range(bars)]


def _mix(shares: dict[str, float]) -> float:
    """The closed form, written out again: the mixture's mean over the root of its second moment less the mean squared."""
    mean = sum(share * SPLIT[name]["mean_annual"] / 8760.0 for name, share in shares.items())
    second = sum(
        share * (SPLIT[name]["vol_annual"] ** 2 / 8760.0 + (SPLIT[name]["mean_annual"] / 8760.0) ** 2)
        for name, share in shares.items()
    )
    return mean / math.sqrt(second - mean**2) * math.sqrt(8760.0)


# --- 1. the state is research's own quantity ---------------------------------------------------------


def test_the_live_state_is_researchs_quantity_over_the_loops_universe(tmp_path: Path, august_dir: Path) -> None:
    symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
    root = tmp_path / "data"
    frames = {}
    for symbol in symbols:
        frame = pd.read_parquet(august_dir / symbol / "1h.parquet")
        frame["close_time"] = frame["open_time"] + HOUR - 1
        KlineStore(root).append(symbol, "1h", frame)
        frames[symbol] = frame.set_index("open_time")
    bars = sorted(frames["BTCUSDT"].index)
    # The loop records a universe on each cycle; here only now and then, and none for the first 48 bars.
    recorded = {bars[48]: symbols[:3], bars[300]: [symbols[0], symbols[1], symbols[3]], bars[500]: symbols[1:]}
    store = StateStore(tmp_path / "live")
    store.cycles_path.write_text(
        "".join(json.dumps({"bar_open_ms": int(bar), "universe": names}) + "\n" for bar, names in recorded.items()),
        encoding="utf-8",
    )

    state = regime_state(store, root)

    # Independently, by the definition: each bar's members are the newest recorded universe at or before it; the
    # basket is their mean close/open - 1; the state at t is the std (ddof 1) of the basket over the 720 bars
    # before t, read once 180 of them exist, annualised by sqrt(8760).
    members = [None] * len(bars)
    for i, bar in enumerate(bars):
        known = [names for at, names in recorded.items() if at <= bar]
        members[i] = known[-1] if known else None
    basket = np.array(
        [
            np.nan
            if names is None
            else np.nanmean([frames[s].loc[bar, "close"] / frames[s].loc[bar, "open"] - 1.0 for s in names])
            for bar, names in zip(bars, members, strict=True)
        ]
    )
    expected = {}
    for i, bar in enumerate(bars):
        past = basket[max(0, i - 720) : i]
        past = past[np.isfinite(past)]
        if past.size >= 180:
            expected[int(bar)] = float(np.std(past, ddof=1) * math.sqrt(8760.0))
    assert "error" not in state, state
    assert sorted(state["by_bar"]) == sorted(expected), "same bars carry a state"
    for bar, value in expected.items():
        assert state["by_bar"][bar] == pytest.approx(value, rel=1e-9), bar
    assert state["through_ms"] == int(bars[-1]) and state["latest"] == pytest.approx(expected[int(bars[-1])])


def test_without_an_archive_the_state_is_an_error_and_not_a_raise(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live")
    store.cycles_path.write_text(json.dumps({"bar_open_ms": BASE, "universe": ["BTCUSDT"]}) + "\n", encoding="utf-8")
    assert "error" in regime_state(store, tmp_path / "no-archive")
    assert regime_state(StateStore(tmp_path / "empty"), tmp_path / "no-archive") == {
        "error": "no cycle recorded a universe"
    }


# --- 2. M-010 mixes the terciles at the live shares ---------------------------------------------------


@pytest.mark.parametrize(
    ("states", "shares"),
    [
        ([LOW] * 96, {"low": 1.0}),
        ([LOW] * 72 + [HIGH] * 24, {"low": 0.75, "high": 0.25}),
        ([HIGH] * 48 + [MID] * 24 + [LOW] * 24, {"high": 0.5, "mid": 0.25, "low": 0.25}),
    ],
)
def test_m010_expects_the_terciles_mixed_at_the_live_shares(
    tmp_path: Path, states: list[float], shares: dict[str, float]
) -> None:
    returns = _window(0.4, 96)
    store = _store(tmp_path, returns)
    plain = income_drift(store, {"tsmom": PROMISED}, equity=EQUITY, since_ms=None)["by_strategy"]["tsmom"]
    row = income_drift(store, {"tsmom": PROMISED}, equity=EQUITY, since_ms=None, regime=_state(states))["by_strategy"][
        "tsmom"
    ]
    assert row["unconditioned"] is None
    assert row["regime_shares"] == dict(sorted(shares.items()))
    assert row["expected_sharpe"] == pytest.approx(_mix(shares), rel=1e-12)
    assert row["expected_unconditional"] == 1.83
    assert row["realised_sharpe"] == plain["realised_sharpe"] and row["bars"] == plain["bars"]
    assert row["z"] == pytest.approx((row["realised_sharpe"] - _mix(shares)) / math.sqrt(365.0 / row["days"]))
    if shares == {"low": 1.0}:
        assert row["expected_sharpe"] == pytest.approx(SPLIT["low"]["mean_annual"] / SPLIT["low"]["vol_annual"])


def test_a_calm_month_that_read_as_health_now_pages(tmp_path: Path) -> None:
    """The finding itself.  Over 30 days a realised -4.5 sits 1.81 standard errors below 1.83 and 2.15 below calm's 3.0."""
    store = _store(tmp_path, _window(-4.5, 720))
    payload = {
        "income_drift": income_drift(
            store, {"tsmom": PROMISED}, equity=EQUITY, since_ms=None, regime=_state([LOW] * 720)
        )
    }
    row = payload["income_drift"]["by_strategy"]["tsmom"]
    assert (row["realised_sharpe"] - 1.83) / math.sqrt(365.0 / 30.0) > -2.0, "unconditioned, this was no alert"
    assert payload["income_drift"]["status"] == "ALERT" and row["z"] < -2.0
    alerts, _ = daily_alerts(payload)
    assert any("按 regime 混合 low 100%" in line and "不分 regime 是 1.83" in line for line in alerts), alerts


def test_bars_past_the_archive_take_its_newest_state(tmp_path: Path) -> None:
    store = _store(tmp_path, _window(0.4, 96))
    state = _state([HIGH] * 24 + [LOW] * 24)  # the archive ends at bar 47; bars 48..95 read bar 47's state
    row = income_drift(store, {"tsmom": PROMISED}, equity=EQUITY, since_ms=None, regime=state)["by_strategy"]["tsmom"]
    assert row["regime_shares"] == {"high": 0.25, "low": 0.75}


# --- 4. a row that cannot condition says why and reads as before -----------------------------------------


@pytest.mark.parametrize(
    ("promised", "state", "why"),
    [
        (PROMISED, None, "no regime state: not read"),
        (PROMISED, {"error": "ValueError: no kline data"}, "no regime state: ValueError: no kline data"),
        ({**PROMISED, "regime_split": None}, _state([LOW] * 96), "its evidence has no regime split"),
        (
            {**PROMISED, "regime_split": OLD_SPLIT},
            _state([LOW] * 96),
            "predates the per-regime q10 and moments (D-049)",
        ),
        (PROMISED, _state([LOW] * 96, execution="close_to_close"), "the live one close_to_close"),
        (PROMISED, _state([LOW] * 48), None),  # bars past the archive are not a gap
    ],
)
def test_a_row_that_cannot_condition_says_why_and_reads_as_before(
    tmp_path: Path, promised: dict[str, Any], state: dict[str, Any] | None, why: str | None
) -> None:
    store = _store(tmp_path, _window(0.4, 96))
    before = income_drift(store, {"tsmom": promised}, equity=EQUITY, since_ms=None)["by_strategy"]["tsmom"]
    row = income_drift(store, {"tsmom": promised}, equity=EQUITY, since_ms=None, regime=state)["by_strategy"]["tsmom"]
    if why is None:
        assert row["unconditioned"] is None
        return
    assert why in str(row["unconditioned"]), row
    assert row["expected_sharpe"] == 1.83 and row["regime_shares"] is None
    for key in ("bars", "days", "realised_sharpe", "expected_sharpe", "z", "pnl"):
        assert row[key] == before[key], key
    assert _regime_note(row).startswith("不分 regime：")


def test_a_bar_with_no_state_unconditions_the_row(tmp_path: Path) -> None:
    store = _store(tmp_path, _window(0.4, 96))
    state = _state([LOW] * 96)
    del state["by_bar"][BASE + 10 * HOUR]
    row = income_drift(store, {"tsmom": PROMISED}, equity=EQUITY, since_ms=None, regime=state)["by_strategy"]["tsmom"]
    assert row["unconditioned"] == "1 of 96 bars have no regime state" and row["expected_sharpe"] == 1.83


# --- 3. each decay window is held to its own regime's q10 ---------------------------------------------


@pytest.mark.parametrize(
    ("opening", "rest", "sharpes", "conditioned", "unconditioned"),
    [
        # Calm: both windows above the one q10 (-1.85), both below calm's own (0.50).
        (LOW, LOW, [-1.0, -0.8], "REVIEW", "OK"),
        # Volatile: both below the one q10, both above the volatile tercile's own (-3.00).
        (HIGH, HIGH, [-2.5, -2.2], "OK", "REVIEW"),
        # Filed by the FIRST bar: each window opens calm and turns volatile after it, and is still held to calm's line.
        (LOW, HIGH, [-1.0, -0.8], "REVIEW", "OK"),
    ],
)
def test_each_window_is_held_to_the_q10_of_the_regime_it_opened_in(
    tmp_path: Path, opening: float, rest: float, sharpes: list[float], conditioned: str, unconditioned: str
) -> None:
    store = _store(tmp_path, [value for sharpe in sharpes for value in _window(sharpe, 24)])
    state = _state(([opening] + [rest] * 23) * 2)
    plain = decay_watch(store, {"tsmom": PROMISED}, equity=EQUITY, window_days=1)["tsmom"]
    row = decay_watch(store, {"tsmom": PROMISED}, equity=EQUITY, window_days=1, regime=state)["tsmom"]
    assert plain["status"] == unconditioned and row["status"] == conditioned
    label = "low" if opening == LOW else "high"
    assert row["opened_in"] == [label, label] and row["unconditioned"] is None
    assert row["lines"] == [SPLIT[label]["window_sharpe_q10"]] * 2 and row["q10"] == -1.85
    if conditioned == "REVIEW":
        (alert,) = _decay_alerts({"tsmom": row})
        assert "分别低于回测 q10 0.50、0.50" in alert and "各按窗口起点的 regime 取" in alert, alert


def test_the_decay_section_names_the_lines_and_the_regimes_the_windows_opened_in(tmp_path: Path) -> None:
    store = _store(tmp_path, [value for sharpe in (-1.0, -0.8) for value in _window(sharpe, 24)])
    row = decay_watch(store, {"tsmom": PROMISED}, equity=EQUITY, window_days=1, regime=_state([MID] * 48))["tsmom"]
    lines = _decay_lines({"decay": {"tsmom": row}, "expectations": {"tsmom": PROMISED}, "evidence_window": {}})
    assert "按窗口起点的 regime 取：low 0.50 / mid -1.00 / high -3.00" in lines["tsmom"], lines
    assert "整窗起点 mid、mid" in lines["tsmom"]
    unconditioned = decay_watch(store, {"tsmom": PROMISED}, equity=EQUITY, window_days=1)["tsmom"]
    text = _decay_lines({"decay": {"tsmom": unconditioned}, "expectations": {"tsmom": PROMISED}, "evidence_window": {}})
    assert "不分 regime：no regime state: not read" in text["tsmom"]


# --- the evidence the registry cites ---------------------------------------------------------------


def test_the_cited_evidence_carries_the_fields_and_mixes_back_to_its_own_oos_sharpe() -> None:
    """Fixtures above are written by hand; this one eats the report the loop runs on."""
    registry = load_registry(ROOT / "config" / "alpha_registry.yaml")
    entry = next(entry for entry in registry.enabled if entry.id == "tsmom")
    report = json.loads((ROOT / str(entry.evidence["report"])).read_text(encoding="utf-8"))
    promised = expectations_from_evidence({"tsmom": report})["tsmom"]
    split = promised["regime_split"]
    assert promised["regime_execution"] == "open_to_close"
    assert all(row["window_sharpe_q10"] is not None for row in split.values()), split
    assert sum(row["windows"] for row in split.values()) == report["walk_forward"]["oos_windows"]
    total = sum(row["bars"] for row in split.values())
    mean = sum(row["bars"] / total * row["mean_annual"] / 8760.0 for row in split.values())
    second = sum(
        row["bars"] / total * (row["vol_annual"] ** 2 / 8760.0 + (row["mean_annual"] / 8760.0) ** 2)
        for row in split.values()
    )
    mixed = mean / math.sqrt(second - mean**2) * math.sqrt(8760.0)
    assert mixed == pytest.approx(promised["oos_sharpe"], rel=1e-3)
