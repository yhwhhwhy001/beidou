"""BD-FIX (V4 coverage): remaining engine defensive-branch coverage.

These tests close the last few uncovered branches of
``beidou_core.engine``: venue-leverage tri-state inputs, credential-health
malformed-environment handling, flat-convergence symbol/amount guards,
stale-order fill-journal failure fallback, startup adjudication timeout, and
the producer-only nearline/offline loop branches.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import beidou_core.engine as engine_module
from beidou_core.engine import AutonomousEngine
from tests.unit.test_full_repository_coverage_engine_runtime import _runtime_engine
from tests.unit.test_full_repository_coverage_engine_v4 import _flat_account, _flat_engine, _FlatStore

# --- venue leverage tri-state inputs (BD-FIX V4 B2) ------------------------


@pytest.mark.asyncio
async def test_sync_venue_leverage_rejects_non_numeric_target(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = SimpleNamespace(value="testnet")
    api = AsyncMock()
    monkeypatch.setattr(engine, "_api_async", api)
    monkeypatch.setenv("BEIDOU_SYNC_VENUE_LEVERAGE", "1")
    assert await engine._sync_venue_leverage("XRPUSDT", "not-a-number") is False  # type: ignore[arg-type]
    api.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_venue_leverage_rejects_malformed_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = SimpleNamespace(value="testnet")
    monkeypatch.setattr(engine, "_api_async", AsyncMock(return_value={"leverage": "twelve"}))
    monkeypatch.setenv("BEIDOU_SYNC_VENUE_LEVERAGE", "1")
    assert await engine._sync_venue_leverage("XRPUSDT", 3.0) is False
    assert not engine._venue_leverage.get("XRPUSDT")


# --- credential health malformed environment -------------------------------


def test_credential_health_unknown_when_env_mode_is_malformed() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    from datetime import datetime, timedelta, timezone

    from beidou_security.identity import Credential, CredentialType

    engine._credential = Credential(
        credential_id="active",
        credential_type=CredentialType.TRADING,
        expires_at=datetime.now(timezone.utc) + timedelta(days=90),
    )
    engine._can_trade = True
    engine._can_withdraw = True
    engine._env_mode = "testnet"  # plain string: .value access raises inside the R9 check
    health = engine._check_credential_health()
    assert health["level"] == "UNKNOWN"
    assert "error" in health


# --- flat convergence guards ------------------------------------------------


def test_flat_convergence_rejects_empty_and_nonfinite_system_position_symbols() -> None:
    engine, _store = _flat_engine()
    engine._system_facts = SimpleNamespace(complete=True, positions={"": SimpleNamespace(amount="1")})
    assert engine._converge_flat_local_positions(_flat_account(), [], []) is False

    engine, _store = _flat_engine()
    engine._system_facts = SimpleNamespace(complete=True, positions={"BTCUSDT": SimpleNamespace(amount="NaN")})
    assert engine._converge_flat_local_positions(_flat_account(), [], []) is False


def test_flat_convergence_skips_durable_row_for_foreign_symbol() -> None:
    store = _FlatStore()
    store.durable_rows.append(
        {
            "protection_id": "foreign-canceled",
            "position_id": "foreign-pos",
            "symbol": "XRPUSDT",
            "status": "CANCELLED",
            "owner_id": "owner",
            "position_generation": 1,
        }
    )
    engine, _store = _flat_engine(store)
    assert engine._converge_flat_local_positions(_flat_account(), [], []) is True
    assert store.removed == ["durable-pos"]


# --- stale order resolution fill-journal failure ----------------------------


@pytest.mark.asyncio
async def test_stale_order_resolution_survives_fill_journal_failure() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._active_order_ids = {"13"}
    engine._owned_order_ids = {"13"}
    engine._order_trackers = {}
    engine._order_symbols = {"13": "BTCUSDT"}
    saved: list[tuple] = []
    engine._store = SimpleNamespace(
        restore_order_states=lambda: [{"order_id": "13", "symbol": "BTCUSDT", "status": "NEW"}],
        restore_fill_events=lambda: (_ for _ in ()).throw(TypeError("journal corrupt")),
        save_order_state=lambda *args, **kwargs: saved.append((args, kwargs)),
    )
    booked: list[str] = []
    engine._book_venue_terminal_fill = lambda oid, _symbol, _raw: asyncio.sleep(0, result=booked.append(oid))

    async def query(_endpoint: str, **_kwargs: object) -> dict[str, object]:
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

    engine._api_async = query
    assert await engine._resolve_stale_order_states(min_age_seconds=0, include_active=True) == 1
    assert saved and saved[0][0][6] == "FILLED"
    assert booked == ["13"]


@pytest.mark.asyncio
async def test_stale_order_resolution_committed_fill_journal_prevents_rebooking() -> None:
    """Restore-fill journal with a COMMITTED row suppresses double booking."""

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._active_order_ids = {"13"}
    engine._owned_order_ids = {"13"}
    engine._order_trackers = {}
    engine._order_symbols = {"13": "BTCUSDT"}
    saved: list[tuple] = []
    engine._store = SimpleNamespace(
        restore_order_states=lambda: [{"order_id": "13", "symbol": "BTCUSDT", "status": "NEW"}],
        restore_fill_events=lambda: [
            {"order_id": "other", "processing_state": "COMMITTED", "cumulative_qty": "5"},
            {"order_id": "13", "processing_state": "PENDING", "cumulative_qty": "5"},
            {"order_id": "13", "processing_state": "COMMITTED", "cumulative_qty": "1"},
        ],
        save_order_state=lambda *args, **kwargs: saved.append((args, kwargs)),
    )
    booked: list[str] = []
    engine._book_venue_terminal_fill = lambda oid, _symbol, _raw: asyncio.sleep(0, result=booked.append(oid))

    async def query(_endpoint: str, **_kwargs: object) -> dict[str, object]:
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

    engine._api_async = query
    assert await engine._resolve_stale_order_states(min_age_seconds=0, include_active=True) == 1
    assert booked == []  # committed journal is authoritative — no re-booking
    assert saved and saved[0][0][6] == "FILLED"


@pytest.mark.asyncio
async def test_run_reports_resolved_startup_order_states(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module.PersistentStore, "_instance", None)
    engine = _runtime_engine(monkeypatch)
    engine._can_write = True

    async def safe_account(_endpoint: str, **_kwargs: object) -> tuple[object, bool]:
        if _endpoint is engine_module.Endpoint.SERVER_TIME:
            return ({"serverTime": 1}, True)
        return (
            {"totalWalletBalance": "1000", "positions": [], "canTrade": True, "canWithdraw": False},
            True,
        )

    engine._api_async_safe = safe_account
    engine._resolve_stale_order_states = lambda **_kwargs: asyncio.sleep(0, result=2)
    await engine.run()
    assert engine.shutdown_seen


def test_quantity_normalization_passes_nonfinite_values_through() -> None:
    from beidou_exchange.core.write_authority import normalize_quantity_string

    assert normalize_quantity_string("NaN") == "NaN"
    assert normalize_quantity_string("0.100") == "0.1"
    assert normalize_quantity_string("not-a-number") == "not-a-number"


def test_guard_quantities_equal_is_fail_closed_on_malformed_inputs() -> None:
    from beidou_exchange.testnet_guard import _quantities_equal

    assert _quantities_equal("0.100", "0.1") is True
    assert _quantities_equal("bad", "0.1") is False
    assert _quantities_equal("NaN", "0.1") is False
    assert _quantities_equal("0.1", "0.2") is False


def test_testnet_cap_rejects_nonpositive_limits() -> None:
    from decimal import Decimal

    from beidou_exchange.testnet_guard import TestnetCap, TestnetGuardError

    with pytest.raises(TestnetGuardError):
        TestnetCap(max_notional=Decimal("0"), max_leverage=Decimal("3"), max_account_exposure=Decimal("25"))
    with pytest.raises(TestnetGuardError):
        TestnetCap(max_notional=Decimal("25"), max_leverage=Decimal("Infinity"), max_account_exposure=Decimal("25"))
    with pytest.raises(TestnetGuardError):
        TestnetCap(max_notional=Decimal("25"), max_leverage=Decimal("3"), max_account_exposure=Decimal("-1"))


# --- startup adjudication timeout --------------------------------------------


@pytest.mark.asyncio
async def test_run_survives_startup_order_state_adjudication_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real constructor binds the process-local store singleton; reset it
    # so this test never leaks a paper-database binding into later tests.
    monkeypatch.setattr(engine_module.PersistentStore, "_instance", None)
    engine = _runtime_engine(monkeypatch)
    engine._can_write = True

    async def safe_account(_endpoint: str, **_kwargs: object) -> tuple[object, bool]:
        if _endpoint is engine_module.Endpoint.SERVER_TIME:
            return ({"serverTime": 1}, True)
        return (
            {"totalWalletBalance": "1000", "positions": [], "canTrade": True, "canWithdraw": False},
            True,
        )

    engine._api_async_safe = safe_account

    async def raise_timeout(*_args: object, **_kwargs: object) -> int:
        raise asyncio.TimeoutError

    engine._resolve_stale_order_states = raise_timeout
    await engine.run()
    assert engine.shutdown_seen


# --- producer-only nearline/offline loop branches -----------------------------


@pytest.mark.asyncio
async def test_producer_only_nearline_and_offline_loops_are_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module.PersistentStore, "_instance", None)
    engine = _runtime_engine(monkeypatch)
    engine._producer_only = True
    captured: list[object] = []
    real_create_task = asyncio.create_task

    def fake_create_task(coro: object) -> object:
        captured.append(coro)
        return real_create_task(asyncio.sleep(0))

    monkeypatch.setattr(engine_module.asyncio, "create_task", fake_create_task)
    await engine.run()
    assert engine.shutdown_seen
    loops = [coro for coro in captured if coro.__qualname__.endswith(("_nearline_loop", "_offline_loop"))]
    assert len(loops) == 2

    async def stop_after_one_iteration(_seconds: float) -> None:
        engine._running = False

    monkeypatch.setattr(engine_module.asyncio, "sleep", stop_after_one_iteration)
    for coro in loops:
        engine._running = True
        await coro
        engine._running = False
    assert engine._last_nearline > 0
    for coro in captured:
        coro.close()  # the realtime loop was deliberately never started
