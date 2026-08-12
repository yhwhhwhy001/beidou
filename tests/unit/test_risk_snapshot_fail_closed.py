"""Fail-closed contracts for immutable and temporally honest risk snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from beidou_safety.risk.engine import RiskSnapshot


def _times(*, age_seconds: float = 1) -> dict[str, str]:
    received = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    observed = received - timedelta(milliseconds=10)
    source = observed - timedelta(milliseconds=10)
    return {
        "source_timestamp": source.isoformat(),
        "observed_at": observed.isoformat(),
        "received_at": received.isoformat(),
    }


def _snapshot(**overrides) -> RiskSnapshot:
    values = {
        "total_exposure": 10_000,
        "margin_used": 2_000,
        "margin_total": 20_000,
        "position_count": 1,
        "pending_orders": 0,
        "leverage": 2,
        "concentration_pct": 50,
        "tail_var_95": 500,
        "account_id": "account-1",
        "positions": {"BTCUSDT": {"quantity": "0.2", "tags": ["owned"]}},
        "orders": {"order-1": {"status": "ACKED"}},
        "dq_tier": "OK",
        "exchange_health": "HEALTHY",
        "reconciliation_status": "MATCHED",
        "portfolio_hash": "portfolio-hash",
        "policy_version": "policy-1",
        "correlation_id": "correlation-1",
        **_times(),
    }
    values.update(overrides)
    return RiskSnapshot(**values)


def test_snapshot_is_deeply_immutable_and_input_is_detached() -> None:
    positions = {"BTCUSDT": {"quantity": "0.2", "tags": ["owned"]}}
    snapshot = _snapshot(positions=positions)
    positions["BTCUSDT"]["quantity"] = "9"
    positions["BTCUSDT"]["tags"].append("mutated")

    assert snapshot.positions["BTCUSDT"]["quantity"] == "0.2"
    assert snapshot.positions["BTCUSDT"]["tags"] == ("owned",)
    with pytest.raises(TypeError):
        snapshot.positions["BTCUSDT"]["quantity"] = "1"
    with pytest.raises(AttributeError):
        snapshot.leverage = 99


def test_snapshot_rejects_conflicting_timestamp_aliases() -> None:
    with pytest.raises(ValueError, match="Conflicting"):
        _snapshot(timestamp="2026-01-01T00:00:00+00:00")


def test_nested_sets_are_frozen_and_canonical_hash_is_stable() -> None:
    times = _times()
    first = _snapshot(positions={"BTCUSDT": {"owners": {"beta", "alpha"}}}, **times)
    second = _snapshot(positions={"BTCUSDT": {"owners": {"alpha", "beta"}}}, **times)
    assert first.positions["BTCUSDT"]["owners"] == frozenset({"alpha", "beta"})
    assert first.snapshot_hash == second.snapshot_hash


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_id", "account-2"),
        ("tail_var_95", 501),
        ("positions", {"ETHUSDT": {"quantity": "1"}}),
        ("orders", {"order-2": {"status": "FILLED"}}),
        ("portfolio_hash", "portfolio-2"),
        ("source_timestamp", "2026-01-01T00:00:00+00:00"),
    ],
)
def test_snapshot_hash_binds_every_authoritative_fact(field: str, value) -> None:
    baseline = _snapshot()
    changed = _snapshot(**{field: value})
    assert baseline.snapshot_hash != changed.snapshot_hash


def test_missing_or_invalid_time_is_never_replaced_with_now() -> None:
    missing = _snapshot(source_timestamp="", observed_at="", received_at="")
    assert missing.timestamp == ""
    assert missing.age_seconds == float("inf")
    assert not missing.is_fresh()
    assert not missing.is_complete()
    assert not missing.is_safe_for_risk_increase()

    naive = _snapshot(received_at="2026-01-01T00:00:00")
    assert not naive.is_complete()
    future = _snapshot(**_times(age_seconds=-60))
    assert not future.is_fresh()
    reversed_order = _snapshot(
        source_timestamp="2026-01-01T00:00:02+00:00",
        observed_at="2026-01-01T00:00:01+00:00",
        received_at="2026-01-01T00:00:00+00:00",
    )
    assert not reversed_order.is_complete()


@pytest.mark.parametrize(
    "overrides",
    [
        {"total_exposure": True},
        {"margin_used": "not-a-number"},
        {"total_exposure": -1},
        {"margin_used": float("nan")},
        {"margin_total": -1},
        {"position_count": -1},
        {"position_count": True},
        {"pending_orders": 1.5},
        {"leverage": 0},
        {"leverage": float("inf")},
        {"concentration_pct": -1},
        {"tail_var_95": float("nan")},
        {"tail_var_95": True},
        {"tail_var_95": "not-a-number"},
        {"positions": []},
        {"orders": []},
    ],
)
def test_invalid_economic_snapshot_cannot_be_constructed(overrides) -> None:
    with pytest.raises(ValueError):
        _snapshot(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"dq_tier": "GARBAGE"},
        {"dq_tier": "BLOCK"},
        {"exchange_health": "GARBAGE"},
        {"exchange_health": "UNSAFE"},
        {"reconciliation_status": "GARBAGE"},
        {"reconciliation_status": "MISMATCHED"},
    ],
)
def test_only_explicit_healthy_matched_facts_allow_risk_increase(overrides) -> None:
    snapshot = _snapshot(**overrides)
    assert not snapshot.is_safe_for_risk_increase()


def test_fresh_complete_snapshot_is_safe_and_has_wall_clock_age() -> None:
    snapshot = _snapshot()
    assert snapshot.is_complete()
    assert snapshot.is_fresh(60)
    assert 0 <= snapshot.age_seconds < 60
    assert snapshot.is_safe_for_risk_increase()
    assert not snapshot.is_fresh(0)
    assert not snapshot.is_fresh(float("nan"))


def test_invalid_timestamp_text_is_incomplete() -> None:
    assert not _snapshot(received_at="not-a-timestamp").is_complete()
