"""Behavior-backed coverage for routing, replay, parser and certificate edges."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from beidou_certification.certificate_chain import CertificateChain, GateCertificate, GateResult
from beidou_certification.g5_scenarios.protocol.clock_skew import _require_ok as skew_require_ok
from beidou_certification.gate_verifier import (
    GateVerification,
    _timestamp,
    verify_evidence_integrity,
    verify_evidence_path_not_sufficient,
    verify_g5_certificate,
    verify_g7_certificate,
)
from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter
from beidou_exchange.core.error_taxonomy import Result
from beidou_exchange.core.protocol import Capability
from beidou_exchange.router.router import CapabilityMatrix, ExchangeRouter
from beidou_research.backtest.replay import ReplayResult, ReplayValidator, simulate_paper_window
from beidou_research.factors.revalidation_engine import revalidate_factor
from beidou_safety.execution.user_events import UserProjectionStatus, UserStreamProjector
from beidou_shared.errors import ErrorCategory
from beidou_shared.types import InstrumentId, Price, Quantity, ResultStatus, VenueId, VenueInstrument


def test_certificate_chain_validity_persistence_and_load_failures(tmp_path: Path) -> None:
    assert not GateCertificate(revoked=True, signature="x").is_valid()
    assert not GateCertificate(signature="").is_valid()
    chain = CertificateChain(_chain_file=str(tmp_path / "chain.json"))
    cert = GateCertificate(gate="G1", result=GateResult.PASS)
    assert chain.issue(cert, "issuer") == (True, "ISSUED:G1")
    cert.signature = "tampered"
    assert chain.verify("G1", "verifier") is GateResult.FAIL
    assert chain.save_chain()

    directory_target = tmp_path / "directory-target"
    directory_target.mkdir()
    assert not CertificateChain(_chain_file=str(directory_target)).save_chain()
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{")
    assert not CertificateChain(_chain_file=str(malformed)).load_chain()


def test_gate_verifier_naive_clock_evidence_read_and_path_edges(tmp_path: Path) -> None:
    naive_now = datetime.fromisoformat("2026-08-08")
    scenarios = {
        name: {"status": "PASS"}
        for name in {
            "create_query_cancel",
            "stable_client_order_id",
            "ack_loss",
            "timeout_unknown_recovery",
            "duplicate_request",
            "partial_fill",
            "cancel_fill_race",
            "user_stream_reconnect",
            "native_protection",
            "process_restart",
            "database_restart",
            "double_worker_fencing",
            "clock_skew",
            "rate_limit",
            "credential_failure",
            "reconciliation_mismatch",
        }
    }
    g5 = {
        "gate": "G5",
        "status": "PASS",
        "certification_mode": "FULL",
        "commit": "abc",
        "testnet_url": "https://demo-fapi.binance.com",
        "mainnet_prohibited": True,
        "is_simulated": False,
        "evidence_hash": "hash",
        "started_at": "2026-08-08T00:00:00",
        "ended_at": "2026-08-08T00:10:00",
        "summary": {"warn": 0, "fail": 0},
        "scenarios": scenarios,
        "account_access": {"can_withdraw": False},
        "max_notional_usdt": 1.0,
    }
    result = verify_g5_certificate(g5, expected_commit="abc", expected_scenarios=set(scenarios), now=naive_now)
    assert result.status in {"PASS", "NOT_VERIFIABLE"}

    now = datetime.fromisoformat("2026-08-09")
    g7 = {
        "gate": "G7",
        "status": "PASS",
        "window_id": "g7",
        "duration_days": 30,
        "started_at": "2026-07-01T00:00:00",
        "ended_at": "2026-08-08T00:00:00",
        "commit": "abc",
        "g5_certificate_hash": "g",
        "evidence_hash": "e",
        "mainnet_prohibited": True,
        "is_simulated": False,
        "summary": {
            "total_sli_samples": 1,
            "sli_pass_rate": "100%",
            "total_incidents": 0,
            "p0_incidents": 0,
            "active_incidents": 0,
            "total_recoveries": 0,
            "daily_reports": 1,
            "resets": 0,
        },
        "disclaimer": "real evidence",
    }
    g7_result = verify_g7_certificate(g7, expected_commit="abc", expected_g5_hash="g", now=now)
    assert g7_result.status in {"PASS", "NOT_VERIFIABLE"}
    assert _timestamp(datetime.fromisoformat("2026-01-01")) is not None

    evidence_file = tmp_path / "evidence.txt"
    evidence_file.write_text("data")
    ok, failures = verify_evidence_integrity({"evidence_hashes": {".": "hash"}}, str(tmp_path))
    assert not ok and any(item.startswith("EVIDENCE_READ_ERROR") for item in failures)
    assert not verify_evidence_path_not_sufficient("", False)
    assert not verify_evidence_path_not_sufficient(str(evidence_file), True)
    assert GateVerification("G1", "PASS").to_dict()["passed"] is True


def test_clock_skew_require_ok_failure_path() -> None:
    with pytest.raises(RuntimeError, match="clock failed"):
        skew_require_ok(Result.failure("clock failed"), "server_time")


def test_router_capability_status_and_best_venue_edges() -> None:
    matrix = CapabilityMatrix()
    venue = VenueId("VENUE-A")
    instrument = InstrumentId("BTCUSDT")
    matrix.register_venue(venue, frozenset({Capability.FUTURES_USD_M}), frozenset({instrument}))
    assert matrix.get_venue_capabilities(venue) == frozenset({Capability.FUTURES_USD_M})
    router = ExchangeRouter(matrix)
    assert (
        router.resolve(VenueInstrument(venue_id=VenueId("MISSING"), instrument_id=instrument), Capability.FUTURES_USD_M)
        is ResultStatus.UNKNOWN
    )
    router._adapters[venue] = object()
    assert (
        router.resolve(VenueInstrument(venue_id=venue, instrument_id=instrument), Capability.SPOT)
        is ResultStatus.UNSUPPORTED
    )
    assert router.get_best_venue(instrument, Capability.FUTURES_USD_M) == venue
    assert router.get_best_venue(instrument, Capability.SPOT) is None
    assert router.get_best_venue(InstrumentId("ETHUSDT"), Capability.SPOT) is None


def test_replay_and_paper_window_edge_paths() -> None:
    validator = ReplayValidator()
    from datetime import datetime

    t0 = datetime.fromisoformat("2026-01-01")
    assert validator.check_event_time_inversion([(t0, t0), (t0.replace(day=2), t0), (t0, t0)]) == [2]
    result = ReplayResult(replay_id="r", deterministic=True, output_hash="hash")
    validator.set_baseline("hash")
    assert validator.verify_determinism(result)
    assert not validator.verify_determinism(replace(result, output_hash="other"))
    factor = [1.0] * 501
    closes = [0.0] + [100.0] * 500
    simulated = simulate_paper_window(factor, closes, cost_bps=5.0, min_window_bars=500)
    assert simulated is not None


def test_factor_revalidation_all_blocking_semantics() -> None:
    cases = [
        {"sharpe": float("nan")},
        {"sharpe": 0.0},
        {"sharpe": 1.0, "evidence_dag_hash": ""},
        {"sharpe": 1.0, "evidence_dag_hash": "dag", "sample_count": 99},
        {"sharpe": 1.0, "evidence_dag_hash": "dag", "sample_count": 100, "cost_verified": False},
        {
            "sharpe": 1.0,
            "evidence_dag_hash": "dag",
            "sample_count": 100,
            "cost_verified": True,
            "capacity_verified": False,
        },
        {
            "sharpe": 1.0,
            "evidence_dag_hash": "dag",
            "sample_count": 100,
            "cost_verified": True,
            "capacity_verified": True,
            "statistics_verified": False,
        },
    ]
    for values in cases:
        result = revalidate_factor("factor", **values)
        assert not result.can_promote


def test_adapter_remaining_transport_ack_recovery_and_parser_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.test_full_repository_coverage_adapter_deep import _instrument, _request, _Transport

    monkeypatch.setenv("BEIDOU_TERMINAL_WRITE_HOLD", "unknown-only")
    transport = _Transport({"ok": True})
    adapter = BinanceUsdmAdapter(rest_client=transport)
    adapter.health_monitor.update_venue_health("HEALTHY")
    assert (asyncio.run(adapter.request("POST", "/fapi/v1/order", params={"reduceOnly": True}))).is_success()
    transport.response = Result.failure("network", category=ErrorCategory.NETWORK)
    assert not asyncio.run(adapter.request("GET", "/fapi/v1/time")).is_success()

    transport.response = {
        "orderId": "1",
        "clientOrderId": "cid",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "origQty": "1",
        "status": "NEW",
        "executedQty": "0",
        "reduceOnly": False,
    }
    request = replace(_request(order_type="LIMIT"), price=Price(amount="100"))
    assert asyncio.run(adapter.create_order(request)).status.value in {"NEW", "UNKNOWN"}
    cancel_invalid = {
        "orderId": "1",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "0",
        "executedQty": "0",
        "status": "CANCELED",
    }
    assert (
        BinanceUsdmAdapter._validate_cancel_ack("1", _instrument(), cancel_invalid)[1] == "CANCEL_ACK_QUANTITY_INVALID"
    )
    transport.response = {
        "orderId": "1",
        "symbol": "BTCUSDT",
        "clientOrderId": "cid",
        "origQty": "0",
        "executedQty": "0",
        "status": "NEW",
    }
    recovery = asyncio.run(adapter.query_order_by_client_id("BTCUSDT", "cid"))
    assert not recovery.is_success()
    assert not BinanceUsdmAdapter.parse_user_order_update({}).is_success()
    assert not BinanceUsdmAdapter.parse_user_account_update({}).is_success()


def test_user_stream_projector_storage_observation_and_trade_lite_edges() -> None:
    from tests.unit.test_user_events import _account_update, _authorize, _lite_update, _update

    class _TransientStore:
        def save_user_stream_event(self, *_args, **_kwargs):
            raise OSError("temporary store failure")

    order_projector = UserStreamProjector()
    order_projector._store = _TransientStore()
    blocked = order_projector.ingest(_update(sequence=1, update_id=1, cumulative="0.1"))
    assert blocked.status is UserProjectionStatus.BLOCKED

    account_projector = UserStreamProjector()
    assert _authorize(account_projector).accepted
    account_projector._store = _TransientStore()
    account_blocked = account_projector.ingest_account_update(_account_update(event_time=2_000))
    assert account_blocked.status is UserProjectionStatus.BLOCKED

    gap_projector = UserStreamProjector()
    assert _authorize(gap_projector).accepted
    first = _account_update(event_time=2_000)
    first = replace(first, event=replace(first.event, sequence=1))
    gap = replace(_account_update(event_time=2_001), event=replace(first.event, event_id="gap", sequence=3))
    assert gap_projector.ingest_account_update(first).status is UserProjectionStatus.ACCEPTED
    assert gap_projector.ingest_account_update(gap).status is UserProjectionStatus.BLOCKED

    lite = _lite_update(event_time=3_000, trade_id=3)
    direct = UserStreamProjector()
    direct._apply_order_update(replace(lite, trade_id=""))
    with pytest.raises(ValueError, match="TRADE_LITE"):
        direct._apply_order_update(replace(lite, last_quantity=Quantity(amount="0")))
