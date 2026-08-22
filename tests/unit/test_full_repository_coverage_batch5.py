"""Behavior-backed coverage for research validation and durable boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from beidou_certification.g5_scenarios.base import NotionalExceededError, NotionalLedger, ScenarioContext
from beidou_certification.g5_scenarios.protocol.credential_failure import (
    CredentialFailureScenario,
    _build_client,
    _received_exchange_error,
    classify_auth_error,
)
from beidou_exchange.core.error_taxonomy import Result
from beidou_exchange.core.protocol import UserStreamEvent
from beidou_exchange.core.user_stream import UserStreamSequencer, UserStreamStatus
from beidou_infra.atomic_persistence import (
    AtomicIntent,
    AtomicOrderAggregate,
    AtomicOutbox,
    AtomicPersistence,
    PersistStatus,
)
from beidou_infra.outbox import OutboxWorker, PostgresIntentOutbox
from beidou_research.mining.contracts import FactorId, HorizonUnit, PredictionKey, PredictionRecord
from beidou_research.mining.evaluation.purged_walk_forward import Fold, FoldConfig, FoldResult, PurgedWalkForward
from beidou_research.mining.leakage_guard import LeakageGuard
from beidou_research.mining.selection.redundancy import RedundancyDetector, _ols_r_squared, _pearson
from beidou_safety.execution.reconciliation import ReconciliationEngine
from beidou_shared.types import InstrumentId, SchemaVersion, VenueId


def _event(sequence: int | None, event_time: int = 1000, event_id: str = "event") -> UserStreamEvent:
    return UserStreamEvent(
        event_type="ORDER_TRADE_UPDATE",
        event_id=event_id,
        event_time_ms=event_time,
        transaction_time_ms=event_time,
        sequence=sequence,
        raw_event={},
    )


def test_user_stream_sequencer_duplicate_replay_and_negative_boundaries() -> None:
    sequencer = UserStreamSequencer()
    assert sequencer.observe(_event(1)).accepted
    duplicate = sequencer.observe(_event(1, event_id="duplicate"))
    assert not duplicate.accepted and duplicate.status is UserStreamStatus.DUPLICATE
    with pytest.raises(ValueError, match="explicitly allow"):
        sequencer.mark_replayed(None)
    with pytest.raises(ValueError, match="non-negative"):
        sequencer.mark_replayed(-1)
    with pytest.raises(ValueError, match="last_event_time_ms"):
        sequencer.mark_replayed(1, last_event_time_ms=-1)
    with pytest.raises(ValueError, match="last_sequence"):
        sequencer.restore(-1)


def test_atomic_persistence_rolls_back_all_records_on_write_failure() -> None:
    class _FailingOrders(dict):
        def __setitem__(self, _key: object, _value: object) -> None:
            raise RuntimeError("order write failed")

    persistence = AtomicPersistence()
    persistence._orders = _FailingOrders()
    intent = AtomicIntent("intent", "client", "BTCUSDT", "BUY", "1", "MARKET")
    outbox = AtomicOutbox("outbox", "")
    order = AtomicOrderAggregate("corr", "", "BTCUSDT")
    ok, reason = persistence.atomic_create_intent(intent, outbox, order)
    assert not ok and reason.startswith("ROLLBACK:")
    assert intent.status is PersistStatus.FAILED
    assert persistence._intents == {} and persistence._outbox == {}


def test_purged_walk_forward_duration_no_evaluator_and_inconsistent_folds() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fold = Fold(
        0, start, start + timedelta(days=2), start + timedelta(days=2), start + timedelta(days=3), start, start, 1, 1
    )
    assert fold.train_duration_days == 2 and fold.test_duration_days == 1
    times = [start + timedelta(hours=index) for index in range(24 * 4 + 1)]
    labels = list(times)
    no_eval = PurgedWalkForward(
        FoldConfig(n_folds=4, train_fraction=0.5, min_train_samples=1, min_test_samples=1, min_folds_for_verdict=4)
    ).run(times, labels, 4.0)
    assert no_eval.failure_reasons[0] == "too_few_completed_folds"

    calls = 0

    def evaluator(train: list[int], test: list[int]) -> FoldResult:
        nonlocal calls
        calls += 1
        return FoldResult(
            fold_id=calls,
            train_samples=len(train),
            test_samples=len(test),
            ic_mean=1.0 if calls % 2 else -1.0,
            ic_std=1.0,
            sharpe=1.0,
            strategy_sharpe=1.0,
        )

    inconsistent = PurgedWalkForward(
        FoldConfig(n_folds=4, train_fraction=0.5, min_train_samples=1, min_test_samples=1, min_folds_for_verdict=4)
    ).run(times, labels, 4.0, evaluator=evaluator)
    assert "fold_inconsistency" in inconsistent.failure_reasons


def _prediction(at: datetime) -> PredictionRecord:
    key = PredictionKey(
        venue=VenueId("BINANCE"),
        symbol=InstrumentId("BTCUSDT"),
        timeframe="1h",
        prediction_time=at,
        data_available_time=at,
        horizon=1,
        horizon_unit=HorizonUnit.BAR,
        factor_id=FactorId("factor"),
        factor_version=SchemaVersion("v1"),
    )
    return PredictionRecord(prediction_key=key, prediction_value=0.1)


def test_leakage_guard_future_prediction_and_batch_violations() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    prediction = _prediction(now)
    future_key = object.__new__(PredictionKey)
    for field_name in prediction.prediction_key.__dataclass_fields__:
        object.__setattr__(future_key, field_name, getattr(prediction.prediction_key, field_name))
    object.__setattr__(future_key, "data_available_time", now + timedelta(minutes=1))
    future = replace(prediction, prediction_key=future_key)
    guard = LeakageGuard()
    report = guard.check_prediction(future)
    assert not report.passed and "FUTURE_DATA" in report.violations[0]
    batch = guard.check_batch([future])
    assert not batch.passed and batch.details["future_data_count"] == 1


def test_redundancy_short_series_constant_and_vif_boundaries() -> None:
    detector = RedundancyDetector()
    corr = detector.compute_correlation_matrix({"a": [1, 2], "b": [1, 2]})
    assert corr["a"]["b"] == 0.0
    assert _pearson([1, 2], [1, 2]) == 0.0
    assert _ols_r_squared([1.0, 1.0, 1.0], [[1.0], [1.0], [1.0]]) == 1.0
    assert detector.compute_vif_scores({"a": [1.0] * 3}) == {"a": 1.0}


def test_reconciliation_order_detail_invalid_and_quantity_mismatch() -> None:
    from tests.unit.test_reconciliation_contract import _facts

    now = datetime.now(timezone.utc)
    system = _facts(
        timestamp=now,
        detail={"order-1": {"symbol": "BTCUSDT", "side": "BUY", "qty": "1", "price": "NaN", "type": "LIMIT"}},
    )
    exchange = _facts(
        timestamp=now,
        source="EXCHANGE",
        detail={"order-1": {"symbol": "BTCUSDT", "side": "BUY", "qty": "2", "price": "100", "type": "LIMIT"}},
    )
    result = ReconciliationEngine.compare(system, exchange, now=now)
    assert not result.matched and any("qty" in item or "price" in item for item in result.differences)
    invalid_qty_system = _facts(
        timestamp=now,
        detail={"order-1": {"symbol": "BTCUSDT", "side": "BUY", "qty": "NaN", "type": "LIMIT"}},
    )
    invalid_qty_exchange = _facts(
        timestamp=now,
        source="EXCHANGE",
        detail={"order-1": {"symbol": "BTCUSDT", "side": "BUY", "qty": "1", "type": "LIMIT"}},
    )
    invalid = ReconciliationEngine.compare(invalid_qty_system, invalid_qty_exchange, now=now)
    assert not invalid.matched and any("INVALID_ORDER_QTY_FACT" in item for item in invalid.differences)


def test_credential_failure_classification_client_and_notional_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert classify_auth_error({"code": "bad"}) == "OTHER"
    client = _build_client()
    assert client._rest_url == "https://demo-fapi.binance.com"
    assert not _received_exchange_error(Result.success({}))

    monkeypatch.setattr(
        "beidou_certification.g5_scenarios.protocol.credential_failure._build_client",
        lambda: (_ for _ in ()).throw(NotionalExceededError("credential_failure", 51.0, 50.0)),
    )
    with pytest.raises(NotionalExceededError):
        asyncio.run(
            CredentialFailureScenario().run(ScenarioContext(None, NotionalLedger(1000), tmp_path, "BTCUSDT", False))
        )


def test_truth_snapshot_hash_and_eligibility_status_edges() -> None:
    from beidou_control.truth import TradingEligibility, derive_eligibility
    from tests.unit.test_full_repository_coverage_batch2 import _fresh_truth

    snapshot = _fresh_truth()
    assert len(snapshot.compute_hash()) == 64
    assert derive_eligibility(replace(snapshot, protection_status="GAP")) is TradingEligibility.NO_NEW_RISK
    assert derive_eligibility(replace(snapshot, risk_status="CRITICAL")) is TradingEligibility.LOCK
    assert derive_eligibility(replace(snapshot, risk_status="WARNING")) is TradingEligibility.NO_NEW_RISK


def test_postgres_outbox_unconfigured_fail_closed_and_datetime_approval(tmp_path: Path) -> None:
    worker = OutboxWorker()
    with pytest.raises(RuntimeError, match="POSTGRES_OUTBOX_UNAVAILABLE"), worker._connection_scope():
        pass
    assert asyncio.run(OutboxWorker(fencing_token=1).recover_inflight()) == 0
    unconfigured_pg = PostgresIntentOutbox.__new__(PostgresIntentOutbox)
    unconfigured_pg._connection_factory = None
    assert unconfigured_pg.stale_child_commands() == []
    assert (
        asyncio.run(
            OutboxWorker(fencing_token=1)._transition_owned(
                "m", from_states=("PENDING",), to_status="SENT", event_type="sent", fields=""
            )
        )
        is False
    )
    assert asyncio.run(OutboxWorker(fencing_token=1).recover_inflight()) == 0

    from tests.unit.test_full_repository_coverage_batch2 import _intent
    from tests.unit.test_outbox_boundary_completeness import _Conn, _Cursor

    intent = replace(
        _intent("datetime-approval"),
        risk_approval_id="approval",
        risk_approval_signature="signature",
        risk_proposal_hash="proposal",
        risk_intent_hash="intent-hash",
        risk_account_snapshot_hash="account-hash",
        risk_snapshot_hash="risk-hash",
        risk_policy_version="v1",
        risk_nonce="nonce",
        risk_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    cursor = _Cursor()
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), fencing_token=1)
    assert store.commit(intent)
