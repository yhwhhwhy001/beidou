"""#31 / RISK-G3: a verified stamp offset is not enough when the source rewrites its own history.

`alignment` was built for a defect of CONVENTION - two sources stamping one bucket differently - and
`verify_stamp_offset` settles that here too: Coin Metrics and blockchain.info both stamp a daily value
with the UTC day's open, measured at 355/355 agreement against 97/354 and 97/355 one day either side.

What that check cannot see is the defect this feed actually has.  Coin Metrics publishes a
`<metric>-status-time` per cell, and BTC's exchange inflow for 2024-03-01 carries **2026-04-09**: the
number a backtest reads today for that day was written 769 days after it, while ETH's value for the
same day was written the next morning.  The offset is right, the values are right, and reading them at
the declared availability is still look-ahead - so the refusal has to be a separate one.

The tests that carry the weight are, as one feed earlier, the ones that must FAIL: a contract declaring
the wrong day offset, a column whose cells were written late, and a bar reading a day that had not been
computed yet.  A gate that cannot fail is the thing being guarded against.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from beidou_data import alignment
from beidou_data.alignment import FAIL, PASS, UNVERIFIABLE, Stamp, UndeclaredColumn, Verification, verify_stamp_offset
from beidou_data.onchain import (
    ADMITTED_METRICS,
    CONTRACTS,
    DAY_MS,
    ONCHAIN,
    PANEL_COLUMNS,
    REFUSED_METRICS,
    STATUS_SUFFIX,
    WITNESS_COLUMN,
    WITNESS_TOLERANCE,
    admits_live_signal,
    align_daily_to_bars,
    contract_for,
    parse_timeseries,
    revision_evidence,
    to_contract_frame,
    witness_frame,
)

START = pd.Timestamp("2026-06-01", tz="UTC").value // 1_000_000
TX = "onchain_tx_count"


def _days(count: int) -> list[int]:
    return [START + i * DAY_MS for i in range(count)]


def _counts(count: int) -> list[float]:
    """A series that MOVES.  The whole check rests on the rivals being refutable, and a flat column
    agrees with itself at every offset - which is the UNVERIFIABLE case, tested separately below."""
    rng = np.random.default_rng(31)
    return [float(600_000 + 120_000 * v) for v in rng.random(count)]


def _payload(days: list[int], values: list[float], *, asset: str = "btc", status: dict[int, str] | None = None):
    rows = []
    for day, value in zip(days, values, strict=True):
        row = {
            "asset": asset,
            "time": f"{pd.Timestamp(day, unit='ms', tz='UTC'):%Y-%m-%dT%H:%M:%S.000000000Z}",
            "TxCnt": f"{value:.0f}",
        }
        if status and day in status:
            row["TxCnt-status"] = "flash"
            row["TxCnt-status-time"] = status[day]
        rows.append(row)
    return {"data": rows}


def _witness(days: list[int], values: list[float], *, drift: float = 0.006):
    """The second source, differing by about the measured 0.6% median - not by nothing.

    Handing the check two copies of one series would make it pass for the wrong reason: `_agreement`
    would then be comparing a number with itself, and no tolerance would ever be exercised.
    """
    return {"values": [{"x": day // 1000, "y": value * (1.0 + drift)} for day, value in zip(days, values, strict=True)]}


def _frames(count: int = 60, **kwargs):
    days, values = _days(count), _counts(count)
    stored = parse_timeseries(_payload(days, values, **kwargs), metrics={"TxCnt": TX})
    return stored, days, values


def _pair(count: int = 60, *, drift: float = 0.006):
    stored, days, values = _frames(count)
    return to_contract_frame(stored, ONCHAIN.archive), witness_frame(_witness(days, values, drift=drift))


def _verify(contract=ONCHAIN, **kwargs) -> Verification:
    archive, rest = _pair(**kwargs)
    return verify_stamp_offset(contract, archive, rest, value_columns=[TX], tolerance=WITNESS_TOLERANCE)


# --------------------------------------------------------------------------------------------------
# The offset, and the declarations that must be refused
# --------------------------------------------------------------------------------------------------


def test_the_declared_day_offset_passes_and_names_the_rivals_it_refuted() -> None:
    result = _verify()
    assert result.verdict == PASS
    assert result.matched == result.compared > 0
    assert result.compared_columns == (TX,)
    # Both one-day rivals, and nothing else: the naive join is already the declared offset here, so
    # `rival_rest_offsets` dedupes it away.  That is the contract's own arithmetic, asserted rather
    # than assumed, because a rival list that silently emptied would make every offset "verified".
    assert sorted(rival.rest_offset_ms for rival in result.rivals) == [-DAY_MS, DAY_MS]
    assert all(rival.matched < rival.compared for rival in result.rivals)


@pytest.mark.parametrize("wrong_offset_ms", [DAY_MS, -DAY_MS, DAY_MS // 2])
def test_a_contract_that_declares_the_wrong_day_offset_fails(wrong_offset_ms: int) -> None:
    """The load-bearing one.  If a wrong declaration still passes, the contract is decoration.

    A day out in either direction is the real defect - it is what DL-D2 found five minutes of, one
    feed earlier - and half a day is here because a sub-period error joins nothing at all, which must
    be refused rather than reported as an empty success.
    """
    wrong = replace(ONCHAIN, rest=Stamp("x", wrong_offset_ms, "wrong on purpose"))
    result = _verify(wrong)
    assert result.verdict in (FAIL, UNVERIFIABLE)
    assert admits_live_signal(TX, result, None)[0] is False


def test_a_column_that_never_moves_is_unverifiable_rather_than_confirmed() -> None:
    """ "算不出来就说算不出来": a flat series agrees with itself at every offset, so it proves nothing."""
    days = _days(60)
    flat = [700_000.0] * 60
    stored = parse_timeseries(_payload(days, flat), metrics={"TxCnt": TX})
    result = verify_stamp_offset(
        ONCHAIN,
        to_contract_frame(stored, ONCHAIN.archive),
        witness_frame(_witness(days, flat, drift=0.0)),
        value_columns=[TX],
        tolerance=WITNESS_TOLERANCE,
    )
    assert result.verdict == UNVERIFIABLE
    assert "do not move enough" in result.reason


def test_the_measured_tolerance_is_what_separates_the_offsets() -> None:
    """Two independent computations differ; the tolerance is a measured number, not a preference.

    The production numbers, restated as a property: at the declared offset the two sources sit inside
    the tolerance and one day off they do not.  A drift larger than the tolerance must FAIL rather than
    quietly widen, because "the sources disagree" and "the offset is wrong" are different findings.
    """
    assert _verify(drift=WITNESS_TOLERANCE / 2).verdict == PASS
    assert _verify(drift=WITNESS_TOLERANCE * 2).verdict == FAIL


# --------------------------------------------------------------------------------------------------
# The backfill: the refusal no stamp arithmetic can reach
# --------------------------------------------------------------------------------------------------


def test_a_cell_written_after_its_declared_availability_is_refused() -> None:
    """The measured shape: BTC's 2024-03-01 inflow carries a status time of 2026-04-09.

    Reconstructed here at the same scale - a value for one day, stamped two years later - because the
    defect is not that the number is wrong.  It is right today.  It was not readable then, and nothing
    else in the pipeline can tell those two apart.
    """
    days, values = _days(30), _counts(30)
    late_day = days[3]
    stored = parse_timeseries(
        _payload(days, values, status={late_day: "2028-04-09T08:42:55.577376000Z"}), metrics={"TxCnt": TX}
    )
    evidence = revision_evidence(stored, TX)

    assert evidence.clean is False
    assert evidence.late == 1 and evidence.stamped == 1
    assert evidence.worst_day_ms == late_day
    assert evidence.worst_lag_ms > 600 * DAY_MS
    assert "written after their declared availability" in evidence.reason

    verification = _verify()
    admitted, reason = admits_live_signal(TX, verification, evidence)
    assert admitted is False and "RISK-G3" in reason


def test_a_column_with_no_revision_stamps_at_all_is_admitted() -> None:
    """The other half: the admitted metrics carry no status field, which is the source saying final."""
    stored, _, _ = _frames()
    assert stored[TX + STATUS_SUFFIX].isna().all()
    evidence = revision_evidence(stored, TX)
    assert evidence.clean is True and evidence.stamped == 0
    admitted, reason = admits_live_signal(TX, _verify(), evidence)
    assert admitted is True and "onchain" in reason


def test_a_stamp_inside_the_declared_availability_is_not_late() -> None:
    """The boundary itself, in both directions - an off-by-one here would make the gate unfalsifiable."""
    days, values = _days(10), _counts(10)
    day = days[2]
    for delta_ms, expected_late in ((-3_600_000, 0), (3_600_000, 1)):
        stamp = pd.Timestamp(day + ONCHAIN.available_offset_ms + delta_ms, unit="ms", tz="UTC")
        stored = parse_timeseries(
            _payload(days, values, status={day: f"{stamp:%Y-%m-%dT%H:%M:%S.000000000Z}"}), metrics={"TxCnt": TX}
        )
        assert revision_evidence(stored, TX).late == expected_late


def test_the_refused_metrics_are_named_rather_than_omitted() -> None:
    """Exchange flows are the columns the source marks revisable; none of them may reach a panel."""
    assert set(REFUSED_METRICS) == {"FlowInExNtv", "FlowOutExNtv"}
    assert set(PANEL_COLUMNS) == set(ADMITTED_METRICS.values())
    for column in REFUSED_METRICS.values():
        assert column not in PANEL_COLUMNS
        with pytest.raises(UndeclaredColumn):
            contract_for(column)
        assert admits_live_signal(column, _verify(), None)[0] is False


# --------------------------------------------------------------------------------------------------
# Admission, and the two registries
# --------------------------------------------------------------------------------------------------


def test_every_panel_column_is_declared_and_no_stray_is() -> None:
    assert set(CONTRACTS) == set(PANEL_COLUMNS)
    assert all(contract_for(column) is ONCHAIN for column in PANEL_COLUMNS)
    with pytest.raises(UndeclaredColumn):
        contract_for("onchain_whatever")


def test_the_two_registries_refuse_for_the_same_reasons() -> None:
    """This module's gate repeats four of alignment's refusals; they must not drift apart.

    A separate registry was chosen over editing `alignment.CONTRACTS` (see this module's own note on
    why), and the price is a copy.  This is the guard on that price: the same four situations, put to
    both functions on each one's own column, must produce the same verdict and the same wording.
    """
    metrics_column, onchain_column = "sum_open_interest", TX
    seen = (metrics_column,), ()
    onchain_seen = (onchain_column,), ()
    cases = [
        (None, None),
        (Verification(FAIL, "x", 60, 0, (), *seen), Verification(FAIL, "x", 60, 0, (), *onchain_seen)),
        (Verification(UNVERIFIABLE, "x", 0, 0, (), *seen), Verification(UNVERIFIABLE, "x", 0, 0, (), *onchain_seen)),
        (Verification(PASS, "60/60", 60, 60, (), (), ()), Verification(PASS, "60/60", 60, 60, (), (), ())),
        (Verification(PASS, "60/60", 60, 60, (), *seen), Verification(PASS, "60/60", 60, 60, (), *onchain_seen)),
    ]
    clean = revision_evidence(_frames()[0], onchain_column)
    for theirs, mine in cases:
        their_verdict, their_reason = alignment.admits_live_signal(metrics_column, theirs)
        my_verdict, my_reason = admits_live_signal(onchain_column, mine, clean)
        assert my_verdict is their_verdict, (theirs, mine)
        assert (
            their_reason.replace(metrics_column, "C").replace("metrics", "N")
            == my_reason.replace(onchain_column, "C").replace("onchain", "N").split("; ")[0]
        )

    # And the fifth, which alignment has no counterpart for: only this side can refuse it.
    assert alignment.admits_live_signal(metrics_column, cases[-1][0])[0] is True
    assert admits_live_signal(onchain_column, cases[-1][1], None)[0] is False


def test_an_undeclared_column_is_refused_before_anything_else_is_asked() -> None:
    admitted, reason = admits_live_signal("onchain_gas_burned", None, None)
    assert admitted is False and "no event-time contract" in reason


# --------------------------------------------------------------------------------------------------
# Causality, and missing-is-missing
# --------------------------------------------------------------------------------------------------


def test_a_bar_never_reads_a_day_that_was_not_computed_yet() -> None:
    """Causality.  The day opening at D is available at D+2, so the first bar that may read it is the
    first bar CLOSING at or after D+2 - and every bar before that reads NaN, not the value."""
    days, values = _days(6), _counts(6)
    stored = parse_timeseries(_payload(days, values), metrics={"TxCnt": TX})
    bars = pd.date_range(pd.Timestamp(days[0], unit="ms", tz="UTC"), periods=6 * 24, freq="1h")
    aligned = align_daily_to_bars(stored, bars, interval_ms=3_600_000)

    available = pd.Timestamp(days[0] + ONCHAIN.available_offset_ms, unit="ms", tz="UTC")
    before = aligned.loc[aligned.index < available - pd.Timedelta(hours=1), TX]
    assert before.isna().all(), "a bar closing before the day was computed must read NaN"
    assert aligned.loc[available - pd.Timedelta(hours=1), TX] == pytest.approx(values[0])
    # And it is the LATEST available day, not the nearest one: the bar that first sees day 1 must not
    # already be carrying day 2, which is the direction a "helpful" fill moves.
    second = pd.Timestamp(days[1] + ONCHAIN.available_offset_ms, unit="ms", tz="UTC")
    assert aligned.loc[second - pd.Timedelta(hours=2), TX] == pytest.approx(values[0])
    assert aligned.loc[second - pd.Timedelta(hours=1), TX] == pytest.approx(values[1])


def test_a_day_the_source_did_not_publish_stays_nan() -> None:
    """Missing is missing: not zero, and not the previous day carried into the hole.

    A daily series read on an hourly grid necessarily repeats a value across the bars of one day; the
    thing that must never happen is a value repeating across a day the source never published, because
    that is indistinguishable from a real reading and moves any level-based signal.
    """
    days, values = _days(4), _counts(4)
    payload = _payload(days, values)
    payload["data"][2]["TxCnt"] = None  # the source published the day and left the metric empty
    stored = parse_timeseries(payload, metrics={"TxCnt": TX})
    assert stored[TX].isna().sum() == 1

    bars = pd.date_range(pd.Timestamp(days[0], unit="ms", tz="UTC"), periods=6 * 24, freq="1h")
    aligned = align_daily_to_bars(stored, bars, interval_ms=3_600_000)
    hole = pd.Timestamp(days[2] + ONCHAIN.available_offset_ms, unit="ms", tz="UTC")
    # The bar that first sees the empty day reads NaN rather than falling back to the day before it,
    # which is the fill this whole module refuses to do.
    # `hole` and `hole - 1h` are both bars whose CLOSE is at or past the empty day's availability, so
    # both read it; the last bar that still reads the day before opens two hours earlier.
    assert np.isnan(aligned.loc[hole, TX])
    assert np.isnan(aligned.loc[hole - pd.Timedelta(hours=1), TX])
    assert aligned.loc[hole - pd.Timedelta(hours=2), TX] == pytest.approx(values[1])
    assert (aligned[TX] == 0).sum() == 0


def test_an_asset_with_no_rows_at_all_yields_an_all_nan_frame() -> None:
    """A perpetual with no on-chain leg reaches the panel as absent, at full width."""
    empty = parse_timeseries({"data": []}, metrics={"TxCnt": TX})
    bars = pd.date_range(pd.Timestamp(START, unit="ms", tz="UTC"), periods=24, freq="1h")
    aligned = align_daily_to_bars(empty, bars, interval_ms=3_600_000)
    assert list(aligned.columns) == list(PANEL_COLUMNS)
    assert aligned.isna().all().all()


def test_the_witness_never_becomes_a_panel_column() -> None:
    """It has no contract and no revision evidence, so by this module's own rule it cannot reach live."""
    assert WITNESS_COLUMN in PANEL_COLUMNS  # it is a canonical NAME, shared so the check can compare
    frame = witness_frame(_witness(_days(3), _counts(3)))
    assert set(frame.columns) == {ONCHAIN.rest.column, WITNESS_COLUMN}
    assert "open_time" not in frame.columns, "the witness is never stored, so it never gets a store key"
