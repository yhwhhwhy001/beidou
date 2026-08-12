"""Fail-closed contracts for protected position lifecycle state."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from beidou_safety.protection.lifecycle import PositionAggregate, PositionManager


def _position(position_id: str = "position-1", **overrides) -> PositionAggregate:
    values = {
        "position_id": position_id,
        "instrument_id": "BTCUSDT",
        "venue_id": "BINANCE",
        "side": "LONG",
        "entry_price": 100.0,
        "quantity": 2.0,
    }
    values.update(overrides)
    return PositionAggregate(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"position_id": ""},
        {"instrument_id": ""},
        {"venue_id": ""},
        {"side": "FLAT"},
        {"entry_price": 0},
        {"entry_price": float("nan")},
        {"quantity": 0},
        {"quantity": float("inf")},
        {"closed_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {"realized_pnl": float("nan")},
        {"protected": True},
        {"take_profit_ids": ["tp-1", "tp-1"]},
        {"created_at": datetime(2026, 1, 1)},  # noqa: DTZ001 - deliberate invalid naive fact
    ],
)
def test_position_creation_rejects_invalid_state(overrides) -> None:
    with pytest.raises(ValueError):
        _position(**overrides)


def test_increase_requires_open_position_and_positive_finite_economics() -> None:
    position = _position()
    for qty, price in ((0, 100), (-1, 100), (1, 0), (float("nan"), 100), (1, float("inf"))):
        with pytest.raises(ValueError):
            position.increase(qty, price)
    position.increase(1, 130)
    assert position.quantity == 3
    assert position.entry_price == pytest.approx(110)

    position.reduce(3, 120)
    with pytest.raises(ValueError, match="open"):
        position.increase(1, 100)


def test_reduce_cannot_be_zero_nonfinite_or_cross_flat() -> None:
    position = _position()
    for qty, price in ((0, 100), (-1, 100), (3, 100), (float("nan"), 100), (1, float("inf"))):
        with pytest.raises(ValueError):
            position.reduce(qty, price)
    assert position.quantity == 2
    assert position.realized_pnl == 0

    assert position.reduce(1, 110) == pytest.approx(10)
    assert position.quantity == 1
    assert position.closed_at is None
    assert position.reduce(1, 90) == pytest.approx(-10)
    assert position.quantity == 0
    assert position.closed_at is not None
    with pytest.raises(ValueError, match="open"):
        position.reduce(1, 100)


def test_protect_requires_open_position_and_valid_unique_ids() -> None:
    position = _position()
    for stop_id, take_ids in (("", None), ("sl-1", [""]), ("sl-1", ["tp-1", "tp-1"])):
        with pytest.raises(ValueError):
            position.protect(stop_id, take_ids)
    take_ids = ["tp-1"]
    position.protect("sl-1", take_ids)
    take_ids.append("mutated")
    assert position.is_protected
    assert position.take_profit_ids == ["tp-1"]

    position.reduce(2, 100)
    with pytest.raises(ValueError, match="open"):
        position.protect("sl-2")


def test_manager_rejects_conflicting_identity_and_reports_truth() -> None:
    manager = PositionManager()
    position = _position()
    assert manager.open(position) is position
    assert manager.open(position) is position
    with pytest.raises(ValueError, match="already exists"):
        manager.open(_position(entry_price=101))

    assert manager.get("position-1") is position
    assert manager.get("missing") is None
    assert manager.total_exposure() == 200
    assert manager.protection_coverage_pct() == 0
    position.protect("sl-1")
    assert manager.protection_coverage_pct() == 100
    assert manager.close_position("missing", 100) == 0
    assert manager.close_position("position-1", 110) == 20
    assert manager.close_position("position-1", 110) == 0
    assert manager.total_exposure() == 0
    assert manager.protection_coverage_pct() == 100
