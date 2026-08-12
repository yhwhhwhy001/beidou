"""Fail-closed branch coverage for execution planning and emergency flattening."""

from __future__ import annotations

import json

import pytest

from beidou_safety.execution.conditional import EmergencyFlattenPolicy, PositionManager
from beidou_safety.execution.execution_plan_engine import (
    BoundPlanSlice,
    ExecutionAlgorithm,
    ExecutionPlanEngine,
)
from beidou_shared.types import AccountId, InstrumentId, Price, Quantity


def _policy() -> EmergencyFlattenPolicy:
    return EmergencyFlattenPolicy("flatten", "1", "signature")


def _slice(quantity: str = "1") -> BoundPlanSlice:
    return BoundPlanSlice.bind(
        slice_id="slice-1",
        symbol="BTCUSDT",
        side="SELL",
        quantity=quantity,
        limit_price="0",
        order_type="MARKET",
        time_in_force="IOC",
        reduce_only=True,
        position_effect="REDUCE_ONLY",
    )


def test_plan_selector_default_and_emergency_validation_failure_paths() -> None:
    engine = ExecutionPlanEngine()
    assert engine.select_algorithm("BTCUSDT", 1, spread_bps=6) is ExecutionAlgorithm.TWAP
    assert engine.validate_emergency_plan([], 0)
    assert not engine.validate_emergency_plan([_slice("not-a-number")], 1)


def test_position_manager_skips_unknown_and_flat_positions() -> None:
    manager = PositionManager()
    manager._positions["account:UNKNOWN"] = {"qty": None, "entry_price": None}
    manager.update_position(AccountId("account"), InstrumentId("FLAT"), Quantity(amount="0"), Price(amount="1"))
    assert manager.funding_rate_aware_position_size(2, 0.0, 1) == 2
    assert manager.funding_rate_aware_position_size(2, 0.002, 1) == pytest.approx(1.6)
    plan = manager.emergency_flatten(_policy())
    assert json.loads(plan["orders"]) == []
    assert plan["order_count"] == "0"


@pytest.mark.asyncio
async def test_execute_flatten_requires_governed_executor_and_valid_plan(monkeypatch) -> None:
    manager = PositionManager()
    manager.update_position(AccountId("account"), InstrumentId("BTCUSDT"), Quantity(amount="1"), Price(amount="100"))
    assert (await manager.execute_flatten(_policy()))["reason"] == "no_engine_reference"
    assert (await manager.execute_flatten(_policy(), object()))["reason"] == "governed_executor_unavailable"

    monkeypatch.setattr(manager, "emergency_flatten", lambda _policy: {"orders": "{"})
    assert (await manager.execute_flatten(_policy(), object()))["reason"] == "order_parse_error"


@pytest.mark.asyncio
async def test_execute_flatten_counts_rejected_and_failed_enqueue() -> None:
    manager = PositionManager()
    manager.update_position(AccountId("account"), InstrumentId("BTCUSDT"), Quantity(amount="1"), Price(amount="100"))
    manager.update_position(AccountId("account"), InstrumentId("ETHUSDT"), Quantity(amount="-2"), Price(amount="100"))

    class Engine:
        async def enqueue_reduce_only_market(self, *, symbol, **_kwargs):
            if symbol == "ETHUSDT":
                raise RuntimeError("venue unavailable")
            return False

    result = await manager.execute_flatten(_policy(), Engine())
    assert result["executed"] == "false"
    assert result["executed_count"] == "0"
    assert result["failed_count"] == "2"


@pytest.mark.asyncio
async def test_execute_flatten_reports_successful_governed_enqueue() -> None:
    manager = PositionManager()
    manager.update_position(AccountId("account"), InstrumentId("BTCUSDT"), Quantity(amount="1"), Price(amount="100"))

    class Engine:
        async def enqueue_reduce_only_market(self, **_kwargs):
            return True

    result = await manager.execute_flatten(_policy(), Engine())
    assert result["executed"] == "true"
    assert result["executed_count"] == "1"
    assert result["failed_count"] == "0"
