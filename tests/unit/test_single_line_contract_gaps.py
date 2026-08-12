from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from beidou_certification.contracts import StagedCertification
from beidou_certification.staged_ladder import GateCertificate, GateLevel, StagedCertificationLadder
from beidou_exchange.core.protocol import Capability, ExchangeAdapter
from beidou_infra.event_store import DomainEvent, EventStore
from beidou_infra.ha import FactSource, InfrastructureTopology
from beidou_observability.monitoring.contracts import TraceStage
from beidou_observability.monitoring.instrumentation import InstrumentationRecorder, TraceEvent
from beidou_observability.monitoring.retention import RetentionPolicy, RetentionTier
from beidou_observability.monitoring.storm_detector import StormDetector
from beidou_research.mining.contracts import HorizonUnit, PredictionKey, PredictionRecord
from beidou_research.mining.selection.ensemble import FactorEnsemble
from beidou_safety.execution.command_aggregate import (
    ChildCommandState,
    ExecutionChildCommand,
    ParentExecutionAggregate,
    ParentExecutionState,
)
from beidou_safety.execution.contracts import ExecutionPlan
from beidou_safety.execution.order_state_machine import OrderState, OrderStateMachine
from beidou_safety.risk.approval import ApprovalSigner
from beidou_shared.types import DataQualityTier, FactorId, InstrumentId, SchemaVersion, VenueId
from beidou_strategy.alpha.extensions import FundingRateSignal
from beidou_strategy.portfolio.contracts import SignedPortfolioTarget
from beidou_strategy.state.real_cost_model import RealCostModel


def test_instrumentation_reports_completed_stage() -> None:
    recorder = InstrumentationRecorder()
    event = TraceEvent("trace", "corr", "strategy", "intent", "client", TraceStage.EXCHANGE_ACKED, 1.0)
    assert recorder.record(event)
    assert recorder.stage_completed("corr", TraceStage.EXCHANGE_ACKED)


def test_storm_incident_child_link_preserves_root_cause() -> None:
    detector = StormDetector()
    parent = detector.build_storm_incident({"root_cause_fingerprint": "root", "unique_count": 5}, "parent")
    child = detector.build_storm_incident({"root_cause_fingerprint": "child"}, "child")
    link = detector.link_child(parent, child)
    assert link.parent_incident_id == "parent"
    assert link.child_incident_id == "child"
    assert link.root_cause_fingerprint == "root"


def test_certification_retention_is_indefinite() -> None:
    policy = RetentionPolicy()
    assert policy.should_retain(
        RetentionTier.CERTIFICATION,
        float("inf"),
        is_certification=True,
    )


def test_funding_signal_rejects_nonfinite_rates() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        FundingRateSignal(VenueId("BINANCE"), InstrumentId("BTCUSDT"), float("nan"), 1.0)


def test_terminal_order_transition_has_explicit_terminal_reason() -> None:
    machine = OrderStateMachine("order", state=OrderState.FILLED)
    ok, reason = machine.transition(OrderState.CANCELED)
    assert not ok
    assert reason == "TERMINAL_STATE:FILLED"

    created = OrderStateMachine("created")
    ok, reason = created.transition(OrderState.FILLED)
    assert not ok
    assert reason == "INVALID_TRANSITION:CREATED→FILLED"


def test_event_store_integrity_reports_in_memory_tampering() -> None:
    event = DomainEvent("stream", "ORDER", 1, "CREATED", {"id": "order"})
    store = EventStore()
    assert store.append(event)
    object.__setattr__(event, "checksum", "tampered")
    assert store.verify_integrity() == ["Checksum mismatch: stream#1"]


def test_covariance_shrinkage_empty_input_has_empty_weights() -> None:
    weights = FactorEnsemble().covariance_shrinkage({})
    assert weights.weights == {}
    assert weights.method == "shrinkage"


def test_approval_verifier_fails_closed_if_key_is_lost() -> None:
    signer = ApprovalSigner("key")
    approval = signer.create_approval("approval", "intent", "portfolio", "account-v1", "policy-v1")
    signer._key = ""
    assert not signer.verify(approval)


def test_real_cost_model_uses_bounded_fallback_without_depth() -> None:
    estimate = RealCostModel().estimate("BTCUSDT", "BUY", 1.0, 100.0, spread_bps=2.0, participation_pct=0.1)
    assert estimate.impact_bps == 0.1


def test_staged_ladder_rejects_missing_prerequisite_certificate() -> None:
    ladder = StagedCertificationLadder()
    evidence = GateCertificate(gate=GateLevel.G1, evidence_hash="hash", commit_family=["commit"])
    assert ladder.certify(GateLevel.G1, evidence) == (False, "PREREQUISITE_MISSING:G0")


def test_unknown_ha_domain_defaults_to_postgresql_authority() -> None:
    topology = InfrastructureTopology("testnet")
    assert topology.get_authoritative_source("unknown-domain") is FactSource.POSTGRESQL


def test_portfolio_target_rejects_nonfinite_exposure() -> None:
    assert not SignedPortfolioTarget(target_exposure=float("nan"), delta=0.0).is_valid()


def test_staged_certification_detects_broken_next_gate_contract() -> None:
    class BrokenCertifiedGate:
        certified = True

        @staticmethod
        def can_skip_to(_target: Any) -> bool:
            return False

    chain = StagedCertification(stages={"G0": BrokenCertifiedGate()})
    assert not chain.is_chain_complete()


def test_non_emergency_plan_never_triggers_emergency_position_bound() -> None:
    assert ExecutionPlan(is_emergency=False).never_increases_absolute_position(0.0)


def test_failed_data_quality_prediction_is_not_evaluable() -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    key = PredictionKey(
        VenueId("BINANCE"),
        InstrumentId("BTCUSDT"),
        "1h",
        now,
        now,
        1,
        HorizonUnit.BAR,
        FactorId("factor"),
        SchemaVersion("1"),
    )
    assert not PredictionRecord(key, 1.0, dq_tier=DataQualityTier.FAIL).can_be_evaluated()


def test_exchange_adapter_capability_lookup_is_explicit() -> None:
    class Adapter(ExchangeAdapter):
        @property
        def venue_id(self):
            return VenueId("TEST")

        @property
        def capabilities(self):
            return frozenset({Capability.FUTURES_USD_M})

        async def get_exchange_info(self):
            raise NotImplementedError

        async def check_health(self, venue_id):
            raise NotImplementedError

        async def get_account_info(self, account_ref):
            raise NotImplementedError

        async def get_balances(self, account_ref):
            raise NotImplementedError

        async def get_positions(self, account_ref):
            raise NotImplementedError

        async def create_order(self, request):
            raise NotImplementedError

        async def cancel_order(self, order_id, venue_instrument):
            raise NotImplementedError

        async def get_order_status(self, order_id, venue_instrument):
            raise NotImplementedError

    adapter = Adapter()
    assert adapter.supports(Capability.FUTURES_USD_M)
    assert not adapter.supports(Capability.SPOT)


def _child(sequence: int) -> ExecutionChildCommand:
    return ExecutionChildCommand.create(
        parent_intent_id="intent",
        sequence=sequence,
        symbol="BTCUSDT",
        side="BUY",
        quantity="1",
        order_type="LIMIT",
        time_in_force="GTC",
        client_order_id=f"child-{sequence}",
        limit_price="50000",
    )


def test_mixed_acked_and_filled_children_make_parent_partially_filled() -> None:
    parent = ParentExecutionAggregate.create("intent", [_child(0), _child(1)])
    parent = parent.transition_child(0, ChildCommandState.SENDING, event_id="send-0")
    parent = parent.transition_child(
        0,
        ChildCommandState.FILLED,
        event_id="fill-0",
        exchange_order_id="venue-0",
        cumulative_filled_quantity=Decimal("1"),
    )
    parent = parent.transition_child(1, ChildCommandState.SENDING, event_id="send-1")
    parent = parent.transition_child(1, ChildCommandState.ACKED, event_id="ack-1", exchange_order_id="venue-1")
    assert parent.state is ParentExecutionState.PARTIALLY_FILLED
