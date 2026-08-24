"""Behavior-backed coverage for certification, telemetry, and protection edges."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_certification.g5_scenarios.base import NotionalExceededError, NotionalLedger, ScenarioContext
from beidou_certification.g5_scenarios.engine import cancel_fill_race as race_module
from beidou_certification.g5_scenarios.protocol import create_query_cancel as cqc_module
from beidou_certification.g5_scenarios.restart import database_restart as db_restart_module
from beidou_certification.g5_scenarios.restart.database_restart import DatabaseRestartScenario
from beidou_certification.g5_scenarios.restart.process_restart import (
    EnginePidAmbiguousError,
    StatusUnreachableError,
)
from beidou_observability.telemetry import AlertSeverity, AlertSuppressor
from beidou_strategy.protection.adaptive import AdaptiveProtectionCalculator


def test_cancel_race_helpers_and_cleanup_failure_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        race_module._read_order_state_store(
            SimpleNamespace(restore_order_states=lambda: [{"record_id": "other"}]), "wanted"
        )
        is None
    )
    matching = race_module._read_order_state_store(
        SimpleNamespace(restore_order_states=lambda: [{"record_id": "wanted", "status": "NEW"}]), "wanted"
    )
    assert matching == {"record_id": "wanted", "status": "NEW"}

    import beidou_core.store as sqlite_store
    import beidou_infra.postgres_store as postgres_store

    with monkeypatch.context() as patch:
        patch.setattr(sqlite_store, "__file__", str(tmp_path / "missing-store.py"))
        with pytest.raises(RuntimeError, match="source missing"):
            race_module.monotonic_guard_source()
    markerless = tmp_path / "markerless.py"
    markerless.write_text("def no_guard():\n    return None\n")
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_store, "__file__", str(markerless))
        patch.setattr(postgres_store, "__file__", str(markerless))
        with pytest.raises(RuntimeError, match="marker missing"):
            race_module.monotonic_guard_source()

    class _Connection:
        def close(self) -> None:
            pass

    monkeypatch.setattr(race_module.psycopg, "connect", lambda *_args, **_kwargs: _Connection())
    monkeypatch.setattr(race_module, "_make_store", lambda _dsn: object())
    monkeypatch.setattr(race_module, "monotonic_guard_source", lambda: {"semantics_consistent": True})
    monkeypatch.setattr(race_module, "_run_sequence", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(race_module, "_cleanup_race_rows", lambda *_args, **_kwargs: {"action": "cleanup_delete"})
    monkeypatch.setattr(race_module, "_read_order_state_raw", lambda *_args, **_kwargs: {"status": "left"})
    with pytest.raises(RuntimeError, match="race rows left behind"):
        race_module._probe_guard_semantics("dsn", "BTCUSDT")

    scenario = race_module.CancelFillRaceScenario()
    monkeypatch.setattr(
        race_module,
        "_probe_guard_semantics",
        lambda *_args: (_ for _ in ()).throw(NotionalExceededError("race", 51.0, 50.0)),
    )
    ctx = SimpleNamespace(ledger=NotionalLedger(1000), dry_run=False, symbol="BTCUSDT")
    with pytest.raises(NotionalExceededError):
        asyncio.run(scenario.run(ctx))


def test_create_query_cancel_cleanup_notional_and_tick_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_g5_scenarios.test_protocol_basic import FakeClient, _ctx

    with pytest.raises(ValueError, match="not in exchange"):
        cqc_module._tick_size("MISSING", {"symbols": []})

    class _QueryFailureClient(FakeClient):
        async def get_order(self, symbol: str, order_id: int):
            raise RuntimeError("query failure")

    failed_query = asyncio.run(cqc_module.CreateQueryCancelScenario().run(_ctx(_QueryFailureClient(), tmp_path)))
    assert failed_query.error_type == "RuntimeError"
    assert any(step.get("action") == "cleanup_cancel" and step.get("ok") for step in failed_query.evidence["steps"])

    class _CleanupFailureClient(_QueryFailureClient):
        async def cancel_order(self, symbol: str, order_id: int):
            raise RuntimeError("cleanup failure")

    failed_cleanup = asyncio.run(cqc_module.CreateQueryCancelScenario().run(_ctx(_CleanupFailureClient(), tmp_path)))
    cleanup_step = next(step for step in failed_cleanup.evidence["steps"] if step.get("action") == "cleanup_cancel")
    assert cleanup_step["ok"] is False

    monkeypatch.setattr(
        cqc_module,
        "min_gate_quantity",
        lambda *_args: (_ for _ in ()).throw(NotionalExceededError("create_query_cancel", 51.0, 50.0)),
    )
    with pytest.raises(NotionalExceededError):
        asyncio.run(cqc_module.CreateQueryCancelScenario().run(_ctx(FakeClient(), tmp_path)))


def test_database_restart_real_wrappers_wait_error_paths_and_special_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        db_restart_module.psycopg,
        "connect",
        lambda dsn, connect_timeout: calls.append((dsn, connect_timeout)) or "connection",
    )
    assert DatabaseRestartScenario._pg_connect_real("dsn") == "connection"
    assert calls == [("dsn", db_restart_module._PG_CONNECT_TIMEOUT_SECONDS)]
    monkeypatch.setattr(db_restart_module.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    DatabaseRestartScenario._brew_restart_real()
    assert calls[-1][0][0] == ["/opt/homebrew/bin/brew", "services", "restart", "postgresql@16"]

    def unreachable() -> dict[str, object]:
        raise StatusUnreachableError("status down")

    scenario = DatabaseRestartScenario(
        now=lambda: 0.0,
        sleep=lambda _seconds: asyncio.sleep(0),
        fetch_status=unreachable,
        pg_connect=lambda _dsn: SimpleNamespace(close=lambda: None),
        engine_pid=lambda: 7,
        poll_interval=0,
        pg_restore_deadline=0,
        ready_deadline=0,
    )
    with pytest.raises(db_restart_module.PgRestartTimeoutError):
        asyncio.run(scenario._wait_for_pg_restored(since=0.0, steps=[]))
    with pytest.raises(db_restart_module.EngineRecoveryTimeoutError):
        asyncio.run(scenario._wait_for_ready(since=0.0, steps=[]))

    def notional_pid() -> int:
        raise NotionalExceededError("database_restart", 51.0, 50.0)

    with pytest.raises(NotionalExceededError):
        asyncio.run(
            DatabaseRestartScenario(
                engine_pid=notional_pid,
                fetch_status=lambda: {"trading_ready": True, "last_reconciliation": {"status": "MATCHED"}},
                pg_connect=lambda _dsn: object(),
                brew_restart=lambda: None,
            ).run(
                ScenarioContext(
                    client=None,
                    ledger=NotionalLedger(1000),
                    evidence_dir=tmp_path,
                    symbol="BTCUSDT",
                    dry_run=False,
                )
            )
        )

    ambiguous = EnginePidAmbiguousError([1, 2], [1, 2])
    result = asyncio.run(
        DatabaseRestartScenario(
            engine_pid=lambda: (_ for _ in ()).throw(ambiguous),
            fetch_status=lambda: {},
            pg_connect=lambda _dsn: object(),
            brew_restart=lambda: None,
        ).run(
            ScenarioContext(
                client=None,
                ledger=NotionalLedger(1000),
                evidence_dir=tmp_path,
                symbol="BTCUSDT",
                dry_run=False,
            )
        )
    )
    assert result.error_type == "engine_pid_ambiguous"


def test_telemetry_suppressor_fingerprint_count_and_action_edges() -> None:
    suppressor = AlertSuppressor(window_seconds=60)
    assert suppressor.get_fingerprint_count("unknown", "missing") == 0
    for index in range(3):
        assert not suppressor.should_suppress(AlertSeverity.WARNING, f"key-{index}", "db", "failure 123")
    assert suppressor.should_suppress(AlertSeverity.WARNING, "key-3", "db", "failure 456")
    assert suppressor.get_fingerprint_count("db", "failure 789") == 4
    assert not hasattr(suppressor, "get_auto_action")


def test_adaptive_protection_blocked_feature_and_rr_boundaries() -> None:
    invalid_price = AdaptiveProtectionCalculator._blocked_config("not-a-price", "invalid")
    assert invalid_price.price_tier.value == "MICRO"
    negative_price = AdaptiveProtectionCalculator._blocked_config(-1, "negative")
    assert negative_price.price_tier.value == "MICRO"
    rr_high_rsi, _ = AdaptiveProtectionCalculator.compute_rr_ratio(1.0, 5.0, 70.0, 0.30)
    rr_low_rsi, _ = AdaptiveProtectionCalculator.compute_rr_ratio(1.0, 5.0, 30.0, 0.30)
    assert rr_high_rsi != rr_low_rsi
    features = {
        "close": 100.0,
        "atr_pct": 1.0,
        "ann_volatility": 0.30,
        "rsi_14": 50.0,
        "trend_20_pct": 1.0,
        "spread_bps": 1.0,
    }
    bad_type = AdaptiveProtectionCalculator.calculate("BTCUSDT", 100.0, {**features, "close": "bad"})
    assert bad_type.metadata["reason"] == "INVALID_MARKET_FEATURES"
    nonfinite = AdaptiveProtectionCalculator.calculate("BTCUSDT", 100.0, {**features, "close": float("nan")})
    assert nonfinite.metadata["reason"] == "NONFINITE_MARKET_FEATURES"
