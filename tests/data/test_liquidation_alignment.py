"""#19 / RISK-G3: the liquidation column's conventions, pinned before anything trades on them.

RISK-G3's mitigation is written as "the DL-D2 pattern: contract first, then downloader", because the
metrics column showed what the alternative costs - archive `create_time` sat one whole bucket earlier
than the REST `timestamp`, nothing raised, and every piece of metrics evidence carried five minutes of
look-ahead until 2026-09-07 measured it.

Four properties are pinned here, each because getting it wrong produces a plausible number rather than
an error:

* the archive doubles every row (measured: 6,438 rows, 17 symbol-days, zero odd groups)
* `SELL` liquidates a LONG, so the position side is the inverse of the order side
* the bar interval is half-open, so an event on a bar's close belongs to the next bar
* a bar with no data and a bar with no liquidations are different, and only one of them is zero
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

HOUR_MS = 60 * 60 * 1000
DAY = pd.Timestamp("2024-10-01", tz="UTC")
REAL_SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "liquidations"


def _ms(text: str) -> int:
    return int(pd.Timestamp(text, tz="UTC").value // 1_000_000)


def _bars(count: int = 4, start: str = "2024-10-01 00:00") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=count, freq="h", tz="UTC")


# --- the archive's doubling -----------------------------------------------------------------------


def test_the_real_archive_sample_carries_every_row_exactly_twice() -> None:
    """The measurement this module is built on, held against bytes the venue actually served.

    Fourteen rows in the fixture, seven distinct liquidations.  Summing the file as it ships would
    report twice the notional and raise nothing at all.
    """
    raw = pd.read_csv(REAL_SAMPLE / "BTCUSD_PERP-liquidationSnapshot-2024-10-01.head.csv")
    group_sizes = raw.groupby(list(raw.columns), sort=False).size()

    assert len(raw) == 14
    assert set(group_sizes) == {2}


def test_the_doubling_is_halved_rather_than_deduplicated() -> None:
    """Two genuinely simultaneous identical liquidations ship as FOUR rows and must come back as two.

    `drop_duplicates` would return one and lose a real event; halving each group is the operation that
    matches what was measured.
    """
    from beidou_data.liquidations import collapse_archive_duplication

    row = {"time": 1, "side": "SELL", "average_price": 100.0, "accumulated_fill_quantity": 2.0}
    quadrupled = pd.DataFrame([row] * 4)

    assert len(collapse_archive_duplication(quadrupled)) == 2


def test_an_odd_group_is_refused_rather_than_rounded() -> None:
    """If the doubling ever stops, rounding is how a 2x error becomes invisible."""
    from beidou_data.liquidations import LiquidationArchiveAnomaly, collapse_archive_duplication

    row = {"time": 1, "side": "SELL", "average_price": 100.0, "accumulated_fill_quantity": 2.0}

    with pytest.raises(LiquidationArchiveAnomaly, match="even number"):
        collapse_archive_duplication(pd.DataFrame([row] * 3))


def test_parsing_the_real_sample_yields_half_as_many_events() -> None:
    from beidou_data.liquidations import parse_archive_csv

    text = (REAL_SAMPLE / "BTCUSD_PERP-liquidationSnapshot-2024-10-01.head.csv").read_text(encoding="utf-8")
    events = parse_archive_csv(text, "BTCUSD_PERP")

    assert len(events) == 7
    assert events["time"].is_monotonic_increasing
    assert set(events["symbol"]) == {"BTCUSD_PERP"}


def test_the_parser_takes_the_average_fill_price_and_the_accumulated_quantity() -> None:
    """`price` is the IOC limit and `last_fill_quantity` is only the final fill of a multi-fill order.

    The real row this pins: original 76, limit 63778.3, average fill 63551.6, last fill 66,
    accumulated 76.  Taking the limit overstates the price; taking the last fill loses ten contracts.
    """
    from beidou_data.liquidations import parse_archive_csv

    text = (REAL_SAMPLE / "BTCUSD_PERP-liquidationSnapshot-2024-10-01.head.csv").read_text(encoding="utf-8")
    events = parse_archive_csv(text, "BTCUSD_PERP")
    multi_fill = events[events["quantity"] == 76.0]

    assert len(multi_fill) == 1
    assert multi_fill["price"].iloc[0] == pytest.approx(63551.6)


# --- the side inversion ---------------------------------------------------------------------------


def test_a_sell_order_means_a_long_was_liquidated() -> None:
    """The sign of every signal built on this column depends on this one mapping."""
    from beidou_data.liquidations import liquidated_side

    assert liquidated_side("SELL") == "long"
    assert liquidated_side("BUY") == "short"


def test_the_order_side_does_not_survive_into_a_parsed_frame() -> None:
    """Two spellings for one fact is how the metrics column got its five minutes; one is kept."""
    from beidou_data.liquidations import parse_archive_csv

    text = (REAL_SAMPLE / "BTCUSD_PERP-liquidationSnapshot-2024-10-01.head.csv").read_text(encoding="utf-8")
    events = parse_archive_csv(text, "BTCUSD_PERP")

    assert set(events["side"]) <= {"long", "short"}
    assert not {"BUY", "SELL"} & set(events["side"])


def test_an_unknown_order_side_is_refused() -> None:
    from beidou_data.liquidations import liquidated_side

    with pytest.raises(ValueError, match="BUY or SELL"):
        liquidated_side("LONG")


def test_the_stream_and_the_archive_parse_the_same_liquidation_to_the_same_row() -> None:
    """The regression that would catch a divergence between the two sources' conventions."""
    from beidou_data.liquidations import parse_archive_csv, parse_stream_events

    stamp = _ms("2024-10-01 00:41:09.082")
    csv = (
        "time,side,order_type,time_in_force,original_quantity,price,average_price,order_status,"
        "last_fill_quantity,accumulated_fill_quantity\n"
        f"{stamp},BUY,LIMIT,IOC,7,63661.4,63425.1,FILLED,7,7\n"
        f"{stamp},BUY,LIMIT,IOC,7,63661.4,63425.1,FILLED,7,7\n"
    )
    payload = {
        "e": "forceOrder",
        "E": stamp + 17,
        "o": {"s": "BTCUSD_PERP", "S": "BUY", "ap": "63425.1", "z": "7", "T": stamp},
    }

    archive = parse_archive_csv(csv, "BTCUSD_PERP")
    stream = parse_stream_events([payload])

    assert archive["time"].tolist() == stream["time"].tolist()
    assert archive["side"].tolist() == stream["side"].tolist()
    assert archive["price"].tolist() == stream["price"].tolist()
    assert archive["quantity"].tolist() == stream["quantity"].tolist()


def test_the_stream_is_stamped_by_trade_time_not_by_push_time() -> None:
    """`E` is when the venue sent the message; joining on it compares two clocks."""
    from beidou_data.liquidations import parse_stream_events

    trade = _ms("2024-10-01 00:41:09.082")
    events = parse_stream_events([{"o": {"s": "X", "S": "SELL", "ap": "1", "z": "1", "T": trade}, "E": trade + 900}])

    assert events["time"].iloc[0] == trade


# --- event time to bars ---------------------------------------------------------------------------


def test_an_event_exactly_on_a_closed_bars_boundary_is_not_pulled_back_into_it() -> None:
    """The half-open boundary, tested where it is actually decided.

    Bars at 00:00 and 05:00; an event at exactly 01:00:00.000, the instant the 00:00 bar CLOSED.
    Reading the guard as `<=` hands it to a bar that had already finished - a whole bar of look-ahead.
    A contiguous bar index cannot tell the two spellings apart, because the next bar would have taken
    the event anyway; only a gap separates them, which is why this test has one.
    """
    from beidou_data.liquidations import aggregate_to_bars, parse_stream_events

    bars = pd.DatetimeIndex([DAY, DAY + pd.Timedelta(hours=5)])
    on_the_close = parse_stream_events(
        [{"o": {"s": "X", "S": "SELL", "ap": "10", "z": "1", "T": _ms("2024-10-01 01:00:00.000")}}]
    )
    inside_it = parse_stream_events(
        [{"o": {"s": "X", "S": "SELL", "ap": "10", "z": "1", "T": _ms("2024-10-01 00:59:59.999")}}]
    )
    covered = np.ones(2, dtype=bool)

    assert aggregate_to_bars(on_the_close, bars, interval_ms=HOUR_MS, covered=covered)["liq_count_long"].tolist() == [
        0.0,
        0.0,
    ]
    assert aggregate_to_bars(inside_it, bars, interval_ms=HOUR_MS, covered=covered)["liq_count_long"].tolist() == [
        1.0,
        0.0,
    ]


def test_a_bars_aggregate_is_not_complete_before_the_bar_closes() -> None:
    from beidou_data.liquidations import usable_from_ms

    open_ms = _ms("2024-10-01 00:00:00")

    assert usable_from_ms(open_ms, HOUR_MS) == open_ms + HOUR_MS


def test_events_land_in_the_bar_that_contains_them_and_nowhere_else() -> None:
    from beidou_data.liquidations import aggregate_to_bars, parse_stream_events

    bars = _bars(3)
    events = parse_stream_events(
        [
            {"o": {"s": "X", "S": "SELL", "ap": "10", "z": "1", "T": _ms("2024-10-01 00:59:59.999")}},
            {"o": {"s": "X", "S": "SELL", "ap": "10", "z": "2", "T": _ms("2024-10-01 01:00:00.000")}},
        ]
    )

    aligned = aggregate_to_bars(events, bars, interval_ms=HOUR_MS, covered=np.ones(3, dtype=bool))

    assert aligned["liq_notional_long"].tolist() == [10.0, 20.0, 0.0]
    assert aligned["liq_count_long"].tolist() == [1.0, 1.0, 0.0]


def test_an_event_outside_every_bar_is_dropped_rather_than_attached_to_the_nearest() -> None:
    """A bar index can have gaps - a delisting, an outage - and "nearest" invents a neighbour there."""
    from beidou_data.liquidations import aggregate_to_bars, parse_stream_events

    bars = pd.DatetimeIndex([DAY, DAY + pd.Timedelta(hours=5)])
    events = parse_stream_events([{"o": {"s": "X", "S": "BUY", "ap": "10", "z": "3", "T": _ms("2024-10-01 03:00")}}])

    aligned = aggregate_to_bars(events, bars, interval_ms=HOUR_MS, covered=np.ones(2, dtype=bool))

    assert aligned["liq_notional_short"].tolist() == [0.0, 0.0]


def test_long_and_short_liquidations_do_not_land_in_the_same_column() -> None:
    from beidou_data.liquidations import aggregate_to_bars, parse_stream_events

    bars = _bars(1)
    events = parse_stream_events(
        [
            {"o": {"s": "X", "S": "SELL", "ap": "100", "z": "1", "T": _ms("2024-10-01 00:10")}},
            {"o": {"s": "X", "S": "BUY", "ap": "100", "z": "4", "T": _ms("2024-10-01 00:20")}},
        ]
    )

    aligned = aggregate_to_bars(events, bars, interval_ms=HOUR_MS, covered=np.ones(1, dtype=bool))

    assert aligned["liq_notional_long"].iloc[0] == pytest.approx(100.0)
    assert aligned["liq_notional_short"].iloc[0] == pytest.approx(400.0)


def test_an_inverse_contract_notional_ignores_the_price() -> None:
    """BTCUSD_PERP quantity is CONTRACTS of 100 USD; price x quantity would be ~63,000x too large."""
    from beidou_data.liquidations import aggregate_to_bars, parse_stream_events

    bars = _bars(1)
    events = parse_stream_events(
        [{"o": {"s": "X", "S": "SELL", "ap": "63551.6", "z": "7", "T": _ms("2024-10-01 00:10")}}]
    )

    aligned = aggregate_to_bars(events, bars, interval_ms=HOUR_MS, covered=np.ones(1, dtype=bool), contract_size=100.0)

    assert aligned["liq_notional_long"].iloc[0] == pytest.approx(700.0)


# --- missing is missing ---------------------------------------------------------------------------


def test_a_bar_with_no_data_is_nan_and_a_bar_with_no_liquidations_is_zero() -> None:
    """The distinction this whole column is built around.

    Collapsing both onto 0.0 would let a symbol nobody ever downloaded read as the calmest one in the
    universe - a real signal value, produced from nothing.
    """
    from beidou_data.liquidations import aggregate_to_bars, empty_events

    bars = _bars(2)

    aligned = aggregate_to_bars(empty_events(), bars, interval_ms=HOUR_MS, covered=np.array([True, False]))

    assert aligned["liq_notional_long"].iloc[0] == 0.0
    assert pd.isna(aligned["liq_notional_long"].iloc[1])
    assert pd.isna(aligned["liq_count_short"].iloc[1])


def test_coverage_must_be_stated_for_every_bar() -> None:
    """There is no permissive default: a coverage argument with one would be the thing that got lost."""
    from beidou_data.liquidations import aggregate_to_bars, empty_events

    with pytest.raises(ValueError, match="one flag per bar"):
        aggregate_to_bars(empty_events(), _bars(3), interval_ms=HOUR_MS, covered=np.ones(2, dtype=bool))


def test_events_on_a_bar_the_record_calls_uncovered_are_a_contradiction() -> None:
    """The coverage record and the data it describes cannot be allowed to disagree quietly."""
    from beidou_data.liquidations import aggregate_to_bars, parse_stream_events

    events = parse_stream_events([{"o": {"s": "X", "S": "SELL", "ap": "1", "z": "1", "T": _ms("2024-10-01 00:10")}}])

    with pytest.raises(ValueError, match="disagree"):
        aggregate_to_bars(events, _bars(2), interval_ms=HOUR_MS, covered=np.array([False, True]))


# --- the same-source obligation -------------------------------------------------------------------


def test_parity_over_no_shared_bars_reports_nothing_rather_than_perfect_agreement() -> None:
    """M-011's rule: zero disagreements out of zero comparisons is not agreement.

    This is the answer the column actually gets today.  The USDⓈ-M archive does not exist and the
    coin-margined one stopped on 2024-10-14, so no stream recorded now shares a bar with any archive -
    the same-source obligation is unmeetable, not merely unmet.
    """
    from beidou_data.liquidations import empty_events, stream_archive_parity

    result = stream_archive_parity(
        empty_events(), empty_events(), pd.DatetimeIndex([], dtype="datetime64[ns, UTC]"), interval_ms=HOUR_MS
    )

    assert result == {"overlapping": 0, "differing": 0, "rate": None}


def test_parity_counts_a_bar_one_source_missed_as_a_disagreement() -> None:
    """Unlike the metrics check, an empty side is not "we never looked" - on a covered bar it is zero.

    Which is exactly what a throttled live stream against a complete archive would look like, and the
    reason the check must not excuse it.
    """
    from beidou_data.liquidations import empty_events, parse_stream_events, stream_archive_parity

    archive = parse_stream_events([{"o": {"s": "X", "S": "SELL", "ap": "10", "z": "1", "T": _ms("2024-10-01 00:10")}}])

    result = stream_archive_parity(empty_events(), archive, _bars(2), interval_ms=HOUR_MS)

    assert result == {"overlapping": 2, "differing": 1, "rate": 0.5}
