"""Behavior-backed V4 coverage for the engine's fail-closed recovery seams.

These tests intentionally assert externally meaningful safety outcomes.  They
do not execute source text or install tracing hooks merely to mark lines as
visited.
"""

from __future__ import annotations

import asyncio
import builtins
import time
from types import SimpleNamespace
from typing import Any

import pytest

import beidou_core.engine as engine_module
from beidou_core.engine import AutonomousEngine, _payload_rows, attach_engine_file_log
from beidou_shared.types import OrderSide


def _raises(exc: Exception):
    def raise_error(*_args: object, **_kwargs: object) -> Any:
        raise exc

    return raise_error


def test_payload_rows_rejects_untyped_containers_and_preserves_mapping_payloads() -> None:
    assert _payload_rows(None) == []
    assert _payload_rows(["bad", {"payload": "bad"}, {"payload": {1: "one"}}, {"plain": 2}]) == [
        {"1": "one"},
        {"plain": 2},
    ]


def test_attach_engine_file_log_failure_keeps_import_logging_non_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "_log_handler", None)
    monkeypatch.setattr(
        engine_module,
        "EvidenceArchiveRotatingFileHandler",
        _raises(OSError("read-only evidence directory")),
    )

    assert attach_engine_file_log("/not-writable/beidou.log") is None
    assert engine_module._log_handler is None


def test_constructor_rejects_producer_capability_outside_testnet() -> None:
    with pytest.raises(ValueError, match="G5_PRODUCER_TESTNET_ONLY"):
        AutonomousEngine(["BTCUSDT"], mode="paper", producer_only=True)


class _ExposureStore:
    def __init__(self) -> None:
        self.record: dict[str, Any] | None = None
        self.writes: list[tuple[str, str, dict[str, Any]]] = []
        self.deletes: list[tuple[str, str]] = []
        self.rows: list[dict[str, Any]] = []
        self.raise_get = False
        self.raise_records = False

    def _get_record(self, _kind: str, _key: str) -> dict[str, Any] | None:
        if self.raise_get:
            raise OSError("read failed")
        return self.record

    def _write_record(self, kind: str, key: str, payload: dict[str, Any]) -> None:
        self.writes.append((kind, key, dict(payload)))

    def _delete_record(self, kind: str, key: str) -> None:
        self.deletes.append((kind, key))

    def _records(self, _kind: str) -> list[dict[str, Any]]:
        if self.raise_records:
            raise OSError("records unavailable")
        return list(self.rows)

    def restore_protections(self) -> list[dict[str, Any]]:
        return []


def test_update_protection_fact_persists_only_well_formed_unowned_gaps() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    store = _ExposureStore()
    store.raise_get = True
    engine._store = store
    engine._protection_owner_id = "owner"
    engine._protection = SimpleNamespace(all_positions=lambda: {})
    engine._last_account = {"positions": []}
    engine._protection_issues = set()
    engine._protection_owner_unknown = False
    engine._assess_protection_coverage = lambda *_args: (
        False,
        {
            "unprotected_symbols": [
                {"symbol": "", "reason": "MISSING_SL"},
                {"symbol": "BTCUSDT", "reason": "MISSING_SL", "position_quantity": "1"},
            ]
        },
    )
    persisted: list[tuple[str, str, bool]] = []
    engine._persist_protection_exposure = lambda symbol, reason, **kwargs: (
        persisted.append((symbol, reason, kwargs["increment"])) or {}
    )

    engine._update_protection_fact(hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=True)

    assert persisted == [("BTCUSDT", "MISSING_SL", False)]
    assert engine._protection_owner_unknown is True
    btc_gap = next(gap for gap in engine._last_protection_gap_detail if gap["symbol"] == "BTCUSDT")
    assert btc_gap["position_abs_qty"] == "1"

    store.raise_get = False
    store.record = {"payload": {"last_reason": "SL_UNPROTECTABLE"}}
    persisted.clear()
    engine._update_protection_fact(hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=True)
    assert persisted == [], "the explicit E2 exposure owner must not be overwritten by a gap refresh"


class _RestoreStore:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def restore_protections(self) -> list[dict[str, Any]]:
        return list(self.rows)


def _active_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "protection_id": "sl",
        "position_id": "pos",
        "symbol": "BTCUSDT",
        "side": "SELL",
        "trigger_price": "90",
        "quantity": "1",
        "order_type": "STOP_MARKET",
        "status": "ACTIVE",
        "stop_type": "ATR_BASED",
        "take_profit_type": "",
        "owner_id": "owner",
        "position_generation": 1,
        "session_id": "session",
        "exchange_order_id": "sl-1",
    }
    row.update(changes)
    return row


def _restore_shell(rows: list[dict[str, Any]]) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._producer_only = True
    engine._store = _RestoreStore(rows)
    engine._protection_owner_id = "owner"
    engine._position_projection = {}
    engine._position_entry_times = {}
    engine._active_algo_ids = {}
    engine._pending_stop_intent = {}
    engine._blocked: list[str] = []
    engine._block_unowned_protection_orders = lambda issues: engine._blocked.extend(issues)
    engine._protection_inventory_semantic_issues = lambda _inventory: []
    engine._protection = SimpleNamespace(
        all_positions=lambda: {},
        restore_position_protection=lambda _projection: None,
    )
    return engine


@pytest.mark.parametrize(
    ("rows", "account", "expected_reason"),
    [
        pytest.param(
            [_active_row(position_id="old"), _active_row(position_id="new", protection_id="sl-new")],
            {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]},
            "PROTECTION_MULTIPLE_ACTIVE_GENERATIONS:BTCUSDT",
            id="multiple-active-generations",
        ),
        pytest.param(
            [_active_row()],
            {"positions": [{"symbol": "BTCUSDT", "positionAmt": "0", "entryPrice": "0"}]},
            "POSITION_FLAT_ON_VENUE:BTCUSDT",
            id="venue-flat",
        ),
        pytest.param(
            [_active_row(side="BUY")],
            {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]},
            "PROTECTION_SIDE_FLIPPED:BTCUSDT",
            id="side-flipped",
        ),
        pytest.param(
            [_active_row(status="PENDING")],
            {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]},
            "PROTECTION_PENDING_REQUIRES_ADOPTION:sl",
            id="pending-needs-write-adoption",
        ),
        pytest.param(
            [_active_row(quantity="0.5")],
            {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]},
            "STOP_COVERAGE_STALE:BTCUSDT",
            id="stale-stop-coverage",
        ),
    ],
)
def test_producer_restore_rejects_every_path_that_would_require_local_mutation(
    rows: list[dict[str, Any]], account: dict[str, Any], expected_reason: str
) -> None:
    engine = _restore_shell(rows)
    inventory = [
        {
            "algoId": str(row["exchange_order_id"]),
            "symbol": str(row["symbol"]),
            "side": str(row["side"]),
            "quantity": str(row["quantity"]),
            "triggerPrice": str(row["trigger_price"]),
            "reduceOnly": True,
        }
        for row in rows
    ]

    assert engine._restore_durable_protection_projection(account, inventory) is False
    assert expected_reason in engine._blocked
    assert engine._active_algo_ids == {}, "read-only producer must not adopt or cancel venue-backed rows"


@pytest.mark.asyncio
async def test_producer_boundaries_never_cancel_or_recover_exchange_protections() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._producer_only = True
    engine._stale_protection_algos = [("BTCUSDT", "7")]
    engine._cancel_algo_order = _raises(AssertionError("venue cancellation must not run"))

    assert await engine._cancel_stale_protection_algos() == set()
    assert engine._stale_protection_algos == [("BTCUSDT", "7")]
    ids = ["7"]
    inventory = [{"algoId": "7"}]
    assert await engine._safe_recover_unowned_testnet_algos(ids, inventory, []) == (ids, inventory)

    engine._control = SimpleNamespace(get_status=_raises(AssertionError("recovery body must not run")))
    assert await engine._ensure_exchange_position_protections() is None


@pytest.mark.parametrize("target", ["bad", float("nan"), 0, -1])
def test_committed_fill_coverage_rejects_invalid_targets(target: object) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = SimpleNamespace(restore_fill_events=lambda: [])
    assert engine._committed_fill_qty_covers("order-1", target) is False


@pytest.mark.parametrize(
    "bad_delta",
    ["bad", "NaN", "0", "-1"],
)
def test_committed_fill_coverage_rejects_malformed_or_nonpositive_committed_deltas(bad_delta: str) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = SimpleNamespace(
        restore_fill_events=lambda: [
            {"order_id": "other", "processing_state": "COMMITTED", "delta_qty": "100"},
            {"order_id": "order-1", "processing_state": "PENDING", "delta_qty": "100"},
            {"order_id": "order-1", "processing_state": "COMMITTED", "delta_qty": bad_delta},
        ]
    )
    assert engine._committed_fill_qty_covers("order-1", 1) is False


def test_committed_fill_coverage_requires_readable_store_and_sufficient_same_order_deltas() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = SimpleNamespace(restore_fill_events=None)
    assert engine._committed_fill_qty_covers("order-1", 1) is False
    engine._store.restore_fill_events = _raises(OSError("journal unavailable"))
    assert engine._committed_fill_qty_covers("order-1", 1) is False
    engine._store.restore_fill_events = lambda: [
        {"order_id": "order-1", "processing_state": "COMMITTED", "delta_qty": "0.4"},
        {"order_id": "order-1", "processing_state": "COMMITTED", "delta_qty": "0.6"},
    ]
    assert engine._committed_fill_qty_covers("order-1", 1) is True


def _reconcile_shell() -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = SimpleNamespace(value="testnet")
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "1"}}
    engine._protection_owner_id = "owner"
    engine._protection = SimpleNamespace(all_positions=lambda: {})
    engine._store = SimpleNamespace(restore_protections=lambda: [], _records=lambda _kind: [])
    engine._build_system_reconciliation_facts = lambda: SimpleNamespace(complete=True, positions={})
    engine._last_algo_inventory = []
    engine._last_algo_inventory_at = time.time()
    engine._last_algo_inventory_genuine = True
    engine._clear_calls: list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    engine._converge_calls = 0
    engine._clear_stale_flat_protection_exposures = lambda account, orders, algos: (
        engine._clear_calls.append((account, orders, algos)) or set()
    )
    engine._converge_flat_local_positions = lambda *_args: (
        setattr(engine, "_converge_calls", engine._converge_calls + 1) or True
    )
    engine._update_protection_fact = lambda **_kwargs: None
    engine._record_reconciliation_failure = lambda *_args, **_kwargs: False

    async def api(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        if endpoint == engine_module.Endpoint.ACCOUNT:
            return {"totalWalletBalance": "100", "positions": [{}]}, True
        return [], True

    engine._api_async_safe = api
    engine._get_open_algo_inventory = lambda: asyncio.sleep(0, result=[])
    return engine


@pytest.mark.asyncio
async def test_reconcile_discovers_every_durable_flat_convergence_candidate() -> None:
    cached = _reconcile_shell()
    cached._update_protection_fact = _raises(OSError("diagnostic persistence must be suppressed"))
    assert await cached._reconcile() is False
    assert cached._converge_calls == 1
    assert cached._clear_calls and cached._clear_calls[0][2] == []

    malformed_projection = _reconcile_shell()
    malformed_projection._position_projection = {"BTCUSDT": {"signed_quantity": "bad"}}
    assert await malformed_projection._reconcile() is False
    assert malformed_projection._converge_calls == 1, "malformed local quantity is UNKNOWN, not flat"

    local_protection = _reconcile_shell()
    local_protection._position_projection = {}
    local_protection._protection = SimpleNamespace(all_positions=lambda: {"pos": object()})
    assert await local_protection._reconcile() is False
    assert local_protection._converge_calls == 1

    durable_protection = _reconcile_shell()
    durable_protection._position_projection = {}
    durable_protection._store.restore_protections = lambda: [
        {"owner_id": "owner", "status": "ACTIVE", "symbol": "BTCUSDT"}
    ]
    assert await durable_protection._reconcile() is False
    assert durable_protection._converge_calls == 1

    missing_exposure_api = _reconcile_shell()
    missing_exposure_api._position_projection = {}
    missing_exposure_api._store = SimpleNamespace(restore_protections=lambda: [])
    assert await missing_exposure_api._reconcile() is False
    assert missing_exposure_api._converge_calls == 1, "an unreadable exposure ledger must stay fail-closed"

    exposure = _reconcile_shell()
    exposure._position_projection = {}
    exposure._store._records = lambda _kind: [{"symbol": "BTCUSDT"}]
    assert await exposure._reconcile() is False
    assert exposure._converge_calls == 1

    replay_position = _reconcile_shell()
    replay_position._position_projection = {}
    replay_position._build_system_reconciliation_facts = lambda: SimpleNamespace(
        complete=True, positions={"BTCUSDT": SimpleNamespace(amount="1")}
    )
    assert await replay_position._reconcile() is False
    assert replay_position._converge_calls == 1


@pytest.mark.asyncio
async def test_reconcile_inventory_read_failure_never_attempts_flat_convergence() -> None:
    engine = _reconcile_shell()
    engine._last_algo_inventory_genuine = False

    async def unavailable_inventory() -> list[dict[str, Any]]:
        raise OSError("inventory unavailable")

    engine._get_open_algo_inventory = unavailable_inventory
    assert await engine._reconcile() is False
    assert engine._converge_calls == 0
    assert engine._clear_calls == []


def test_persist_protection_exposure_handles_clock_store_and_generation_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = None
    engine._position_generation = {}
    assert engine._persist_protection_exposure("BTCUSDT", "MISSING_SL") == {}

    store = _ExposureStore()
    store.raise_get = True
    engine._store = store
    engine._position_generation = {"BTCUSDT": 2}
    before = time.time()
    payload = engine._persist_protection_exposure("BTCUSDT", "MISSING_SL", now=object())
    assert before <= payload["unprotectable_since"] <= time.time()
    assert payload["attempts"] == 1
    assert store.writes[-1][2] == payload

    store.raise_get = False
    store.record = {
        "payload": {
            "position_generation": 1,
            "unprotectable_since": 10.0,
            "attempts": 9,
            "last_reason": "OLD",
        }
    }
    payload = engine._persist_protection_exposure("BTCUSDT", "NEW", now=100.0, increment=False)
    assert store.deletes[-1] == ("protection_exposure", "exposure:BTCUSDT")
    assert payload == {
        "symbol": "BTCUSDT",
        "position_generation": 2,
        "unprotectable_since": 100.0,
        "last_reason": "NEW",
        "attempts": 1,
    }

    engine._store = None
    engine._clear_protection_exposure("BTCUSDT")


@pytest.mark.asyncio
async def test_slow_fuse_skips_dirty_rows_and_counts_only_accepted_reduce_only_closes() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    store = _ExposureStore()
    store.rows = [
        {"symbol": "BAD", "unprotectable_since": "not-a-number"},
        {"symbol": "BTCUSDT", "unprotectable_since": 0},
        {"symbol": "ETHUSDT", "unprotectable_since": 0},
        {"symbol": "XRPUSDT", "unprotectable_since": 0},
    ]
    engine._store = store
    engine._env_mode = SimpleNamespace(value="testnet")
    engine._position_projection = {
        "BTCUSDT": {"signed_quantity": "bad"},
        "ETHUSDT": {"signed_quantity": "1"},
        "XRPUSDT": {"signed_quantity": "-2"},
    }
    attempts: list[tuple[str, OrderSide, float]] = []

    async def close(_pos_id: str, symbol: str, projection: object, **_kwargs: object) -> bool:
        attempts.append((symbol, projection.side, projection.quantity))
        if symbol == "ETHUSDT":
            raise OSError("enqueue unavailable")
        return True

    engine._maybe_emergency_close_unprotectable = close
    assert await engine._run_slow_fuse(now=8_000.0, env={}) == 1
    assert attempts == [
        ("ETHUSDT", OrderSide.BUY, 1.0),
        ("XRPUSDT", OrderSide.SELL, 2.0),
    ]

    store.raise_records = True
    assert await engine._run_slow_fuse(now=8_000.0, env={}) == 0


def test_stuck_marker_removal_and_write_failures_have_explicit_nonfatal_semantics(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    store = _ExposureStore()
    store.raise_records = True
    engine._store = store
    marker = tmp_path / "stuck"
    marker.write_text("stale")
    real_open = builtins.open

    def fail_cleared(path: object, *args: object, **kwargs: object):
        if str(path).endswith("stuck.cleared"):
            raise OSError("tombstone unavailable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_cleared)
    assert engine._update_stuck_marker(now=2_000.0, state_dir=str(tmp_path)) is False
    assert not marker.exists(), "resolved exposure removes the active stuck marker even if tombstone writing fails"

    store.raise_records = False
    store.rows = [{"symbol": "BTCUSDT", "last_reason": "MISSING_SL", "unprotectable_since": 0}]

    def fail_marker(path: object, *args: object, **kwargs: object):
        if str(path).endswith("/stuck"):
            raise OSError("marker unavailable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_marker)
    assert engine._update_stuck_marker(now=2_000.0, state_dir=str(tmp_path)) is True
    assert not marker.exists(), "the return value reports a stuck condition, not file-write success"


class _FlatStore:
    def __init__(self) -> None:
        self.durable_rows: list[Any] = [
            {"status": "CANCELLED", "owner_id": "other", "symbol": "BTCUSDT"},
            {"status": "ACTIVE", "owner_id": "other", "symbol": "FOREIGNUSDT"},
            {
                "protection_id": "pending-sl",
                "position_id": "durable-pos",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "trigger_price": "90",
                "order_price": None,
                "quantity": "1",
                "order_type": "STOP_MARKET",
                "status": "PENDING",
                "stop_type": "ATR_BASED",
                "take_profit_type": None,
                "owner_id": "owner",
                "position_generation": 2,
                "session_id": "session",
                "exchange_order_id": None,
            },
        ]
        self.exposure_rows: list[Any] = []
        self.order_rows: list[Any] = [{"symbol": "BTCUSDT", "status": "FILLED"}]
        self.fill_rows: list[Any] = [
            {
                "fill_event_id": "fill-1",
                "symbol": "BTCUSDT",
                "processing_state": "COMMITTED",
            }
        ]
        self.saved_opening: list[dict[str, Any]] = []
        self.saved_positions: list[tuple[Any, ...]] = []
        self.removed: list[str] = []
        self.saved_protections: list[dict[str, Any]] = []
        self.deleted_records: list[tuple[str, str]] = []
        self.delete_fail_symbols: set[str] = set()
        self.fail_restore_protections = False
        self.fail_records = False
        self.fail_orders = False
        self.fail_fills = False
        self.fail_opening = False
        self.fail_position = False
        self.fail_remove = False
        self.fail_save_protection = False

    def restore_protections(self) -> list[Any]:
        if self.fail_restore_protections:
            raise OSError("protection journal unavailable")
        return list(self.durable_rows)

    def _records(self, _kind: str) -> list[Any]:
        if self.fail_records:
            raise OSError("exposure journal unavailable")
        return list(self.exposure_rows)

    def restore_order_states(self) -> list[Any]:
        if self.fail_orders:
            raise OSError("orders unavailable")
        return list(self.order_rows)

    def restore_fill_events(self) -> list[Any]:
        if self.fail_fills:
            raise OSError("fills unavailable")
        return list(self.fill_rows)

    def save_account_opening_projection(self, **payload: Any) -> None:
        if self.fail_opening:
            raise OSError("opening baseline unavailable")
        self.saved_opening.append(payload)

    def save_position_projection(self, *payload: Any) -> None:
        if self.fail_position:
            raise OSError("position journal unavailable")
        self.saved_positions.append(payload)

    def remove_protection(self, position_id: str) -> None:
        if self.fail_remove:
            raise OSError("protection removal unavailable")
        self.removed.append(position_id)

    def save_protection(self, **payload: Any) -> None:
        if self.fail_save_protection:
            raise OSError("protection status unavailable")
        self.saved_protections.append(payload)

    def _delete_record(self, kind: str, key: str) -> None:
        symbol = key.removeprefix("exposure:")
        if symbol in self.delete_fail_symbols:
            raise OSError("exposure cleanup unavailable")
        self.deleted_records.append((kind, key))


def _flat_engine(store: _FlatStore | None = None) -> tuple[AutonomousEngine, _FlatStore]:
    actual_store = store or _FlatStore()
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = SimpleNamespace(name="TESTNET", value="testnet")
    engine._can_write = True
    engine._producer_only = False
    engine._last_account_at = time.time()
    engine._last_algo_inventory_genuine = True
    engine._protection_owner_id = "owner"
    engine._local_positions: dict[str, Any] = {
        "local-pos": SimpleNamespace(instrument_id="BTCUSDT", position_generation=2)
    }
    engine._protection = SimpleNamespace(all_positions=lambda: engine._local_positions)
    engine._store = actual_store
    engine._position_projection = {
        "BTCUSDT": {
            "signed_quantity": "1",
            "entry_price": "100",
            "position_generation": 2,
        }
    }
    engine._system_facts = SimpleNamespace(
        complete=True,
        positions={"BTCUSDT": SimpleNamespace(amount="1")},
    )
    engine._build_system_reconciliation_facts = lambda: engine._system_facts
    engine._outbox = SimpleNamespace(
        unacked=lambda: [],
        stats=lambda: {"state_counts": {"PENDING": 0, "SENDING": 0, "UNKNOWN": 0, "DEAD_LETTER": 0}},
    )
    engine._pending_fill_retry = set()
    engine._pending_fill_previous_qty = {}
    engine._pending_protection_persist = {}
    engine._pending_protection_retry = set()
    engine._pending_stop_intent = {"other-pos": True}
    engine._active_algo_ids = {"local-pos": {"sl-live"}, "durable-pos": {"sl-pending"}}
    engine._venue_missing_streaks = {"": 1, "sl-live": 1}
    engine._position_entry_times = {"local-pos": 1.0, "durable-pos": 2.0}
    engine._protection_issues = set()
    engine._protection_owner_unknown = False
    engine._last_protection_gap_detail = [
        {"symbol": "BTCUSDT", "reason": "MISSING_SL"},
        {"symbol": "ETHUSDT", "reason": "MISSING_TP"},
    ]
    engine._local_cleanup: list[tuple[str, str]] = []
    engine._cleared_exposures: list[str] = []
    engine._remove_protection_with_cleanup = lambda position_id, symbol: engine._local_cleanup.append(
        (position_id, symbol)
    )
    engine._clear_protection_exposure = lambda symbol: engine._cleared_exposures.append(symbol)
    engine._api_async = _raises(AssertionError("flat convergence must never call a venue write endpoint"))
    return engine, actual_store


def _flat_account(amount: str = "0") -> dict[str, Any]:
    return {
        "totalWalletBalance": "100",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": amount}],
        "updateTime": 123,
    }


def test_validated_account_positions_rejects_ambiguous_rows_and_accepts_finite_unique_amounts() -> None:
    assert AutonomousEngine._validated_account_position_amounts({}) is None
    for positions in (
        ["bad"],
        [{"symbol": "", "positionAmt": "0"}],
        [{"symbol": "BTCUSDT", "positionAmt": "0"}, {"symbol": "btcusdt", "positionAmt": "1"}],
        [{"symbol": "BTCUSDT", "positionAmt": "bad"}],
        [{"symbol": "BTCUSDT", "positionAmt": "NaN"}],
    ):
        assert AutonomousEngine._validated_account_position_amounts({"positions": positions}) is None
    assert AutonomousEngine._validated_account_position_amounts(
        {"positions": [{"symbol": "btcusdt", "positionAmt": "-1.5"}]}
    ) == {"BTCUSDT": engine_module.Decimal("-1.5")}


class _ChangingAmount:
    def __init__(self, second: str) -> None:
        self._values = iter(("1", second))

    @property
    def amount(self) -> str:
        return next(self._values)


@pytest.mark.parametrize(
    "case",
    [
        "write-disallowed",
        "stale-account",
        "missing-wallet",
        "malformed-inventory",
        "invalid-account-positions",
        "local-protection-read-error",
        "missing-store",
        "missing-protection-api",
        "durable-protection-read-error",
        "malformed-projection",
        "invalid-projection-number",
        "nonfinite-projection",
        "blank-local-symbol",
        "invalid-local-generation",
        "negative-local-generation",
        "malformed-durable-row",
        "blank-durable-symbol",
        "invalid-durable-generation",
        "negative-durable-generation",
        "missing-exposure-api",
        "exposure-read-error",
        "malformed-exposure-row",
        "blank-exposure-symbol",
        "invalid-exposure-generation",
        "negative-exposure-generation",
        "missing-system-builder",
        "system-builder-error",
        "incomplete-system-facts",
        "invalid-system-position",
        "no-local-symbols",
        "venue-position-not-flat",
        "malformed-open-order",
        "untyped-open-order",
        "target-open-order",
        "malformed-algo-order",
        "untyped-algo-order",
        "target-algo-order",
        "order-journal-read-error",
        "malformed-order-journal-row",
        "unresolved-local-order",
        "fill-journal-read-error",
        "malformed-fill-journal-row",
        "invalid-fill-semantics",
        "pending-local-fill",
        "missing-outbox-unacked",
        "unacked-command",
        "unacked-read-error",
        "outbox-stats-error",
        "invalid-outbox-stats",
        "invalid-outbox-count",
        "unresolved-outbox-count",
        "pending-fill-retry",
        "pending-protection-persist",
        "pending-protection-retry",
        "pending-stop-intent",
        "nonfinite-replayed-position",
        "invalid-replayed-position-second-read",
        "missing-opening-writer",
    ],
)
def test_flat_convergence_preconditions_fail_closed_before_local_cleanup(case: str) -> None:
    engine, store = _flat_engine()
    account: Any = _flat_account()
    open_orders: Any = []
    algo_inventory: Any = []

    if case == "write-disallowed":
        engine._can_write = False
    elif case == "stale-account":
        engine._last_account_at = 1.0
    elif case == "missing-wallet":
        account = {"positions": []}
    elif case == "malformed-inventory":
        open_orders = None
    elif case == "invalid-account-positions":
        account["positions"] = ["bad"]
    elif case == "local-protection-read-error":
        engine._protection.all_positions = _raises(OSError("projection unavailable"))
    elif case == "missing-store":
        engine._store = None
    elif case == "missing-protection-api":
        store.restore_protections = None  # type: ignore[method-assign]
    elif case == "durable-protection-read-error":
        store.fail_restore_protections = True
    elif case == "malformed-projection":
        engine._position_projection = {"BTCUSDT": "bad"}
    elif case == "invalid-projection-number":
        engine._position_projection["BTCUSDT"]["signed_quantity"] = "bad"
    elif case == "nonfinite-projection":
        engine._position_projection["BTCUSDT"]["signed_quantity"] = "NaN"
    elif case == "blank-local-symbol":
        engine._local_positions["local-pos"].instrument_id = ""
    elif case == "invalid-local-generation":
        engine._local_positions["local-pos"].position_generation = "bad"
    elif case == "negative-local-generation":
        engine._local_positions["local-pos"].position_generation = -1
    elif case == "malformed-durable-row":
        store.durable_rows = ["bad"]
    elif case == "blank-durable-symbol":
        store.durable_rows = [_active_row(symbol="", owner_id="owner")]
    elif case == "invalid-durable-generation":
        store.durable_rows = [_active_row(position_generation="bad")]
    elif case == "negative-durable-generation":
        store.durable_rows = [_active_row(position_generation=-1)]
    elif case == "missing-exposure-api":
        store._records = None  # type: ignore[method-assign]
    elif case == "exposure-read-error":
        store.fail_records = True
    elif case == "malformed-exposure-row":
        store.exposure_rows = ["bad"]
    elif case == "blank-exposure-symbol":
        store.exposure_rows = [{"symbol": "", "position_generation": 1}]
    elif case == "invalid-exposure-generation":
        store.exposure_rows = [{"symbol": "BTCUSDT", "position_generation": "bad"}]
    elif case == "negative-exposure-generation":
        store.exposure_rows = [{"symbol": "BTCUSDT", "position_generation": -1}]
    elif case == "missing-system-builder":
        engine._build_system_reconciliation_facts = None
    elif case == "system-builder-error":
        engine._build_system_reconciliation_facts = _raises(OSError("replay unavailable"))
    elif case == "incomplete-system-facts":
        engine._system_facts.complete = False
    elif case == "invalid-system-position":
        engine._system_facts.positions = None
    elif case == "no-local-symbols":
        engine._position_projection = {}
        engine._local_positions = {}
        store.durable_rows = []
        engine._system_facts.positions = {}
    elif case == "venue-position-not-flat":
        account = _flat_account("1")
    elif case == "malformed-open-order":
        open_orders = ["bad"]
    elif case == "untyped-open-order":
        open_orders = [{"orderId": "", "symbol": ""}]
    elif case == "target-open-order":
        open_orders = [{"orderId": "1", "symbol": "BTCUSDT"}]
    elif case == "malformed-algo-order":
        algo_inventory = ["bad"]
    elif case == "untyped-algo-order":
        algo_inventory = [{"algoId": "", "symbol": ""}]
    elif case == "target-algo-order":
        algo_inventory = [{"algoId": "1", "symbol": "BTCUSDT"}]
    elif case == "order-journal-read-error":
        store.fail_orders = True
    elif case == "malformed-order-journal-row":
        store.order_rows = ["bad"]
    elif case == "unresolved-local-order":
        store.order_rows = [{"symbol": "BTCUSDT", "status": "UNKNOWN"}]
    elif case == "fill-journal-read-error":
        store.fail_fills = True
    elif case == "malformed-fill-journal-row":
        store.fill_rows = ["bad"]
    elif case == "invalid-fill-semantics":
        store.fill_rows = [{"symbol": "", "processing_state": "INVALID"}]
    elif case == "pending-local-fill":
        store.fill_rows = [{"symbol": "BTCUSDT", "processing_state": "PENDING"}]
    elif case == "missing-outbox-unacked":
        engine._outbox = SimpleNamespace(stats=engine._outbox.stats)
    elif case == "unacked-command":
        engine._outbox.unacked = lambda: [object()]
    elif case == "unacked-read-error":
        engine._outbox.unacked = _raises(OSError("outbox unavailable"))
    elif case == "outbox-stats-error":
        engine._outbox.stats = _raises(OSError("stats unavailable"))
    elif case == "invalid-outbox-stats":
        engine._outbox.stats = lambda: {"state_counts": None}
    elif case == "invalid-outbox-count":
        engine._outbox.stats = lambda: {"state_counts": {"UNKNOWN": "bad"}}
    elif case == "unresolved-outbox-count":
        engine._outbox.stats = lambda: {"state_counts": {"UNKNOWN": 1}}
    elif case == "pending-fill-retry":
        engine._pending_fill_retry = {"fill"}
    elif case == "pending-protection-persist":
        engine._pending_protection_persist = {"pos": [object()]}
    elif case == "pending-protection-retry":
        engine._pending_protection_retry = {"durable-pos"}
    elif case == "pending-stop-intent":
        engine._pending_protection_retry = set()
        engine._pending_stop_intent = {"durable-pos": True}
    elif case == "nonfinite-replayed-position":
        engine._system_facts.positions = {"BTCUSDT": _ChangingAmount("NaN")}
    elif case == "invalid-replayed-position-second-read":
        engine._system_facts.positions = {"BTCUSDT": _ChangingAmount("bad")}
    elif case == "missing-opening-writer":
        store.save_account_opening_projection = None  # type: ignore[method-assign]
    else:  # pragma: no cover - parametrization contract
        raise AssertionError(case)

    assert engine._converge_flat_local_positions(account, open_orders, algo_inventory) is False
    assert store.saved_opening == []
    assert store.saved_positions == []
    assert engine._local_cleanup == []


@pytest.mark.parametrize(
    "case",
    [
        "opening-write-failure",
        "position-write-failure",
        "local-cleanup-failure",
        "durable-cleanup-failure",
        "missing-pending-status-writer",
        "pending-status-write-failure",
    ],
)
def test_flat_convergence_persistence_failures_leave_gate_closed_and_auditable(case: str) -> None:
    engine, store = _flat_engine()
    if case == "opening-write-failure":
        store.fail_opening = True
    elif case == "position-write-failure":
        store.fail_position = True
    elif case == "local-cleanup-failure":
        engine._remove_protection_with_cleanup = _raises(OSError("local cleanup unavailable"))
    elif case == "durable-cleanup-failure":
        store.fail_remove = True
    elif case == "missing-pending-status-writer":
        store.save_protection = None  # type: ignore[method-assign]
    elif case == "pending-status-write-failure":
        store.fail_save_protection = True

    assert engine._converge_flat_local_positions(_flat_account(), [], []) is False
    if case != "opening-write-failure":
        assert store.saved_opening and store.saved_opening[0]["complete"] is True
    if case in {
        "position-write-failure",
        "local-cleanup-failure",
        "durable-cleanup-failure",
        "missing-pending-status-writer",
        "pending-status-write-failure",
    }:
        assert engine._protection_owner_unknown is True
        assert "FLAT_RECONCILIATION_CLEANUP_INCOMPLETE" in engine._protection_issues


def test_flat_convergence_success_rebases_only_local_facts_and_clears_stale_safety_state() -> None:
    engine, store = _flat_engine()

    assert engine._converge_flat_local_positions(_flat_account(), [], []) is True

    assert store.saved_opening[0]["source"] == "TESTNET_VENUE_FLAT_RECONCILIATION"
    assert store.saved_opening[0]["positions"] == {}
    assert store.saved_positions[0][0:4] == ("BTCUSDT", "0", "0", 2)
    assert engine._position_projection["BTCUSDT"]["signed_quantity"] == "0"
    assert engine._local_cleanup == [("local-pos", "BTCUSDT")]
    assert store.removed == ["durable-pos"]
    assert store.saved_protections[0]["status"] == "CANCELLED"
    assert engine._cleared_exposures == ["BTCUSDT"]
    assert "local-pos" not in engine._active_algo_ids
    assert engine._pending_stop_intent == {"other-pos": True}
    assert engine._last_protection_gap_detail == [{"symbol": "ETHUSDT", "reason": "MISSING_TP"}]


def _stale_exposure_shell() -> tuple[AutonomousEngine, _FlatStore, dict[str, Any]]:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    store = _FlatStore()
    store.durable_rows = []
    store.exposure_rows = [
        {"payload": {"symbol": "BTCUSDT", "unprotectable_since": 1}},
        {"symbol": "ETHUSDT", "unprotectable_since": 2},
    ]
    engine._env_mode = SimpleNamespace(name="TESTNET", value="testnet")
    engine._can_write = True
    engine._last_account_at = time.time()
    engine._last_algo_inventory_genuine = True
    engine._store = store
    engine._position_projection = {
        "BTCUSDT": {"signed_quantity": "0"},
        "ETHUSDT": {"signed_quantity": "0"},
    }
    engine._protection = SimpleNamespace(all_positions=lambda: {})
    engine._outbox = SimpleNamespace(unacked=lambda: [])
    engine._pending_fill_retry = set()
    engine._pending_fill_previous_qty = {}
    engine._pending_protection_persist = {}
    engine._pending_protection_retry = set()
    engine._pending_stop_intent = {}
    account = {
        "totalWalletBalance": "100",
        "positions": [
            {"symbol": "BTCUSDT", "positionAmt": "0"},
            {"symbol": "ETHUSDT", "positionAmt": "0"},
        ],
    }
    return engine, store, account


@pytest.mark.parametrize(
    "case",
    [
        "write-disallowed",
        "stale-account",
        "missing-wallet",
        "malformed-inventory",
        "inventory-not-genuine",
        "invalid-account-positions",
        "missing-store-delete-api",
        "exposure-read-error",
        "malformed-exposure",
        "blank-exposure-symbol",
        "no-exposures",
        "venue-position-present",
        "malformed-open-order",
        "untyped-open-order",
        "target-open-order",
        "malformed-algo-order",
        "untyped-algo-order",
        "target-algo-order",
        "malformed-projections-container",
        "malformed-projection-row",
        "invalid-projection-quantity",
        "nonfinite-projection-quantity",
        "nonzero-local-projection",
        "local-protection-read-error",
        "local-protection-present",
        "malformed-durable-protection",
        "durable-protection-present",
        "execution-journal-read-error",
        "malformed-order-row",
        "unresolved-order",
        "malformed-fill-row",
        "pending-fill",
        "missing-outbox-unacked",
        "unacked-command",
        "unacked-read-error",
        "pending-process-state",
    ],
)
def test_stale_exposure_cleanup_requires_complete_independent_flat_evidence(case: str) -> None:
    engine, store, account = _stale_exposure_shell()
    open_orders: Any = [{"orderId": "foreign", "symbol": "OTHERUSDT"}]
    algo_inventory: Any = [{"algoId": "foreign", "symbol": "OTHERUSDT"}]

    if case == "write-disallowed":
        engine._can_write = False
    elif case == "stale-account":
        engine._last_account_at = 1.0
    elif case == "missing-wallet":
        account = {"positions": []}
    elif case == "malformed-inventory":
        open_orders = None
    elif case == "inventory-not-genuine":
        engine._last_algo_inventory_genuine = False
    elif case == "invalid-account-positions":
        account["positions"] = ["bad"]
    elif case == "missing-store-delete-api":
        store._delete_record = None  # type: ignore[method-assign]
    elif case == "exposure-read-error":
        store.fail_records = True
    elif case == "malformed-exposure":
        store.exposure_rows = ["bad"]
    elif case == "blank-exposure-symbol":
        store.exposure_rows = [{"symbol": ""}]
    elif case == "no-exposures":
        store.exposure_rows = []
    elif case == "venue-position-present":
        account["positions"][0]["positionAmt"] = "1"
        account["positions"][1]["positionAmt"] = "1"
    elif case == "malformed-open-order":
        open_orders = ["bad"]
    elif case == "untyped-open-order":
        open_orders = [{"orderId": "", "symbol": ""}]
    elif case == "target-open-order":
        store.exposure_rows = [{"symbol": "BTCUSDT"}]
        open_orders = [{"orderId": "1", "symbol": "BTCUSDT"}]
    elif case == "malformed-algo-order":
        algo_inventory = ["bad"]
    elif case == "untyped-algo-order":
        algo_inventory = [{"algoId": "", "symbol": ""}]
    elif case == "target-algo-order":
        store.exposure_rows = [{"symbol": "BTCUSDT"}]
        algo_inventory = [{"algoId": "1", "symbol": "BTCUSDT"}]
    elif case == "malformed-projections-container":
        engine._position_projection = []
    elif case == "malformed-projection-row":
        engine._position_projection["BTCUSDT"] = "bad"
    elif case == "invalid-projection-quantity":
        engine._position_projection["BTCUSDT"]["signed_quantity"] = "bad"
    elif case == "nonfinite-projection-quantity":
        engine._position_projection["BTCUSDT"]["signed_quantity"] = "NaN"
    elif case == "nonzero-local-projection":
        engine._position_projection["BTCUSDT"]["signed_quantity"] = "1"
        engine._position_projection["ETHUSDT"]["signed_quantity"] = "1"
    elif case == "local-protection-read-error":
        engine._protection.all_positions = _raises(OSError("projection unavailable"))
    elif case == "local-protection-present":
        engine._protection.all_positions = lambda: {
            "btc": SimpleNamespace(instrument_id="BTCUSDT"),
            "eth": SimpleNamespace(instrument_id="ETHUSDT"),
        }
    elif case == "malformed-durable-protection":
        store.durable_rows = ["bad"]
    elif case == "durable-protection-present":
        store.durable_rows = [{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}]
    elif case == "execution-journal-read-error":
        store.fail_orders = True
    elif case == "malformed-order-row":
        store.order_rows = ["bad"]
    elif case == "unresolved-order":
        store.order_rows = [
            {"symbol": "BTCUSDT", "status": "UNKNOWN"},
            {"symbol": "ETHUSDT", "status": "NEW"},
        ]
    elif case == "malformed-fill-row":
        store.fill_rows = ["bad"]
    elif case == "pending-fill":
        store.fill_rows = [
            {"symbol": "BTCUSDT", "processing_state": "PENDING"},
            {"symbol": "ETHUSDT", "processing_state": "UNKNOWN"},
        ]
    elif case == "missing-outbox-unacked":
        engine._outbox = SimpleNamespace()
    elif case == "unacked-command":
        engine._outbox.unacked = lambda: [object()]
    elif case == "unacked-read-error":
        engine._outbox.unacked = _raises(OSError("outbox unavailable"))
    elif case == "pending-process-state":
        engine._pending_fill_retry = {"fill"}
    else:  # pragma: no cover - parametrization contract
        raise AssertionError(case)

    assert engine._clear_stale_flat_protection_exposures(account, open_orders, algo_inventory) == set()
    assert store.deleted_records == [], "UNKNOWN or non-flat evidence must preserve every durable exposure marker"


def test_stale_exposure_cleanup_continues_after_one_delete_failure() -> None:
    engine, store, account = _stale_exposure_shell()
    store.delete_fail_symbols = {"ETHUSDT"}

    assert engine._clear_stale_flat_protection_exposures(account, [], []) == {"BTCUSDT"}
    assert store.deleted_records == [("protection_exposure", "exposure:BTCUSDT")]
