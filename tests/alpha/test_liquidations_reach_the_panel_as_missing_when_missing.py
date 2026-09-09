"""#19 / RISK-G3: the liquidation columns reach a Panel, and an unfetched symbol stays unknown there.

They ride in `Panel.metrics`, the dict already defined as "one wide frame per column, bars x symbols,
ALREADY ALIGNED to these bars".  Reusing it rather than adding a field is the point: `from_frames`
already refuses a frame that is not on the bar index instead of reindexing it helpfully, and a second
field would need a second copy of that refusal - which is the copy that drifts.

The look-ahead this guards against is not the metrics one.  These aggregates are a FLOW over the bar,
complete at the bar's close, like `volume`; the hazard is the half-open boundary (an event on a bar's
close belongs to the next bar) and, above all, the difference between a bar with no liquidations and a
bar nobody downloaded.  Only the first of those is zero.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from beidou_alpha.panel import Panel

HOUR_MS = 60 * 60 * 1000
BARS = pd.date_range("2024-10-01", periods=6, freq="h", tz="UTC")
SYMBOLS = ("BTCUSDT", "ETHUSDT")

CSV = (
    "time,side,order_type,time_in_force,original_quantity,price,average_price,order_status,"
    "last_fill_quantity,accumulated_fill_quantity\n"
    "1727740869082,BUY,LIMIT,IOC,7,63661.4,63425.1,FILLED,7,7\n"
    "1727740869082,BUY,LIMIT,IOC,7,63661.4,63425.1,FILLED,7,7\n"
)


def _frames() -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(19)
    out = {}
    for symbol in SYMBOLS:
        close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.005, len(BARS))))
        out[symbol] = pd.DataFrame(
            {"open": close, "high": close * 1.001, "low": close * 0.999, "close": close, "volume": 1_000.0},
            index=BARS,
        )
    return out


def _store_with_only_btc(tmp_path: Path):  # type: ignore[no-untyped-def]
    from beidou_data.liquidations import LiquidationStore, parse_archive_csv

    store = LiquidationStore(tmp_path)
    store.write_day("BTCUSDT", "2024-10-01", parse_archive_csv(CSV, "BTCUSDT"))
    return store


def test_the_columns_build_a_panel_and_keep_an_unfetched_symbol_unknown(tmp_path: Path) -> None:
    from beidou_data.liquidations import to_panel_columns

    columns = to_panel_columns(_store_with_only_btc(tmp_path), list(SYMBOLS), BARS, interval_ms=HOUR_MS)
    panel = Panel.from_frames(_frames(), "1h", metrics=columns)
    shorts = panel.metric("liq_notional_short")

    assert shorts is not None
    assert shorts["ETHUSDT"].isna().all(), "a symbol nobody downloaded must not read as a calm one"
    assert shorts["BTCUSDT"].iloc[0] == pytest.approx(7 * 63425.1)
    assert (shorts["BTCUSDT"].iloc[1:] == 0.0).all(), "a fetched hour with no liquidations is a real zero"


def test_a_panel_that_never_got_the_columns_answers_none_rather_than_zero() -> None:
    """`metric()` already draws this line for the metrics columns; the liquidation ones inherit it."""
    panel = Panel.from_frames(_frames(), "1h")

    assert panel.metric("liq_notional_long") is None


def test_a_frame_off_the_bar_index_is_refused_here_too(tmp_path: Path) -> None:
    """The refusal that makes "aligned by the caller" safe, exercised on this column's frames."""
    from beidou_data.liquidations import to_panel_columns

    shifted = BARS + pd.Timedelta(minutes=5)
    columns = to_panel_columns(_store_with_only_btc(tmp_path), list(SYMBOLS), shifted, interval_ms=HOUR_MS)

    with pytest.raises(ValueError, match="align"):
        Panel.from_frames(_frames(), "1h", metrics=columns)


def test_no_bar_carries_a_liquidation_that_had_not_happened_yet(tmp_path: Path) -> None:
    """Causality, stated as the property rather than as an example.

    Every non-zero bar must contain at least one event whose time is inside it, and no bar may carry
    notional from an event that happens after the bar closes.  A one-bar shift - the shape the metrics
    column shipped with for months - fails this without any test needing to know the shift's size.
    """
    from beidou_data.liquidations import LiquidationStore, parse_stream_events, to_panel_columns

    store = LiquidationStore(tmp_path)
    stamps = [int(pd.Timestamp(f"2024-10-01 0{hour}:30", tz="UTC").value // 1_000_000) for hour in (1, 3)]
    events = parse_stream_events([{"o": {"s": "BTCUSDT", "S": "SELL", "ap": "10", "z": "1", "T": t}} for t in stamps])
    store.write_day("BTCUSDT", "2024-10-01", events)

    longs = to_panel_columns(store, ["BTCUSDT"], BARS, interval_ms=HOUR_MS)["liq_notional_long"]["BTCUSDT"]
    hit = np.flatnonzero(longs.to_numpy() > 0)

    assert hit.tolist() == [1, 3]
    for position in hit:
        low = int(BARS[position].value // 1_000_000)
        assert any(low <= stamp < low + HOUR_MS for stamp in stamps)
