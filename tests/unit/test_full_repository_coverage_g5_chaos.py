"""Behavioral coverage for certification scenarios and the chaos injector.

The database-backed probes are kept out of the unit suite, but the production
scenario state machines are exercised through their public ``run`` methods.
The probe seams are replaced with deterministic evidence-producing doubles so
the tests still assert the PASS/FAIL/NOT_VERIFIABLE contracts rather than merely
importing the modules.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    NotionalLedger,
    ScenarioContext,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.engine import ack_loss, timeout_unknown_recovery
from beidou_certification.g5_scenarios.protocol import duplicate_request
from beidou_chaos.engine import ChaosEngine, KillScenario
from beidou_chaos.fault_injection import FaultScenario, run_fault_scenario


def _context(tmp_path: Path, *, client: object | None = object(), dry_run: bool = False) -> ScenarioContext:
    return ScenarioContext(
        client=client,
        ledger=NotionalLedger(limit_usdt=50.0),
        evidence_dir=tmp_path,
        symbol="BTCUSDT",
        dry_run=dry_run,
    )


def test_unknown_and_dedupe_verdicts_are_explicit() -> None:
    assert ack_loss.unknown_state_verdict("UNKNOWN", "GONE") == (True, "recoverable_no_duplicate")
    assert ack_loss.unknown_state_verdict("UNKNOWN", "ACTIVE") == (False, "anchor_must_hold")
    assert ack_loss.unknown_state_verdict("FILLED", "GONE") == (True, "terminal_consistent")
    assert ack_loss.unknown_state_verdict("FILLED", "ACTIVE") == (False, "unresolved")

    assert duplicate_request.dedupe_verdict({"code": -4015}, []) == (True, "exchange_rejected")
    assert duplicate_request.dedupe_verdict({"orderId": 7}, [7]) == (True, "same_order_returned")
    assert duplicate_request.dedupe_verdict({"orderId": 8}, [7]) == (False, "new_order_created")
    assert duplicate_request.dedupe_verdict({"unexpected": True}, []) == (False, "unrecognized_response")


class _Cursor:
    def __init__(self, *, rows=None, rowcount=1, fail_on_execute=False) -> None:
        self.rows = rows
        self.rowcount = rowcount
        self.fail_on_execute = fail_on_execute
        self.executed: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()) -> None:
        self.executed.append((sql, params))
        if self.fail_on_execute:
            raise RuntimeError("cursor failure")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows or [])


class _Connection:
    def __init__(self, cursors: list[_Cursor]) -> None:
        self._cursors = iter(cursors)
        self.closed = False
        self.autocommit = False

    def transaction(self):
        return self

    def cursor(self):
        return next(self._cursors)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def close(self) -> None:
        self.closed = True


def test_ack_loss_database_helpers_and_probe_are_cleanup_safe(monkeypatch) -> None:
    insert_cursor = _Cursor()
    conn = _Connection([insert_cursor])
    inserted = ack_loss._insert_test_outbox_row(conn, key="k", intent_id="i", message_id="m", client_order_id="c")
    assert inserted["status"] == "SENDING"
    assert len(insert_cursor.executed) == 2

    status_cursor = _Cursor(rows=[("UNKNOWN",)])
    assert ack_loss._read_outbox_status(_Connection([status_cursor]), "i") == "UNKNOWN"
    with pytest.raises(RuntimeError, match="outbox row missing"):
        ack_loss._read_outbox_status(_Connection([_Cursor(rows=[])]), "i")

    counts_cursor = _Cursor(rows=[("UNKNOWN", 2), ("FAILED", 3)])
    assert ack_loss._outbox_state_counts(_Connection([counts_cursor])) == {"UNKNOWN": 2, "FAILED": 3}
    assert ack_loss._outbox_state_counts(_Connection([_Cursor(rows=[])])) == {}

    recovery_cursor = _Cursor(rowcount=None)
    assert ack_loss._recover_to_failed(_Connection([recovery_cursor]), intent_id="i")["updated"] == 0
    cleanup_cursors = [_Cursor(rowcount=2)]
    assert ack_loss._cleanup_test_rows(_Connection(cleanup_cursors), intent_id="i", message_id="m") == {
        "action": "cleanup_delete",
        "events_deleted": 2,
        "outbox_deleted": 2,
        "intents_deleted": 2,
    }

    class Client:
        async def get_open_orders(self, _symbol):
            from beidou_exchange.core.error_taxonomy import Result

            return Result.success([{"clientOrderId": "c"}])

    venue_status, _venue_evidence = asyncio.run(ack_loss._venue_status(Client(), "BTCUSDT", "c"))
    assert venue_status == "ACTIVE"

    class ProbeOutbox:
        def __init__(self, **_kwargs) -> None:
            pass

        def mark_unknown(self, _intent_id, _reason) -> None:
            return None

    probe_cursor_sequence = [
        _Cursor(),
        _Cursor(rows=[("UNKNOWN",)]),
        _Cursor(rowcount=1),
        _Cursor(rowcount=1),
        _Cursor(rowcount=1),
    ]
    probe_conn = _Connection(probe_cursor_sequence)
    monkeypatch.setattr(ack_loss, "PostgresIntentOutbox", ProbeOutbox)
    monkeypatch.setattr(ack_loss.psycopg, "connect", lambda _dsn: probe_conn)
    monkeypatch.setattr(
        ack_loss, "_venue_status", lambda *_args: asyncio.sleep(0, result=("GONE", {"action": "venue"}))
    )
    steps, ok, reason, venue = asyncio.run(ack_loss._probe_ack_loss("dsn", "BTCUSDT", Client()))
    assert ok is True and reason == "recoverable_no_duplicate" and venue == "GONE"
    assert any(step.get("action") == "mark_unknown" for step in steps)
    assert probe_conn.closed is True


def test_ack_loss_venue_failure_and_cleanup_failure_are_explicit(monkeypatch) -> None:
    from beidou_exchange.core.error_taxonomy import Result

    class Client:
        async def get_open_orders(self, _symbol):
            return Result.failure("offline")

    with pytest.raises(RuntimeError, match="get_open_orders failed"):
        asyncio.run(ack_loss._venue_status(Client(), "BTCUSDT", "c"))

    class FailingCursor(_Cursor):
        def execute(self, sql, params=()):
            super().execute(sql, params)
            raise RuntimeError("cleanup failure")

    class ProbeOutbox:
        def __init__(self, **_kwargs) -> None:
            pass

        def mark_unknown(self, _intent_id, _reason) -> None:
            return None

    conn = _Connection([_Cursor(), _Cursor(rows=[("UNKNOWN",)]), FailingCursor()])
    monkeypatch.setattr(ack_loss, "PostgresIntentOutbox", ProbeOutbox)
    monkeypatch.setattr(ack_loss.psycopg, "connect", lambda _dsn: conn)
    monkeypatch.setattr(
        ack_loss, "_venue_status", lambda *_args: asyncio.sleep(0, result=("GONE", {"action": "venue"}))
    )
    steps, ok, _reason, _venue = asyncio.run(ack_loss._probe_ack_loss("dsn", "BTCUSDT", Client()))
    assert ok is True
    assert any(step.get("action") == "cleanup" and step.get("ok") is False for step in steps)


def test_ack_loss_probe_and_runner_preserve_fail_closed_exception_contract(monkeypatch) -> None:
    class ProbeOutbox:
        def __init__(self, **_kwargs) -> None:
            pass

        def mark_unknown(self, _intent_id, _reason) -> None:
            return None

    conn = _Connection([_Cursor(), _Cursor(rowcount=1), _Cursor(rowcount=1)])
    monkeypatch.setattr(ack_loss, "PostgresIntentOutbox", ProbeOutbox)
    monkeypatch.setattr(ack_loss.psycopg, "connect", lambda _dsn: conn)
    monkeypatch.setattr(ack_loss, "_read_outbox_status", lambda *_args: "SENDING")
    with pytest.raises(RuntimeError, match="did not reach UNKNOWN"):
        asyncio.run(ack_loss._probe_ack_loss("dsn", "BTCUSDT", object()))
    assert conn.closed is True

    class OverLimitLedger:
        def record(self, *_args) -> None:
            raise NotionalExceededError("ack_loss", 51.0, 50.0)

    ctx = ScenarioContext(
        client=object(), ledger=OverLimitLedger(), evidence_dir=Path("."), symbol="BTCUSDT", dry_run=False
    )
    with pytest.raises(NotionalExceededError):
        asyncio.run(ack_loss.AckLossScenario().run(ctx))


def test_timeout_probe_covers_status_count_recovery_and_cleanup_guards(monkeypatch, tmp_path) -> None:
    class ProbeOutbox:
        def __init__(self, **_kwargs) -> None:
            pass

        def mark_unknown(self, _intent_id, _reason) -> None:
            return None

    monkeypatch.setattr(timeout_unknown_recovery, "PostgresIntentOutbox", ProbeOutbox)

    # The persisted state must be UNKNOWN before the durable gate is evaluated.
    status_conn = _Connection([_Cursor(), _Cursor(), _Cursor(rowcount=1)])
    monkeypatch.setattr(timeout_unknown_recovery.psycopg, "connect", lambda _dsn: status_conn)
    monkeypatch.setattr(timeout_unknown_recovery, "_read_outbox_status", lambda *_args: "SENDING")
    with pytest.raises(RuntimeError, match="did not reach UNKNOWN"):
        asyncio.run(timeout_unknown_recovery._probe_timeout_unknown_recovery("dsn", "BTCUSDT", object()))

    # A missing increment blocks recovery, but cleanup still runs.
    count_conn = _Connection([_Cursor(rows=[]), _Cursor(), _Cursor(rows=[]), _Cursor(rowcount=1)])
    monkeypatch.setattr(timeout_unknown_recovery.psycopg, "connect", lambda _dsn: count_conn)
    monkeypatch.setattr(timeout_unknown_recovery, "_read_outbox_status", lambda *_args: "UNKNOWN")
    with pytest.raises(RuntimeError, match="UNKNOWN count not raised"):
        asyncio.run(timeout_unknown_recovery._probe_timeout_unknown_recovery("dsn", "BTCUSDT", object()))

    # A recovery that does not restore the baseline must fail closed.
    recovery_conn = _Connection(
        [
            _Cursor(rows=[]),
            _Cursor(),
            _Cursor(rows=[("UNKNOWN",)]),
            _Cursor(rows=[("UNKNOWN", 1)]),
            _Cursor(rowcount=1),
            _Cursor(rows=[("UNKNOWN", 2)]),
            _Cursor(rowcount=1),
            _Cursor(rows=[("UNKNOWN", 1)]),
        ]
    )
    monkeypatch.setattr(timeout_unknown_recovery.psycopg, "connect", lambda _dsn: recovery_conn)
    monkeypatch.setattr(timeout_unknown_recovery, "_read_outbox_status", ack_loss._read_outbox_status)

    class Client:
        async def get_open_orders(self, _symbol):
            from beidou_exchange.core.error_taxonomy import Result

            return Result.success([])

    with pytest.raises(RuntimeError, match="not restored after recover"):
        asyncio.run(timeout_unknown_recovery._probe_timeout_unknown_recovery("dsn", "BTCUSDT", Client()))
    assert recovery_conn.closed is True

    # Cleanup is independently checked against the baseline, even after a
    # successful recovery verdict.
    cleanup_conn = _Connection(
        [
            _Cursor(rows=[]),
            _Cursor(),
            _Cursor(rows=[("UNKNOWN",)]),
            _Cursor(rows=[("UNKNOWN", 1)]),
            _Cursor(rowcount=1),
            _Cursor(rows=[("UNKNOWN", 0)]),
            _Cursor(rowcount=1),
            _Cursor(rows=[("UNKNOWN", 1)]),
        ]
    )
    monkeypatch.setattr(timeout_unknown_recovery.psycopg, "connect", lambda _dsn: cleanup_conn)
    steps, ok, reason = asyncio.run(
        timeout_unknown_recovery._probe_timeout_unknown_recovery("dsn", "BTCUSDT", Client())
    )
    assert ok is True and reason == "recoverable_no_duplicate"
    assert any(step.get("action") == "cleanup" and step.get("ok") is False for step in steps)
    assert cleanup_conn.closed is True

    class FailingLedger:
        def record(self, *_args) -> None:
            raise NotionalExceededError("timeout_unknown_recovery", 51.0, 50.0)

    ctx = ScenarioContext(
        client=object(), ledger=FailingLedger(), evidence_dir=tmp_path, symbol="BTCUSDT", dry_run=False
    )
    with pytest.raises(NotionalExceededError):
        asyncio.run(timeout_unknown_recovery.TimeoutUnknownRecoveryScenario().run(ctx))

    monkeypatch.setattr(
        timeout_unknown_recovery,
        "_probe_timeout_unknown_recovery",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("timeout probe")),
    )
    failed = asyncio.run(timeout_unknown_recovery.TimeoutUnknownRecoveryScenario().run(_context(tmp_path)))
    assert failed.status is ScenarioStatus.FAIL and failed.error_type == "RuntimeError"


def test_duplicate_runner_preserves_notional_and_probe_failure_contract(monkeypatch, tmp_path) -> None:
    class FailingLedger:
        def record(self, *_args) -> None:
            raise NotionalExceededError("duplicate_request", 51.0, 50.0)

    ctx = ScenarioContext(client=None, ledger=FailingLedger(), evidence_dir=tmp_path, symbol="BTCUSDT", dry_run=False)
    with pytest.raises(NotionalExceededError):
        asyncio.run(duplicate_request.DuplicateRequestScenario().run(ctx))

    monkeypatch.setattr(
        duplicate_request,
        "_probe_idempotency",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("duplicate probe")),
    )
    failed = asyncio.run(duplicate_request.DuplicateRequestScenario().run(_context(tmp_path)))
    assert failed.status is ScenarioStatus.FAIL and failed.error_type == "RuntimeError"


@pytest.mark.asyncio
async def test_timeout_probe_and_duplicate_probe_cover_durable_recovery_paths(monkeypatch) -> None:
    class ProbeOutbox:
        def __init__(self, **_kwargs) -> None:
            pass

        def mark_unknown(self, _intent_id, _reason) -> None:
            return None

    timeout_conn = _Connection(
        [
            _Cursor(rows=[("FAILED", 1)]),
            _Cursor(),
            _Cursor(rows=[("UNKNOWN",)]),
            _Cursor(rows=[("UNKNOWN", 1)]),
            _Cursor(rowcount=1),
            _Cursor(rows=[("FAILED", 2)]),
            _Cursor(rowcount=1),
            _Cursor(rows=[("FAILED", 1)]),
        ]
    )
    monkeypatch.setattr(timeout_unknown_recovery, "PostgresIntentOutbox", ProbeOutbox)
    monkeypatch.setattr(timeout_unknown_recovery.psycopg, "connect", lambda _dsn: timeout_conn)
    monkeypatch.setattr(
        timeout_unknown_recovery,
        "_venue_status",
        lambda *_args: asyncio.sleep(0, result=("GONE", {"action": "venue"})),
    )
    steps, ok, reason = await timeout_unknown_recovery._probe_timeout_unknown_recovery("dsn", "BTCUSDT", object())
    assert ok is True and reason == "recoverable_no_duplicate"
    assert any(step.get("action") == "state_counts_after_recover" for step in steps)
    assert timeout_conn.closed is True

    class DuplicateCursor(_Cursor):
        def __init__(self, *, duplicate: bool = False, **kwargs) -> None:
            super().__init__(**kwargs)
            self.duplicate = duplicate

        def execute(self, sql, params=()):
            super().execute(sql, params)
            if self.duplicate:
                import psycopg

                raise psycopg.errors.UniqueViolation("duplicate key")

    duplicate_conn = _Connection([DuplicateCursor(), DuplicateCursor(duplicate=True), DuplicateCursor(rowcount=1)])
    monkeypatch.setattr(duplicate_request.psycopg, "connect", lambda _dsn: duplicate_conn)
    evidence, accepted, duplicate_reason = duplicate_request._probe_idempotency("dsn")
    assert accepted is True and duplicate_reason == "exchange_rejected"
    assert evidence[-1]["action"] == "cleanup"
    assert duplicate_conn.closed is True

    accepted_conn = _Connection([DuplicateCursor(), DuplicateCursor(), DuplicateCursor(rowcount=0)])
    monkeypatch.setattr(duplicate_request.psycopg, "connect", lambda _dsn: accepted_conn)
    evidence, accepted, duplicate_reason = duplicate_request._probe_idempotency("dsn")
    assert accepted is False and duplicate_reason == "duplicate_accepted"
    assert evidence[-1]["action"] == "cleanup"

    class CleanupFailureCursor(DuplicateCursor):
        def execute(self, sql, params=()):
            super().execute(sql, params)
            if sql.startswith("DELETE"):
                raise RuntimeError("cleanup")

    failing_cleanup_conn = _Connection([DuplicateCursor(), DuplicateCursor(duplicate=True), CleanupFailureCursor()])
    monkeypatch.setattr(duplicate_request.psycopg, "connect", lambda _dsn: failing_cleanup_conn)
    evidence, accepted, _ = duplicate_request._probe_idempotency("dsn")
    assert accepted is True
    assert evidence[-1]["ok"] is False


@pytest.mark.asyncio
async def test_certification_scenarios_cover_dry_run_unverifiable_and_probe_results(tmp_path, monkeypatch) -> None:
    ack = ack_loss.AckLossScenario()
    timeout = timeout_unknown_recovery.TimeoutUnknownRecoveryScenario()
    duplicate = duplicate_request.DuplicateRequestScenario()

    for scenario in (ack, timeout, duplicate):
        result = await scenario.run(_context(tmp_path, dry_run=True))
        assert result.status is ScenarioStatus.PASS
        assert result.evidence["dry_run"] is True

    for scenario in (ack, timeout):
        result = await scenario.run(_context(tmp_path, client=None))
        assert result.status is ScenarioStatus.NOT_VERIFIABLE
        assert result.error_type == "CLIENT_UNAVAILABLE"

    async def ack_probe(*_args, **_kwargs):
        return ([{"action": "verdict", "ok": True}], True, "recoverable_no_duplicate", "GONE")

    async def timeout_probe(*_args, **_kwargs):
        return ([{"action": "durable_gate", "ok": True}], True, "recoverable_no_duplicate")

    monkeypatch.setattr(ack_loss, "_probe_ack_loss", ack_probe)
    monkeypatch.setattr(timeout_unknown_recovery, "_probe_timeout_unknown_recovery", timeout_probe)
    monkeypatch.setattr(ack_loss, "PG_DSN", "test-dsn")
    monkeypatch.setattr(timeout_unknown_recovery, "PG_DSN", "test-dsn")
    assert (await ack.run(_context(tmp_path))).status is ScenarioStatus.PASS
    assert (await timeout.run(_context(tmp_path))).status is ScenarioStatus.PASS

    monkeypatch.setattr(
        duplicate_request, "_probe_idempotency", lambda *_args: ([{"action": "cleanup"}], True, "exchange_rejected")
    )
    assert (await duplicate.run(_context(tmp_path))).status is ScenarioStatus.PASS

    monkeypatch.setattr(
        ack_loss, "_probe_ack_loss", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("probe"))
    )
    failed = await ack.run(_context(tmp_path))
    assert failed.status is ScenarioStatus.FAIL
    assert failed.error_type == "RuntimeError"


def test_chaos_engine_records_independent_evidence_and_rejects_invalid_observations() -> None:
    chaos = ChaosEngine()
    exp = chaos.inject(KillScenario.EXCHANGE_DISCONNECT)
    chaos.verify_recovery(exp, recovered=True, time_s=1.25, invariants_ok=True)
    assert exp.observed_recovery is True
    assert exp.recovery_time_seconds == 1.25
    assert chaos.all_experiments_passed() is True

    with pytest.raises(TypeError):
        chaos.verify_recovery(exp, recovered=1, time_s=None, invariants_ok=True)
    with pytest.raises(ValueError):
        chaos.verify_recovery(exp, recovered=True, time_s=float("inf"), invariants_ok=True)

    combo = chaos.combination_fault_test([KillScenario.EXCHANGE_DISCONNECT, KillScenario.DATABASE_FAILURE])
    assert [item.scenario for item in combo] == [KillScenario.EXCHANGE_DISCONNECT, KillScenario.DATABASE_FAILURE]
    assert chaos.all_experiments_passed() is False


def test_kill_register_tracks_single_and_combination_scenarios() -> None:
    chaos = ChaosEngine()
    chaos._register.register(KillScenario.EXCHANGE_DISCONNECT)
    chaos._register.register(KillScenario.DATABASE_FAILURE)
    combination = [KillScenario.EXCHANGE_DISCONNECT, KillScenario.DATABASE_FAILURE]
    chaos._register.register_combination(combination)
    assert chaos._register.all_registered() == [KillScenario.EXCHANGE_DISCONNECT, KillScenario.DATABASE_FAILURE]
    assert chaos._register.combination_scenarios == [combination]


def test_chaos_injection_lifecycle_and_unknown_observer_path() -> None:
    chaos = ChaosEngine()
    latency = chaos.inject_latency(0.25)
    error = chaos.inject_exchange_error(-1003, "rate limited")
    memory = chaos.inject_memory_pressure(size_mb=0)
    assert "latency_injected: 0.25s" in latency.evidence
    assert "exchange_error: code=-1003 msg=rate limited" in error.evidence
    assert any("memory_pressure: 0MB allocated" in item for item in memory.evidence)
    chaos.cleanup_experiment(memory)
    assert memory.evidence[-1] == "cleanup: completed"

    results = chaos.run_chaos_cycle()
    assert len(results) == 3
    assert all(item.observed_recovery is False for item in results)
    assert all("cleanup: completed" in item.evidence for item in results)


def test_chaos_cycle_requires_strict_observer_contract() -> None:
    chaos = ChaosEngine()

    class BadObserver:
        def observe_chaos_recovery(self, _exp):
            return {"recovered": "yes", "time_s": 1.0, "invariants_ok": True}

    result = chaos.run_chaos_cycle(BadObserver())
    assert len(result) == 3
    assert all(item.observed_recovery is False for item in result)
    assert all(any("chaos_cycle_error" in evidence for evidence in item.evidence) for item in result)


def test_chaos_cycle_accepts_valid_observer_and_rejects_bad_shapes(monkeypatch) -> None:
    class ValidObserver:
        def observe_chaos_recovery(self, _exp):
            return {"recovered": True, "time_s": 0.5, "invariants_ok": True}

    chaos = ChaosEngine()
    result = chaos.run_chaos_cycle(ValidObserver())
    assert len(result) == 3
    assert all(item.observed_recovery and item.invariants_preserved for item in result)
    assert all("recovery_observation: independent observer" in item.evidence for item in result)

    class ListObserver:
        def observe_chaos_recovery(self, _exp):
            return []

    result = ChaosEngine().run_chaos_cycle(ListObserver())
    assert all(any("chaos_cycle_error" in evidence for evidence in item.evidence) for item in result)

    class BadTimeObserver:
        def observe_chaos_recovery(self, _exp):
            return {"recovered": True, "time_s": "slow", "invariants_ok": True}

    result = ChaosEngine().run_chaos_cycle(BadTimeObserver())
    assert all(any("chaos_cycle_error" in evidence for evidence in item.evidence) for item in result)

    import beidou_chaos.engine as chaos_module

    def raise_memory(_size):
        raise MemoryError("test allocation")

    monkeypatch.setattr(chaos_module, "bytearray", raise_memory, raising=False)
    memory = ChaosEngine().inject_memory_pressure(size_mb=1)
    assert memory.evidence == ["memory_pressure: allocation FAILED (1MB)"]


def test_chaos_cycle_handles_unsupported_injection_and_finally_recovery(monkeypatch) -> None:
    chaos = ChaosEngine()
    monkeypatch.setattr(chaos, "inject_cpu_stress", lambda **_kwargs: None)
    result = chaos.run_chaos_cycle()
    assert result[0].observed_recovery is False
    assert any("unsupported chaos method" in evidence for evidence in result[0].evidence)

    chaos = ChaosEngine()
    monkeypatch.setattr(chaos, "inject_cpu_stress", lambda **_kwargs: None)
    calls = {"count": 0}
    original_inject = chaos.inject

    def inject_with_first_failure(scenario):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("inject failure")
        return original_inject(scenario)

    monkeypatch.setattr(chaos, "inject", inject_with_first_failure)
    with pytest.raises(RuntimeError, match="inject failure"):
        chaos.run_chaos_cycle()
    assert calls["count"] >= 2


def test_fault_scenario_definitions_are_machine_decidable() -> None:
    result = run_fault_scenario(FaultScenario.TIMEOUT)
    assert result.passed is True
    assert result.is_machine_decidable() is True
    assert result.recovery_executed is True
    assert "order_timeout_log" in result.evidence_collected

    unknown = run_fault_scenario("unknown")
    assert unknown.passed is False
    assert unknown.is_machine_decidable() is False
