"""Behavior-backed coverage for nearline and offline engine boundaries."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import beidou_core.engine as engine_module
from beidou_control.plane import ControlAction
from beidou_core.engine import AutonomousEngine
from beidou_core.health import HealthState
from beidou_lifecycle.lifecycle import DegradationLevel
from beidou_shared.types import OrderSide, RiskDecision, StrategyId
from beidou_strategy.alpha import SignalDirection
from beidou_strategy.alpha.signal_fusion import SignalFuser
from beidou_strategy.risk.manager import StrategyRiskLevel


def _nearline_engine(
    *,
    features: dict | None = None,
    kernel_result: object | None = None,
    active_symbols: list[str] | None = None,
    env: str = "paper",
    positions: dict | None = None,
) -> AutonomousEngine:
    """Build a complete in-process nearline authority with exchange writes disabled."""

    engine = AutonomousEngine.__new__(AutonomousEngine)
    now = datetime.now(timezone.utc)
    feature_payload = features or {
        "symbol": "BTCUSDT",
        "close": 100.0,
        "bar_is_closed": True,
        "bar_open_time": now - timedelta(minutes=2),
        "bar_close_time": now - timedelta(minutes=1),
        "bar_available_at": now - timedelta(minutes=1),
        "n_candles": 20,
        "ann_volatility": 0.1,
        "spread_bps": 2.0,
        "atr_pct": 2.0,
    }
    risk_state = SimpleNamespace(
        risk_level=StrategyRiskLevel.NORMAL,
        active_circuit_breakers=[],
        current_drawdown_pct=0.0,
        daily_pnl=0.0,
        peak_equity=10_000.0,
    )
    budget = SimpleNamespace(
        compute_position_size=lambda *_args: 10.0,
    )
    proposal = SimpleNamespace(side=OrderSide.BUY, strength=0.8, confidence=0.9)
    if kernel_result is None:
        kernel_result = {"kernel": "typed_graph", "proposal": proposal}

    class Outbox:
        def __init__(self) -> None:
            self._outbox: list[object] = []
            self._processed: set[str] = set()
            self._inbox: dict[str, object] = {}
            self.duplicate_probe = lambda: 0
            self.inflight_probe = lambda _symbol: 0

        def inflight_signed_quantity(self, symbol: str):
            return self.inflight_probe(symbol)

        def duplicate_order_count_24h(self):
            return self.duplicate_probe()

        def commit(self, intent: object) -> None:
            self._outbox.append(intent)

    outbox = Outbox()
    engine._strategy_risk = SimpleNamespace(
        get_state=lambda _sid: risk_state,
        is_trading_allowed=lambda _sid: True,
        get_budget=lambda _sid: budget,
    )
    engine._autopilot_strategy_id = StrategyId("autopilot")
    engine._lifecycle = SimpleNamespace(get_degradation_level=lambda: SimpleNamespace(value="ACTIVE"))
    engine._control = SimpleNamespace(
        get_status=lambda: ControlAction.RESUME,
        should_accept=lambda _intent: True,
    )
    engine._env_mode = SimpleNamespace(value=env)
    engine._can_write = False
    engine._can_simulate = False
    engine._can_trade = True
    engine._risk_can_withdraw = False
    engine._running = True
    engine._tick_count = 10
    engine._error_count = 0
    engine._last_realtime = time.time()
    engine._last_realtime_mono = time.monotonic()
    engine._last_stale_resolve_mono = time.monotonic()
    engine._last_order_state_resolve_mono = time.monotonic()
    engine._last_market_state = SimpleNamespace(state_hash="market-hash")
    engine._last_account = {
        "totalWalletBalance": "10000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "0"}],
    }
    engine._last_prices = {"BTCUSDT": 100.0}
    engine._last_reconciliation_result = SimpleNamespace(checked_at=now, matched=True)
    engine._last_risk_fact_at = 0.0
    engine._symbols = ["BTCUSDT"]
    engine._policy_id_active = None
    engine._policy_version = None
    engine._policy_params = {}
    engine._policy_error = None
    engine._position_entry_times = {}
    engine._active_order_ids = set()
    engine._loss_count = 0
    engine._account = None
    engine._protection_owner_unknown = False
    engine._protection = SimpleNamespace(
        all_positions=lambda: positions or {},
        position_count=lambda: len(positions or {}),
    )
    engine._trading_pool = SimpleNamespace(
        active_instruments=lambda: list(active_symbols if active_symbols is not None else ["BTCUSDT"]),
        is_tradable=lambda _symbol: True,
    )

    async def get_features(_symbol: str, timeframe: str, _limit: int):
        return feature_payload if timeframe == "1m" else {}

    engine._feed = SimpleNamespace(async_get_kline_features=get_features)
    engine._advance_factor_bar = lambda *_args: True
    engine._store_factor_predictions = lambda *_args: None
    engine._estimate_market_state = lambda *_args, **_kwargs: {
        "direction": "UP",
        "stress": "NORMAL",
        "quality": "RELIABLE",
    }
    engine._strategy_kernel = SimpleNamespace(
        evaluate=lambda _context: asyncio.sleep(0, result=kernel_result),
    )
    engine._symbol_precision = {"BTCUSDT": {"min_quantity": 0.001, "quantity": 3, "min_notional": 5.0}}
    engine._settings = SimpleNamespace(
        production=SimpleNamespace(
            max_leverage=3.0,
            max_concentration_pct=100.0,
            max_drawdown_pct=20.0,
            max_daily_loss_pct=5.0,
            max_consecutive_losses=5,
            min_sharpe_rolling=-10.0,
            max_total_leverage=3.0,
        )
    )
    engine._drift_detector = SimpleNamespace(is_calibrated=lambda: False, _baseline={})
    engine._outbox = outbox
    engine._optimizer = SimpleNamespace(
        resolve_conflicts=lambda targets: (targets, 0),
        allocate_capital=lambda _ids, _total: {StrategyId("autopilot"): SimpleNamespace(amount="1000")},
    )
    engine._cost_model = SimpleNamespace(
        set_fee_tier=lambda *_args: None,
        estimate_order=lambda *_args, **_kwargs: SimpleNamespace(total_fee_bps=1.0),
    )
    engine._fuser = SignalFuser()
    engine._venue_exact_quantity = lambda _symbol, amount, **_kwargs: amount
    engine._approval = SimpleNamespace(
        issue_for_approved_risk=lambda *_args, **_kwargs: "signature",
        verify=lambda *_args, **_kwargs: asyncio.sleep(0, result=True),
    )
    engine._risk_sm = SimpleNamespace(approve_if_verified=lambda *_args, **_kwargs: RiskDecision.APPROVED)
    engine._pre_risk = SimpleNamespace(
        check=lambda _context: asyncio.sleep(0, result=[SimpleNamespace(decision=RiskDecision.APPROVED, reason="ok")])
    )

    async def full_evaluate(*_args, **_kwargs):
        return [SimpleNamespace(decision=RiskDecision.APPROVED, rule_level=SimpleNamespace(value="R0"), reason="ok")]

    engine._risk_engine = SimpleNamespace(full_evaluate=full_evaluate)
    engine._post_risk = SimpleNamespace(record_violation=lambda *_args: None, _violations=[])
    engine._apply_fee_tier = lambda *_args: None
    engine._capital_budget_amount = lambda _balance: SimpleNamespace(amount="1000", currency="USDT")
    engine._check_post_risk_safety = lambda *_args: None
    engine._retry_missing_protections = lambda *_args: asyncio.sleep(0)
    engine._cleanup_excess_orders = lambda: asyncio.sleep(0)
    engine._cancel_algo_orders = lambda *_args: asyncio.sleep(0)
    engine._remove_protection_with_cleanup = lambda *_args: None
    engine._sync_venue_leverage = lambda *_args: asyncio.sleep(0, result=True)
    engine._fresh_matched_reconciliation = lambda **_kwargs: True
    engine._recently_matched_reconciliation = lambda **_kwargs: True
    engine._check_liveness = lambda: HealthState.HEALTHY
    engine._realtime_age_seconds = lambda: 0.0
    return engine


def _patch_risk_rules(monkeypatch) -> None:
    monkeypatch.setattr(
        engine_module.RiskRuleRegistry,
        "evaluate_all",
        classmethod(lambda _cls, _ctx: {f"R{i}": engine_module.RuleDecision.PASS for i in range(11)}),
    )
    monkeypatch.setattr(engine_module.RiskRuleRegistry, "is_approved", classmethod(lambda _cls, _results: True))


@pytest.mark.asyncio
async def test_nearline_resolution_ghost_cleanup_and_pool_gates(monkeypatch) -> None:
    _patch_risk_rules(monkeypatch)

    engine = _nearline_engine(active_symbols=[])
    engine._tick_count = 20
    engine._last_stale_resolve_mono = 0.0
    engine._last_order_state_resolve_mono = 0.0
    engine._resolve_stale_execution_commands = lambda: (_ for _ in ()).throw(RuntimeError("stale"))
    engine._resolve_stale_order_states = lambda: (_ for _ in ()).throw(RuntimeError("orders"))
    calls: list[tuple[str, str]] = []
    engine._last_account = {
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "bad"}, {"symbol": "ETHUSDT", "positionAmt": "1"}]
    }
    engine._protection = SimpleNamespace(
        all_positions=lambda: {"ghost": SimpleNamespace(instrument_id="BTCUSDT")},
        position_count=lambda: 1,
    )
    engine._ghost_absence_count = {"BTCUSDT": 2}
    engine._remove_protection_with_cleanup = lambda pid, symbol: calls.append(("remove", f"{pid}:{symbol}"))
    engine._cancel_algo_orders = lambda pid, symbol: asyncio.sleep(
        0, result=calls.append(("cancel", f"{pid}:{symbol}"))
    )
    retry_symbols: list[set[str]] = []
    engine._retry_missing_protections = lambda symbols: asyncio.sleep(0, result=retry_symbols.append(set(symbols)))
    await AutonomousEngine._nearline_tick(engine)
    assert calls == [("remove", "ghost:BTCUSDT"), ("cancel", "ghost:BTCUSDT")]
    assert retry_symbols == [{"ETHUSDT"}]
    assert engine._last_risk_fact_at > 0

    reappeared = _nearline_engine(active_symbols=[])
    reappeared._ghost_absence_count = {"BTCUSDT": 2}
    reappeared._protection = SimpleNamespace(
        all_positions=lambda: {"pos": SimpleNamespace(instrument_id="BTCUSDT")},
        position_count=lambda: 1,
    )
    reappeared._last_account = {
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}],
    }
    await AutonomousEngine._nearline_tick(reappeared)
    assert reappeared._ghost_absence_count == {}

    fallback = _nearline_engine(active_symbols=[])
    fallback._control.get_status = lambda: ControlAction.NO_NEW_RISK
    fallback._fresh_matched_reconciliation = lambda **_kwargs: False
    fallback._last_account = {
        "positions": [{"symbol": "ETHUSDT", "positionAmt": "bad"}, {"symbol": "ETHUSDT", "positionAmt": "1"}]
    }
    fallback_symbols: list[set[str]] = []
    fallback._retry_missing_protections = lambda symbols: asyncio.sleep(0, result=fallback_symbols.append(set(symbols)))
    await AutonomousEngine._nearline_tick(fallback)
    assert fallback_symbols == [{"ETHUSDT"}]

    testnet = _nearline_engine(env="testnet", active_symbols=[f"S{i}" for i in range(11)])
    testnet._feed.async_get_kline_features = lambda *_args: asyncio.sleep(0, result={})
    await AutonomousEngine._nearline_tick(testnet)
    assert 0 <= testnet._nearline_batch_idx < 11

    degraded = _nearline_engine(active_symbols=[])
    degraded._lifecycle.get_degradation_level = lambda: DegradationLevel.EXIT_ONLY
    await AutonomousEngine._nearline_tick(degraded)


@pytest.mark.asyncio
async def test_nearline_timeframe_and_kernel_boundaries(monkeypatch) -> None:
    _patch_risk_rules(monkeypatch)
    now = datetime.now(timezone.utc)
    valid = {
        "close": 100.0,
        "bar_is_closed": True,
        "bar_open_time": now - timedelta(minutes=2),
        "bar_close_time": now - timedelta(minutes=1),
        "bar_available_at": now - timedelta(minutes=1),
        "n_candles": 20,
        "ann_volatility": 0.1,
        "spread_bps": 2.0,
        "atr_pct": 2.0,
    }

    failing_position = SimpleNamespace(
        instrument_id="BTCUSDT",
        update_price_extremes=lambda _close: (_ for _ in ()).throw(RuntimeError("position")),
        entry_price=90.0,
        side=OrderSide.BUY,
    )
    engine = _nearline_engine(features=valid, positions={"p": failing_position})
    engine._advance_factor_bar = lambda *_args: False
    await AutonomousEngine._nearline_tick(engine)

    stress = _nearline_engine(features=valid)
    stress._estimate_market_state = lambda *_args, **_kwargs: {"direction": "UP", "stress": "HIGH"}
    await AutonomousEngine._nearline_tick(stress)

    low_candles = _nearline_engine(features={**valid, "n_candles": 9})
    await AutonomousEngine._nearline_tick(low_candles)

    non_dict = _nearline_engine(features=valid, kernel_result=[])
    await AutonomousEngine._nearline_tick(non_dict)

    exit_signal = SimpleNamespace(direction=SignalDirection.SHORT, instrument_id="BTCUSDT")
    exits = _nearline_engine(
        features=valid,
        kernel_result={"kernel": "typed_graph", "proposal": None, "exit_signals": [exit_signal]},
    )
    await AutonomousEngine._nearline_tick(exits)
    assert exits._exit_signal_audit

    dag_signal = SimpleNamespace(direction=SignalDirection.LONG, strength=0.7, confidence=0.8)
    dag = _nearline_engine(features=valid, kernel_result={"signals": [dag_signal]})
    dag._last_account = {
        "totalWalletBalance": "10000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "0"}],
    }
    await AutonomousEngine._nearline_tick(dag)
    assert len(dag._outbox._outbox) == 1

    side_unknown = _nearline_engine(
        features=valid,
        kernel_result={"kernel": "typed_graph", "proposal": SimpleNamespace(side=None, strength=0.9, confidence=0.9)},
    )
    await AutonomousEngine._nearline_tick(side_unknown)

    position_info = _nearline_engine(
        features=valid,
        kernel_result={},
        positions={
            "p": SimpleNamespace(
                instrument_id="BTCUSDT",
                entry_price=90.0,
                side=OrderSide.BUY,
            )
        },
    )
    await AutonomousEngine._nearline_tick(position_info)

    short_position = _nearline_engine(
        features=valid,
        positions={
            "p": SimpleNamespace(
                instrument_id="BTCUSDT",
                entry_price=110.0,
                side=OrderSide.SELL,
            )
        },
    )
    await AutonomousEngine._nearline_tick(short_position)

    kernel_error = _nearline_engine(features=valid)

    async def fail_kernel(_context):
        raise RuntimeError("kernel")

    kernel_error._strategy_kernel.evaluate = fail_kernel
    await AutonomousEngine._nearline_tick(kernel_error)


@pytest.mark.asyncio
async def test_nearline_sizing_and_execution_fail_closed_boundaries(monkeypatch) -> None:
    _patch_risk_rules(monkeypatch)
    now = datetime.now(timezone.utc)
    valid = {
        "close": 100.0,
        "bar_is_closed": True,
        "bar_open_time": now - timedelta(minutes=2),
        "bar_close_time": now - timedelta(minutes=1),
        "bar_available_at": now - timedelta(minutes=1),
        "n_candles": 20,
        "ann_volatility": 0.1,
        "spread_bps": 2.0,
        "atr_pct": 2.0,
    }

    def run_engine(**kwargs):
        kwargs.setdefault("features", valid)
        return _nearline_engine(**kwargs)

    missing_balance = run_engine()
    missing_balance._last_account = {"positions": []}
    missing_balance._strategy_kernel = SimpleNamespace(
        evaluate=lambda _context: asyncio.sleep(
            0,
            result={
                "kernel": "typed_graph",
                "proposal": SimpleNamespace(side=OrderSide.BUY, strength=0.8, confidence=0.8),
            },
        )
    )
    await AutonomousEngine._nearline_tick(missing_balance)

    missing_features = run_engine(features={**valid, "atr_pct": None})
    await AutonomousEngine._nearline_tick(missing_features)

    budget_unknown = run_engine()
    budget_unknown._strategy_risk.get_budget = lambda _sid: None
    await AutonomousEngine._nearline_tick(budget_unknown)

    precision_invalid = run_engine()
    precision_invalid._symbol_precision = {"BTCUSDT": {"min_quantity": 0, "quantity": "bad", "min_notional": 5}}
    await AutonomousEngine._nearline_tick(precision_invalid)

    min_qty_unknown = run_engine()
    min_qty_unknown._symbol_precision = {"BTCUSDT": {"min_quantity": 0, "min_notional": 5}}
    await AutonomousEngine._nearline_tick(min_qty_unknown)

    below_qty = run_engine()
    below_qty._strategy_risk.get_budget = lambda _sid: SimpleNamespace(compute_position_size=lambda *_args: 0.00001)
    await AutonomousEngine._nearline_tick(below_qty)

    min_notional_unknown = run_engine()
    min_notional_unknown._symbol_precision = {"BTCUSDT": {"min_quantity": 0.001}}
    await AutonomousEngine._nearline_tick(min_notional_unknown)

    below_notional = run_engine()
    below_notional._strategy_risk.get_budget = lambda _sid: SimpleNamespace(compute_position_size=lambda *_args: 0.01)
    below_notional._symbol_precision = {"BTCUSDT": {"min_quantity": 0.000001, "min_notional": 1000}}
    await AutonomousEngine._nearline_tick(below_notional)

    no_action = run_engine(
        kernel_result={"signals": [SimpleNamespace(direction=SignalDirection.NO_ACTION, strength=0.8, confidence=0.8)]}
    )
    no_action._fuser = SimpleNamespace(
        fuse=lambda _signals: SimpleNamespace(
            direction=SignalDirection.NO_ACTION,
            strength=0.8,
            confidence=0.8,
            conflict_detected=False,
        )
    )
    await AutonomousEngine._nearline_tick(no_action)

    pool_blocked = run_engine()
    pool_blocked._trading_pool.is_tradable = lambda _symbol: False
    await AutonomousEngine._nearline_tick(pool_blocked)

    leverage_error = run_engine()
    leverage_error._sync_venue_leverage = lambda *_args: (_ for _ in ()).throw(RuntimeError("leverage"))
    await AutonomousEngine._nearline_tick(leverage_error)


@pytest.mark.asyncio
async def test_nearline_execution_gate_matrix(monkeypatch) -> None:
    _patch_risk_rules(monkeypatch)
    now = datetime.now(timezone.utc)
    valid = {
        "close": 100.0,
        "bar_is_closed": True,
        "bar_open_time": now - timedelta(minutes=2),
        "bar_close_time": now - timedelta(minutes=1),
        "bar_available_at": now - timedelta(minutes=1),
        "n_candles": 20,
        "ann_volatility": 0.1,
        "spread_bps": 2.0,
        "atr_pct": 2.0,
    }

    def make(**kwargs):
        kwargs.setdefault("features", valid)
        return _nearline_engine(**kwargs)

    duplicate = make()
    duplicate._outbox.duplicate_probe = lambda: (_ for _ in ()).throw(RuntimeError("duplicate fact"))
    await AutonomousEngine._nearline_tick(duplicate)

    spread_features = dict(valid)
    spread_unknown = make(features=spread_features)
    spread_unknown._strategy_kernel = SimpleNamespace(
        evaluate=lambda _context: asyncio.sleep(
            0,
            result={
                "kernel": "typed_graph",
                "proposal": SimpleNamespace(side=OrderSide.BUY, strength=0.8, confidence=0.8),
            },
        )
    )
    spread_unknown._optimizer.resolve_conflicts = lambda targets: (
        spread_features.__setitem__("spread_bps", None) or (targets, 0)
    )
    await AutonomousEngine._nearline_tick(spread_unknown)

    position_unknown = make()
    position_unknown._last_account = {
        "totalWalletBalance": "10000",
        "positions": [
            {"symbol": "BTCUSDT", "positionAmt": "0"},
            {"symbol": "BTCUSDT", "positionAmt": "0"},
        ],
    }
    await AutonomousEngine._nearline_tick(position_unknown)

    inflight_unknown = make()
    inflight_unknown._outbox.inflight_probe = lambda _symbol: (_ for _ in ()).throw(RuntimeError("inflight"))
    await AutonomousEngine._nearline_tick(inflight_unknown)

    target_satisfied = make()
    target_quantity = 1.0 / engine_module.adaptive_position_pct(0.8, 0.1, 2.0)
    target_satisfied._strategy_risk.get_budget = lambda _sid: SimpleNamespace(
        compute_position_size=lambda *_args: target_quantity
    )
    target_satisfied._last_account = {
        "totalWalletBalance": "10000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}],
    }

    def satisfy_target(targets):
        target_satisfied._signed_position_from_account = lambda *_args: Decimal(str(targets[0].target_quantity.amount))
        return targets, 0

    target_satisfied._optimizer.resolve_conflicts = satisfy_target
    await AutonomousEngine._nearline_tick(target_satisfied)

    delta_min_unknown = make()

    def set_unknown_delta_rules(targets):
        delta_min_unknown._symbol_precision = {"BTCUSDT": {"min_quantity": 0, "min_notional": 0}}
        return targets, 0

    delta_min_unknown._optimizer.resolve_conflicts = set_unknown_delta_rules
    await AutonomousEngine._nearline_tick(delta_min_unknown)

    delta_below_min = make()

    def set_large_delta_minimums(targets):
        delta_below_min._symbol_precision = {"BTCUSDT": {"min_quantity": 1000, "min_notional": 5}}
        return targets, 0

    delta_below_min._optimizer.resolve_conflicts = set_large_delta_minimums
    delta_below_min._last_account = {
        "totalWalletBalance": "10000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "0"}],
    }
    await AutonomousEngine._nearline_tick(delta_below_min)

    invalid_target = make()
    invalid_target.build_portfolio_target = lambda **_kwargs: SimpleNamespace(is_valid=lambda: False)
    await AutonomousEngine._nearline_tick(invalid_target)

    high_cost = make()
    high_cost._cost_model.estimate_order = lambda *_args, **_kwargs: SimpleNamespace(total_fee_bps=31)
    await AutonomousEngine._nearline_tick(high_cost)

    malformed_position = make()
    malformed_position._last_account = {
        "totalWalletBalance": "10000",
        "positions": [
            {"symbol": "ETHUSDT", "positionAmt": "1"},
            {"symbol": "BTCUSDT", "positionAmt": "bad"},
        ],
    }
    malformed_position._signed_position_from_account = lambda *_args: Decimal("0")
    await AutonomousEngine._nearline_tick(malformed_position)

    bad_quantity = make()
    bad_quantity._last_account = {
        "totalWalletBalance": "10000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "bad"}],
    }
    bad_quantity._signed_position_from_account = lambda *_args: Decimal("0")
    await AutonomousEngine._nearline_tick(bad_quantity)

    nonfinite_quantity = make()
    nonfinite_quantity._last_account = {
        "totalWalletBalance": "10000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "NaN"}],
    }
    nonfinite_quantity._signed_position_from_account = lambda *_args: Decimal("0")
    await AutonomousEngine._nearline_tick(nonfinite_quantity)

    foreign_testnet = make(env="testnet")
    foreign_testnet._last_account = {
        "totalWalletBalance": "10000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "liquidationPrice": "90"}],
    }
    foreign_testnet._signed_position_from_account = lambda *_args: Decimal("0")
    await AutonomousEngine._nearline_tick(foreign_testnet)

    liquidation_bad = make()
    liquidation_bad._last_account = {
        "totalWalletBalance": "10000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "liquidationPrice": "bad"}],
    }
    liquidation_bad._signed_position_from_account = lambda *_args: Decimal("0")
    await AutonomousEngine._nearline_tick(liquidation_bad)

    testnet_derive_bad = make(env="testnet")
    testnet_derive_bad._last_account = {
        "totalWalletBalance": "10000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "bad"}],
    }
    testnet_derive_bad._position_generation = {"BTCUSDT": 1}
    testnet_derive_bad._signed_position_from_account = lambda *_args: Decimal("0")
    await AutonomousEngine._nearline_tick(testnet_derive_bad)

    naive_checked_at = make()
    naive_checked_at._last_reconciliation_result = SimpleNamespace(checked_at=datetime.fromisoformat("2026-01-01"))
    await AutonomousEngine._nearline_tick(naive_checked_at)

    liquidation_valid = make()
    liquidation_valid._last_account = {
        "totalWalletBalance": "10000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "liquidationPrice": "90"}],
    }
    liquidation_valid._signed_position_from_account = lambda *_args: Decimal("0")
    await AutonomousEngine._nearline_tick(liquidation_valid)

    testnet_derive_valid = make(env="testnet")
    testnet_derive_valid._position_generation = {"BTCUSDT": 1}
    testnet_derive_valid._last_account = {
        "totalWalletBalance": "10000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
    }
    testnet_derive_valid._signed_position_from_account = lambda *_args: Decimal("0")
    await AutonomousEngine._nearline_tick(testnet_derive_valid)


@pytest.mark.asyncio
async def test_nearline_risk_approval_and_commit_gates(monkeypatch) -> None:
    _patch_risk_rules(monkeypatch)
    now = datetime.now(timezone.utc)
    valid = {
        "close": 100.0,
        "bar_is_closed": True,
        "bar_open_time": now - timedelta(minutes=2),
        "bar_close_time": now - timedelta(minutes=1),
        "bar_available_at": now - timedelta(minutes=1),
        "n_candles": 20,
        "ann_volatility": 0.1,
        "spread_bps": 2.0,
        "atr_pct": 2.0,
    }

    def make():
        return _nearline_engine(features=valid)

    exposure = make()
    exposure._portfolio_exposure_projection = lambda *_args, **_kwargs: (True, 40_000.0)
    await AutonomousEngine._nearline_tick(exposure)

    aux_reject = make()

    async def aux_fail(*_args, **_kwargs):
        return [SimpleNamespace(decision=RiskDecision.REJECTED, rule_level=SimpleNamespace(value="R7"), reason="bad")]

    aux_reject._risk_engine.full_evaluate = aux_fail
    await AutonomousEngine._nearline_tick(aux_reject)

    aux_error = make()

    async def aux_error_fn(*_args, **_kwargs):
        raise RuntimeError("risk unavailable")

    aux_error._risk_engine.full_evaluate = aux_error_fn
    await AutonomousEngine._nearline_tick(aux_error)

    policy_blocked = make()
    policy_blocked._can_write = True
    policy_blocked._policy_error = "unsigned"
    policy_blocked._policy_float = lambda _key, default: default
    policy_blocked._policy_int = lambda _key, default: default
    await AutonomousEngine._nearline_tick(policy_blocked)

    exact_unknown = make()
    exact_unknown._venue_exact_quantity = lambda *_args, **_kwargs: None
    await AutonomousEngine._nearline_tick(exact_unknown)

    approval_reject = make()
    approval_reject._approval.verify = lambda *_args, **_kwargs: asyncio.sleep(0, result=False)
    await AutonomousEngine._nearline_tick(approval_reject)

    state_reject = make()
    state_reject._risk_sm.approve_if_verified = lambda *_args, **_kwargs: RiskDecision.REJECTED
    await AutonomousEngine._nearline_tick(state_reject)

    pre_reject = make()
    pre_reject._pre_risk.check = lambda _ctx: asyncio.sleep(
        0, result=[SimpleNamespace(decision=RiskDecision.REJECTED, reason="pre")]
    )
    await AutonomousEngine._nearline_tick(pre_reject)

    control_reject = make()
    control_reject._control.should_accept = lambda _intent: False
    await AutonomousEngine._nearline_tick(control_reject)

    duplicate_commit = make()

    def duplicate_commit_fn(_intent):
        raise ValueError("duplicate")

    duplicate_commit._outbox.commit = duplicate_commit_fn
    await AutonomousEngine._nearline_tick(duplicate_commit)
