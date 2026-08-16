"""V3 fail-closed regression tests.

These tests intentionally exercise the public safety seams instead of private
exchange calls.  They are the first proof slice for BD-V3-01/BD-V3-03.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from beidou_core.engine import AutonomousEngine
from beidou_core.health import HealthServer
from beidou_launcher.g7_tracker import G7LiveTracker
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus
from beidou_launcher.preflight import run_preflight


class _PreflightCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _PreflightConnection:
    def __init__(self, migration_rows: list[tuple[str, str]], missing_table: str | None = None) -> None:
        self._migration_rows = migration_rows
        self._missing_table = missing_table

    def __enter__(self) -> "_PreflightConnection":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> _PreflightCursor:
        if "to_regclass" in sql:
            table = str(params[0]).removeprefix("public.")
            if table == self._missing_table:
                return _PreflightCursor([(None,)])
            return _PreflightCursor([(table,)])
        return _PreflightCursor(self._migration_rows)


def _migration_rows(repo: Path) -> list[tuple[str, str]]:
    from beidou_infra.postgres_store import PostgresPersistentStore

    return [
        (
            version,
            hashlib.sha256((repo / "migrations" / f"{version}.sql").read_bytes()).hexdigest(),
        )
        for version in PostgresPersistentStore._required_migration_versions
    ]


def test_postgres_preflight_requires_real_authority_and_migration_head(monkeypatch) -> None:
    from beidou_launcher.preflight import _postgres_authority_probe

    repo = Path(__file__).parents[2]
    fake_psycopg = SimpleNamespace(
        connect=lambda *_args, **_kwargs: _PreflightConnection(_migration_rows(repo)),
    )
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    ok, message, evidence = _postgres_authority_probe(repo, "postgresql://user:secret@db/beidou")

    assert ok is True
    assert "verified" in message
    assert evidence["missing_tables"] == []
    assert evidence["missing_migrations"] == []
    assert evidence["checksum_mismatch"] == []
    assert "secret" not in message


def test_postgres_preflight_does_not_leak_dsn_on_connection_failure(monkeypatch) -> None:
    from beidou_launcher.preflight import _postgres_authority_probe

    class BrokenPsycopg:
        @staticmethod
        def connect(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("password is required for db secret")

    monkeypatch.setitem(sys.modules, "psycopg", BrokenPsycopg)

    repo = Path(__file__).parents[2]
    ok, message, evidence = _postgres_authority_probe(repo, "postgresql://user:secret@db/beidou")

    assert ok is False
    assert message == "PostgreSQL authority connection failed"
    assert evidence["error_type"] == "RuntimeError"
    assert "secret" not in message


def test_postgres_preflight_rejects_schema_and_checksum_drift(monkeypatch) -> None:
    from beidou_launcher.preflight import _postgres_authority_probe

    repo = Path(__file__).parents[2]
    rows = _migration_rows(repo)
    rows[0] = (rows[0][0], "drifted-checksum")
    fake_psycopg = SimpleNamespace(
        connect=lambda *_args, **_kwargs: _PreflightConnection(rows, missing_table="v3_transactional_outbox"),
    )
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    ok, message, evidence = _postgres_authority_probe(repo, "postgresql://user:secret@db/beidou")

    assert ok is False
    assert message == "PostgreSQL required tables are missing"
    assert evidence["missing_tables"] == ["v3_transactional_outbox"]
    assert evidence["checksum_mismatch"] == ["001_initial_schema.up"]

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda *_args, **_kwargs: _PreflightConnection(rows)),
    )
    ok, message, evidence = _postgres_authority_probe(repo, "postgresql://user:secret@db/beidou")
    assert ok is False
    assert message == "PostgreSQL migration checksum drift detected"
    assert evidence["checksum_mismatch"] == ["001_initial_schema.up"]


def test_testnet_without_signing_key_is_a_p0_startup_blocker(monkeypatch, tmp_path: Path) -> None:
    """A writable environment must never fall back to a built-in signing key."""

    monkeypatch.setattr(
        "beidou_launcher.preflight._postgres_authority_probe",
        lambda *_args: (True, "PostgreSQL authority and migration head verified", {}),
    )
    monkeypatch.delenv("BEIDOU_SIGNING_KEY", raising=False)
    monkeypatch.setenv("BEIDOU_BINANCE_API_KEY", "testnet-api-key-value")
    monkeypatch.setenv("BEIDOU_BINANCE_API_SECRET", "testnet-api-secret-value")

    checks, _settings = run_preflight(tmp_path, "testnet", 19090)

    signing = next(item for item in checks if item.check_id == "preflight.signing_key")
    assert signing.status == CheckStatus.FAIL
    assert signing.severity == CheckSeverity.P0
    assert "mock" not in signing.message.lower()


def test_testnet_without_signed_policy_is_a_p0_startup_blocker(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "beidou_launcher.preflight._postgres_authority_probe",
        lambda *_args: (True, "PostgreSQL authority and migration head verified", {}),
    )
    monkeypatch.setenv("BEIDOU_BINANCE_API_KEY", "testnet-api-key-value")
    monkeypatch.setenv("BEIDOU_BINANCE_API_SECRET", "testnet-api-secret-value")
    monkeypatch.setenv("BEIDOU_SIGNING_KEY", "testnet-signing-key-value")

    checks, _settings = run_preflight(tmp_path, "testnet", 19091)

    policy = next(item for item in checks if item.check_id == "preflight.signed_policy")
    assert policy.status == CheckStatus.FAIL
    assert policy.severity == CheckSeverity.P0


def test_supervisor_write_interlock_requires_scoped_authority_even_when_runtime_ready(tmp_path: Path) -> None:
    """Runtime readiness is not a scoped terminal-write capability."""

    from beidou_launcher.supervisor import BeidouSupervisor

    calls: list[tuple[str, str]] = []

    async def original_async(
        path: str, method: str = "GET", signed: bool = False, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        del signed, params
        calls.append((method, path))
        return {"ok": True}

    def original_sync(
        path: str, method: str = "GET", signed: bool = False, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        del signed, params
        calls.append((method, path))
        return {"ok": True}

    supervisor = BeidouSupervisor(project_root=tmp_path, mode="testnet", symbols=["BTCUSDT"], port=19090)
    control = SimpleNamespace(get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"))
    supervisor.engine = SimpleNamespace(
        _can_write=True,
        _api_async=original_async,
        _api=original_sync,
        _control=control,
    )
    supervisor._install_exchange_write_interlock()

    blocked = asyncio.run(supervisor.engine._api_async("/order", method="POST"))
    assert blocked["error"] == -3
    assert calls == []

    supervisor._resume_authorized = True
    supervisor.report.supervisor_state = "RUNNING"
    supervisor.engine._control = SimpleNamespace(get_status=lambda: SimpleNamespace(value="RESUME"))
    still_blocked = asyncio.run(supervisor.engine._api_async("/order", method="POST"))
    assert still_blocked["error"] == -3
    assert calls == []


def test_typed_adapter_write_path_cannot_bypass_supervisor_interlock(tmp_path: Path) -> None:
    """Typed order/protection helpers must share the REST authority gate."""

    from beidou_launcher.supervisor import BeidouSupervisor

    calls: list[tuple[str, str]] = []

    async def adapter_request(
        method: str,
        path: str,
        signed: bool = False,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del signed, params
        calls.append((method, path))
        return {"ok": True}

    supervisor = BeidouSupervisor(project_root=tmp_path, mode="testnet", symbols=["BTCUSDT"], port=19090)
    control = SimpleNamespace(get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"))
    adapter = SimpleNamespace(request=adapter_request)
    supervisor.engine = SimpleNamespace(
        _can_write=True,
        _api_async=lambda *args, **kwargs: None,
        _api=lambda *args, **kwargs: None,
        _control=control,
        _adapter=adapter,
    )
    supervisor._install_exchange_write_interlock()

    blocked = asyncio.run(adapter.request("POST", "/order", True, {"symbol": "BTCUSDT"}))
    assert blocked.is_success() is False
    assert calls == []

    blocked_false_string = asyncio.run(adapter.request("POST", "/order", True, {"reduceOnly": "false"}))
    assert blocked_false_string.is_success() is False
    assert calls == []

    # HTTP method and reduce-only flags are classification hints, not ownership proof.
    for method, path, params in (
        ("POST", "/order", {"reduceOnly": "true"}),
        ("POST", "/order", {"closePosition": True}),
        ("DELETE", "/order", {"orderId": "17"}),
        ("DELETE", "/algoOrder", {"algoId": "19"}),
    ):
        denied = asyncio.run(adapter.request(method, path, True, params))
        assert denied.is_success() is False
    typed_denied = asyncio.run(
        adapter.request(
            "POST",
            "/order",
            True,
            {"symbol": "BTCUSDT"},
            write_account_id="dedicated-test-account",
        )
    )
    assert typed_denied.is_success() is False
    assert calls == []


def test_supervisor_has_no_transient_authority_bypass() -> None:
    from beidou_launcher.supervisor import BeidouSupervisor

    # BD-FIX（无人值守目标）: 瞬时检查集仅豁免网络类检查的 LOCKED
    # 防抖（各自有恢复机制）；execution/protection 类持久事实失真
    # 仍进入 LOCKED 防抖，fail-closed 不弱化。
    assert (
        frozenset(
            {
                "runtime.safety.position_mode",
                "runtime.safety.user_stream",
                "runtime.safety.reconciliation",
                "runtime.safety.reconciliation_authority",
            }
        )
        == BeidouSupervisor._TRANSIENT_CHECK_IDS
    )
    # 关键持久检查不得进入瞬时豁免集
    for critical in (
        "runtime.safety.protection_coverage",
        "runtime.safety.account",
        "runtime.health.lifecycle",
        "runtime.safety.write_interlock",
    ):
        assert critical not in BeidouSupervisor._TRANSIENT_CHECK_IDS


def test_monitor_loop_health_uses_monotonic_clock() -> None:
    from beidou_observability.monitoring.checks.monitor_self import check_monitor_loop_health
    from beidou_observability.monitoring.contracts import CheckStatus

    now = time.monotonic()
    assert check_monitor_loop_health(now - 1.0, max_stall=30.0).status is CheckStatus.PASS
    assert check_monitor_loop_health(now - 31.0, max_stall=30.0).status is CheckStatus.FAIL


def test_startup_report_does_not_claim_pass_before_live_readiness() -> None:
    from beidou_launcher.models import StartupReport

    report = StartupReport("testnet", ["BTCUSDT"], 19090, "commit")
    assert report.passed is False
    report.trading_ready = True
    report.supervisor_state = "RUNNING"
    assert report.passed is True


def test_readiness_requires_running_supervisor_state() -> None:
    from beidou_launcher.supervisor import BeidouSupervisor

    supervisor = BeidouSupervisor(project_root=Path("."), mode="testnet", symbols=["BTCUSDT"], port=19090)
    supervisor._resume_authorized = True
    supervisor.engine = SimpleNamespace(_control=SimpleNamespace(get_status=lambda: SimpleNamespace(value="RESUME")))
    supervisor.report.supervisor_state = "STARTING"
    assert supervisor._is_trading_ready() is False
    supervisor.report.supervisor_state = "RUNNING"
    assert supervisor._is_trading_ready() is True


def test_configured_trading_pool_entries_are_not_active_without_evidence() -> None:
    import inspect

    from beidou_data.trading_pool_lifecycle import PoolStatus, TradingPool

    pool = TradingPool()
    entry = pool.add("BTCUSDT")
    assert entry.status is PoolStatus.OBSERVING
    assert pool.active_count() == 0
    assert pool.is_tradable("BTCUSDT") is False
    # Guard the startup seam as well: Testnet must not reintroduce the old
    # direct ACTIVE/bootstrap assignment after the lifecycle unit test passes.
    source = inspect.getsource(AutonomousEngine.__init__)
    assert "entry.status = PoolStatus.ACTIVE" not in source
    assert "Bootstrap:" not in source


def test_testnet_reconciliation_failure_cannot_enter_active() -> None:
    import inspect

    source = inspect.getsource(AutonomousEngine.run)
    assert "testnet: reconciliation deferred" not in source
    assert 'self._env_mode.value != "testnet"' not in source
    assert "if not recon_ok:" in source


def test_health_server_defaults_to_loopback() -> None:
    assert HealthServer()._bind_host == "127.0.0.1"


def test_g7_is_not_eligible_without_real_elapsed_window_or_samples() -> None:
    tracker = G7LiveTracker()
    summary = tracker.summary()
    assert summary["g7_eligible"] is False
    assert summary["all_slis_passing"] is False
    assert summary["durable_window_running"] is False


def test_g7_diagnostic_eligibility_requires_durable_window_binding() -> None:
    tracker = G7LiveTracker(minimum_elapsed_seconds=0.0, minimum_cycles=1)
    tracker.set_durable_window_state(running=False, evidence_state_complete=True)
    assert tracker.summary()["g7_eligible"] is False
    tracker.set_durable_window_state(running=True, evidence_state_complete=True)
    # Other SLI samples are intentionally absent, so binding alone never
    # certifies the window.
    assert tracker.summary()["g7_eligible"] is False


def test_g7_p0_invalidates_the_window() -> None:
    tracker = G7LiveTracker()
    from beidou_launcher.models import CheckResult

    tracker.feed(
        [
            CheckResult(
                "runtime.health.realtime_heartbeat",
                "heartbeat",
                CheckStatus.FAIL,
                CheckSeverity.P0,
                "stale",
            )
        ]
    )
    summary = tracker.summary()
    assert summary["g7_eligible"] is False
    assert summary["invalidated"] is True


def test_g7_recovery_sli_requires_explicit_supervisor_context() -> None:
    tracker = G7LiveTracker()
    assert tracker._slis["recovery_bounded"].window_pass_rate == 0.0
    tracker.feed_recovery_context(recovery_count=0, max_restarts=3)
    assert tracker._slis["recovery_bounded"].window_pass_rate == 1.0


def test_g7_cost_pnl_sli_does_not_alias_runtime_error_rate() -> None:
    tracker = G7LiveTracker()
    from beidou_launcher.models import CheckResult

    tracker.feed(
        [
            CheckResult(
                "runtime.health.errors",
                "errors",
                CheckStatus.PASS,
                CheckSeverity.P2,
                "no errors",
            )
        ]
    )
    summary = tracker.summary()
    assert "cost_and_pnl_reporting" in summary["slis"]
    assert "error_rate" not in summary["slis"]
    assert summary["slis"]["cost_and_pnl_reporting"]["latest_value"] == 0.0


def test_g7_elapsed_window_uses_monotonic_clock(monkeypatch) -> None:
    import beidou_launcher.g7_tracker as g7_tracker

    mono = [100.0]
    wall = [1_000.0]
    monkeypatch.setattr(g7_tracker.time, "monotonic", lambda: mono[0])
    monkeypatch.setattr(g7_tracker.time, "time", lambda: wall[0])
    tracker = g7_tracker.G7LiveTracker(minimum_elapsed_seconds=10.0, minimum_cycles=0)

    wall[0] = 10_000_000.0
    mono[0] = 110.0
    assert tracker.summary()["uptime_seconds"] == 10.0


def test_config_safe_defaults_do_not_offer_network_write() -> None:
    from beidou_shared.config import ConfigProvider, Environment

    settings = ConfigProvider().load(environment="unknown-v3-environment")
    assert settings.environment is Environment.SAFETY_ONLY
    assert settings.can_write_trades is False
    assert settings.infrastructure.health_host == "127.0.0.1"
    assert settings.exchange.rest_base_url == ""
    assert settings.exchange.ws_base_url == ""
    assert Environment.SHADOW.can_write_trades is False


def test_unsupported_state_backend_cannot_report_trading_ready() -> None:
    engine = object.__new__(AutonomousEngine)
    engine._state_backend_supported = False
    assert engine._check_ready() is False
    assert engine._check_trading_ready() == (False, "STATE_BACKEND_UNSUPPORTED")


def test_unsupported_state_backend_rejects_live_risk_increase_at_executor() -> None:
    rejected: list[tuple[str, str, str]] = []
    engine = object.__new__(AutonomousEngine)
    engine._can_write = True
    engine._state_backend_supported = False
    engine._outbox = SimpleNamespace(
        reject=lambda intent_id, reason, idempotency_key="": rejected.append((intent_id, reason, idempotency_key))
    )
    intent = SimpleNamespace(intent_id="intent-backend-block", idempotency_key="idem-backend-block")

    asyncio.run(engine._place_order(intent))

    assert rejected == [("intent-backend-block", "STATE_BACKEND_UNSUPPORTED", "idem-backend-block")]


def test_durable_protection_coverage_is_per_position_side_quantity_and_generation() -> None:
    engine = object.__new__(AutonomousEngine)
    engine._protection_owner_id = "owner-1"
    engine._position_generation = {"BTCUSDT": 7}
    engine._position_projection = {"BTCUSDT": {"position_generation": 7}}
    positions = [{"symbol": "BTCUSDT", "positionAmt": "1.0"}]
    protections = [
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "quantity": "1.0",
            "order_type": "STOP_MARKET",
            "stop_type": "FIXED_PERCENT",
            "position_generation": 7,
            "exchange_order_id": "algo-sl-7",
            "owner_id": "owner-1",
            "status": "ACTIVE",
        },
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "quantity": "0.5",
            "order_type": "TAKE_PROFIT_MARKET",
            "take_profit_type": "FIXED_RR",
            "position_generation": 7,
            "exchange_order_id": "algo-tp-7",
            "owner_id": "owner-1",
            "status": "ACTIVE",
        },
    ]

    covered, evidence = engine._assess_protection_coverage(positions, protections)
    assert covered is True
    assert evidence["unprotected_symbols"] == []

    # A stale generation and a wrong-side order must not satisfy coverage.
    stale_or_wrong_side = [
        {**protections[0], "side": "BUY"},
        {**protections[1], "position_generation": 6},
    ]
    covered, evidence = engine._assess_protection_coverage(positions, stale_or_wrong_side)
    assert covered is False
    assert evidence["unprotected_symbols"][0]["reason"] == "STOP_LOSS_QUANTITY_UNCOVERED"


def test_durable_protection_coverage_rejects_partial_stop_even_when_total_quantity_matches() -> None:
    engine = object.__new__(AutonomousEngine)
    engine._protection_owner_id = "owner-1"
    engine._position_generation = {"ETHUSDT": 2}
    engine._position_projection = {"ETHUSDT": {"position_generation": 2}}
    positions = [{"symbol": "ETHUSDT", "positionAmt": "2"}]
    protections = [
        {
            "symbol": "ETHUSDT",
            "side": "SELL",
            "quantity": "1",
            "order_type": "STOP_MARKET",
            "stop_type": "FIXED_PERCENT",
            "position_generation": 2,
            "exchange_order_id": "algo-sl-2",
            "owner_id": "owner-1",
            "status": "ACTIVE",
        },
        {
            "symbol": "ETHUSDT",
            "side": "SELL",
            "quantity": "1",
            "order_type": "TAKE_PROFIT_MARKET",
            "take_profit_type": "FIXED_RR",
            "position_generation": 2,
            "exchange_order_id": "algo-tp-2",
            "owner_id": "owner-1",
            "status": "ACTIVE",
        },
    ]

    covered, evidence = engine._assess_protection_coverage(positions, protections)
    assert covered is False
    assert evidence["unprotected_symbols"][0]["stop_quantity"] == "1"


def test_durable_protection_coverage_rejects_orphan_exchange_ack() -> None:
    engine = object.__new__(AutonomousEngine)
    engine._position_generation = {}
    engine._position_projection = {}
    orphan = {
        "symbol": "SOLUSDT",
        "side": "SELL",
        "quantity": "3",
        "order_type": "STOP_MARKET",
        "stop_type": "FIXED_PERCENT",
        "position_generation": 1,
        "exchange_order_id": "algo-orphan",
    }

    covered, evidence = engine._assess_protection_coverage([], [orphan])
    assert covered is False
    assert evidence["unprotected_symbols"] == [
        {"symbol": "SOLUSDT", "reason": "ORPHAN_PROTECTION_WITHOUT_VENUE_POSITION"}
    ]


def test_unknown_protection_config_freezes_new_risk_without_synthetic_defaults() -> None:
    from beidou_control.plane import ControlAction

    actions: list[ControlAction] = []
    incidents: list[tuple[object, ...]] = []
    engine = object.__new__(AutonomousEngine)
    engine._control = SimpleNamespace(
        get_status=lambda: ControlAction.RESUME,
        execute_action=lambda action: actions.append(action),
    )
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: incidents.append(args))
    config = SimpleNamespace(metadata={"blocked": True, "reason": "KLINE_UNKNOWN"}, stop_pct=0.0)

    with pytest.raises(RuntimeError, match="PROTECTION_CONFIG_UNKNOWN:BTCUSDT"):
        engine._require_protection_config("BTCUSDT", config)

    assert actions == [ControlAction.NO_NEW_RISK]
    assert engine._protection_config_unknown is True
    assert config.stop_pct == 0.0
    assert incidents


def test_protection_retry_cannot_widen_approved_trigger_at_runtime() -> None:
    """A rejected venue protection must stay UNKNOWN, not become a new policy."""

    source = (Path(__file__).parents[2] / "beidou_core/engine.py").read_text(encoding="utf-8")
    retry_section = source[
        source.index("async def _retry_missing_protections") : source.index("async def _nearline_tick")
    ]
    assert "widen_factor" not in retry_section
    assert "base_pct" not in retry_section
    assert "widened_sl" not in retry_section
    assert "widened_tp" not in retry_section
    assert "approved trigger" in retry_section


def test_factor_promotion_gate_never_allows_unverified_active() -> None:
    from beidou_research.factors.factor import FactorLifecycle, FactorPromotionGate

    gate = FactorPromotionGate(strict=False)
    decision = gate.validate_evidence(
        "factor-1",
        FactorLifecycle.CHALLENGER,
        FactorLifecycle.ACTIVE,
        falsifier="test",
    )
    assert decision.approved is False
    assert "Missing required evidence" in decision.reason


def test_factor_promotion_gate_binds_active_to_sealed_bundle() -> None:
    from beidou_research.factors.factor import FactorLifecycle, FactorPromotionGate
    from beidou_research.mining.evidence import EvidenceBundle

    gate = FactorPromotionGate()
    bundle = EvidenceBundle(
        bundle_id="bundle-1",
        candidate_id="candidate-1",
        factor_id="factor-1",
        factor_version="1.0.0",
        candidate_hash="c" * 64,
        factor_code_hash="f" * 64,
        dataset_manifest_hash="d" * 64,
        feature_manifest_hash="e" * 64,
        label_spec_hash="label-1",
        cost_model_version="cost-1",
        policy_version="policy-1",
        gate_decision="PASS",
    )
    bundle.seal()
    performance = SimpleNamespace(icir=0.2, sample_count=500, ic_mean=0.1)
    decision = gate.validate_evidence(
        "factor-1",
        FactorLifecycle.CHALLENGER,
        FactorLifecycle.ACTIVE,
        performance=performance,
        evidence_ids=[
            "sealed_oos_verified",
            "cost_capacity_verified",
            "paper_shadow_verified",
            "active_approval",
        ],
        factor_version="1.0.0",
        commit="a" * 40,
        dataset_hash="d" * 64,
        policy_version="policy-1",
        falsifier="operator@example.invalid",
        evidence_bundle=bundle,
    )

    assert decision.approved is True

    mismatch = gate.validate_evidence(
        "factor-1",
        FactorLifecycle.CHALLENGER,
        FactorLifecycle.ACTIVE,
        performance=performance,
        evidence_ids=[
            "sealed_oos_verified",
            "cost_capacity_verified",
            "paper_shadow_verified",
            "active_approval",
        ],
        factor_version="1.0.0",
        commit="a" * 40,
        dataset_hash="x" * 64,
        policy_version="policy-1",
        falsifier="operator@example.invalid",
        evidence_bundle=bundle,
    )
    assert mismatch.approved is False
    assert "dataset_manifest_hash mismatch" in mismatch.reason


def test_startup_wait_does_not_authorize_resume_with_runtime_blocker(tmp_path: Path) -> None:
    from beidou_launcher.supervisor import BeidouSupervisor

    def _make_supervisor() -> BeidouSupervisor:
        supervisor = BeidouSupervisor(
            project_root=tmp_path,
            mode="testnet",
            symbols=["BTCUSDT"],
            port=19090,
            startup_timeout=0.02,
        )

        class _Thread:
            @staticmethod
            def is_alive() -> bool:
                return True

        supervisor.engine = SimpleNamespace(
            _lifecycle=SimpleNamespace(state=SimpleNamespace(value="ACTIVE")),
            _health=SimpleNamespace(_thread=_Thread()),
        )
        return supervisor

    async def _run(supervisor: BeidouSupervisor) -> bool:
        async def _running_engine() -> None:
            await asyncio.sleep(1)

        supervisor._engine_task = asyncio.create_task(_running_engine())
        try:
            return await supervisor._wait_for_startup()
        finally:
            supervisor._engine_task.cancel()
            with suppress(asyncio.CancelledError):
                await supervisor._engine_task

    # BD-FIX（C5 审查）: 运行时检查（心跳/对账等）不再阻断启动 ——
    # 瞬时 P0/P1 在 demo 抖动下必然出现，旧语义导致启动超时循环。
    # 启动门禁只按关键启动检查集判定。
    supervisor = _make_supervisor()
    runtime_blocker = CheckResult(
        "runtime.health.realtime_heartbeat",
        "实时循环心跳",
        CheckStatus.FAIL,
        CheckSeverity.P0,
        "stale",
    )
    supervisor._runtime_checks = lambda: [runtime_blocker]  # type: ignore[method-assign]
    supervisor._merge_monitoring_checks = lambda checks: checks  # type: ignore[method-assign]

    async def _noop() -> None:
        return None

    supervisor._refresh_exchange_account_snapshot = _noop  # type: ignore[method-assign]
    supervisor._refresh_position_mode = _noop  # type: ignore[method-assign]
    supervisor._refresh_exchange_algo_snapshot = _noop  # type: ignore[method-assign]

    assert asyncio.run(_run(supervisor)) is True  # runtime blocker 不阻断启动

    # 关键集 blocker（如账户安全）仍阻断启动 —— fail-closed 语义保留
    supervisor2 = _make_supervisor()
    critical_blocker = CheckResult(
        "runtime.safety.account",
        "账户事实",
        CheckStatus.FAIL,
        CheckSeverity.P0,
        "missing",
    )
    supervisor2._runtime_checks = lambda: [critical_blocker]  # type: ignore[method-assign]
    supervisor2._merge_monitoring_checks = lambda checks: checks  # type: ignore[method-assign]
    supervisor2._refresh_exchange_account_snapshot = _noop  # type: ignore[method-assign]
    supervisor2._refresh_position_mode = _noop  # type: ignore[method-assign]
    supervisor2._refresh_exchange_algo_snapshot = _noop  # type: ignore[method-assign]

    if "runtime.safety.account" in supervisor2._STARTUP_CRITICAL_CHECKS:
        assert asyncio.run(_run(supervisor2)) is False
    else:
        assert asyncio.run(_run(supervisor2)) is True  # 该检查不在关键集


def test_persistent_blocker_cannot_leave_supervisor_running() -> None:
    from beidou_launcher.supervisor import _state_after_persistent_block

    assert _state_after_persistent_block("RUNNING", True) == "DEGRADED"
    assert _state_after_persistent_block("STARTING", True) == "DEGRADED"
    assert _state_after_persistent_block("DEGRADED", True) == "DEGRADED"
    assert _state_after_persistent_block("LOCKED", True) == "LOCKED"
    assert _state_after_persistent_block("RUNNING", False) == "RUNNING"
