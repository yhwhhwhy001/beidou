"""Fail-closed contracts for pre-risk and snapshot risk evaluation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from beidou_safety.risk import PreRiskChecker, PreRiskContext
from beidou_safety.risk.engine import RiskEngineImpl, RiskRuleLevel, RiskSnapshot, _RiskCheckResult
from beidou_shared.types import (
    AccountId,
    AccountRef,
    CorrelationId,
    InstrumentId,
    MonetaryValue,
    OrderId,
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


def test_pre_risk_rejects_notional_exceeding_margin() -> None:
    """M10-F01: 保证金检查（原引擎内联,收敛到 checker）—— 名义 > 余额×杠杆 → 拒绝。"""
    import asyncio

    from beidou_safety.risk import PreRiskChecker, PreRiskContext
    from beidou_safety.risk.engine import RiskDecision

    checker = PreRiskChecker(max_leverage=3.0)
    context = PreRiskContext(
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        order_quantity=Quantity(amount="10"),
        order_price=MonetaryValue(amount="200", currency="USDT"),
        leverage=3.0,
        account_balance=500.0,  # 名义 2000 > 500×3=1500
    )
    results = asyncio.run(checker.check(context))
    assert any(r.decision != RiskDecision.APPROVED and "margin" in r.reason for r in results)


def test_pre_risk_rejects_too_many_pending_orders() -> None:
    """M10-F01: 挂单上限检查（原引擎内联,收敛到 checker）。"""
    import asyncio

    from beidou_safety.risk import PreRiskChecker, PreRiskContext
    from beidou_safety.risk.engine import RiskDecision

    checker = PreRiskChecker(max_leverage=3.0, max_pending_orders=5)
    context = PreRiskContext(
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        order_quantity=Quantity(amount="1"),
        order_price=MonetaryValue(amount="100", currency="USDT"),
        leverage=2.0,
        account_balance=10000.0,
        pending_orders=[OrderId(f"o{i}") for i in range(5)],
    )
    results = asyncio.run(checker.check(context))
    assert any(r.decision != RiskDecision.APPROVED and "pending" in r.reason for r in results)


def test_pre_risk_approves_within_all_limits() -> None:
    import asyncio

    from beidou_safety.risk import PreRiskChecker, PreRiskContext
    from beidou_safety.risk.engine import RiskDecision

    checker = PreRiskChecker(max_leverage=3.0, max_pending_orders=5)
    context = PreRiskContext(
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        order_quantity=Quantity(amount="1"),
        order_price=MonetaryValue(amount="100", currency="USDT"),
        leverage=2.0,
        account_balance=10000.0,
        pending_orders=[OrderId("o0")],
    )
    results = asyncio.run(checker.check(context))
    assert all(r.decision == RiskDecision.APPROVED for r in results)


# --- M10-R2: 对抗审查反例回归 ---


def test_pre_risk_call_site_shape_no_type_error() -> None:
    """生产调用点原样形状(engine nearline):AccountRef 必须关键字构造。

    回归 M10#1/M11#2: 位置参数构造抛 pydantic v2 TypeError,且函数内
    局部 import AccountRef 使上游意图构造 UnboundLocalError —— 近线
    下单路径整体死亡。按调用点形状构造必须不抛任何异常。
    """
    from beidou_safety.risk.engine import _PreRiskContext

    pre_context = _PreRiskContext(
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("default")),
        instrument_id=InstrumentId("BTCUSDT"),
        order_quantity=Quantity(amount="0.1"),
        order_price=MonetaryValue(amount="50000", currency="USDT"),
        leverage=3.0,
        account_balance=10000.0,
        pending_orders=[str(oid) for oid in (OrderId("o-1"), OrderId("o-2"))],
        risk_increasing=True,
    )
    checker = PreRiskChecker()
    results = asyncio.run(checker.check(pre_context))
    assert all(r.decision == RiskDecision.APPROVED for r in results)


def test_pre_risk_margin_missing_fail_closed_for_risk_increasing() -> None:
    """balance 缺失 + risk_increasing → REJECTED(fail-closed),不再静默跳过。"""
    checker = PreRiskChecker()
    context = _context(
        account_balance=None,
        risk_increasing=True,
    )
    results = asyncio.run(checker.check(context))
    assert any(
        r.decision != RiskDecision.APPROVED and "margin check unavailable" in r.reason.lower()
        for r in results
    )


def test_pre_risk_margin_missing_does_not_block_reduce() -> None:
    """balance 缺失 + 非风险增加方向 → 不因保证金检查拒绝(减仓永不封锁)。"""
    checker = PreRiskChecker()
    context = _context(
        account_balance=None,
        risk_increasing=False,
        order_quantity=Quantity(amount="0.05"),  # 低于 current position,减仓意图
        current_position=Quantity(amount="0.1"),
    )
    results = asyncio.run(checker.check(context))
    assert not any("margin" in r.reason.lower() for r in results)


def test_pre_risk_margin_effective_balance_deduction() -> None:
    """保证金口径:可用权益 = balance - current_margin,扣减后超限拒绝。"""
    checker = PreRiskChecker()
    context = _context(
        account_balance=1000.0,
        current_margin=MonetaryValue(amount="800", currency="USDT"),
        order_quantity=Quantity(amount="5"),
        order_price=MonetaryValue(amount="100", currency="USDT"),
        leverage=2.0,
        risk_increasing=True,
    )
    results = asyncio.run(checker.check(context))
    # notional=500 > (1000-800)*2=400 → 拒绝
    assert any(r.decision != RiskDecision.APPROVED and "exceeds margin" in r.reason for r in results)


def test_projection_counts_extra_inflight_notional() -> None:
    """同轮已批准敞口计入投影(batch 集体绕过窗口闭合)。"""
    from types import SimpleNamespace

    from beidou_core.engine import AutonomousEngine

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._symbols = ["BTCUSDT"]
    engine._outbox = SimpleNamespace(inflight_signed_quantity=lambda sym: 0.0)
    engine._last_prices = {"BTCUSDT": 100.0}
    engine._protection = SimpleNamespace(all_positions=lambda: {})
    risk_increasing, projected = engine._portfolio_exposure_projection(
        "BTCUSDT", 100.0, extra_inflight_notional=500.0
    )
    assert risk_increasing is True
    assert projected == 600.0


def test_projection_batch_accumulation_blocks_collective_bypass() -> None:
    """两单批处理:第一单过门后累加,第二单投影必须包含第一单敞口。"""
    from types import SimpleNamespace

    from beidou_core.engine import AutonomousEngine

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._symbols = ["BTCUSDT", "ETHUSDT"]
    engine._outbox = SimpleNamespace(inflight_signed_quantity=lambda sym: 0.0)
    engine._last_prices = {"BTCUSDT": 100.0, "ETHUSDT": 100.0}
    engine._protection = SimpleNamespace(all_positions=lambda: {})

    _ri1, projected_first = engine._portfolio_exposure_projection("BTCUSDT", 100.0)
    assert projected_first == 100.0
    batch_approved = 100.0  # 第一单过门后累加
    _ri2, projected_second = engine._portfolio_exposure_projection(
        "ETHUSDT", 100.0, extra_inflight_notional=batch_approved
    )
    assert projected_second == 200.0
