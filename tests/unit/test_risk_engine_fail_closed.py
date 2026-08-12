"""Fail-closed contracts for pre-risk and snapshot risk evaluation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from beidou_safety.risk import PreRiskContext
from beidou_safety.risk.engine import RiskEngineImpl, RiskRuleLevel, RiskSnapshot, _RiskCheckResult
from beidou_shared.types import (
    AccountId,
    AccountRef,
    CorrelationId,
    InstrumentId,
    Price,
    Quantity,
    RiskDecision,
    VenueId,
)


def _context(**overrides) -> PreRiskContext:
    values = {
        "account_ref": AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        "instrument_id": InstrumentId("BTCUSDT"),
        "order_quantity": Quantity(amount="0.1"),
        "order_price": Price(amount="50000"),
        "leverage": 2.0,
        "correlation_id": CorrelationId("pre-risk"),
    }
    values.update(overrides)
    return PreRiskContext(**values)


def _snapshot(**overrides) -> RiskSnapshot:
    received = datetime.now(timezone.utc) - timedelta(seconds=1)
    values = {
        "total_exposure": 5_000,
        "margin_used": 2_500,
        "margin_total": 10_000,
        "position_count": 1,
        "pending_orders": 0,
        "leverage": 2,
        "concentration_pct": 50,
        "account_id": "account-1",
        "dq_tier": "OK",
        "exchange_health": "HEALTHY",
        "reconciliation_status": "MATCHED",
        "portfolio_hash": "portfolio-1",
        "policy_version": "policy-1",
        "correlation_id": "risk-eval",
        "source_timestamp": (received - timedelta(milliseconds=20)).isoformat(),
        "observed_at": (received - timedelta(milliseconds=10)).isoformat(),
        "received_at": received.isoformat(),
    }
    values.update(overrides)
    return RiskSnapshot(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"leverage": None},
        {"leverage": float("nan")},
        {"order_price": None},
        {"order_price": Price(amount="NaN")},
        {"order_price": Price(amount="not-a-number")},
        {"order_quantity": Quantity(amount="Infinity")},
    ],
)
def test_pre_risk_rejects_unknown_or_nonfinite_order_economics(overrides) -> None:
    from beidou_safety.risk.engine import PreRiskCheckerImpl

    results = asyncio.run(PreRiskCheckerImpl().check(_context(**overrides)))
    assert results[0].decision is RiskDecision.REJECTED


def test_pre_risk_always_enforces_order_notional() -> None:
    from beidou_safety.risk.engine import PreRiskCheckerImpl

    results = asyncio.run(
        PreRiskCheckerImpl(max_position_notional=100).check(_context(current_margin=None, current_position=None))
    )
    assert results[0].rule_level is RiskRuleLevel.R2
    assert results[0].decision is RiskDecision.REJECTED


@pytest.mark.parametrize(
    ("pre_risk_passed", "results"),
    [
        (False, []),
        (True, []),
        (
            True,
            [
                _RiskCheckResult(
                    RiskRuleLevel.R4,
                    RiskDecision.REJECTED,
                    "rejected",
                    CorrelationId("risk-eval"),
                )
            ],
        ),
    ],
)
def test_risk_decision_never_approves_missing_or_rejected_evidence(pre_risk_passed, results) -> None:
    decision = asyncio.run(RiskEngineImpl().evaluate(pre_risk_passed, results))
    assert decision is RiskDecision.REJECTED


def test_risk_decision_approves_present_all_pass_evidence() -> None:
    result = _RiskCheckResult(
        RiskRuleLevel.R0,
        RiskDecision.APPROVED,
        "passed",
        CorrelationId("risk-eval"),
    )
    assert asyncio.run(RiskEngineImpl().evaluate(True, [result])) is RiskDecision.APPROVED


def test_full_evaluate_rejects_incomplete_or_unsafe_snapshot_before_limits() -> None:
    engine = RiskEngineImpl()
    config = {"max_leverage": 3.0, "max_concentration_pct": 60.0}

    incomplete = _snapshot(account_id="")
    unsafe = _snapshot(reconciliation_status="MISMATCHED")

    assert asyncio.run(engine.full_evaluate(incomplete, config))[0].decision is RiskDecision.REJECTED
    assert asyncio.run(engine.full_evaluate(unsafe, config))[0].decision is RiskDecision.REJECTED


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"max_leverage": float("nan"), "max_concentration_pct": 60},
        {"max_leverage": 3, "max_concentration_pct": 0},
    ],
)
def test_full_evaluate_rejects_missing_or_invalid_policy_limits(config) -> None:
    results = asyncio.run(RiskEngineImpl().full_evaluate(_snapshot(), config))
    assert results[0].decision is RiskDecision.REJECTED


def test_full_evaluate_enforces_both_limits_and_registered_rules() -> None:
    engine = RiskEngineImpl()
    engine.add_rule(
        RiskRuleLevel.R8,
        lambda _snapshot, _config: _RiskCheckResult(
            RiskRuleLevel.R8,
            RiskDecision.REJECTED,
            "custom rejection",
            CorrelationId("custom"),
        ),
    )
    results = asyncio.run(
        engine.full_evaluate(
            _snapshot(leverage=4, concentration_pct=70),
            {"max_leverage": 3.0, "max_concentration_pct": 60.0},
        )
    )
    assert {result.rule_level for result in results} == {RiskRuleLevel.R4, RiskRuleLevel.R5, RiskRuleLevel.R8}
    assert all(result.decision is RiskDecision.REJECTED for result in results)


def test_full_evaluate_returns_builtin_approval_when_no_rule_rejects() -> None:
    results = asyncio.run(
        RiskEngineImpl().full_evaluate(
            _snapshot(),
            {"max_leverage": 3.0, "max_concentration_pct": 60.0},
        )
    )
    assert len(results) == 1
    assert results[0].rule_level is RiskRuleLevel.R0
    assert results[0].decision is RiskDecision.APPROVED


def test_registered_rule_failure_or_invalid_result_rejects() -> None:
    def broken(_snapshot, _config):
        raise RuntimeError("rule unavailable")

    for callback in (broken, lambda _snapshot, _config: object()):
        engine = RiskEngineImpl()
        engine.add_rule(RiskRuleLevel.R10, callback)
        results = asyncio.run(
            engine.full_evaluate(
                _snapshot(),
                {"max_leverage": 3.0, "max_concentration_pct": 60.0},
            )
        )
        assert results[-1].rule_level is RiskRuleLevel.R10
        assert results[-1].decision is RiskDecision.REJECTED


def test_add_rule_rejects_non_callable_and_async_rule_is_awaited() -> None:
    engine = RiskEngineImpl()
    with pytest.raises(TypeError, match="callable"):
        engine.add_rule(RiskRuleLevel.R1, None)

    async def passing(_snapshot, _config):
        return _RiskCheckResult(
            RiskRuleLevel.R1,
            RiskDecision.APPROVED,
            "custom pass",
            CorrelationId("custom"),
        )

    engine.add_rule(RiskRuleLevel.R1, passing)
    results = asyncio.run(
        engine.full_evaluate(
            _snapshot(),
            {"max_leverage": 3.0, "max_concentration_pct": 60.0},
        )
    )
    assert results == [
        _RiskCheckResult(
            RiskRuleLevel.R1,
            RiskDecision.APPROVED,
            "custom pass",
            CorrelationId("custom"),
            timestamp=results[0].timestamp,
        )
    ]
