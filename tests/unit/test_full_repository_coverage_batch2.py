"""Behavior-backed coverage for the next repository boundary batch."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_control import truth as truth_module
from beidou_control.plane import ControlAction, ControlPlane, RejectionRecord
from beidou_control.truth import TradingEligibility, TruthSnapshot
from beidou_exchange.account_discovery.checker import (
    AccountCapabilityChecker,
    AccountQueryResult,
    AccountQueryStatus,
)
from beidou_exchange.core.rule_snapshot import InstrumentRuleSnapshot
from beidou_observability.monitoring.repository import MonitoringRepository
from beidou_research.data.backfill import _to_open_time_ms, backfill_all, backfill_symbol
from beidou_research.data.kline_store import KlineStore
from beidou_research.mining.generators.symbolic_gp import GPConfig, SymbolicGPGenerator
from beidou_safety.execution import OrderIntent
from beidou_safety.execution.command_aggregate import ChildCommandState, ExecutionChildCommand
from beidou_safety.execution.intent import IntentOutbox
from beidou_shared.types import (
    AccountId,
    AccountRef,
    InstrumentId,
    OrderSide,
    OrderType,
    Quantity,
    ResultStatus,
    VenueId,
)


def _intent(intent_id: str) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("coverage")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="1"),
        client_order_id=f"client-{intent_id}",
        idempotency_key=f"idem-{intent_id}",
    )


def _child(intent_id: str) -> ExecutionChildCommand:
    return ExecutionChildCommand.create(
        parent_intent_id=intent_id,
        sequence=0,
        symbol="BTCUSDT",
        side="BUY",
        quantity="1",
        order_type="MARKET",
        time_in_force="GTC",
        client_order_id=f"child-{intent_id}",
        rule_snapshot_hash="rules-v1",
    )


def _fresh_truth(**overrides: object) -> TruthSnapshot:
    now = datetime.now(timezone.utc).timestamp()
    values: dict[str, object] = {
        "snapshot_id": "coverage-snapshot",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "market_hash": "market",
        "account_hash": "account",
        "order_hash": "order",
        "position_hash": "position",
        "ledger_hash": "ledger",
        "reconciliation_hash": "reconciliation",
        "protection_hash": "protection",
        "risk_hash": "risk",
        "config_hash": "config",
        "policy_hash": "policy",
        "market_freshness": now,
        "account_freshness": now,
        "order_freshness": now,
        "position_freshness": now,
        "ledger_freshness": now,
        "reconciliation_freshness": now,
        "protection_freshness": now,
        "risk_freshness": now,
        "config_freshness": now,
        "policy_freshness": now,
        "reconciliation_status": "MATCHED",
        "protection_status": "ACTIVE",
        "risk_status": "NORMAL",
    }
    values.update(overrides)
    return TruthSnapshot(**values)


def test_account_capability_classification_report_and_owner_boundaries() -> None:
    checker = AccountCapabilityChecker()
    assert checker.classify_result(ResultStatus.ERROR, None, "other failure") is AccountQueryStatus.ERROR
    assert checker.classify_result(ResultStatus.UNSUPPORTED, None) is AccountQueryStatus.ERROR

    report = checker.generate_report(
        VenueId("BINANCE"),
        AccountId("coverage"),
        AccountQueryResult(AccountQueryStatus.EMPTY),
        AccountQueryResult(AccountQueryStatus.ERROR),
        AccountQueryResult(AccountQueryStatus.EMPTY),
        AccountQueryResult(AccountQueryStatus.UNKNOWN),
        AccountQueryResult(AccountQueryStatus.EMPTY),
    )
    assert not report.is_safe_for_trading()
    assert report.trading_blockers() == ["POSITION_QUERY_FAILED", "TRADE_PERMISSION_UNKNOWN"]

    first_unknown = SimpleNamespace(
        is_safe_for_trading=lambda: True,
        can_read_balances=SimpleNamespace(should_fail_closed=lambda: True),
        can_read_positions=SimpleNamespace(should_fail_closed=lambda: False),
        can_trade=SimpleNamespace(should_fail_closed=lambda: False),
    )
    assert checker.owner_disconnected_check(first_unknown) is ResultStatus.UNKNOWN
    all_known = SimpleNamespace(
        is_safe_for_trading=lambda: True,
        can_read_balances=SimpleNamespace(should_fail_closed=lambda: False),
        can_read_positions=SimpleNamespace(should_fail_closed=lambda: False),
        can_trade=SimpleNamespace(should_fail_closed=lambda: False),
    )
    assert checker.owner_disconnected_check(all_known) is ResultStatus.SUCCESS


def test_control_plane_cas_persistence_authorization_and_eligibility_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import beidou_control.plane as plane_module

    cp = ControlPlane()
    with pytest.raises(RuntimeError, match="CAS rejected"):
        cp.execute_action(ControlAction.NO_NEW_RISK, expected_version=99)
    cp._rejections.append(RejectionRecord("now", "hash", "intent", "NO_NEW_RISK", 0, "INCREASE", "BLOCKED", "blocked"))
    assert cp.recent_rejections(1)[0]["intent_id"] == "intent"

    state_path = tmp_path / "control-state.json"
    monkeypatch.setattr(plane_module, "_STATE_FILE", str(state_path))
    assert cp.restore_state() is False
    state_path.write_text(json.dumps({"version": 0, "control_action": "NO_NEW_RISK"}))
    assert cp.restore_state() is False
    state_path.write_text("{")
    assert cp.restore_state() is False

    original_derive = truth_module.derive_eligibility
    monkeypatch.setattr(truth_module, "derive_eligibility", lambda _snapshot: TradingEligibility.ELIGIBLE)
    stale = SimpleNamespace(is_stale=lambda **_kwargs: True, has_unknown_components=lambda: [])
    assert cp.authorize_resume(stale) == (False, "RESUME rejected: TruthSnapshot is stale (>300s)")
    unknown = SimpleNamespace(is_stale=lambda **_kwargs: False, has_unknown_components=lambda: ["risk"])
    allowed, reason = cp.authorize_resume(unknown)
    assert not allowed and "UNKNOWN components: risk" in reason
    monkeypatch.setattr(truth_module, "derive_eligibility", original_derive)
    assert cp.evaluate_eligibility(_fresh_truth()) is TradingEligibility.ELIGIBLE


def test_rule_snapshot_invalid_quantization_and_staleness_edges() -> None:
    snapshot = InstrumentRuleSnapshot(
        symbol="BTCUSDT",
        tick_size="0.1",
        step_size="0.001",
        min_qty="0.001",
        min_notional="20",
        price_precision=1,
        qty_precision=3,
        observed_at=datetime.now(timezone.utc).isoformat(),
    )
    with pytest.raises(ValueError, match="finite and positive"):
        InstrumentRuleSnapshot._precision("0")
    with pytest.raises(ValueError, match="quantity or step"):
        snapshot.quantize_quantity("0")
    with pytest.raises(ValueError, match="price or tick"):
        snapshot.quantize_price("0", side="BUY")
    with pytest.raises(ValueError, match="side"):
        snapshot.quantize_price("1", side="HOLD")
    with pytest.raises(ValueError, match="rounds to zero"):
        snapshot.quantize_quantity("0.0001")
    with pytest.raises(ValueError, match="rounds to zero"):
        snapshot.quantize_price("0.01", side="BUY")
    assert InstrumentRuleSnapshot(symbol="BTCUSDT").is_stale
    assert InstrumentRuleSnapshot(symbol="BTCUSDT", observed_at="not-a-timestamp").is_stale


def test_monitoring_repository_singleton_context_and_empty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MonitoringRepository, "_instance", None)
    path = str(tmp_path / "monitoring.db")
    repository = MonitoringRepository.get_instance(path)
    assert MonitoringRepository.get_instance(path) is repository
    with MonitoringRepository(str(tmp_path / "context.db")) as context:
        assert context is not None
    repository._get_conn().execute("DELETE FROM monitor_frequency_state")
    repository._get_conn().commit()
    assert repository.get_frequency_state().level.value == "ALERT"
    assert repository.can_disable_check("missing-check", "testnet") == (True, "")
    repository.close()


class _BackfillFeed:
    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        self.pages = pages

    def fetch_klines(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return self.pages.pop(0) if self.pages else []


def _kline(open_time: int) -> dict[str, object]:
    return {
        "open_time": open_time,
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100.5,
        "volume": 3,
        "is_closed": True,
    }


def test_backfill_integer_progress_pause_and_all_symbols(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _to_open_time_ms(123) == 123
    first_page = [_kline(index * 60_000) for index in range(1000)]
    sleeps: list[float] = []
    progress: list[str] = []
    monkeypatch.setattr("beidou_research.data.backfill.time.sleep", sleeps.append)
    store = KlineStore(root=str(tmp_path / "klines"))
    report = backfill_symbol(
        _BackfillFeed([first_page, []]),
        store,
        "BTCUSDT",
        "1m",
        start_ms=0,
        end_ms=1000 * 60_000 + 1,
        page_pause_seconds=0.25,
        on_progress=progress.append,
    )
    assert report["pages"] == 1 and report["rows"] == 1000
    assert progress and sleeps == [0.25]

    reports = backfill_all(
        _BackfillFeed([[_kline(1)], [_kline(2)]]),
        KlineStore(root=str(tmp_path / "all")),
        ["BTCUSDT", "ETHUSDT"],
        ["1m"],
        start_ms=0,
        end_ms=10,
        max_pages=1,
        page_pause_seconds=0,
    )
    assert len(reports) == 2 and {item["symbol"] for item in reports} == {"BTCUSDT", "ETHUSDT"}


def test_symbolic_gp_validation_empty_population_and_failed_generation() -> None:
    for config in (
        GPConfig(population_size=0),
        GPConfig(max_tree_depth=0),
        GPConfig(max_node_count=0),
        GPConfig(population_size=2, max_search_budget=1),
    ):
        with pytest.raises(ValueError):
            SymbolicGPGenerator(config).initialize_population(["close"])

    generator = SymbolicGPGenerator(GPConfig(population_size=2, max_search_budget=2))
    with pytest.raises(ValueError, match="empty population"):
        generator.select_parent([])
    with pytest.raises(ValueError, match="not initialized"):
        generator.evolve_one_generation(lambda _hash: {"sharpe": 1.0})
    with pytest.raises(ValueError, match="negative"):
        SymbolicGPGenerator(GPConfig(generations=-1)).run(["close"], lambda _hash: {"sharpe": 1.0})

    failed = SymbolicGPGenerator(GPConfig(population_size=2, generations=1, max_search_budget=4))
    failed.initialize_population(["close"])
    failed.evolve_one_generation = lambda _evaluator: (_ for _ in ()).throw(RuntimeError("generation failed"))  # type: ignore[method-assign]
    assert failed.run(["close"], lambda _hash: {"sharpe": 1.0, "pareto_rank": 0.0})

    budget = SymbolicGPGenerator(GPConfig(population_size=2, generations=1, max_search_budget=2))
    result = budget.run(["close"], lambda _hash: {"sharpe": 1.0, "pareto_rank": 0.0})
    assert result and budget.get_search_statistics()["seen_hashes"] == 2


def test_intent_outbox_remaining_memory_and_sqlite_fail_closed_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = IntentOutbox(str(tmp_path / "missing-plan.db"))
    with pytest.raises(ValueError, match="EXECUTION_PLAN_NOT_FOUND"):
        db.transition_execution_child("missing", 0, ChildCommandState.SENDING, event_id="missing")

    memory = IntentOutbox()
    intent = _intent("memory-coverage")
    memory.commit(intent)
    assert memory.claim("coverage") is intent
    memory.persist_execution_plan(intent.intent_id, [_child(intent.intent_id)])
    assert memory.inflight_signed_quantity("BTCUSDT") == 1
    for index in range(21):
        memory.dead_letter(f"dead-{index}", "coverage")
    assert len(memory.dead_letter_ids) == 20
    assert IntentOutbox().restore_pending_approvals() == []

    class _BrokenPath:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def mkdir(self, **_kwargs: object) -> None:
            raise OSError("evidence filesystem unavailable")

    grooming = IntentOutbox()
    grooming._MAX_OUTBOX_SIZE = 2
    grooming.commit(_intent("groom-1"))
    grooming.commit(_intent("groom-2"))
    with monkeypatch.context() as path_patch:
        path_patch.setattr("beidou_safety.execution.intent.Path", _BrokenPath)
        grooming.commit(_intent("groom-3"))
    assert grooming.dead_letter_count == 2

    processed = IntentOutbox()
    processed_intent = _intent("processed")
    processed.commit(processed_intent)
    processed._processed.add(processed_intent.intent_id)
    assert processed.claim("coverage") is None

    db_claim = IntentOutbox(str(tmp_path / "claim-rowcount.db"))
    claim_intent = _intent("rowcount")
    db_claim.commit(claim_intent)
    real_connect = db_claim._connect

    class _CursorProxy:
        def __init__(self, cursor: object, rowcount: int | None = None) -> None:
            self._cursor = cursor
            self.rowcount = rowcount if rowcount is not None else getattr(cursor, "rowcount", -1)

        def __getattr__(self, name: str) -> object:
            return getattr(self._cursor, name)

    class _ConnectionProxy:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def __enter__(self) -> "_ConnectionProxy":
            self._connection.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._connection.__exit__(*args)

        def close(self) -> None:
            self._connection.close()

        def execute(self, sql: str, params: object = ()) -> object:
            cursor = self._connection.execute(sql, params)
            if sql.lstrip().upper().startswith("UPDATE INTENT_OUTBOX SET STATE"):
                return _CursorProxy(cursor, rowcount=0)
            return cursor

    monkeypatch.setattr(db_claim, "_connect", lambda: _ConnectionProxy(real_connect()))
    assert db_claim.claim("coverage") is None
