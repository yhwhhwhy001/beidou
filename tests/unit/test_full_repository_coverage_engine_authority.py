"""Behavior-backed coverage for engine authority and stream boundaries."""

from __future__ import annotations

import asyncio
import builtins
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import ClassVar

import pytest

import beidou_core.engine as engine_module
from beidou_core.engine import AutonomousEngine
from beidou_data.trading_pool_lifecycle import PoolStatus
from beidou_exchange.core.protocol import UserOrderUpdate, UserStreamEvent
from beidou_research.factors.factor import FactorLifecycle
from beidou_safety.execution import OrderIntent
from beidou_safety.execution.reconciliation import AccountFactSnapshot
from beidou_safety.execution.user_events import UserProjectionStatus
from beidou_security.identity import Credential, CredentialType, KeyRotationStatus, KeyRotator
from beidou_shared.types import (
    AccountId,
    AccountRef,
    InstrumentId,
    MonetaryValue,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
    VenueId,
)


def _bare() -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._state_backend_supported = True
    engine._policy_error = None
    engine._risk_sm = SimpleNamespace(
        is_valid_for_use=lambda *_args, **_kwargs: True,
        consume=lambda _approval: setattr(engine, "consumed", True),
        is_consumed=lambda _approval: getattr(engine, "consumed", False),
    )
    engine._approval = SimpleNamespace(verify=lambda *_args, **_kwargs: asyncio.sleep(0, result=True))
    return engine


def _intent() -> OrderIntent:
    return OrderIntent(
        intent_id="authority-intent",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("default")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="1"),
        client_order_id="cid-authority",
        idempotency_key="idem-authority",
        risk_approval_id="approval-1",
        risk_approval_signature="sig",
        risk_nonce="nonce",
        risk_snapshot_hash="risk",
        risk_policy_version="policy:1",
    )


@pytest.mark.asyncio
async def test_verify_intent_at_send_fail_closed_matrix() -> None:
    intent = _intent()
    intent = replace(intent, risk_intent_hash=engine_module.order_intent_binding_hash(intent))
    engine = _bare()

    engine._can_write = False
    assert await engine._verify_intent_at_send(intent) is True
    engine._can_write = True
    engine._state_backend_supported = False
    assert await engine._verify_intent_at_send(intent) is False
    intent = replace(
        intent,
        reduce_only=True,
        risk_intent_hash=engine_module.order_intent_binding_hash(replace(intent, reduce_only=True)),
    )
    assert await engine._verify_intent_at_send(intent) is True
    intent = replace(
        intent,
        reduce_only=False,
        risk_intent_hash=engine_module.order_intent_binding_hash(replace(intent, reduce_only=False)),
    )
    engine._state_backend_supported = True
    engine._policy_error = "unsigned"
    assert await engine._verify_intent_at_send(intent) is False
    intent = replace(
        intent,
        reduce_only=True,
        risk_intent_hash=engine_module.order_intent_binding_hash(replace(intent, reduce_only=True)),
    )
    assert await engine._verify_intent_at_send(intent) is True
    intent = replace(intent, risk_approval_id="RISK_EXEMPT_CLOSE")
    assert await engine._verify_intent_at_send(intent) is False
    intent = replace(intent, risk_approval_id="")
    assert await engine._verify_intent_at_send(intent) is False
    intent = replace(intent, risk_approval_id="approval-1", risk_intent_hash="tampered")
    assert await engine._verify_intent_at_send(intent) is False

    intent = replace(intent, risk_intent_hash=engine_module.order_intent_binding_hash(intent))
    engine._risk_sm.is_valid_for_use = lambda *_args, **_kwargs: False
    assert await engine._verify_intent_at_send(intent) is False

    engine._risk_sm.is_valid_for_use = lambda *_args, **_kwargs: True
    engine._approval.verify = lambda *_args, **_kwargs: asyncio.sleep(0, result=False)
    assert await engine._verify_intent_at_send(intent) is False
    engine._approval.verify = lambda *_args, **_kwargs: asyncio.sleep(0, result=True)
    assert await engine._verify_intent_at_send(intent, consume_nonce=True) is True
    engine._approval.verify = lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad signature"))
    assert await engine._verify_intent_at_send(intent) is False


def test_protection_algo_params_rejects_rounding_to_zero() -> None:
    order = SimpleNamespace(
        quantity=SimpleNamespace(amount="1"),
        trigger_price=SimpleNamespace(amount="0.4"),
        order_type="STOP_MARKET",
        side=OrderSide.SELL,
    )
    with pytest.raises(ValueError, match="trigger rounds to zero"):
        AutonomousEngine._protection_algo_params(
            order, symbol="BTCUSDT", side="SELL", precision={"quantity": 0, "price": 0}
        )


def _facts() -> AccountFactSnapshot:
    return AccountFactSnapshot(
        account_id=AccountId("default"),
        venue_id=VenueId("BINANCE"),
        balance=MonetaryValue(amount="1000", currency="USDT"),
        positions={},
        open_orders=[],
        timestamp=datetime.now(timezone.utc),
        source="test",
        fact_version="1",
        complete=True,
    )


def test_record_reconciliation_failure_persists_event_and_fail_closed() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    result = SimpleNamespace(
        checked_at=datetime.now(timezone.utc),
        status=SimpleNamespace(value="INCOMPLETE"),
        matched=False,
        differences=["missing"],
        event_facts=_facts(),
    )
    saved: list[tuple] = []
    engine._store = SimpleNamespace(
        save_reconciliation_snapshot=lambda *args: saved.append(("snapshot", args)),
        save_reconciliation_result=lambda *args, **kwargs: saved.append(("result", args, kwargs)),
    )
    engine._control = SimpleNamespace(get_status=lambda: "RESUME")
    engine._safe_no_new_risk = lambda reason: setattr(engine, "blocked", reason)
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: setattr(engine, "incident", (args, kwargs)))
    assert engine._record_reconciliation_failure(result, system_facts=_facts(), exchange_facts=_facts()) is False
    assert any(item[0] == "snapshot" and item[1][1] == "EVENT_STREAM" for item in saved[0:3])
    assert engine.blocked == "auto"
    assert engine.incident[1]["category"] == "reconciliation"

    failing = SimpleNamespace(
        checked_at=datetime.now(timezone.utc),
        status=SimpleNamespace(value="ERROR"),
        matched=False,
        differences=[],
    )
    engine._store.save_reconciliation_result = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk"))
    assert engine._record_reconciliation_failure(failing) is False
    assert any("RECON_PERSISTENCE_ERROR: OSError" in item for item in failing.differences)


class _Projection:
    def __init__(self, status: UserProjectionStatus, event_id: str = "event") -> None:
        self.status = status
        self.event_id = event_id
        self.reason = "blocked"


def _update(event_type: str, status: str = "FILLED") -> UserOrderUpdate:
    return UserOrderUpdate(
        event=UserStreamEvent(
            event_type=event_type,
            event_id=f"event-{event_type}",
            event_time_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            transaction_time_ms=None,
            sequence=1,
            raw_event={},
        ),
        order_id="order-1",
        client_order_id="client-1",
        symbol=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        order_status=OrderStatus(status),
        execution_type="TRADE",
        original_quantity=Quantity(amount="1"),
        cumulative_quantity=Quantity(amount="0.5"),
        last_quantity=Quantity(amount="0.5"),
        last_price=Price(amount="100"),
        average_price=Price(amount="100"),
        trade_id="trade-1",
        commission=MonetaryValue(amount="0", currency="USDT"),
    )


def test_user_stream_order_and_account_authority_boundaries() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = SimpleNamespace(
        save_order_state=lambda *args, **kwargs: setattr(engine, "saved_order", (args, kwargs))
    )
    engine._outbox = SimpleNamespace(project_user_order_update=lambda _update: None)
    engine._active_order_ids = set()
    engine._recon = SimpleNamespace(update_event_facts=lambda facts: setattr(engine, "event_facts", facts))
    engine._record_execution_fact_failure_env_guarded = lambda reason: setattr(engine, "blocked", reason)
    engine._record_trade_lite_fill = lambda _update: setattr(engine, "trade_lite", True)
    engine._consume_cumulative_fill = lambda *_args, **_kwargs: (0.5, 100.0, "fill-1")
    engine._record_partial_fill_to_ledger = lambda *args, **kwargs: setattr(engine, "partial", (args, kwargs))

    class Projector:
        def __init__(self, result: _Projection) -> None:
            self.result = result

        def ingest(self, _update: object) -> _Projection:
            return self.result

        def ingest_account_update(self, _update: object) -> _Projection:
            return self.result

        def fact_snapshot(self) -> object:
            return {"facts": True}

    accepted = Projector(_Projection(UserProjectionStatus.ACCEPTED))
    engine._user_stream_projector = accepted
    assert engine.ingest_user_order_update(_update("ORDER_TRADE_UPDATE")) is True
    assert engine.partial[0][0] == "order-1"

    engine._user_stream_projector = Projector(_Projection(UserProjectionStatus.ACCEPTED))
    assert engine.ingest_user_order_update(_update("TRADE_LITE")) is True
    assert engine.trade_lite is True

    engine._user_stream_projector = Projector(_Projection(UserProjectionStatus.ACCEPTED))
    assert engine.ingest_user_account_update(SimpleNamespace()) is True
    assert engine.event_facts == {"facts": True}

    engine._user_stream_projector = Projector(_Projection(UserProjectionStatus.BLOCKED))
    assert engine.ingest_user_account_update(SimpleNamespace()) is False
    engine._user_stream_projector = Projector(_Projection(UserProjectionStatus.BLOCKED))
    assert engine.ingest_user_order_update(_update("ORDER_TRADE_UPDATE")) is False

    engine._user_stream_projector = Projector(_Projection(UserProjectionStatus.ACCEPTED))
    engine._outbox.project_user_order_update = lambda _update: (_ for _ in ()).throw(RuntimeError("projection"))
    assert engine.ingest_user_order_update(_update("ORDER_TRADE_UPDATE", status="NEW")) is False

    engine._user_stream_projector = Projector(_Projection(UserProjectionStatus.ACCEPTED))
    engine._outbox.project_user_order_update = lambda _update: None
    engine._active_order_ids = set()
    engine._store.save_order_state = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("state"))
    assert engine.ingest_user_order_update(_update("ORDER_TRADE_UPDATE", status="CANCELED")) is True

    # A TRADE_LITE accounting failure is observable but must not turn an
    # otherwise accepted user event into a projection failure.
    engine._user_stream_projector = Projector(_Projection(UserProjectionStatus.ACCEPTED))
    engine._record_trade_lite_fill = lambda _update: (_ for _ in ()).throw(RuntimeError("ledger"))
    assert engine.ingest_user_order_update(_update("TRADE_LITE")) is True

    # A blocked TRADE_LITE event is deferred by the stream boundary and does
    # not invoke the execution-fact failure gate.
    engine._user_stream_projector = Projector(_Projection(UserProjectionStatus.BLOCKED))
    assert engine.ingest_user_order_update(_update("TRADE_LITE")) is False

    # Exercise the real projector-construction path.  No replay baseline is
    # authorized here, so the event remains fail-closed at the sequencer.
    fresh = AutonomousEngine.__new__(AutonomousEngine)
    fresh._store = None
    fresh._active_order_ids = set()
    fresh._record_execution_fact_failure_env_guarded = lambda reason: setattr(fresh, "blocked", reason)
    assert fresh.ingest_user_order_update(_update("ORDER_TRADE_UPDATE")) is False


def test_user_stream_replay_and_account_config_branches() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = SimpleNamespace()
    engine._recon = SimpleNamespace(update_event_facts=lambda facts: setattr(engine, "event_facts", facts))
    engine._record_execution_fact_failure_env_guarded = lambda reason: setattr(engine, "blocked", reason)

    class Projector:
        def __init__(self, result: _Projection) -> None:
            self.result = result

        def authorize_replay_baseline(self, *_args, **_kwargs) -> _Projection:
            return self.result

        def fact_snapshot(self) -> object:
            return "snapshot"

    engine._user_stream_projector = Projector(_Projection(UserProjectionStatus.ACCEPTED))
    assert engine.authorize_user_stream_replay(_facts(), evidence_hash="h", approval_id="a") is True
    engine._user_stream_projector = Projector(_Projection(UserProjectionStatus.BLOCKED))
    assert engine.authorize_user_stream_replay(_facts(), evidence_hash="h", approval_id="a") is False

    engine._leverage_cache = {"BTCUSDT": 5, "ETHUSDT": 3}
    assert engine._ingest_account_config_update({"ac": {"s": "BTCUSDT", "l": "10"}, "ai": {"j": "true"}})
    assert "BTCUSDT" not in engine._leverage_cache
    assert engine._ingest_account_config_update({"ac": {"s": "ETHUSDT", "l": "bad"}, "ai": {"j": object()}})
    assert engine._ingest_account_config_update({})

    class BadString:
        def __str__(self) -> str:
            raise TypeError("malformed joint flag")

    assert engine._ingest_account_config_update({"ai": {"j": BadString()}})

    fresh = AutonomousEngine.__new__(AutonomousEngine)
    fresh._store = None
    fresh._recon = SimpleNamespace(update_event_facts=lambda facts: setattr(fresh, "event_facts", facts))
    fresh._record_execution_fact_failure_env_guarded = lambda reason: setattr(fresh, "blocked", reason)
    assert (
        fresh.authorize_user_stream_replay(_facts(), evidence_hash="h", approval_id="a", allow_unsequenced=True) is True
    )
    account_update = SimpleNamespace(
        event=UserStreamEvent(
            event_type="ACCOUNT_UPDATE",
            event_id="account-event",
            event_time_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            transaction_time_ms=None,
            sequence=1,
            raw_event={},
        ),
        reason="ORDER",
        balances=(),
        positions=(),
    )
    assert fresh.ingest_user_account_update(account_update) is True


def test_trading_eligibility_delegates_to_the_single_truth_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    snapshot = object()
    eligibility = object()
    engine.build_truth_snapshot = lambda: snapshot
    monkeypatch.setattr(engine_module, "derive_eligibility", lambda value: eligibility if value is snapshot else None)
    assert engine.evaluate_trading_eligibility() is eligibility


def test_credential_health_permissions_post_risk_and_signed_position() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: setattr(engine, "alerts", (args, kwargs)))
    engine._can_trade = True
    engine._can_withdraw = False
    engine._key_rotator = KeyRotator()
    engine._env_mode = SimpleNamespace(value="live")

    expired = Credential(
        credential_id="expired",
        credential_type=CredentialType.TRADING,
        status=KeyRotationStatus.EXPIRED,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    engine._credential = expired
    health = engine._check_credential_health()
    assert health["level"] == "CRITICAL" and engine._can_trade is False

    warning = Credential(
        credential_id="warning",
        credential_type=CredentialType.TRADING,
        expires_at=datetime.now(timezone.utc) + timedelta(days=10),
    )
    engine._credential = warning
    engine._can_trade = True
    engine._key_rotator.register(warning)
    health = engine._check_credential_health()
    assert health["level"] == "WARNING" and health["rotation_started"] == "SUCCESS"

    rotating = Credential(
        credential_id="rotating",
        credential_type=CredentialType.TRADING,
        status=KeyRotationStatus.ROTATING,
        expires_at=datetime.now(timezone.utc) + timedelta(days=60),
    )
    pending = Credential(credential_id="new", credential_type=CredentialType.TRADING)
    engine._credential = rotating
    engine._key_rotator.register(rotating)
    engine._pending_credential = pending
    health = engine._check_credential_health()
    assert health["level"] == "WARNING" and health["rotation_completed"] == "SUCCESS"

    active = Credential(credential_id="withdraw", credential_type=CredentialType.TRADING)
    engine._credential = active
    engine._can_trade = True
    engine._can_withdraw = True
    health = engine._check_credential_health()
    assert health["level"] == "CRITICAL" and health["r9_violation"] == "WITHDRAW_ENABLED"

    expiring_error = Credential(
        credential_id="expiring-error",
        credential_type=CredentialType.TRADING,
        expires_at=datetime.now(timezone.utc) + timedelta(days=10),
    )
    engine._credential = expiring_error
    engine._can_withdraw = False
    engine._key_rotator = SimpleNamespace(
        start_rotation=lambda _credential_id: (_ for _ in ()).throw(RuntimeError("rotation")),
    )
    health = engine._check_credential_health()
    assert health["level"] == "WARNING" and health["rotation_error"] == "RuntimeError"

    rotating_error = Credential(
        credential_id="rotating-error",
        credential_type=CredentialType.TRADING,
        status=KeyRotationStatus.ROTATING,
        expires_at=datetime.now(timezone.utc) + timedelta(days=60),
    )
    engine._credential = rotating_error
    engine._pending_credential = pending
    engine._key_rotator = SimpleNamespace(
        complete_rotation=lambda *_args: (_ for _ in ()).throw(RuntimeError("complete")),
    )
    health = engine._check_credential_health()
    assert health["level"] == "WARNING" and health["rotation_error"] == "RuntimeError"

    engine._credential = expired
    engine._alerts = SimpleNamespace(
        send_incident=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("alert")),
    )
    health = engine._check_credential_health()
    assert health["level"] == "UNKNOWN" and "RuntimeError" in health["error"]

    engine._last_account = {"totalWalletBalance": "1000"}
    engine._last_reconciliation_result = SimpleNamespace(checked_at=datetime.now(timezone.utc))
    engine._protection = SimpleNamespace(position_count=lambda: 1)
    engine._active_order_ids = {"pending"}
    engine._policy_version = "1"
    engine._fresh_matched_reconciliation = lambda: True
    engine._check_liveness = lambda: engine_module.HealthState.HEALTHY
    engine._post_risk = SimpleNamespace(
        record_violation=lambda reason: setattr(engine, "violation", reason), recommend_degradation=lambda: False
    )
    engine._check_post_risk_safety("BTCUSDT", 100.0, 2.0)
    assert not hasattr(engine, "violation")
    engine._check_liveness = lambda: engine_module.HealthState.UNHEALTHY
    engine._check_post_risk_safety("BTCUSDT", 10_000.0, 1.0)
    assert hasattr(engine, "violation")

    engine._last_reconciliation_result = SimpleNamespace(checked_at=datetime.fromisoformat("2026-01-01"))
    engine._post_risk = SimpleNamespace(
        record_violation=lambda reason: setattr(engine, "violation", reason),
        _violations=["bad"],
        recommend_degradation=lambda: True,
    )
    engine._check_liveness = lambda: engine_module.HealthState.HEALTHY
    engine._check_post_risk_safety("BTCUSDT", 100.0, 2.0)
    engine._protection = SimpleNamespace(position_count=lambda: (_ for _ in ()).throw(RuntimeError("post-risk")))
    engine._check_post_risk_safety("BTCUSDT", 100.0, 2.0)

    assert engine._apply_venue_account_permissions(None) == (False, "ACCOUNT_PERMISSION_UNKNOWN")
    assert engine._apply_venue_account_permissions({"canTrade": True}) == (False, "ACCOUNT_PERMISSION_UNKNOWN")
    assert engine._apply_venue_account_permissions({"canTrade": True, "canWithdraw": True}) == (
        False,
        "WITHDRAWAL_PERMISSION_ENABLED",
    )
    engine._env_mode = SimpleNamespace(value="testnet")
    assert engine._apply_venue_account_permissions({"canTrade": True, "canWithdraw": True}) == (True, "OK")
    assert engine._risk_can_withdraw is False
    assert engine._apply_venue_account_permissions({"canTrade": False, "canWithdraw": False}) == (
        False,
        "VENUE_TRADING_DISABLED",
    )

    assert AutonomousEngine._signed_position_from_account({}, "BTCUSDT") is None
    assert AutonomousEngine._signed_position_from_account({"positions": []}, "BTCUSDT") == 0
    assert (
        AutonomousEngine._signed_position_from_account(
            {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}, {"symbol": "BTCUSDT", "positionAmt": "2"}]},
            "BTCUSDT",
        )
        is None
    )
    assert (
        AutonomousEngine._signed_position_from_account(
            {"positions": [{"symbol": "BTCUSDT", "positionAmt": "bad"}]}, "BTCUSDT"
        )
        is None
    )
    assert (
        AutonomousEngine._signed_position_from_account(
            {"positions": [{"symbol": "BTCUSDT", "positionAmt": "-1.5"}]}, "BTCUSDT"
        )
        == -1.5
    )


def _factor_engine() -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    record = SimpleNamespace(
        definition=SimpleNamespace(version="1.0.0"),
        performance=[],
        lifecycle=FactorLifecycle.CHALLENGER,
        degrade=lambda reason: setattr(record, "degraded_reason", reason),
    )
    engine._factor_pairs_by_scope = {}
    engine._factor_registry = SimpleNamespace(
        get=lambda _fid: record,
        degrade=lambda _fid, reason: setattr(record, "degraded_reason", reason),
    )
    engine._factor_gate = SimpleNamespace(promote=lambda *_args, **_kwargs: SimpleNamespace(approved=True))
    engine._autopilot_strategy_id = "autopilot"
    engine._policy_float_audited = lambda _key, default: default
    engine._active_champion_id = None
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: setattr(engine, "alert", (args, kwargs)))
    engine._model_registry = SimpleNamespace(
        models=[],
        list_models=lambda _sid: engine._model_registry.models,
        register_model=lambda model: engine._model_registry.models.append(model),
        get_champion=lambda _sid: getattr(engine, "champion", None),
        promote_to_champion=lambda _sid, _mid: setattr(engine, "promoted", True) or True,
        demote_champion=lambda _sid, reason: setattr(engine, "demoted", reason),
    )
    engine._record = record
    return engine


def test_live_factor_pair_evaluation_covers_scope_and_lifecycle_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)
    engine = _factor_engine()
    assert engine._evaluate_live_factor_pairs(now) is False

    pairs = [(float(i), float(i) / 10.0) for i in range(1, 61)]
    engine._factor_pairs_by_scope = {("BINANCE", "BTCUSDT", "1h", 1): {"factor": pairs}}
    monkeypatch.setattr(engine_module.FactorEvaluator, "compute_icir", staticmethod(lambda _ics: 0.5))
    assert engine._evaluate_live_factor_pairs(now) is True
    assert len(engine._record.performance) == 1
    assert getattr(engine, "promoted", False) is True

    # A missing registry record is a diagnostic-only sample, not a lifecycle
    # mutation.  Two independent scopes also exercise the champion isolation.
    engine_missing = _factor_engine()
    engine_missing._factor_registry = SimpleNamespace(get=lambda _fid: None)
    engine_missing._factor_pairs_by_scope = {
        ("BINANCE", "BTCUSDT", "1h", 1): {"factor": pairs[:20]},
        ("BINANCE", "ETHUSDT", "1h", 1): {"factor": pairs[:20]},
    }
    assert engine_missing._evaluate_live_factor_pairs(now) is False

    # Anti-correlated samples drive the active-factor degradation path and a
    # low existing champion through the separate model demotion gate.
    degraded = _factor_engine()
    degraded._record.lifecycle = FactorLifecycle.ACTIVE
    monkeypatch.setattr(engine_module.FactorEvaluator, "compute_icir", staticmethod(lambda _ics: 0.0))
    degraded._factor_pairs_by_scope = {
        ("BINANCE", "BTCUSDT", "1h", 1): {"factor": [(float(i), -float(i) / 10.0) for i in range(1, 61)]}
    }
    degraded.champion = SimpleNamespace(
        model_id="champion",
        status=SimpleNamespace(value="CHAMPION"),
        metrics={"icir": 0.0, "sample_count": 60.0, "scope": "BINANCE:BTCUSDT:1h:h1"},
    )
    assert degraded._evaluate_live_factor_pairs(now) is True
    assert hasattr(degraded, "alert")
    assert hasattr(degraded, "demoted")

    # Short windows remain diagnostic while a second scope prevents a
    # strategy-wide champion decision.  Both are real data-quality states,
    # not coverage-only fixtures.
    mixed = _factor_engine()
    mixed._factor_pairs_by_scope = {
        ("BINANCE", "BTCUSDT", "1h", 1): {"factor": pairs[:5]},
        ("BINANCE", "ETHUSDT", "1h", 1): {"factor": pairs[:20]},
    }
    assert mixed._evaluate_live_factor_pairs(now) is False

    existing = _factor_engine()
    existing._factor_pairs_by_scope = {("BINANCE", "BTCUSDT", "1h", 1): {"factor": pairs}}
    monkeypatch.setattr(engine_module.FactorEvaluator, "compute_icir", staticmethod(lambda _ics: 0.5))
    assert existing._evaluate_live_factor_pairs(now) is True
    assert existing._evaluate_live_factor_pairs(now) is True
    assert len(existing._record.performance) == 2

    warning = _factor_engine()
    warning._record.lifecycle = FactorLifecycle.ACTIVE
    warning._policy_float_audited = lambda key, default: 1000.0 if key == "champion_min_samples" else default
    warning._factor_pairs_by_scope = {("BINANCE", "BTCUSDT", "1h", 1): {"factor": pairs}}
    assert warning._evaluate_live_factor_pairs(now) is False

    champion = _factor_engine()
    champion._factor_pairs_by_scope = {("BINANCE", "BTCUSDT", "1h", 1): {"factor": pairs}}
    champion_model = SimpleNamespace(
        model_id="factor-v1.0.0",
        status=engine_module.ModelStatus.CHAMPION,
        metrics={"icir": 0.5, "sample_count": 60.0, "scope": "BINANCE:BTCUSDT:1h:h1"},
    )
    champion._model_registry.promote_to_champion = lambda _sid, _mid: (
        setattr(champion, "champion", champion_model) or True
    )
    assert champion._evaluate_live_factor_pairs(now) is True
    assert champion._active_champion_id == "factor-v1.0.0"

    recovering = _factor_engine()
    recovering._record.lifecycle = FactorLifecycle.DEGRADED
    recovering._factor_pairs_by_scope = {("BINANCE", "BTCUSDT", "1h", 1): {"factor": pairs}}
    assert recovering._evaluate_live_factor_pairs(now) is True


@pytest.mark.asyncio
async def test_trading_universe_evaluation_covers_input_and_promotion_boundaries() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._trading_pool = None
    await engine._evaluate_trading_universe()

    class Entry:
        def __init__(self, status: PoolStatus) -> None:
            self.status = status

    class Pool:
        def __init__(self) -> None:
            self._pool = {
                "BTCUSDT": Entry(PoolStatus.OBSERVING),
                "ETHUSDT": Entry(PoolStatus.ACTIVE),
                "XRPUSDT": Entry(PoolStatus.QUARANTINED),
            }
            self.scored: list[object] = []
            self.promoted: list[object] = []

        def score(self, instrument: object, score: object) -> None:
            self.scored.append((instrument, score))

        def try_promote(self, instrument: object) -> bool:
            self.promoted.append(instrument)
            return instrument == "BTCUSDT"

        def activate(self, instrument: object) -> None:
            self._pool[instrument].status = PoolStatus.ACTIVE

        def active_instruments(self) -> list[str]:
            return [key for key, entry in self._pool.items() if entry.status is PoolStatus.ACTIVE]

    pool = Pool()
    engine._trading_pool = pool
    ticker_values = {
        "BTCUSDT": {"bidPrice": "99", "askPrice": "101", "lastPrice": "100", "volume": "100000"},
        "ETHUSDT": {"bidPrice": "0", "askPrice": "0", "lastPrice": "100", "volume": "1"},
    }

    class Feed:
        def get_last_ticker(self, symbol: str) -> dict:
            if symbol == "ETHUSDT":
                return ticker_values[symbol]
            return ticker_values[symbol]

        def get_last_orderbook(self, symbol: str) -> dict | None:
            if symbol == "BTCUSDT":
                return {"bids": [["99", "1000"]], "asks": [["101", "1000"]]}
            return None

        async def async_get_kline_features(self, symbol: str, _tf: str, _limit: int) -> dict:
            if symbol == "ETHUSDT":
                return {"close": 100.0}
            return {"close": 100.0, "high": 101.0, "low": 99.0, "ann_volatility": 0.1}

    engine._feed = Feed()
    await engine._evaluate_trading_universe()
    assert [item[0] for item in pool.scored] == ["BTCUSDT"]
    assert pool._pool["BTCUSDT"].status is PoolStatus.ACTIVE

    empty_pool = Pool()
    empty_pool._pool = {}
    engine._trading_pool = empty_pool
    await engine._evaluate_trading_universe()

    error_pool = Pool()
    error_pool._pool = {"BTCUSDT": Entry(PoolStatus.OBSERVING)}
    engine._trading_pool = error_pool

    class ErrorFeed:
        def get_last_ticker(self, _symbol: str) -> dict:
            raise RuntimeError("ticker")

    engine._feed = ErrorFeed()
    await engine._evaluate_trading_universe()

    empty_feed_pool = Pool()
    empty_feed_pool._pool = {"BTCUSDT": Entry(PoolStatus.OBSERVING)}
    engine._trading_pool = empty_feed_pool

    class EmptyFeed:
        def get_last_ticker(self, _symbol: str) -> dict:
            return {}

        def get_last_orderbook(self, _symbol: str) -> dict:
            return {}

        async def async_get_kline_features(self, _symbol: str, _tf: str, _limit: int) -> dict:
            return {"close": 100.0}

    engine._feed = EmptyFeed()
    await engine._evaluate_trading_universe()


@pytest.mark.asyncio
async def test_offline_tick_runs_reporting_recovery_and_cleanup() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._last_rule_snapshot_refresh = 0.0
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(0, result={"symbols": []})
    engine._sync_adapter_rule_snapshots = lambda _info: True
    engine._evaluate_live_factor_pairs = lambda _now: True
    engine._rebuild_alpha_graph = lambda: setattr(engine, "graph_rebuilt", True)
    engine._trade_pnls = [1.0, -0.5]
    engine._win_count = 1
    engine._loss_count = 1
    engine._drift_detector = SimpleNamespace(
        is_calibrated=lambda: False,
        set_baseline=lambda baseline: setattr(engine, "baseline", baseline),
        detect=lambda _metrics: {"sharpe": True},
        should_retire=lambda _drift: True,
    )
    engine._cold_start_baseline = False
    engine._evaluate_trading_universe = lambda: asyncio.sleep(0)
    engine._last_daily_reset_day = datetime.now(timezone.utc).day - 1
    engine._strategy_risk = SimpleNamespace(
        reset_daily_pnl=lambda: setattr(engine, "daily_reset", True),
        get_state=lambda _sid: SimpleNamespace(
            risk_level=SimpleNamespace(value="NORMAL"),
            current_drawdown_pct=1.0,
            daily_pnl=-2.0,
            peak_equity=1000.0,
        ),
        update_equity=lambda *_args: {"action": "DEGRADE", "new_level": "EXIT_ONLY", "reason": "loss"},
    )
    engine._autopilot_strategy_id = "autopilot"
    engine._last_account = {"totalWalletBalance": "1000"}
    report = SimpleNamespace(generate_daily_report=lambda **kwargs: setattr(engine, "report", kwargs))
    engine._alerts = SimpleNamespace(
        get_report_generator=lambda: report,
        get_alert_stats=lambda: {"total": 3},
        get_active_incidents=lambda: [{"incident_id": "incident-1"}],
        send_incident=lambda *args, **kwargs: setattr(engine, "alert", (args, kwargs)),
    )
    engine._tick_count = 5
    engine._order_count = 2
    engine._error_count = 51
    engine._protection = SimpleNamespace(position_count=lambda: 1)
    engine._factor_registry = SimpleNamespace(get_active=lambda: [1], get_challengers=lambda: [2])
    engine._ledger = SimpleNamespace(is_balanced=lambda: False)
    engine._lifecycle = SimpleNamespace(state=SimpleNamespace(value="ACTIVE"))
    engine._mapek = SimpleNamespace(
        decide_action=lambda *_args: (engine_module.RecoveryAction.RESTART_MODULE, "too many errors"),
        execute_recovery_governed=lambda *_args, **_kwargs: SimpleNamespace(value="SUCCESS"),
        verify_recovery=lambda *_args: True,
        save_checkpoint=lambda *args, **kwargs: setattr(engine, "mapek_checkpoint", (args, kwargs)),
    )
    engine._control = SimpleNamespace()
    engine._store = SimpleNamespace(
        save_checkpoint=lambda *args: setattr(engine, "store_checkpoint", args),
        cleanup_old_data=lambda **_kwargs: 2,
    )
    engine._check_credential_health = lambda: {
        "level": "OK",
        "credential_type": "TRADING",
        "status": "ACTIVE",
        "days_to_expiry": 90,
        "can_trade": True,
        "can_withdraw": False,
    }
    engine._intent_retry_count = {str(i): i for i in range(201)}
    engine._paper_fills = list(range(5001))
    engine._protection_exchange_attempted = {str(i) for i in range(1001)}

    await engine._offline_tick()

    assert engine.graph_rebuilt is True
    assert engine.daily_reset is True
    assert engine.report["risk_events_24h"] == 3
    assert engine.mapek_checkpoint[1]["invariants_valid"] is False
    assert engine.store_checkpoint[0].startswith("cp-")
    assert len(engine._intent_retry_count) == 100
    assert len(engine._paper_fills) == 2000
    assert len(engine._protection_exchange_attempted) == 500

    # Cold-start and already-calibrated no-trade states are distinct: the
    # former records a neutral baseline, while the latter remains diagnostic.
    del engine._last_daily_reset_day
    engine._trade_pnls = []
    engine._drift_detector = SimpleNamespace(
        is_calibrated=lambda: False,
        set_baseline=lambda baseline: setattr(engine, "cold_baseline", baseline),
    )
    await engine._offline_tick()
    assert engine.cold_baseline["sharpe"] == 0.0

    engine._drift_detector = SimpleNamespace(
        is_calibrated=lambda: True,
        detect=lambda _metrics: {"sharpe": 0.1},
        should_retire=lambda _drift: False,
    )
    engine._trade_pnls = []
    await engine._offline_tick()

    engine._drift_detector = SimpleNamespace(
        is_calibrated=lambda: True,
        set_baseline=lambda _baseline: None,
        detect=lambda _metrics: {"sharpe": 0.1},
        should_retire=lambda _drift: True,
    )
    engine._cold_start_baseline = False
    engine._trade_pnls = [1.0, -0.5]
    engine._drift_detector.should_retire = lambda _drift: False
    await engine._offline_tick()
    engine._drift_detector.should_retire = lambda _drift: True
    engine._mapek.execute_recovery_governed = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("recover"))
    engine._check_credential_health = lambda: (_ for _ in ()).throw(RuntimeError("credential"))
    await engine._offline_tick()

    broken = AutonomousEngine.__new__(AutonomousEngine)
    broken._last_rule_snapshot_refresh = 0.0
    broken._api_async = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("exchange-info"))
    broken._evaluate_live_factor_pairs = lambda _now: (_ for _ in ()).throw(RuntimeError("offline"))
    broken._error_count = 0
    await broken._offline_tick()
    assert broken._error_count == 1


@pytest.mark.asyncio
async def test_user_stream_keepalive_and_restart_boundary_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._user_stream_stopping = False
    engine._user_stream_fault = lambda reason, terminal=False: setattr(engine, "fault", (reason, terminal))
    engine._update_user_stream_runtime = lambda **kwargs: setattr(engine, "runtime", kwargs)

    # A missing adapter capability is a terminal, observable fault.
    engine._adapter = SimpleNamespace()

    async def stop_after_first_sleep(_seconds: float) -> None:
        engine._user_stream_stopping = False

    monkeypatch.setattr(engine_module.asyncio, "sleep", stop_after_first_sleep)
    await engine._user_stream_keepalive_loop("listen-key")
    assert engine.fault == ("ADAPTER_USER_STREAM_KEEPALIVE_UNSUPPORTED", True)

    class Result:
        def __init__(self, ok: bool) -> None:
            self.ok = ok

        def is_success(self) -> bool:
            return self.ok

    calls = 0
    engine._user_stream_stopping = False

    async def keepalive(_key: str) -> object:
        nonlocal calls
        calls += 1
        return Result(calls < 3)

    engine._adapter = SimpleNamespace(keepalive_user_listen_key=keepalive)

    async def bounded_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(engine_module.asyncio, "sleep", bounded_sleep)
    await engine._user_stream_keepalive_loop("listen-key")
    assert engine.fault == ("LISTEN_KEY_KEEPALIVE_FAILED", True)

    engine._user_stream_stopping = False
    engine._user_stream_restart_attempts = 0
    engine._user_stream_restarting = False
    engine._running = True
    engine._USER_STREAM_RESTART_BACKOFF_S = 0

    async def stop_stream() -> None:
        return None

    async def start_stream() -> bool:
        return True

    engine._stop_user_stream = stop_stream
    engine._start_user_stream = start_stream
    engine._user_stream_fault = lambda reason, terminal=False: setattr(engine, "restart_fault", (reason, terminal))
    engine._user_stream_stopping = True
    await engine._restart_user_stream_after_fault("restart")
    assert engine._user_stream_restart_attempts == 0


def test_system_reconciliation_facts_replays_durable_opening_and_fill_edges() -> None:
    now = datetime.now(timezone.utc)
    opening = {
        "complete": 1,
        "source": "operator-baseline",
        "fact_version": "opening-v1",
        "evidence_hash": "evidence",
        "approval_id": "approval",
        "captured_at": now.isoformat(),
        "positions": {"BTCUSDT": "1"},
        "balance_amount": "1000",
        "balance_currency": "USDT",
        "balance_decimals": 8,
    }
    active_orders = [
        {
            "order_id": "order-1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": "1",
            "price": "100",
            "order_type": "LIMIT",
            "reduce_only": "False",
            "stop_price": "",
        }
    ]
    after = (now + timedelta(seconds=1)).isoformat()
    fills = [
        {"event_time": "not-a-time"},
        {"event_time": (now - timedelta(seconds=1)).isoformat(), "side": "BUY", "delta_qty": "1"},
        {"event_time": after, "processing_state": "PENDING", "symbol": "BTCUSDT", "side": "BUY", "delta_qty": "1"},
        {"event_time": after, "processing_state": "COMMITTED", "symbol": "", "side": "BUY", "delta_qty": "1"},
        {"event_time": after, "processing_state": "COMMITTED", "symbol": "BTCUSDT", "side": "BUY", "delta_qty": "NaN"},
        {
            "event_time": after,
            "processing_state": "COMMITTED",
            "symbol": "BTCUSDT",
            "side": "SIDEWAYS",
            "delta_qty": "1",
        },
        {"event_time": after, "processing_state": "COMMITTED", "symbol": "BTCUSDT", "side": "BUY", "delta_qty": "0.5"},
        {
            "event_time": after,
            "processing_state": "COMMITTED",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "delta_qty": "0.25",
        },
    ]
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = SimpleNamespace(
        restore_account_opening_projection=lambda *_args: opening,
        restore_fill_events=lambda: fills,
        get_active_orders=lambda: active_orders,
    )
    facts = engine._build_system_reconciliation_facts()
    assert facts.complete is False
    assert facts.positions[InstrumentId("BTCUSDT")].amount == "1.25"
    assert facts.open_orders == ["order-1"]
    assert facts.open_orders_detail["order-1"]["reduce_only"] == "False"

    malformed_opening = dict(opening, captured_at="bad-time")
    engine._store.restore_account_opening_projection = lambda *_args: malformed_opening
    facts = engine._build_system_reconciliation_facts()
    assert facts.complete is False


def test_reconciliation_rule_steps_fail_closed_and_use_only_valid_steps() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    assert engine._reconciliation_rule_steps(set()) == ({}, "OK")
    engine._adapter = SimpleNamespace()
    engine._symbol_precision = {}
    assert engine._reconciliation_rule_steps({"BTCUSDT"}) == ({}, "POSITION_RULE_AUTHORITY_UNAVAILABLE")

    engine._symbol_precision = {"BTCUSDT": {"step_size": "0.001"}}
    assert engine._reconciliation_rule_steps({"BTCUSDT"}) == ({"BTCUSDT": "0.001"}, "OK")
    for value, reason in (
        ("bad", "POSITION_RULE_STEP_INVALID"),
        ("0", "POSITION_RULE_STEP_INVALID"),
        ("NaN", "POSITION_RULE_STEP_INVALID"),
    ):
        engine._symbol_precision = {"BTCUSDT": {"step_size": value}}
        result, detail = engine._reconciliation_rule_steps({"BTCUSDT"})
        assert result == {} and detail.startswith(reason)

    engine._symbol_precision = {"BTCUSDT": {"step_size": "0.01"}}
    engine._adapter = SimpleNamespace(
        get_rule_snapshot=lambda _symbol: SimpleNamespace(is_known=False, is_stale=True, step_size="0.001")
    )
    assert engine._reconciliation_rule_steps({"BTCUSDT"}) == ({"BTCUSDT": "0.01"}, "OK")
    engine._symbol_precision = {}
    result, detail = engine._reconciliation_rule_steps({"BTCUSDT"})
    assert result == {} and detail.startswith("POSITION_RULE_UNKNOWN_OR_STALE")

    engine._adapter = SimpleNamespace(
        get_rule_snapshot=lambda _symbol: SimpleNamespace(is_known=True, is_stale=False, step_size="bad")
    )
    result, detail = engine._reconciliation_rule_steps({"BTCUSDT"})
    assert result == {} and detail == "POSITION_RULE_STEP_INVALID:BTCUSDT"


def test_seed_pool_history_handles_import_quality_and_feature_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import pandas as pd

    from beidou_research.data.kline_store import KlineStore

    class Pool:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def seed_historical_observation(self, *args, **kwargs) -> bool:
            self.calls.append((args, kwargs))
            return True

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._trading_pool = None
    engine._seed_pool_from_history(["BTCUSDT"])

    engine._trading_pool = Pool()
    original_import = builtins.__import__

    def blocked_import(name: str, *args, **kwargs):
        if name == "beidou_research.data.kline_store":
            raise ImportError("store unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    engine._seed_pool_from_history(["BTCUSDT"])
    monkeypatch.setattr(builtins, "__import__", original_import)

    def load(_store: object, symbol: str, _timeframe: str):
        if symbol == "ERRUSDT":
            raise OSError("history")
        if symbol == "SHORTUSDT":
            return pd.DataFrame({"close": [1.0] * 99, "volume": [100.0] * 99})
        if symbol == "ZEROUSDT":
            return pd.DataFrame({"close": [1.0] * 119 + [0.0], "volume": [100.0] * 120})
        if symbol == "BADUSDT":
            return pd.DataFrame({"close": [object()] * 120, "volume": [100.0] * 120})
        return pd.DataFrame({"close": [100.0 + (i % 3) for i in range(120)], "volume": [1000.0] * 120})

    monkeypatch.setattr(KlineStore, "load", load)
    engine._seed_pool_from_history(["ERRUSDT", "SHORTUSDT", "ZEROUSDT", "BADUSDT", "BTCUSDT"])
    assert len(engine._trading_pool.calls) == 1


def test_incident_resolution_and_replay_authorization_are_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved: list[str] = []
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._user_stream_runtime = {"status": "HEALTHY"}
    engine._protection_owner_unknown = False
    engine._ledger = SimpleNamespace(is_frozen=False)
    engine._alerts = SimpleNamespace(
        _active_incidents={
            "exec": SimpleNamespace(root_cause_category="execution_fact"),
            "recon": SimpleNamespace(root_cause_category="reconciliation"),
            "stream": SimpleNamespace(root_cause_category="user_stream"),
            "protect": SimpleNamespace(root_cause_category="protection"),
            "other": SimpleNamespace(root_cause_category="other"),
        },
        resolve_incident=lambda iid: resolved.append(iid),
    )
    engine._maybe_auto_resolve_incidents()
    assert set(resolved) == {"exec", "recon", "stream", "protect"}

    frozen = AutonomousEngine.__new__(AutonomousEngine)
    frozen._user_stream_runtime = {}
    frozen._protection_owner_unknown = True
    frozen._ledger = SimpleNamespace(is_frozen=True)
    frozen._alerts = SimpleNamespace(
        _active_incidents={"exec": SimpleNamespace(root_cause_category="execution_fact")},
        resolve_incident=lambda _iid: (_ for _ in ()).throw(AssertionError("frozen incident must remain")),
    )
    frozen._maybe_auto_resolve_incidents()

    broken = AutonomousEngine.__new__(AutonomousEngine)
    broken._user_stream_runtime = {}
    broken._protection_owner_unknown = False
    broken._ledger = SimpleNamespace(is_frozen=False)
    broken._alerts = SimpleNamespace(
        _active_incidents={"x": SimpleNamespace(root_cause_category="reconciliation")},
        resolve_incident=lambda _iid: (_ for _ in ()).throw(RuntimeError("alert store")),
    )
    broken._maybe_auto_resolve_incidents()

    blocked = AutonomousEngine.__new__(AutonomousEngine)
    blocked._can_write = False
    blocked._env_mode = SimpleNamespace(value="testnet")
    blocked._user_stream_projector = SimpleNamespace(sequencer=SimpleNamespace(status="UNKNOWN"))
    assert blocked._maybe_authorize_user_stream_baseline(_facts(), "r", result=SimpleNamespace(differences=[])) is False

    no_seq = AutonomousEngine.__new__(AutonomousEngine)
    no_seq._can_write = True
    no_seq._env_mode = SimpleNamespace(value="testnet")
    no_seq._user_stream_projector = SimpleNamespace(sequencer=None)
    assert no_seq._maybe_authorize_user_stream_baseline(_facts(), "r") is False

    authorized = AutonomousEngine.__new__(AutonomousEngine)
    authorized._can_write = True
    authorized._env_mode = SimpleNamespace(value="testnet")
    authorized._USER_STREAM_RESTART_MAX_ATTEMPTS = 3
    authorized._user_stream_restart_attempts = 3
    authorized._user_stream_restarting = False
    authorized._user_stream_runtime = {"status": "FAILED"}
    authorized._recon = SimpleNamespace(update_event_facts=lambda facts: setattr(authorized, "event_facts", facts))
    authorized._user_stream_projector = SimpleNamespace(
        sequencer=SimpleNamespace(status="DEGRADED"),
        authorize_replay_baseline=lambda *args, **kwargs: _Projection(UserProjectionStatus.ACCEPTED),
        fact_snapshot=lambda: "facts",
    )

    def fake_task(coro):
        coro.close()
        return "restart-task"

    monkeypatch.setattr(engine_module.asyncio, "create_task", fake_task)
    assert authorized._maybe_authorize_user_stream_baseline(_facts(), "recon-1") is True
    assert authorized._user_stream_restart_attempts == 0


@pytest.mark.asyncio
async def test_safe_recover_unowned_algos_is_symbol_scoped_and_owned_only() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = SimpleNamespace(value="testnet")
    blocked: list[list[str]] = []
    cancelled: list[tuple[str, int]] = []
    engine._block_unowned_protection_orders = lambda ids: blocked.append(list(ids))

    inventory = [
        {"algoId": "1", "symbol": "BTCUSDT", "clientAlgoId": "beidou-entry-1"},
        {"algoId": "2", "symbol": "ETHUSDT", "clientAlgoId": "beidou-entry-2"},
        {"algoId": "3", "symbol": "XRPUSDT", "clientAlgoId": "foreign-worker"},
        {"algoId": "4", "symbol": "SOLUSDT", "clientAlgoId": "bdp-protection-4"},
        {"algoId": "5", "symbol": "ADAUSDT", "clientAlgoId": "bdp-protection-5"},
    ]

    async def cancel(symbol: str, algo_id: int) -> object:
        cancelled.append((symbol, algo_id))
        if algo_id == 5:
            raise RuntimeError("rate limited")
        return {"code": "200"}

    engine._cancel_algo_order = cancel
    refreshed = [{"algoId": "2", "symbol": "ETHUSDT", "clientAlgoId": "beidou-entry-2"}]
    engine._get_open_algo_inventory = lambda: asyncio.sleep(0, result=refreshed)
    remaining, snapshot = await engine._safe_recover_unowned_testnet_algos(
        ["1", "2", "3", "4", "5"],
        inventory,
        [{"symbol": "ETHUSDT", "positionAmt": "1"}, {"symbol": "BTCUSDT", "positionAmt": "0"}],
    )
    assert cancelled == [("BTCUSDT", 1), ("SOLUSDT", 4), ("ADAUSDT", 5)]
    assert remaining == ["2", "5"]
    assert snapshot == refreshed
    assert blocked == [["2", "5"]]

    no_inventory = AutonomousEngine.__new__(AutonomousEngine)
    no_inventory._env_mode = SimpleNamespace(value="testnet")
    no_inventory._block_unowned_protection_orders = lambda ids: blocked.append(list(ids))
    no_inventory._cancel_algo_order = cancel
    no_inventory._get_open_algo_inventory = lambda: (_ for _ in ()).throw(OSError("inventory refresh"))
    remaining, snapshot = await no_inventory._safe_recover_unowned_testnet_algos(
        ["4"], [{"algoId": "4", "symbol": "SOLUSDT", "clientAlgoId": "bdp-4"}], []
    )
    assert remaining == [] and snapshot == [{"algoId": "4", "symbol": "SOLUSDT", "clientAlgoId": "bdp-4"}]

    live = AutonomousEngine.__new__(AutonomousEngine)
    live._env_mode = SimpleNamespace(value="live")
    live._block_unowned_protection_orders = lambda ids: blocked.append(list(ids))
    assert await live._safe_recover_unowned_testnet_algos(["9"], [], []) == (["9"], [])


@pytest.mark.asyncio
async def test_user_stream_start_failures_and_parser_gates_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    import beidou_exchange.binance_usdm as binance_module
    import beidou_exchange.binance_usdm.adapter as adapter_module

    class FakeWebSocket:
        instances: ClassVar[list["FakeWebSocket"]] = []

        def __init__(self, *, base_url: str) -> None:
            self.base_url = base_url
            self.state_callback = None
            self.event_callback = None
            self.closed = False
            self.__class__.instances.append(self)

        def on_state_change(self, callback) -> None:
            self.state_callback = callback

        async def subscribe(self, _key: str, callback) -> None:
            self.event_callback = callback

        async def run(self) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    class Adapter:
        async def create_user_listen_key(self):
            return SimpleNamespace(is_success=lambda: True, data={"listenKey": "lk"}, error=None)

    async def stop(engine: AutonomousEngine) -> None:
        await engine._stop_user_stream()

    monkeypatch.setattr(binance_module, "BinanceUsdmWebSocketClient", FakeWebSocket)

    not_required = AutonomousEngine.__new__(AutonomousEngine)
    not_required._can_write = False
    not_required._update_user_stream_runtime = lambda **updates: setattr(not_required, "runtime", updates)
    assert await not_required._start_user_stream() is True

    already_started = AutonomousEngine.__new__(AutonomousEngine)
    already_started._can_write = True
    already_started._user_ws_task = SimpleNamespace(done=lambda: False)
    already_started._user_stream_readiness = lambda: (True, {"status": "CONNECTED"})
    assert await already_started._start_user_stream() is True

    failed_key = AutonomousEngine.__new__(AutonomousEngine)
    failed_key._can_write = True
    failed_key._env_mode = SimpleNamespace(value="testnet")
    failed_key._adapter = SimpleNamespace(
        create_user_listen_key=lambda: asyncio.sleep(
            0,
            result=SimpleNamespace(
                is_success=lambda: False,
                data=None,
                error=SimpleNamespace(message="key denied"),
            ),
        )
    )
    failed_key._user_stream_fault = lambda reason, terminal=False: setattr(failed_key, "fault", (reason, terminal))
    failed_key._update_user_stream_runtime = lambda **updates: None
    assert await failed_key._start_user_stream() is False
    assert failed_key.fault[0].startswith("LISTEN_KEY_CREATE_FAILED")

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._env_mode = SimpleNamespace(value="testnet")
    engine._adapter = Adapter()
    engine._user_stream_runtime = {"status": "DEGRADED"}
    engine._user_stream_fault = lambda reason, terminal=False: setattr(engine, "fault", (reason, terminal))
    engine._update_user_stream_runtime = AutonomousEngine._update_user_stream_runtime.__get__(engine)
    engine._recon = SimpleNamespace(update_event_facts=lambda _facts: None)
    engine.ingest_user_order_update = lambda _update: True
    engine.ingest_user_account_update = lambda _update: True
    engine._ingest_account_config_update = lambda _data: True

    fail = SimpleNamespace(is_success=lambda: False, data=None, error=None)
    success = SimpleNamespace(is_success=lambda: True, data=SimpleNamespace(), error=None)
    monkeypatch.setattr(adapter_module.BinanceUsdmAdapter, "parse_user_order_update", staticmethod(lambda _data: fail))
    monkeypatch.setattr(
        adapter_module.BinanceUsdmAdapter, "parse_user_account_update", staticmethod(lambda _data: fail)
    )
    assert await engine._start_user_stream() is True
    ws = FakeWebSocket.instances[-1]
    await ws.event_callback("lk", {"e": "ORDER_TRADE_UPDATE"})
    await ws.event_callback("lk", {"e": "ACCOUNT_UPDATE"})
    await ws.event_callback("lk", {"e": "TRADE_LITE"})
    assert "TRADE_LITE" not in str(getattr(engine, "fault", ("",))[0])
    await stop(engine)

    # Live rejects a malformed/lightweight event rather than silently
    # treating it as informational.
    live = AutonomousEngine.__new__(AutonomousEngine)
    live._can_write = True
    live._env_mode = SimpleNamespace(value="live")
    live._adapter = Adapter()
    live._user_stream_runtime = {"status": "DEGRADED"}
    live._user_stream_fault = lambda reason, terminal=False: setattr(live, "fault", (reason, terminal))
    live._update_user_stream_runtime = AutonomousEngine._update_user_stream_runtime.__get__(live)
    live.ingest_user_order_update = lambda _update: False
    monkeypatch.setattr(adapter_module.BinanceUsdmAdapter, "parse_user_order_update", staticmethod(lambda _data: fail))
    assert await live._start_user_stream() is True
    ws = FakeWebSocket.instances[-1]
    await ws.event_callback("lk", {"e": "TRADE_LITE"})
    assert live.fault[0] == "TRADE_LITE_PARSE_UNKNOWN"
    await stop(live)

    projection_testnet = AutonomousEngine.__new__(AutonomousEngine)
    projection_testnet._can_write = True
    projection_testnet._env_mode = SimpleNamespace(value="testnet")
    projection_testnet._adapter = Adapter()
    projection_testnet._user_stream_runtime = {"status": "DEGRADED"}
    projection_testnet._user_stream_fault = lambda reason, terminal=False: setattr(
        projection_testnet, "fault", (reason, terminal)
    )
    projection_testnet._update_user_stream_runtime = AutonomousEngine._update_user_stream_runtime.__get__(
        projection_testnet
    )
    projection_testnet.ingest_user_order_update = lambda _update: False
    monkeypatch.setattr(
        adapter_module.BinanceUsdmAdapter, "parse_user_order_update", staticmethod(lambda _data: success)
    )
    assert await projection_testnet._start_user_stream() is True
    ws = FakeWebSocket.instances[-1]
    await ws.event_callback("lk", {"e": "TRADE_LITE"})
    assert projection_testnet._user_stream_runtime["status"] == "HEALTHY"
    await stop(projection_testnet)

    generic = AutonomousEngine.__new__(AutonomousEngine)
    generic._can_write = True
    generic._env_mode = SimpleNamespace(value="live")
    generic._adapter = Adapter()
    generic._user_stream_runtime = {"status": "DEGRADED"}
    generic._user_stream_fault = lambda reason, terminal=False: setattr(generic, "fault", (reason, terminal))
    generic._update_user_stream_runtime = AutonomousEngine._update_user_stream_runtime.__get__(generic)
    generic.ingest_user_order_update = lambda _update: False
    assert await generic._start_user_stream() is True
    ws = FakeWebSocket.instances[-1]
    await ws.event_callback("lk", {"e": "ORDER_TRADE_UPDATE"})
    assert generic.fault[0] == "USER_EVENT_REJECTED:ORDER_TRADE_UPDATE"
    generic._user_stream_stopping = True
    ws.state_callback(None, SimpleNamespace(value="CONNECTED"), 1)
    await stop(generic)

    class SubscribeError(FakeWebSocket):
        async def subscribe(self, _key: str, _callback) -> None:
            raise OSError("subscribe")

    monkeypatch.setattr(binance_module, "BinanceUsdmWebSocketClient", SubscribeError)
    outer = AutonomousEngine.__new__(AutonomousEngine)
    outer._can_write = True
    outer._env_mode = SimpleNamespace(value="testnet")
    outer._adapter = Adapter()
    outer._user_stream_runtime = {"status": "STARTING"}
    outer._user_stream_fault = lambda reason, terminal=False: setattr(outer, "fault", (reason, terminal))
    outer._update_user_stream_runtime = lambda **updates: None
    assert await outer._start_user_stream() is False
    assert outer.fault[0] == "USER_STREAM_START_FAILED:OSError"
    assert outer._user_ws_client is None and outer._user_stream_listen_key is None


@pytest.mark.asyncio
async def test_user_stream_account_wrapper_restart_guards_and_retry_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    class Projection:
        def __init__(self, status: UserProjectionStatus) -> None:
            self.status = status
            self.event_id = "account-event"
            self.reason = "blocked"

    projector = SimpleNamespace(
        ingest_account_update=lambda _update: Projection(UserProjectionStatus.ACCEPTED),
        fact_snapshot=lambda: "account-facts",
    )
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = SimpleNamespace()
    engine._user_stream_projector = None
    engine._recon = SimpleNamespace(update_event_facts=lambda facts: setattr(engine, "event_facts", facts))
    engine._record_execution_fact_failure_env_guarded = lambda reason: setattr(engine, "blocked", reason)
    monkeypatch.setattr(engine_module, "UserStreamProjector", lambda **_kwargs: projector)
    assert engine.ingest_user_account_update({"event": "accepted"}) is True
    assert engine.event_facts == "account-facts"

    projector.ingest_account_update = lambda _update: Projection(UserProjectionStatus.BLOCKED)
    assert engine.ingest_user_account_update({"event": "blocked"}) is False
    assert "account user-stream event" in engine.blocked

    engine._leverage_cache = {"BTCUSDT": 20}
    assert engine._ingest_account_config_update({"ac": {"s": "BTCUSDT", "l": "5"}, "ai": {"j": "true"}}) is True
    assert "BTCUSDT" not in engine._leverage_cache
    assert engine._ingest_account_config_update({"ac": {"s": "", "l": "bad"}, "ai": {"j": object()}}) is True

    engine._user_stream_stopping = False
    engine._running = True
    engine._user_stream_restarting = True
    engine._user_stream_restart_attempts = 0
    engine._USER_STREAM_RESTART_MAX_ATTEMPTS = 3
    engine._USER_STREAM_RESTART_BACKOFF_S = 0
    assert await engine._restart_user_stream_after_fault("reentrant") is None

    engine._user_stream_restarting = False
    engine._stop_user_stream = lambda: asyncio.sleep(0)
    engine._start_user_stream = lambda: asyncio.sleep(0, result=True)

    async def stop_during_backoff(_seconds: float) -> None:
        engine._user_stream_stopping = True

    monkeypatch.setattr(engine_module.asyncio, "sleep", stop_during_backoff)
    await engine._restart_user_stream_after_fault("shutdown-race")
    assert engine._user_stream_restart_attempts == 1

    async def no_sleep(_seconds: float, result: object = None) -> object:
        return result

    monkeypatch.setattr(engine_module.asyncio, "sleep", no_sleep)
    faults: list[tuple[str, bool]] = []
    engine._user_stream_stopping = False
    engine._user_stream_fault = lambda reason, terminal=False: faults.append((reason, terminal))
    engine._start_user_stream = lambda: asyncio.sleep(0, result=False)
    await engine._restart_user_stream_after_fault("failed-start")
    assert faults[-1] == ("USER_STREAM_RESTART_FAILED:failed-start", True)

    async def cancelled_start() -> bool:
        raise asyncio.CancelledError()

    engine._start_user_stream = cancelled_start
    with pytest.raises(asyncio.CancelledError):
        await engine._restart_user_stream_after_fault("cancelled")
    assert engine._user_stream_restarting is False

    async def error_start() -> bool:
        raise RuntimeError("start")

    engine._user_stream_restart_attempts = 0
    engine._start_user_stream = error_start
    await engine._restart_user_stream_after_fault("error")
    assert faults[-1] == ("USER_STREAM_RESTART_ERROR:RuntimeError", True)


@pytest.mark.asyncio
async def test_user_stream_live_trade_lite_projection_rejection_and_keepalive_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import beidou_exchange.binance_usdm as binance_module
    import beidou_exchange.binance_usdm.adapter as adapter_module

    class FakeWebSocket:
        instance: "FakeWebSocket | None" = None

        def __init__(self, *, base_url: str) -> None:
            self.callback = None
            self.__class__.instance = self

        def on_state_change(self, callback) -> None:
            self.state = callback

        async def subscribe(self, _key: str, callback) -> None:
            self.callback = callback

        async def run(self) -> None:
            return None

        async def close(self) -> None:
            return None

    class Adapter:
        async def create_user_listen_key(self):
            return SimpleNamespace(is_success=lambda: True, data={"listenKey": "lk"}, error=None)

    parsed = SimpleNamespace(is_success=lambda: True, data=SimpleNamespace(), error=None)
    monkeypatch.setattr(binance_module, "BinanceUsdmWebSocketClient", FakeWebSocket)
    monkeypatch.setattr(
        adapter_module.BinanceUsdmAdapter, "parse_user_order_update", staticmethod(lambda _data: parsed)
    )
    live = AutonomousEngine.__new__(AutonomousEngine)
    live._can_write = True
    live._env_mode = SimpleNamespace(value="live")
    live._adapter = Adapter()
    live._user_stream_runtime = {"status": "DEGRADED"}
    live._user_stream_fault = lambda reason, terminal=False: setattr(live, "fault", (reason, terminal))
    live._update_user_stream_runtime = AutonomousEngine._update_user_stream_runtime.__get__(live)
    live.ingest_user_order_update = lambda _update: False
    assert await live._start_user_stream() is True
    await FakeWebSocket.instance.callback("lk", {"e": "TRADE_LITE"})
    assert live.fault == ("TRADE_LITE_PROJECTION_REJECTED", False)
    await live._stop_user_stream()

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._user_stream_stopping = False
    engine._user_stream_fault = lambda reason, terminal=False: setattr(engine, "fault", (reason, terminal))
    engine._update_user_stream_runtime = lambda **_kwargs: None
    calls = 0

    class Result:
        def __init__(self, ok: bool) -> None:
            self.ok = ok

        def is_success(self) -> bool:
            return self.ok

    async def keepalive(_key: str) -> Result:
        nonlocal calls
        calls += 1
        return Result([False, True, False, False][calls - 1] if calls <= 4 else False)

    engine._adapter = SimpleNamespace(keepalive_user_listen_key=keepalive)
    sleeps = 0

    async def bounded_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 6:
            engine._user_stream_stopping = True

    monkeypatch.setattr(engine_module.asyncio, "sleep", bounded_sleep)
    await engine._user_stream_keepalive_loop("lk")
    assert calls >= 4

    # The stopping check immediately after a transient failure is observable.
    engine._user_stream_stopping = False
    calls = 0

    async def stop_after_retry_sleep(_seconds: float) -> None:
        if _seconds == 60:
            engine._user_stream_stopping = True

    monkeypatch.setattr(engine_module.asyncio, "sleep", stop_after_retry_sleep)

    async def always_fail(_key: str) -> Result:
        return Result(False)

    engine._adapter = SimpleNamespace(keepalive_user_listen_key=always_fail)
    await engine._user_stream_keepalive_loop("lk")
    assert engine._user_stream_stopping is True


def test_system_reconciliation_naive_timestamps_are_normalized() -> None:
    naive = datetime.fromisoformat("2026-01-01T00:00:00")
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = SimpleNamespace(
        restore_account_opening_projection=lambda *_args: {
            "complete": 1,
            "source": "operator",
            "fact_version": "v1",
            "evidence_hash": "hash",
            "approval_id": "approval",
            "captured_at": naive.isoformat(),
            "positions": {},
            "balance_amount": "1000",
            "balance_currency": "USDT",
            "balance_decimals": 8,
        },
        restore_fill_events=lambda: [
            {
                "event_time": (naive.replace(second=1)).isoformat(),
                "processing_state": "COMMITTED",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "delta_qty": "1",
            }
        ],
        get_active_orders=lambda: [],
    )
    facts = engine._build_system_reconciliation_facts()
    assert facts.complete is True
    assert facts.positions[InstrumentId("BTCUSDT")].amount == "1"


@pytest.mark.asyncio
async def test_paper_reconciliation_is_not_verifiable_and_skips_venue_reads() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = SimpleNamespace(value="paper")
    assert await engine._reconcile() is True


@pytest.mark.asyncio
async def test_reconciliation_fact_completeness_and_persistence_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every incomplete venue fact closes the gate before any self-healing.

    The successful case uses an in-process reconciliation authority and a
    durable-store failure to keep the persistence error observable as well.
    """

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(engine_module.asyncio, "sleep", no_sleep)

    def shell(api: object) -> AutonomousEngine:
        engine = AutonomousEngine.__new__(AutonomousEngine)
        engine._env_mode = SimpleNamespace(value="testnet")
        engine._api_async_safe = api
        engine._record_reconciliation_failure = lambda *_args, **_kwargs: False
        engine._record_reconciliation_truth = lambda *_args, **_kwargs: None
        engine._build_system_reconciliation_facts = lambda: _facts()
        return engine

    async def unavailable_account(_endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        return {}, False

    assert await shell(unavailable_account)._reconcile() is False

    async def incomplete_account(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        if "account" in str(endpoint).lower():
            return {"totalWalletBalance": "100"}, True
        raise AssertionError(endpoint)

    assert await shell(incomplete_account)._reconcile() is False

    async def unavailable_orders(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        if "account" in str(endpoint).lower():
            return {"totalWalletBalance": "100", "positions": []}, True
        return {}, False

    assert await shell(unavailable_orders)._reconcile() is False

    async def malformed_order(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        if "account" in str(endpoint).lower():
            return {"totalWalletBalance": "100", "positions": []}, True
        return [{}], True

    assert await shell(malformed_order)._reconcile() is False

    async def malformed_position(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        if "account" in str(endpoint).lower():
            return {"totalWalletBalance": "100", "positions": [{}]}, True
        return [], True

    assert await shell(malformed_position)._reconcile() is False

    async def rule_failure_api(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        if "account" in str(endpoint).lower():
            return {
                "totalWalletBalance": "100",
                "positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}],
                "updateTime": 1,
            }, True
        return [], True

    rule_failure = shell(rule_failure_api)
    rule_failure._reconciliation_rule_steps = lambda _symbols: ({}, "POSITION_RULE_UNKNOWN_OR_STALE:BTCUSDT")
    assert await rule_failure._reconcile() is False

    class Recon:
        def update_system_facts(self, _facts: object) -> None:
            return None

        def update_exchange_facts(self, _facts: object) -> None:
            return None

        def reconcile(self, _account: object, _venue: object) -> object:
            from beidou_safety.execution.reconciliation import ReconciliationStatus

            return SimpleNamespace(
                matched=True,
                status=ReconciliationStatus.MATCHED,
                differences=[],
                checked_at=datetime.now(timezone.utc),
            )

    async def complete_api(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        if "account" in str(endpoint).lower():
            return {"totalWalletBalance": "100", "positions": [], "updateTime": 1}, True
        return [], True

    persistence = shell(complete_api)
    persistence._recon = Recon()
    persistence._maybe_authorize_user_stream_baseline = lambda *_args, **_kwargs: None
    persistence._store = SimpleNamespace(
        save_reconciliation_snapshot=lambda *_args, **_kwargs: None,
        save_reconciliation_result=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")),
    )
    failures: list[object] = []
    persistence._record_reconciliation_failure = lambda result, **_kwargs: failures.append(result) or False
    assert await persistence._reconcile() is False
    assert failures and any("RECON_PERSISTENCE_ERROR: OSError" in item.differences for item in failures)

    persistence._store.save_reconciliation_result = lambda *_args, **_kwargs: None
    assert await persistence._reconcile() is True


def test_reconcile_and_coverage_helpers_cover_fail_closed_edges() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    # The helper is deliberately defensive when the venue snapshot is absent.
    engine._env_mode = SimpleNamespace(value="testnet")
    engine._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}]}
    engine._last_account_at = 0.0
    assert engine._venue_position_gone("BTCUSDT") is False


def test_auto_resolve_incidents_no_alert_authority_is_a_noop() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._alerts = None
    engine._maybe_auto_resolve_incidents()
    assert engine._alerts is None


class _ExposureStore:
    """与 _MemStore 同形的持久化替身: 记录活在其内部 dict, 返回 bare dict 行。"""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict] = {}

    def _get_record(self, record_type: str, record_id: str):
        return self.records.get((record_type, record_id))

    def _write_record(self, record_type: str, record_id: str, payload: dict, **kw) -> bool:
        self.records[(record_type, record_id)] = dict(payload)
        return True

    def _delete_record(self, record_type: str, record_id: str) -> None:
        self.records.pop((record_type, record_id), None)


class _ExposureClock:
    """可控时钟: 默认不推进 (紧凑重试), 由 advance() 跨过 60s 下限。"""

    def __init__(self) -> None:
        self.t = 10_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float = 60.0) -> None:
        self.t += seconds


@pytest.mark.asyncio
async def test_emergency_close_and_protection_lifecycle_boundaries() -> None:
    """紧急平仓判据: 持久化裸露时钟 + 60s 跨度下限 + 数量取投影。

    BD-FIX (naked exposure ledger): 连击计数移入 protection_exposure 记录,
    不再因引擎重启/pos_id 翻转清零;E2 要求 3 次确认跨 ≥60s。
    """
    clock = _ExposureClock()
    pp = SimpleNamespace(quantity=2.0, side=OrderSide.BUY)

    def _base_engine() -> AutonomousEngine:
        engine = AutonomousEngine.__new__(AutonomousEngine)
        engine._store = _ExposureStore()
        engine._position_generation = {"BTCUSDT": 1}
        engine._sl_unprotectable_streak = {}
        engine._policy_id_active = ""
        engine._policy_version = ""
        engine._policy_signature = ""
        engine._position_projection = {"BTCUSDT": {"signed_quantity": "bad"}}
        return engine

    def _seed_exposure(engine: AutonomousEngine, *, clock: _ExposureClock) -> None:
        engine._store._write_record(
            "protection_exposure",
            "exposure:BTCUSDT",
            {
                "symbol": "BTCUSDT",
                "position_generation": 1,
                "unprotectable_since": clock() - 120,
                "last_reason": "SL_UNPROTECTABLE",
                "attempts": 3,
            },
        )

    engine = _base_engine()
    # 前两轮连击不足 3 → 等待确认; 第 3 轮达线但跨度 <60s → 紧凑重试被拦。
    assert await engine._maybe_emergency_close_unprotectable("p", "BTCUSDT", pp, now=clock) is False
    assert await engine._maybe_emergency_close_unprotectable("p", "BTCUSDT", pp, now=clock) is False
    assert await engine._maybe_emergency_close_unprotectable("p", "BTCUSDT", pp, now=clock) is False

    # 跨度满足后签名策略缺失 → 仍拒绝紧急平仓 (fail-closed)。
    clock.advance(120)
    assert await engine._maybe_emergency_close_unprotectable("p", "BTCUSDT", pp, now=clock) is False

    engine._policy_id_active = "policy"
    engine._policy_version = "v1"
    engine._policy_signature = "sig"
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "1.25"}}
    calls: list[dict] = []
    engine.enqueue_reduce_only_market = lambda **kwargs: calls.append(kwargs) or asyncio.sleep(0, result=False)
    # 放行后数量取引擎投影 1.25, 方向按持仓 BUY → SELL 平仓。
    assert await engine._maybe_emergency_close_unprotectable("p", "BTCUSDT", pp, now=clock) is False
    assert calls[-1]["side"] == "SELL" and calls[-1]["quantity"] == 1.25
    assert calls[-1]["correlation_id"] == "sl-unprotectable-p"

    # enqueue 异常 → 失败, 记录保留 (不误判成功)。
    clock.advance(120)
    engine.enqueue_reduce_only_market = lambda **kwargs: (_ for _ in ()).throw(OSError("outbox"))
    assert await engine._maybe_emergency_close_unprotectable("p", "BTCUSDT", pp, now=clock) is False

    # enqueue 成功 → True 且清除该 symbol 的裸露时钟记录。
    clock.advance(120)
    engine.enqueue_reduce_only_market = lambda **kwargs: asyncio.sleep(0, result=True)
    assert await engine._maybe_emergency_close_unprotectable("p", "BTCUSDT", pp, now=clock) is True
    assert engine._store._get_record("protection_exposure", "exposure:BTCUSDT") is None

    # 投影数量非法 → 回退 pp.quantity (2.0) 仍可平仓; 此处 enqueue 返回 False。
    bad_projection = _base_engine()
    _seed_exposure(bad_projection, clock=clock)
    bad_projection._policy_id_active = "policy"
    bad_projection._policy_version = "v1"
    bad_projection._policy_signature = "sig"
    bad_projection._position_projection = {"BTCUSDT": {"signed_quantity": "not-a-number"}}
    bad_projection.enqueue_reduce_only_market = lambda **_kwargs: asyncio.sleep(0, result=False)
    assert await bad_projection._maybe_emergency_close_unprotectable("bad", "BTCUSDT", pp, now=clock) is False

    # 投影缺失且 pp.quantity=0 → 数量 UNKNOWN, 拒绝平仓 (fail-closed)。
    no_qty = _base_engine()
    _seed_exposure(no_qty, clock=clock)
    no_qty._policy_id_active = "policy"
    no_qty._policy_version = "v1"
    no_qty._policy_signature = "sig"
    no_qty._position_projection = {}
    assert (
        await no_qty._maybe_emergency_close_unprotectable(
            "p", "BTCUSDT", SimpleNamespace(quantity=0, side=OrderSide.BUY), now=clock
        )
        is False
    )

    malformed = AutonomousEngine.__new__(AutonomousEngine)
    malformed._protection = SimpleNamespace(all_positions=lambda: (_ for _ in ()).throw(RuntimeError("memory")))
    assert malformed._dedup_ghost_protection_positions() == 0

    store_error = AutonomousEngine.__new__(AutonomousEngine)
    store_error._protection = SimpleNamespace(all_positions=lambda: {})
    store_error._store = SimpleNamespace(restore_protections=lambda: (_ for _ in ()).throw(OSError("store")))
    assert store_error._dedup_ghost_protection_positions() == 0

    dedup_cancel_error = AutonomousEngine.__new__(AutonomousEngine)
    ghost = SimpleNamespace(
        instrument_id="BTCUSDT",
        quantity=1.0,
        side=OrderSide.BUY,
        stop_loss=SimpleNamespace(is_active=lambda: True),
        take_profits=[],
    )
    active = SimpleNamespace(
        instrument_id="BTCUSDT",
        quantity=1.0,
        side=OrderSide.BUY,
        stop_loss=SimpleNamespace(is_active=lambda: True),
        take_profits=[],
    )
    dedup_cancel_error._protection = SimpleNamespace(all_positions=lambda: {"ghost": ghost, "keeper": active})
    dedup_cancel_error._store = SimpleNamespace(
        restore_protections=lambda: [{"position_id": "ghost", "exchange_order_id": "algo-ghost"}]
    )
    dedup_cancel_error._cancel_stale_protection_rows = lambda *_args: (_ for _ in ()).throw(OSError("cancel"))
    dedup_cancel_error._remove_protection_with_cleanup = lambda *_args: None
    dedup_cancel_error._active_algo_ids = {"ghost": {"algo-ghost"}}
    dedup_cancel_error._pending_protection_retry = set()
    assert dedup_cancel_error._dedup_ghost_protection_positions() == 1

    class _OutboxError:
        def restore_execution_plan(self, _intent_id):
            return SimpleNamespace(all_children_acknowledged=True)

        def ack(self, *_args, **_kwargs):
            raise RuntimeError("race")

    assert AutonomousEngine.__new__(AutonomousEngine)._reconcile_terminal_child_parent(SimpleNamespace()) is False
    terminal = AutonomousEngine.__new__(AutonomousEngine)
    terminal._outbox = SimpleNamespace(
        restore_execution_plan=lambda _id: SimpleNamespace(all_children_acknowledged=True)
    )
    assert terminal._reconcile_terminal_child_parent(SimpleNamespace(intent_id="x")) is False
    terminal._outbox = _OutboxError()
    assert terminal._reconcile_terminal_child_parent(SimpleNamespace(intent_id="x")) is False
    missing_restore = AutonomousEngine.__new__(AutonomousEngine)
    missing_restore._outbox = SimpleNamespace(restore_execution_plan=object())
    assert missing_restore._reconcile_terminal_child_parent(SimpleNamespace(intent_id="x")) is False


def test_protection_freshness_and_venue_position_gone_reject_malformed_facts() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = None
    assert engine._protection_row_fresh("x") is False
    engine._store = SimpleNamespace(restore_protections=lambda: (_ for _ in ()).throw(OSError("store")))
    assert engine._protection_row_fresh("x") is False
    engine._store = SimpleNamespace(
        restore_protections=lambda: [
            {"exchange_order_id": "x", "created_at": ""},
            {"exchange_order_id": "y", "created_at": "bad"},
        ]
    )
    assert engine._protection_row_fresh("x") is False
    assert engine._protection_row_fresh("y") is False

    engine._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "bad"}, "malformed"]}
    engine._last_account_at = __import__("time").time()
    assert engine._venue_position_gone("") is False
    assert engine._venue_position_gone("BTCUSDT") is False
    engine._last_account = {"positions": ["malformed"]}
    assert engine._venue_position_gone("BTCUSDT") is True


@pytest.mark.asyncio
async def test_stale_execution_and_order_state_recovery_maps_every_known_venue_status() -> None:
    from beidou_safety.execution.command_aggregate import ChildCommandState

    stale_rows = [
        {"symbol": "", "client_order_id": "cid", "parent_intent_id": "i"},
        {
            "symbol": "BTCUSDT",
            "client_order_id": "cid-error",
            "parent_intent_id": "i-error",
            "sequence": 1,
            "state": "SENDING",
        },
        {
            "symbol": "BTCUSDT",
            "client_order_id": "cid-notfound",
            "parent_intent_id": "i-nf",
            "sequence": 1,
            "state": "SENDING",
        },
        {
            "symbol": "BTCUSDT",
            "client_order_id": "cid-cancel",
            "parent_intent_id": "i-cancel",
            "sequence": 2,
            "state": "ACKED",
            "exchange_order_id": "77",
        },
        {
            "symbol": "BTCUSDT",
            "client_order_id": "cid-filled",
            "parent_intent_id": "i-filled",
            "sequence": 3,
            "state": "ACKED",
        },
        {
            "symbol": "BTCUSDT",
            "client_order_id": "cid-canceled",
            "parent_intent_id": "i-canceled",
            "sequence": 4,
            "state": "ACKED",
        },
        {
            "symbol": "BTCUSDT",
            "client_order_id": "cid-expired",
            "parent_intent_id": "i-expired",
            "sequence": 5,
            "state": "ACKED",
        },
        {
            "symbol": "BTCUSDT",
            "client_order_id": "cid-rejected",
            "parent_intent_id": "i-rejected",
            "sequence": 6,
            "state": "ACKED",
        },
        {
            "symbol": "BTCUSDT",
            "client_order_id": "cid-new",
            "parent_intent_id": "i-new",
            "sequence": 7,
            "state": "ACKED",
        },
        {
            "symbol": "BTCUSDT",
            "client_order_id": "cid-partial",
            "parent_intent_id": "i-partial",
            "sequence": 8,
            "state": "ACKED",
        },
        {
            "symbol": "BTCUSDT",
            "client_order_id": "cid-unknown",
            "parent_intent_id": "i-unknown",
            "sequence": 9,
            "state": "ACKED",
        },
        {
            "symbol": "BTCUSDT",
            "client_order_id": "cid-planned",
            "parent_intent_id": "i-planned",
            "sequence": 10,
            "state": "PLANNED",
        },
        {
            "symbol": "BTCUSDT",
            "client_order_id": "cid-bad-recover",
            "parent_intent_id": "i-bad",
            "sequence": 11,
            "state": "ACKED",
        },
    ]
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    recovered: list[tuple] = []

    class Outbox:
        def stale_child_commands(self, _age: float) -> list[dict]:
            return stale_rows

        def recover_stale_child(self, *args, **kwargs) -> None:
            if args[0] == "i-bad":
                raise RuntimeError("aggregate race")
            recovered.append((args, kwargs))

    engine._outbox = Outbox()

    async def query(_endpoint, **kwargs):
        cid = kwargs["params"]["origClientOrderId"]
        if cid == "cid-error":
            raise OSError("venue")
        values = {
            "cid-notfound": {"code": -2013, "msg": "Order does not exist."},
            "cid-cancel": {"code": -2013, "msg": "Unknown order"},
            "cid-filled": {"status": "FILLED", "orderId": "101", "executedQty": "1"},
            "cid-canceled": {"status": "CANCELED", "orderId": "102", "executedQty": "0.5"},
            "cid-expired": {"status": "EXPIRED", "orderId": "103", "executedQty": "0"},
            "cid-rejected": {"status": "REJECTED", "orderId": "104"},
            "cid-new": {"status": "NEW", "orderId": "105"},
            "cid-partial": {"status": "PARTIALLY_FILLED", "orderId": "106", "executedQty": "0.2"},
            "cid-unknown": {"status": "PENDING_CANCEL", "orderId": "107"},
            "cid-planned": {"status": "NEW", "orderId": "108"},
            "cid-bad-recover": {"status": "FILLED", "orderId": "109"},
        }
        return values[cid]

    engine._api_async = query
    assert await engine._resolve_stale_execution_commands() == 9
    assert any(item[0][2] is ChildCommandState.SENDING for item in recovered if item[0][0] == "i-planned")

    disabled = AutonomousEngine.__new__(AutonomousEngine)
    disabled._can_write = False
    disabled._outbox = Outbox()
    assert await disabled._resolve_stale_execution_commands() == 0

    missing = AutonomousEngine.__new__(AutonomousEngine)
    missing._can_write = True
    missing._outbox = SimpleNamespace()
    assert await missing._resolve_stale_execution_commands() == 0

    listing_error = AutonomousEngine.__new__(AutonomousEngine)
    listing_error._can_write = True
    listing_error._outbox = SimpleNamespace(
        stale_child_commands=lambda _age: (_ for _ in ()).throw(OSError("list")),
        recover_stale_child=lambda *args, **kwargs: None,
    )
    assert await listing_error._resolve_stale_execution_commands() == 0

    terminal = AutonomousEngine.__new__(AutonomousEngine)
    terminal._outbox = SimpleNamespace(project_order_terminal=lambda *_args: None)
    terminal._project_order_terminal("id", "FILLED", "1")
    terminal._outbox = SimpleNamespace(
        project_order_terminal=lambda *_args: (_ for _ in ()).throw(RuntimeError("project"))
    )
    terminal._project_order_terminal("id", "FILLED", "1")
    terminal._outbox = None
    terminal._project_order_terminal("id", "CANCELED", "")


@pytest.mark.asyncio
async def test_stale_order_state_recovery_preserves_unknown_and_books_terminal_fills() -> None:
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    rows = [
        {"order_id": "1", "symbol": "BTCUSDT", "status": "NEW"},
        {
            "order_id": "2",
            "symbol": "BTCUSDT",
            "status": "UNKNOWN",
            "updated_at": (now + timedelta(seconds=1)).isoformat(),
        },
        {"order_id": "3", "symbol": "BTCUSDT", "status": "UNKNOWN", "updated_at": "bad"},
        {"order_id": "4", "symbol": "", "status": "UNKNOWN"},
        {"order_id": "5", "symbol": "BTCUSDT", "status": "UNKNOWN"},
        {"order_id": "6", "symbol": "BTCUSDT", "status": "UNKNOWN"},
        {"order_id": "7", "symbol": "BTCUSDT", "status": "UNKNOWN"},
        {"order_id": "8", "symbol": "BTCUSDT", "status": "UNKNOWN"},
        {"order_id": "9", "symbol": "BTCUSDT", "status": "UNKNOWN"},
        {"order_id": "10", "symbol": "BTCUSDT", "status": "UNKNOWN"},
        {"order_id": "11", "symbol": "BTCUSDT", "status": "UNKNOWN"},
        {"order_id": "12", "symbol": "BTCUSDT", "status": "UNKNOWN"},
    ]
    saved: list[tuple] = []
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._active_order_ids = {str(i) for i in range(1, 13)}
    engine._owned_order_ids = set(engine._active_order_ids)
    engine._order_trackers = {}
    engine._order_symbols = {str(i): "BTCUSDT" for i in range(1, 13)}

    def save(*args, **kwargs):
        if args and args[0] == "6":
            raise OSError("persist")
        saved.append((args, kwargs))

    engine._store = SimpleNamespace(restore_order_states=lambda: rows, save_order_state=save)
    booked: list[str] = []
    engine._book_venue_terminal_fill = lambda oid, _symbol, _raw: asyncio.sleep(0, result=booked.append(oid))

    async def query(_endpoint, **kwargs):
        oid = str(kwargs["params"]["orderId"])
        if oid == "5":
            raise TypeError("bad id")
        return {
            "1": {"status": "NEW"},
            "3": {"status": "PARTIALLY_FILLED", "executedQty": "0.2"},
            "5": {"status": "FILLED", "executedQty": "1", "side": "BUY", "type": "MARKET", "origQty": "1"},
            "6": {"status": "CANCELED", "executedQty": "0.5"},
            "7": {"status": "EXPIRED", "executedQty": "0"},
            "8": {"status": "REJECTED"},
            "9": {"code": -2013, "msg": "Order does not exist"},
            "10": {"status": "PENDING_CANCEL"},
            "11": None,
            "12": {"status": "FILLED", "executedQty": "1", "side": "BUY", "type": "MARKET", "origQty": "1"},
        }[oid]

    engine._api_async = query
    assert await engine._resolve_stale_order_states(batch_limit=20, min_age_seconds=120) == 4
    assert {row[0][0] for row in saved} == {"7", "8", "9", "12"}
    assert booked == ["12"]
    assert "12" not in engine._active_order_ids

    startup_saved: list[tuple] = []
    startup_booked: list[str] = []
    startup = AutonomousEngine.__new__(AutonomousEngine)
    startup._can_write = True
    startup._active_order_ids = {"13"}
    startup._owned_order_ids = {"13"}
    startup._order_trackers = {}
    startup._order_symbols = {"13": "BTCUSDT"}
    startup._store = SimpleNamespace(
        restore_order_states=lambda: [{"order_id": "13", "symbol": "BTCUSDT", "status": "NEW"}],
        restore_fill_events=lambda: [],
        save_order_state=lambda *args, **kwargs: startup_saved.append((args, kwargs)),
    )
    startup._book_venue_terminal_fill = lambda oid, _symbol, _raw: asyncio.sleep(0, result=startup_booked.append(oid))

    async def startup_query(_endpoint, **_kwargs):
        return {
            "orderId": "13",
            "symbol": "BTCUSDT",
            "status": "FILLED",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "1",
            "executedQty": "1",
            "avgPrice": "100",
        }

    startup._api_async = startup_query
    assert await startup._resolve_stale_order_states(min_age_seconds=0, include_active=True) == 1
    assert startup_saved[0][0][0] == "13"
    assert startup_saved[0][0][6] == "FILLED"
    assert startup_booked == ["13"]

    absent = AutonomousEngine.__new__(AutonomousEngine)
    absent._can_write = True
    absent._store = SimpleNamespace(
        restore_order_states=lambda: (_ for _ in ()).throw(OSError("read")), save_order_state=save
    )
    assert await absent._resolve_stale_order_states() == 0


@pytest.mark.asyncio
async def test_stale_resolution_empty_unknown_and_malformed_fact_boundaries() -> None:
    class ChildOutbox:
        def __init__(self, rows: list[dict]) -> None:
            self.rows = rows

        def stale_child_commands(self, _age: float) -> list[dict]:
            return self.rows

        def recover_stale_child(self, *_args, **_kwargs) -> None:
            return None

    empty = AutonomousEngine.__new__(AutonomousEngine)
    empty._can_write = True
    empty._outbox = ChildOutbox([])
    assert await empty._resolve_stale_execution_commands() == 0

    unknown_raw = AutonomousEngine.__new__(AutonomousEngine)
    unknown_raw._can_write = True
    unknown_raw._outbox = ChildOutbox(
        [
            {"symbol": "BTCUSDT", "client_order_id": "unknown", "parent_intent_id": "i1", "state": "ACKED"},
            {"symbol": "BTCUSDT", "client_order_id": "not-dict", "parent_intent_id": "i2", "state": "ACKED"},
        ]
    )

    async def unknown_query(_endpoint: object, **kwargs: object) -> object:
        return {"code": -1000, "msg": "temporary"} if kwargs["params"]["origClientOrderId"] == "unknown" else "bad"

    unknown_raw._api_async = unknown_query
    assert await unknown_raw._resolve_stale_execution_commands() == 0

    missing_store = AutonomousEngine.__new__(AutonomousEngine)
    missing_store._can_write = True
    missing_store._store = SimpleNamespace(save_order_state=None)
    assert await missing_store._resolve_stale_order_states() == 0

    saved: list[tuple] = []
    naive = AutonomousEngine.__new__(AutonomousEngine)
    naive._can_write = True
    naive._active_order_ids = {"1", "2", "3", "4"}
    naive._owned_order_ids = set(naive._active_order_ids)
    naive._order_trackers = {}
    naive._order_symbols = {str(i): "BTCUSDT" for i in range(1, 5)}
    naive._store = SimpleNamespace(
        restore_order_states=lambda: [
            {"order_id": "1", "symbol": "BTCUSDT", "status": "UNKNOWN", "updated_at": "2020-01-01T00:00:00"},
            {"order_id": "2", "symbol": "BTCUSDT", "status": "UNKNOWN", "updated_at": "2020-01-01T00:00:00"},
            {"order_id": "3", "symbol": "BTCUSDT", "status": "UNKNOWN", "updated_at": "2020-01-01T00:00:00"},
            {"order_id": "4", "symbol": "BTCUSDT", "status": "UNKNOWN", "updated_at": "2020-01-01T00:00:00"},
        ],
        save_order_state=lambda *args, **kwargs: saved.append((args, kwargs)),
    )

    async def naive_query(_endpoint: object, **kwargs: object) -> object:
        oid = str(kwargs["params"]["orderId"])
        if oid == "1":
            return {"status": "FILLED", "executedQty": "bad"}
        return {"status": "PENDING_CANCEL"}

    naive._api_async = naive_query
    assert await naive._resolve_stale_order_states(batch_limit=1, min_age_seconds=120) == 1
    assert saved and saved[0][0][0] == "1"

    no_candidates = AutonomousEngine.__new__(AutonomousEngine)
    no_candidates._can_write = True
    no_candidates._store = SimpleNamespace(
        restore_order_states=lambda: [{"order_id": "9", "symbol": "BTCUSDT", "status": "NEW"}],
        save_order_state=lambda *_args, **_kwargs: None,
    )
    assert await no_candidates._resolve_stale_order_states() == 0


@pytest.mark.asyncio
async def test_venue_leverage_and_user_stream_keepalive_exception_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = SimpleNamespace(value="testnet")
    engine._venue_leverage = {}
    monkeypatch.setenv("BEIDOU_SYNC_VENUE_LEVERAGE", "1")
    engine._api_async = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("venue"))
    assert await engine._sync_venue_leverage("BTCUSDT", 5) is False
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(0, result={"error": True, "msg": "rejected"})
    assert await engine._sync_venue_leverage("BTCUSDT", 5) is False
    engine._user_stream_stopping = False
    engine._adapter = SimpleNamespace(keepalive_user_listen_key=lambda _key: (_ for _ in ()).throw(OSError("network")))
    engine._user_stream_fault = lambda reason, terminal=False: setattr(engine, "keepalive_fault", (reason, terminal))

    async def stop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(engine_module.asyncio, "sleep", stop_sleep)
    await engine._user_stream_keepalive_loop("key")
    assert engine.keepalive_fault == ("LISTEN_KEY_KEEPALIVE_FAILED", True) or engine.keepalive_fault[0].startswith(
        "LISTEN_KEY_"
    )
