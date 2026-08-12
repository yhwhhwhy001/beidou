"""Fail-closed contracts for the fill-to-position authority chain."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from beidou_safety.position.position_aggregate import FillEvent as LegacyFill
from beidou_safety.position.position_aggregate import PositionAggregate as LegacyPosition
from beidou_safety.position.projection import (
    FillEvent,
    PositionAggregate,
    PositionProjection,
    PositionSide,
)
from beidou_shared.types import InstrumentId, MonetaryValue, Price, Quantity, VenueId


def _fill(
    trade_id: str = "trade-1",
    *,
    side: PositionSide = PositionSide.LONG,
    quantity: str = "1",
    price: str = "100",
    fee: str = "0",
    sequence: int = 1,
    instrument: str = "BTCUSDT",
    venue: str = "BINANCE",
) -> FillEvent:
    return FillEvent(
        fill_id=f"fill-{trade_id}",
        trade_id=trade_id,
        venue_id=VenueId(venue),
        instrument_id=InstrumentId(instrument),
        side=side,
        quantity=Quantity(amount=quantity),
        price=Price(amount=price),
        fee=MonetaryValue(amount=fee),
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sequence=sequence,
    )


@pytest.mark.parametrize(
    "fill",
    [
        LegacyFill("", "BTCUSDT", "BUY", 1, 100),
        LegacyFill("fill-1", "", "BUY", 1, 100),
        LegacyFill("fill-1", "BTCUSDT", "BUY", float("nan"), 100),
        LegacyFill("fill-1", "BTCUSDT", "BUY", 1, float("inf")),
        LegacyFill("fill-1", "BTCUSDT", "BUY", 1, 100, commission=-1),
        LegacyFill("fill-1", "BTCUSDT", "BUY", 1, 100, timestamp=float("nan")),
    ],
)
def test_legacy_position_rejects_invalid_economic_facts(fill: LegacyFill) -> None:
    with pytest.raises(ValueError):
        LegacyPosition(symbol="BTCUSDT").apply_fill(fill)


def test_legacy_position_duplicate_identity_is_idempotent_but_conflict_fails() -> None:
    fill = LegacyFill("fill-1", "BTCUSDT", "BUY", 1, 100)
    position = LegacyPosition(symbol="BTCUSDT").apply_fill(fill)
    assert position.apply_fill(fill) is position
    with pytest.raises(ValueError, match="Conflicting fill identity"):
        position.apply_fill(replace(fill, price=101))


def test_legacy_position_rejects_corrupt_state_and_nonfinite_reduce_only() -> None:
    with pytest.raises(ValueError, match="state"):
        LegacyPosition(symbol="BTCUSDT", net_position=float("nan")).apply_fill(
            LegacyFill("fill-1", "BTCUSDT", "BUY", 1, 100)
        )
    position = LegacyPosition(symbol="BTCUSDT", net_position=1, avg_entry_price=100)
    assert not position.is_reduce_only_compliant(LegacyFill("r1", "BTCUSDT", "SELL", float("nan"), 100))
    assert not position.is_reduce_only_compliant(LegacyFill("r2", "ETHUSDT", "SELL", 1, 100))
    assert not position.is_reduce_only_compliant(LegacyFill("r3", "BTCUSDT", "HOLD", 1, 100))


def test_legacy_position_flatten_and_short_realized_pnl() -> None:
    flat = LegacyPosition(symbol="BTCUSDT", net_position=1, avg_entry_price=100).apply_fill(
        LegacyFill("flatten", "BTCUSDT", "SELL", 1, 110)
    )
    assert flat.net_position == 0
    assert flat.avg_entry_price == 0
    assert flat.realized_pnl == 10

    reduced_short = LegacyPosition(symbol="BTCUSDT", net_position=-2, avg_entry_price=100).apply_fill(
        LegacyFill("reduce", "BTCUSDT", "BUY", 1, 90)
    )
    assert reduced_short.net_position == -1
    assert reduced_short.avg_entry_price == 100
    assert reduced_short.realized_pnl == 10


@pytest.mark.parametrize(
    "fill",
    [
        replace(_fill(), fill_id=""),
        replace(_fill(), trade_id=""),
        _fill(instrument=""),
        _fill(venue=""),
        _fill(side=PositionSide.FLAT),
        _fill(quantity="0"),
        _fill(quantity="NaN"),
        _fill(price="Infinity"),
        _fill(fee="-1"),
        _fill(sequence=0),
        replace(_fill(), timestamp=datetime(2026, 1, 1)),  # noqa: DTZ001 - deliberate invalid naive fact
    ],
)
def test_projection_rejects_invalid_fill_atomically(fill: FillEvent) -> None:
    projection = PositionProjection()
    with pytest.raises(ValueError):
        projection.apply(fill)
    assert projection.fill_count == 0
    assert projection.rebuild() == {}


def test_projection_distinguishes_exact_duplicate_from_identity_conflict() -> None:
    projection = PositionProjection()
    fill = _fill()
    assert projection.apply(fill)
    assert not projection.apply(fill)

    with pytest.raises(ValueError, match="Conflicting trade identity"):
        projection.apply(replace(fill, price=Price(amount="101")))
    with pytest.raises(ValueError, match="Conflicting fill identity"):
        projection.apply(replace(fill, trade_id="trade-2"))
    with pytest.raises(ValueError, match="Conflicting fill sequence"):
        projection.apply(_fill("trade-3", sequence=1))
    assert projection.fill_count == 1


def test_position_aggregate_requires_matching_identity_and_monotonic_sequence() -> None:
    aggregate = PositionAggregate(InstrumentId("BTCUSDT"), VenueId("BINANCE"))
    aggregate.apply_fill(_fill())
    with pytest.raises(ValueError, match="instrument"):
        aggregate.apply_fill(_fill("trade-2", instrument="ETHUSDT", sequence=2))
    with pytest.raises(ValueError, match="venue"):
        aggregate.apply_fill(_fill("trade-2", venue="OTHER", sequence=2))
    with pytest.raises(ValueError, match="sequence"):
        aggregate.apply_fill(_fill("trade-2", sequence=1))

    corrupt = PositionAggregate(InstrumentId("BTCUSDT"), VenueId("BINANCE"), side=PositionSide.FLAT, quantity=1)
    with pytest.raises(ValueError, match="invalid economic state"):
        corrupt.apply_fill(_fill())


def test_projection_rebuilds_long_flat_reverse_and_short_pnl() -> None:
    projection = PositionProjection()
    for fill in (
        _fill("open-long", quantity="2", price="100", sequence=1),
        _fill("flatten-long", side=PositionSide.SHORT, quantity="2", price="110", sequence=2),
        _fill("open-short", side=PositionSide.SHORT, quantity="2", price="120", sequence=3),
        _fill("reduce-short", side=PositionSide.LONG, quantity="1", price="100", sequence=4),
        _fill("reverse-long", side=PositionSide.LONG, quantity="2", price="90", sequence=5),
    ):
        assert projection.apply(fill)

    position = projection.rebuild()["BTCUSDT:BINANCE"]
    assert position.side is PositionSide.LONG
    assert position.quantity == pytest.approx(1)
    assert position.avg_entry_price == pytest.approx(90)
    assert position.realized_pnl == pytest.approx(70)
    assert position.notional == pytest.approx(90)
    assert position.unrealized_pnl == 0


def test_reduce_only_and_reconciliation_use_signed_finite_quantities() -> None:
    aggregate = PositionAggregate(
        InstrumentId("BTCUSDT"), VenueId("BINANCE"), side=PositionSide.SHORT, quantity=1, avg_entry_price=100
    )
    assert aggregate.is_reduce_only_safe(1)
    assert not aggregate.is_reduce_only_safe(0)
    assert not aggregate.is_reduce_only_safe(-1)
    assert not aggregate.is_reduce_only_safe(float("nan"))

    projection = PositionProjection()
    assert projection.apply(_fill(side=PositionSide.SHORT))
    assert projection.reconcile_against({"BTCUSDT:BINANCE": -1}) == (True, [])
    ok, diffs = projection.reconcile_against({"BTCUSDT:BINANCE": 1})
    assert not ok
    assert diffs == ["BTCUSDT:BINANCE: system=-1.0 exchange=1"]

    ok, diffs = projection.reconcile_against({"BTCUSDT:BINANCE": float("nan")})
    assert not ok
    assert diffs == ["BTCUSDT:BINANCE: invalid exchange quantity"]
    ok, diffs = projection.reconcile_against({"BTCUSDT:BINANCE": object()})
    assert not ok
    assert diffs == ["BTCUSDT:BINANCE: invalid exchange quantity"]


@pytest.mark.parametrize(
    "fill",
    [
        _fill(quantity="not-a-number"),
        replace(_fill(), realized_pnl=MonetaryValue(amount="Infinity")),
    ],
)
def test_projection_rejects_malformed_optional_economics(fill: FillEvent) -> None:
    with pytest.raises(ValueError):
        PositionProjection().apply(fill)
