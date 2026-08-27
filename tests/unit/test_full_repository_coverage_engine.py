"""Repository-wide coverage for the engine's real initialization boundary.

The engine is constructed in an isolated Paper environment so the test exercises
the production wiring, persistence bootstrap and fail-closed startup branches
without contacting an exchange or creating a trading order.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import ClassVar

import pytest

from beidou_control.plane import ControlAction
from beidou_core.engine import (
    AutonomousEngine,
    MeanReversionEntry,
    MomentumFilter,
    RealMarketStateEstimator,
    TimeExit,
    TrailingExit,
    VolatilityFilter,
)
from beidou_core.health import HealthState
from beidou_exchange.core.error_taxonomy import ErrorCategory, Result
from beidou_shared.config import (
    DatabaseConfig,
    Environment,
    ExchangeConfig,
    InfrastructureConfig,
    TypedSettings,
)
from beidou_shared.types import RiskDecision
from beidou_strategy.state.market_state import (
    DirectionState,
    MarketStateVector,
    QualityState,
    StressState,
)


def test_autonomous_engine_paper_initialization_is_wired_and_isolated(tmp_path, monkeypatch) -> None:
    import beidou_core.engine as engine_module

    settings = TypedSettings(
        environment=Environment.PAPER,
        database=DatabaseConfig(url=f"sqlite:///{tmp_path / 'state.db'}"),
        exchange=ExchangeConfig(
            rest_base_url="https://paper.invalid",
            ws_base_url="wss://paper.invalid",
        ),
        infrastructure=InfrastructureConfig(
            health_host="127.0.0.1",
            health_port=0,
        ),
    )
    monkeypatch.setattr(
        engine_module.ConfigProvider,
        "load",
        lambda _self, environment="": settings,
    )
    monkeypatch.setenv("BEIDOU_ENGINE_LOG", str(tmp_path / "engine.log"))

    engine = engine_module.AutonomousEngine(["btcusdt", "BTCUSDT"], mode="paper")
    try:
        assert engine._symbols == ["BTCUSDT"]
        assert engine._env_mode.value == Environment.PAPER.value
        assert engine._can_write is False
        assert engine._can_simulate is True
        assert engine._state_backend_supported is True
        assert engine._exchange._rest_url == "https://paper.invalid"
        assert engine._adapter is not None
        assert engine._feed is not None
        assert engine._health is not None
        assert engine._control.get_status().value == "NO_NEW_RISK"
        assert engine._shadow_runner is not None
        assert engine._health._readiness_check is not None
        assert engine._health._trading_readiness_check is not None
        assert engine._health._exit_readiness_check is not None
        assert engine._health._factor_provider is not None
        assert engine._health._resume_handler is not None
        assert engine._health._metrics_collector is not None
        assert engine._health._status_info is not None
    finally:
        # PaperShadowRunner is an in-process accumulator and owns no thread;
        # deleting the reference is sufficient cleanup for this isolated test.
        del engine


def _shell_engine() -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._policy_params = {}
    engine._policy_error = None
    engine._can_write = False
    engine._symbols = ["BTCUSDT", "ETHUSDT"]
    engine._last_prices = {"BTCUSDT": 110.0, "ETHUSDT": 0.0}
    engine._protection = SimpleNamespace(
        all_positions=lambda: {
            "long": SimpleNamespace(instrument_id="BTCUSDT", quantity=2.0, entry_price=100.0),
            "short": SimpleNamespace(instrument_id="ETHUSDT", quantity=1.0, entry_price=50.0),
            "bad": SimpleNamespace(instrument_id="BAD", quantity="bad", entry_price=1.0),
        }
    )
    engine._outbox = SimpleNamespace(inflight_signed_quantity=lambda symbol: {"BTCUSDT": "0.5"}.get(symbol, "0"))
    engine._cost_model = SimpleNamespace(fees=[])
    engine._cost_model.set_fee_tier = lambda *args: engine._cost_model.fees.append(args)
    return engine


def test_engine_policy_and_exposure_helpers_cover_defaults_invalid_values_and_guards() -> None:
    engine = _shell_engine()

    assert engine._policy_float("missing", 1.5) == 1.5
    assert engine._policy_int("missing", 2) == 2
    engine._policy_params = {"x": "2.5", "n": "3"}
    assert engine._policy_float("x", 0.0) == 2.5
    assert engine._policy_int("n", 0) == 3
    engine._can_write = True
    with pytest.raises(RuntimeError, match="SIGNED_POLICY_PARAMETER_MISSING"):
        engine._policy_float("missing", 1.0)
    with pytest.raises(RuntimeError, match="SIGNED_POLICY_PARAMETER_MISSING"):
        engine._policy_int("missing", 1)

    engine._can_write = False
    engine._policy_params = {
        "maker_fee_bps": 1,
        "taker_fee_bps": 2,
        "stop_loss_min_pct": 1,
        "stop_loss_max_pct": 5,
        "stop_loss_atr_multiplier": 2,
        "capital_budget_ratio": 0.1,
        "max_margin_ratio": 0.5,
        "leverage_low_vol": 4,
        "leverage_mid_vol": 3,
        "leverage_high_vol": 2,
        "leverage_extreme_vol": 1,
        "vol_tier_1": 0.1,
        "vol_tier_2": 0.2,
        "vol_tier_3": 0.3,
        "position_pct_base": 0.02,
        "position_cap_ratio": 0.5,
        "champion_min_icir": 2,
        "champion_min_samples": 100,
        "champion_degrade_icir": 1,
    }
    engine._validate_audited_policy_params()
    assert engine._policy_error is None
    assert engine._policy_float_audited("maker_fee_bps", 9) == 1
    assert engine._policy_float_audited("new_default", 9) == 9
    assert engine._policy_float_audited("new_default", 10) == 10
    assert engine._compute_stop_loss_pct(10) == 5
    engine._apply_fee_tier(SimpleNamespace(value="BINANCE"))
    assert engine._cost_model.fees
    assert engine._capital_budget_amount(1000).amount == "100.0"

    engine._policy_params = {"maker_fee_bps": "bad", "stop_loss_min_pct": 5, "stop_loss_max_pct": 2}
    engine._policy_error = None
    engine._validate_audited_policy_params()
    assert engine._policy_error is not None
    engine._policy_params = {"maker_fee_bps": float("nan")}
    engine._policy_error = None
    assert engine._policy_float_audited("maker_fee_bps", 2) == 2
    assert engine._policy_error is not None

    assert engine._portfolio_total_exposure() == pytest.approx(270.0)
    assert engine._portfolio_total_exposure(exclude_symbol="BTCUSDT") == pytest.approx(50.0)
    assert engine._portfolio_total_exposure(only_symbol="BTCUSDT") == pytest.approx(220.0)
    risk_increasing, projected = engine._portfolio_exposure_projection("BTCUSDT", 300.0, extra_inflight_notional=10.0)
    assert risk_increasing is True
    assert projected == pytest.approx(415.0)
    assert engine._diag_throttle("same") is True
    assert engine._diag_throttle("same") is False


@pytest.mark.asyncio
async def test_engine_adapter_boundaries_and_leverage_resolution_are_fail_closed() -> None:
    engine = _shell_engine()

    class Adapter:
        def __init__(self) -> None:
            self.request_result = Result.success({"ok": True})
            self.inventory_result = Result.success([SimpleNamespace(raw_response={"algoId": "7"})])
            self.algo_result = Result.success(SimpleNamespace(raw_response={"algoId": "7"}))
            self.cancel_result = Result.success({"code": 200})

        async def request(self, *_args, **_kwargs):
            return self.request_result

        async def get_open_algo_orders(self):
            return self.inventory_result

        async def create_algo_order(self, _params):
            return self.algo_result

        async def cancel_algo_order(self, _symbol, _algo_id):
            return self.cancel_result

    adapter = Adapter()
    engine._adapter = adapter
    request_method = adapter.request
    assert await engine._api_async("/x") == {"ok": True}
    adapter.request_result = Result.failure("bad", http_status=503, category=ErrorCategory.EXCHANGE_UNAVAILABLE)
    assert await engine._api_async("/x") == {"error": 503, "msg": "bad"}
    assert await engine._api_async_safe("/x") == (None, False)

    async def raises(*_args, **_kwargs):
        raise OSError("offline")

    adapter.request = raises
    assert await engine._api_async_safe("/x") == (None, False)
    adapter.request = request_method
    adapter.inventory_result = Result.success([SimpleNamespace(raw_response={"algoId": "7"})])
    assert await engine._get_open_algo_inventory() == [{"algoId": "7"}]
    adapter.inventory_result = Result.failure("inventory", http_status=500)
    engine._env_mode = SimpleNamespace(value="paper")
    assert await engine._get_open_algo_inventory() is None
    adapter.inventory_result = Result.failure("inventory", http_status=500)
    engine._env_mode = SimpleNamespace(value="testnet")
    assert await engine._get_open_algo_inventory() == []
    adapter.algo_result = Result.success(SimpleNamespace(raw_response={"algoId": "8"}))
    assert await engine._create_algo_order({"symbol": "BTCUSDT"}) == {"algoId": "8"}
    adapter.algo_result = Result.failure("rejected", raw={"code": -2010, "msg": "no margin"})
    failed = await engine._create_algo_order({})
    assert failed["error"] == -2010 and failed["retryable"] is False
    adapter.algo_result = Result(False)
    assert (await engine._create_algo_order({}))["category"] == "UNKNOWN"
    adapter.algo_result = Result.success({"code": 200})
    assert await engine._cancel_algo_order("BTCUSDT", 7) == {"code": 200}
    adapter.cancel_result = Result.failure("cancel failed")
    assert (await engine._cancel_algo_order("BTCUSDT", 7))["error"] == -1

    adapter.request_result = Result.success({"answer": 1})
    assert await asyncio.to_thread(engine._api, "/x") == {"answer": 1}
    adapter.request_result = Result.failure("sync fail", http_status=502)
    assert (await asyncio.to_thread(engine._api, "/x"))["error"] == 502

    async def in_async_context() -> dict:
        return engine._api("/x")

    assert (await in_async_context())["error"] == -2

    calls = []

    async def leverage_api(_path, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"leverage": "3"}
        if len(calls) == 2:
            return {"code": -2028}
        return [{"leverage": "2"}]

    engine._api_async = leverage_api
    assert await engine._ensure_leverage("BTCUSDT", 3) == 3
    assert await engine._ensure_leverage("BTCUSDT", 3) == 3
    assert await engine._ensure_leverage("ETHUSDT", 5) == 2
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(0, result={"code": -2010, "msg": "reject"})
    assert await engine._ensure_leverage("SOLUSDT", 2) == -1


def test_engine_liveness_and_reconciliation_freshness_boundaries() -> None:
    engine = _shell_engine()
    engine._last_realtime = time.time()
    engine._last_realtime_mono = time.monotonic()
    engine._running = True
    engine._env_mode = SimpleNamespace(value="paper")
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._last_reconciliation_result = None
    assert engine._realtime_age_seconds() >= 0
    assert engine._check_liveness() is HealthState.HEALTHY
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.NO_NEW_RISK)
    assert engine._check_liveness() is HealthState.DEGRADED
    engine._can_write = True
    engine._running = False
    assert engine._check_liveness() is HealthState.UNHEALTHY
    engine._running = True
    engine._user_stream_readiness = lambda: (False, {})
    assert engine._check_liveness() is HealthState.UNHEALTHY
    engine._can_write = False
    engine._user_stream_readiness = lambda: (True, {})
    engine._last_realtime_mono = time.monotonic() - 20
    assert engine._check_liveness() is HealthState.UNHEALTHY

    engine._last_reconciliation_result = SimpleNamespace(
        matched=True,
        checked_at=datetime.now(timezone.utc),
    )
    assert engine._fresh_matched_reconciliation()
    engine._last_reconciliation_result.checked_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    assert not engine._fresh_matched_reconciliation()
    engine._last_reconciliation_result = SimpleNamespace(matched=False, checked_at=datetime.now(timezone.utc))
    assert not engine._fresh_matched_reconciliation()
    engine._last_matched_reconciliation_mono = time.monotonic()
    assert engine._recently_matched_reconciliation()
    engine._last_matched_reconciliation_mono = time.monotonic() - 400
    assert not engine._recently_matched_reconciliation()


def test_engine_market_projection_operational_facts_and_factor_bar_pairing() -> None:
    engine = _shell_engine()
    engine._market_state_estimator = __import__(
        "beidou_strategy.state.market_state", fromlist=["MarketStateEstimator"]
    ).MarketStateEstimator()
    projected = engine._estimate_market_state(
        {
            "symbol": "BTCUSDT",
            "prices": [100.0, 100.5, 101.0, 102.0, 103.0],
            "close": 103.0,
            "sma_5": 102.0,
            "sma_20": 100.0,
            "trend_20_pct": 3.0,
            "ann_volatility": 0.1,
            "data_quality": "PASS",
        }
    )
    assert set(projected) == {"direction", "stress", "quality"}
    assert engine._last_market_state is not None

    for stress, quality, expected_stress, expected_quality in (
        ("NORMAL", "GOOD", "NORMAL", "RELIABLE"),
        ("ELEVATED", "DEGRADED", "NORMAL", "DEGRADED"),
        ("CRISIS", "UNKNOWN", "HIGH", "UNKNOWN"),
    ):
        state = MarketStateVector(
            venue_id="BINANCE",
            instrument_id="BTCUSDT",
            direction=DirectionState(regime="UP", probability=0.8, uncertainty=0.2),
            stress=StressState(level=stress, probability=0.8),
            quality=QualityState(tier=quality),
            timestamp=datetime.now(timezone.utc),
            policy_version="test",
            policy_hash="hash",
        )
        legacy = RealMarketStateEstimator._legacy_projection(state)
        assert legacy["stress"] == expected_stress
        assert legacy["quality"] == expected_quality

    engine._last_reconciliation_result = SimpleNamespace(
        matched=True,
        status=SimpleNamespace(value="MATCHED"),
        differences=[],
        checked_at=datetime.now(timezone.utc),
    )
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._lifecycle = SimpleNamespace(state=SimpleNamespace(value="ACTIVE"))
    engine._env_mode = SimpleNamespace(value="paper")
    engine._strategy_risk = SimpleNamespace(
        get_state=lambda _sid: SimpleNamespace(
            risk_level=SimpleNamespace(value="NORMAL"), current_drawdown_pct=1.2, consecutive_losses=0
        )
    )
    engine._autopilot_strategy_id = "autopilot"
    engine._factor_registry = SimpleNamespace(
        get_active=lambda: [SimpleNamespace(factor_id="active", lifecycle=SimpleNamespace(value="ACTIVE"))],
        get_challengers=lambda: [
            SimpleNamespace(factor_id="challenger", lifecycle=SimpleNamespace(value="CHALLENGER"))
        ],
    )
    engine._health = SimpleNamespace(uptime_seconds=lambda: 1.0)
    engine._service_identity = SimpleNamespace(service_id="svc", roles=frozenset({"trader"}))
    engine._credential = SimpleNamespace(
        credential_type=SimpleNamespace(value="TRADING"),
        status=SimpleNamespace(value="ACTIVE"),
        expires_at=datetime.now(timezone.utc) + timedelta(days=90),
    )
    engine._credential_health = {}
    engine._protection = SimpleNamespace(
        position_count=lambda: 0,
        all_positions=lambda: {},
    )
    engine._lifecycle.state = SimpleNamespace(value="ACTIVE")
    engine._last_realtime = time.time()
    engine._last_nearline = time.time()
    engine._state_backend_supported = True
    engine._state_backend_error = None
    engine._terminal_write_hold_error = None
    engine._policy_id_active = None
    engine._policy_version = None
    engine._policy_error = None
    engine._running = True
    engine._tick_count = 2
    engine._order_count = 0
    engine._error_count = 0
    engine._active_order_ids = set()
    engine._win_count = 0
    engine._loss_count = 0
    engine._user_stream_runtime = {}
    engine._protection_owner_unknown = False
    engine._user_stream_projector = SimpleNamespace(
        sequencer=SimpleNamespace(last_sequence=None, status=SimpleNamespace(value="UNKNOWN")), frozen_reason=None
    )
    engine._can_trade = False
    engine._can_withdraw = False
    engine._last_account = {}
    engine._position_generation = {}
    engine._position_projection = {}
    assert set(engine.collect_operational_facts()) == {"reconciliation", "control", "protection", "lifecycle", "risk"}
    assert engine._list_factors_for_api()[0]["factor_id"] == "active"
    assert engine._collect_metrics()["tick_count"] == 2
    assert engine._get_status_info()["mode"] == "paper"

    assert AutonomousEngine._factor_timeframe_seconds("5m") == 300.0
    assert AutonomousEngine._factor_timeframe_seconds("bad") == 0.0
    engine._factor_last_bar_by_scope = {}
    engine._factor_pending_by_scope = {}
    engine._factor_pairs_by_scope = {}
    now = datetime.now(timezone.utc)
    assert engine._advance_factor_bar("BTCUSDT", "5m", now, 100.0)
    engine._store_factor_predictions("BTCUSDT", "5m", now, 100.0, {"f": 0.2, "bad": "nan"})
    assert not engine._advance_factor_bar("BTCUSDT", "5m", now, 101.0)
    assert engine._advance_factor_bar("BTCUSDT", "5m", now + timedelta(minutes=5), 102.0)
    assert engine._factor_pairs_by_scope[("BINANCE", "BTCUSDT", "5m", 1)]["f"] == [(0.2, 0.02)]
    assert not engine._advance_factor_bar("BTCUSDT", "5m", now - timedelta(minutes=1), 99.0)
    engine._store_factor_predictions("BTCUSDT", "5m", now + timedelta(minutes=5), 102.0, {"f": 0.3})
    assert engine._advance_factor_bar("BTCUSDT", "5m", now + timedelta(minutes=20), 104.0)


def test_engine_alpha_graph_rebuild_and_backfill_source_wiring() -> None:
    engine = _shell_engine()
    engine._can_simulate = True
    engine._factor_component_registry = {
        "meanrev_entry_v1": (MeanReversionEntry, ()),
        "momentum_filter_v1": (MomentumFilter, ()),
        "trailing_exit_v1": (TrailingExit, ()),
        "time_exit_v1": (TimeExit, ()),
        "volatility_filter_v1": (VolatilityFilter, ()),
    }
    engine._entry_ids = {"meanrev_entry_v1"}
    engine._filter_ids = {"momentum_filter_v1", "volatility_filter_v1"}
    engine._exit_ids = {"trailing_exit_v1", "time_exit_v1"}
    engine._factor_predictions = {}
    engine._feed = SimpleNamespace(async_get_klines_raw=lambda *_args, **_kwargs: [])
    engine._strategy_kernel = SimpleNamespace(set_alpha_graph=lambda _graph: None, set_typed_graph=lambda _graph: None)
    records = [
        SimpleNamespace(
            definition=SimpleNamespace(factor_id="meanrev_entry_v1"),
            has_authorized_active_evidence=lambda: True,
        ),
        SimpleNamespace(
            definition=SimpleNamespace(factor_id="momentum_filter_v1"),
            has_authorized_active_evidence=lambda: True,
        ),
        SimpleNamespace(definition=SimpleNamespace(factor_id="trailing_exit_v1")),
    ]
    engine._factor_registry = SimpleNamespace(get_active=lambda: records[:2], get_challengers=lambda: records[2:])
    engine._rebuild_alpha_graph()
    assert engine._factor_predictions.keys() >= {"meanrev_entry_v1", "momentum_filter_v1", "trailing_exit_v1"}
    assert engine._alpha_graph.topological_order()
    component = SimpleNamespace(set_backfill_source=lambda source: setattr(component, "source", source))
    engine._wire_factor_backfill({"x": component})
    assert component.source is engine._feed.async_get_klines_raw


def test_engine_policy_cross_constraints_and_exposure_fail_closed() -> None:
    engine = _shell_engine()
    engine._policy_params = {
        "stop_loss_min_pct": "bad",
        "stop_loss_max_pct": "also-bad",
        "champion_min_icir": "bad",
        "champion_degrade_icir": "also-bad",
        "position_pct_base": "bad",
    }
    engine._policy_error = None
    engine._validate_audited_policy_params()
    assert engine._policy_error and "SIGNED_POLICY_INVALID_PARAM" in engine._policy_error

    engine._policy_params = {
        "vol_tier_1": 0.3,
        "vol_tier_2": 0.2,
        "vol_tier_3": 0.1,
        "leverage_low_vol": 1,
        "leverage_mid_vol": 2,
        "leverage_high_vol": 3,
        "leverage_extreme_vol": 4,
        "position_pct_base": 0.5,
        "position_cap_ratio": 1.0,
    }
    engine._policy_error = None
    engine._validate_audited_policy_params()
    assert engine._policy_error is not None

    engine._policy_params = {"x": "not-a-number"}
    engine._can_write = True
    with pytest.raises(ValueError):
        engine._policy_float("x", 1.0)
    with pytest.raises(RuntimeError, match="SIGNED_POLICY_PARAMETER_MISSING"):
        engine._policy_int("missing", 1)

    engine._can_write = False
    engine._outbox = SimpleNamespace(inflight_signed_quantity=lambda _symbol: (_ for _ in ()).throw(RuntimeError("db")))
    assert engine._portfolio_exposure_projection("BTCUSDT", 400.0) == (True, 450.0)
    engine._protection = SimpleNamespace(
        all_positions=lambda: {"invalid": SimpleNamespace(instrument_id="BTCUSDT", quantity="bad", entry_price=1)}
    )
    assert engine._portfolio_total_exposure() == 0.0
    engine._protection = SimpleNamespace(
        all_positions=lambda: {"zero": SimpleNamespace(instrument_id="BTCUSDT", quantity=0, entry_price=1)}
    )
    assert engine._portfolio_total_exposure() == 0.0


@pytest.mark.asyncio
async def test_engine_emergency_reduce_intent_is_governed_and_fail_closed() -> None:
    engine = _shell_engine()
    engine._venue_exact_quantity = lambda _symbol, amount, tag="nearline": amount
    engine._approval = SimpleNamespace(
        issue_for_approved_risk=lambda *_args, **_kwargs: "signed",
        verify=lambda *_args, **_kwargs: asyncio.sleep(0, result=True),
    )
    engine._risk_sm = SimpleNamespace(approve_if_verified=lambda *_args, **_kwargs: RiskDecision.APPROVED)
    engine._control = SimpleNamespace(should_accept=lambda _intent: True)
    committed = []
    engine._outbox = SimpleNamespace(commit=lambda intent: committed.append(intent))
    assert (
        await engine.enqueue_reduce_only_market(
            symbol="BTCUSDT",
            side="SELL",
            quantity=1,
            correlation_id="corr",
            policy_id="p",
            policy_version="1",
            policy_signature="sig",
        )
        is True
    )
    assert committed and committed[0].reduce_only is True and committed[0].close_position is True

    assert (
        await engine.enqueue_reduce_only_market(
            symbol="BTCUSDT",
            side="BAD",
            quantity=1,
            correlation_id=None,
            policy_id="p",
            policy_version="1",
            policy_signature="sig",
        )
        is False
    )
    engine._venue_exact_quantity = lambda *_args, **_kwargs: None
    assert (
        await engine.enqueue_reduce_only_market(
            symbol="BTCUSDT",
            side="SELL",
            quantity=1,
            correlation_id=None,
            policy_id="p",
            policy_version="1",
            policy_signature="sig",
        )
        is False
    )

    engine._venue_exact_quantity = lambda *_args, **_kwargs: 1
    engine._approval.issue_for_approved_risk = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("key"))
    assert (
        await engine.enqueue_reduce_only_market(
            symbol="BTCUSDT",
            side="SELL",
            quantity=1,
            correlation_id=None,
            policy_id="p",
            policy_version="1",
            policy_signature="sig",
        )
        is False
    )

    engine._approval.issue_for_approved_risk = lambda *_args, **_kwargs: "signed"
    engine._risk_sm.approve_if_verified = lambda *_args, **_kwargs: RiskDecision.REJECTED
    assert (
        await engine.enqueue_reduce_only_market(
            symbol="BTCUSDT",
            side="SELL",
            quantity=1,
            correlation_id=None,
            policy_id="p",
            policy_version="1",
            policy_signature="sig",
        )
        is False
    )
    engine._risk_sm.approve_if_verified = lambda *_args, **_kwargs: RiskDecision.APPROVED
    engine._control.should_accept = lambda _intent: False
    assert (
        await engine.enqueue_reduce_only_market(
            symbol="BTCUSDT",
            side="SELL",
            quantity=1,
            correlation_id=None,
            policy_id="p",
            policy_version="1",
            policy_signature="sig",
        )
        is False
    )
    engine._control.should_accept = lambda _intent: True
    engine._outbox.commit = lambda _intent: (_ for _ in ()).throw(RuntimeError("outbox"))
    assert (
        await engine.enqueue_reduce_only_market(
            symbol="BTCUSDT",
            side="SELL",
            quantity=1,
            correlation_id=None,
            policy_id="p",
            policy_version="1",
            policy_signature="sig",
        )
        is False
    )


@pytest.mark.asyncio
async def test_engine_api_exception_and_leverage_readback_boundaries() -> None:
    engine = _shell_engine()

    class Adapter:
        async def request(self, *_args, **_kwargs):
            raise OSError("offline")

        async def get_open_algo_orders(self):
            raise OSError("offline")

        async def create_algo_order(self, _params):
            raise OSError("offline")

        async def cancel_algo_order(self, _symbol, _algo_id):
            raise OSError("offline")

    engine._adapter = Adapter()
    assert await engine._api_async_safe("/x") == (None, False)
    engine._env_mode = SimpleNamespace(value="paper")
    assert await engine._get_open_algo_inventory() is None
    engine._env_mode = SimpleNamespace(value="testnet")
    assert await engine._get_open_algo_inventory() == []
    assert (await engine._create_algo_order({}))["error"] == -1
    assert (await engine._cancel_algo_order("BTCUSDT", 1))["error"] == -1
    assert (await asyncio.to_thread(engine._api, "/x"))["error"] == -1

    responses = [{"code": -2028, "msg": '{"code": -2028}'}, RuntimeError("query")]

    async def leverage_api(*_args, **_kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    engine._api_async = leverage_api
    assert await engine._ensure_leverage("BTCUSDT", 5) == -1
    responses[:] = [{"code": -2028, "msg": "not-json"}, {"unexpected": True}]
    assert await engine._ensure_leverage("ETHUSDT", 5) == -1


def test_engine_operational_fact_fallbacks() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    facts = engine.collect_operational_facts()
    assert set(facts) == {"reconciliation", "control", "protection", "lifecycle", "risk"}


def test_engine_constructor_rejects_implicit_universe_and_initializes_zero_write_modes(tmp_path, monkeypatch) -> None:
    import beidou_core.engine as engine_module

    with pytest.raises(ValueError, match="EXPLICIT_SYMBOL_UNIVERSE_REQUIRED"):
        engine_module.AutonomousEngine([], mode="paper")
    with pytest.raises(ValueError, match="EXPLICIT_SYMBOL_UNIVERSE_REQUIRED"):
        engine_module.AutonomousEngine(["ALL"], mode="paper")
    monkeypatch.setattr(engine_module.PersistentStore, "_instance", None)

    def settings_for(_name: str) -> TypedSettings:
        return TypedSettings(
            environment=Environment.PAPER,
            database=DatabaseConfig(url=f"sqlite:///{tmp_path / 'zero-write-state.db'}"),
            exchange=ExchangeConfig(rest_base_url="https://paper.invalid", ws_base_url="wss://paper.invalid"),
            infrastructure=InfrastructureConfig(health_host="127.0.0.1", health_port=0),
        )

    monkeypatch.setattr(engine_module.ConfigProvider, "load", lambda _self, environment="": settings_for(environment))
    shadow = engine_module.AutonomousEngine(["ethusdt"], mode="shadow")
    safety = engine_module.AutonomousEngine(["solusdt"], mode="safety_only")
    try:
        assert shadow._env_mode.value == "shadow"
        assert shadow._can_simulate is True and shadow._can_write is False
        assert safety._env_mode.value == "safety_only"
        assert safety._can_simulate is False and safety._can_write is False
        assert shadow._shadow_runner is None
        assert safety._shadow_runner is None
    finally:
        del shadow, safety


def test_engine_constructor_writable_hold_and_postgres_diagnostic_paths(tmp_path, monkeypatch) -> None:
    import beidou_core.engine as engine_module

    settings = TypedSettings(
        environment=Environment.TESTNET,
        database=DatabaseConfig(url="postgresql://user:pass@invalid/db"),
        exchange=ExchangeConfig(rest_base_url="https://testnet.invalid", ws_base_url="wss://testnet.invalid"),
        infrastructure=InfrastructureConfig(health_host="127.0.0.1", health_port=0),
    )
    required = {
        "max_leverage": 3,
        "max_concentration_pct": 20,
        "max_position_notional": 1000,
        "max_total_leverage": 3,
        "max_instruments": 5,
        "drift_threshold": 0.2,
        "max_drawdown_pct": 20,
        "max_daily_loss_pct": 5,
        "max_consecutive_losses": 5,
        "risk_per_trade_pct": 1,
        "min_sharpe_rolling": 0.1,
    }
    envelope = SimpleNamespace(
        parameters=required,
        policy_id="test-policy",
        version="1",
        signature="signed",
        validate_risk_parameters=lambda: (True, "complete"),
    )
    monkeypatch.setattr(engine_module.ConfigProvider, "load", lambda _self, environment="": settings)
    monkeypatch.setattr(engine_module, "PolicyLoader", lambda policy_dir: SimpleNamespace(load=lambda _id: envelope))
    monkeypatch.setattr(
        engine_module.PersistentStore,
        "get_instance",
        lambda _path: SimpleNamespace(
            restore_position_projection=lambda: [],
            restore_protections=lambda: [],
            restore_ledger_transactions=lambda: [],
            restore_ledger_entries=lambda: [],
            restore_order_states=lambda: [],
            restore_fill_events=lambda: [],
            restore_user_stream_projection=lambda *_args: None,
            restore_user_stream_events=lambda **_kwargs: [],
            restore_trading_pool_state=lambda: [],
            save_trading_pool_event=lambda _event: None,
        ),
    )

    class Outbox:
        def __init__(self, **_kwargs):
            self.recovered = 0

        def recover_inflight(self):
            self.recovered += 1
            return 1

        def restore_pending_approvals(self):
            return []

    monkeypatch.setattr(engine_module, "IntentOutbox", Outbox)
    import beidou_infra.outbox as outbox_module
    import beidou_infra.postgres_store as postgres_module

    monkeypatch.setattr(outbox_module, "PostgresIntentOutbox", Outbox)
    monkeypatch.setattr(
        postgres_module,
        "PostgresPersistentStore",
        type(
            "PGStore",
            (),
            {
                "get_instance": staticmethod(lambda _dsn: engine_module.PersistentStore.get_instance("pg")),
            },
        ),
    )
    monkeypatch.delenv("BEIDOU_TERMINAL_WRITE_HOLD", raising=False)
    monkeypatch.setenv("BEIDOU_FENCING_TOKEN", "1")
    engine = engine_module.AutonomousEngine(["BTCUSDT"], mode="testnet")
    try:
        assert engine._can_write is True
        assert engine._terminal_write_hold_error == "TERMINAL_WRITE_HOLD_UNSET"
        assert engine._state_backend_supported is True
    finally:
        del engine


@pytest.mark.asyncio
async def test_engine_alpha_and_state_projection_error_boundaries(monkeypatch) -> None:
    import beidou_core.engine as engine_module

    context = {
        "features": {
            "prices": [100.0] * 30,
            "close": 100.0,
            "sma_20": 100.0,
            "rsi_14": 50.0,
            "ann_volatility": 0.2,
            "spread_bps": 1.0,
            "atr_pct": 1.0,
        },
        "instrument_id": "BTCUSDT",
        "venue_id": "BINANCE",
    }
    # A high-confidence ACTION is not blocked; patching the contract predicate
    # exercises the immutable fail-closed normalization path explicitly.
    entry = engine_module.MeanReversionEntry()
    monkeypatch.setattr(
        entry._engine,
        "evaluate",
        lambda **_kwargs: SimpleNamespace(
            signal_direction="LONG",
            strength=0.8,
            confidence=0.9,
            z_score=2.0,
            half_life_hours=12.0,
            regime_allowed=True,
            cost_viable=True,
        ),
    )
    monkeypatch.setattr(engine_module.StrategySignal, "is_blocking", lambda _self: True)
    blocked = await entry.generate(context)
    assert blocked.direction is engine_module.SignalDirection.NO_ACTION
    assert blocked.strength == 0.0 and blocked.confidence == 0.0

    momentum = engine_module.MomentumFilter()
    momentum._momentum._periods = []
    assert momentum.validate() is False
    trailing = engine_module.TrailingExit()
    bad = await trailing.generate(
        {
            "features": {**context["features"], "atr_pct": 0.0},
            "_position_info": {"has_position": True, "entry_price": 100.0, "side": "LONG"},
        }
    )
    assert bad.metadata["reason"] == "MARKET_FEATURES_UNKNOWN"

    assert (
        engine_module.RealMarketStateEstimator._legacy_projection(
            MarketStateVector(
                direction=DirectionState(regime="RANGING", probability=0.5, uncertainty=0.5),
                stress=StressState(level="ELEVATED", probability=0.5),
                quality=QualityState(tier="GOOD"),
                venue_id=__import__("beidou_shared.types", fromlist=["VenueId"]).VenueId("BINANCE"),
                instrument_id=__import__("beidou_shared.types", fromlist=["InstrumentId"]).InstrumentId("BTCUSDT"),
            )
        )["stress"]
        == "NORMAL"
    )
    assert engine_module.RealMarketStateEstimator._legacy_projection(
        MarketStateVector(
            direction=DirectionState(regime="RANGING", probability=0.5, uncertainty=0.5),
            stress=StressState(level="CRISIS", probability=1.0),
            quality=QualityState(tier="POOR"),
            venue_id=__import__("beidou_shared.types", fromlist=["VenueId"]).VenueId("BINANCE"),
            instrument_id=__import__("beidou_shared.types", fromlist=["InstrumentId"]).InstrumentId("BTCUSDT"),
        )
    ) == {"direction": "RANGING", "stress": "HIGH", "quality": "POOR"}


def test_engine_readiness_and_protection_coverage_fail_closed_matrix() -> None:

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = False
    engine._can_simulate = True
    engine._env_mode = SimpleNamespace(value="paper")
    engine._protection_owner_id = "svc"
    engine._position_generation = {"BTCUSDT": 1}
    engine._position_projection = {"BTCUSDT": {"position_generation": 1}}
    engine._symbol_precision = {"BTCUSDT": {"min_quantity": "bad"}}
    engine._protection = SimpleNamespace(all_positions=lambda: {})

    positions = [
        {"symbol": "", "positionAmt": "1"},
        {"symbol": "BADQ", "positionAmt": "not-a-number"},
        {"symbol": "NANQ", "positionAmt": "NaN"},
        {"symbol": "ZERO", "positionAmt": "0"},
        {"symbol": "BTCUSDT", "positionAmt": "1"},
    ]
    protections = [
        {"symbol": "OTHER", "status": "ACTIVE", "exchange_order_id": "other", "owner_id": "svc"},
        {"symbol": "BTCUSDT", "status": "ACTIVE", "exchange_order_id": "", "owner_id": "svc"},
        {"symbol": "BTCUSDT", "status": "ACTIVE", "exchange_order_id": "wrong-owner", "owner_id": "other"},
        {
            "symbol": "BTCUSDT",
            "status": "ACTIVE",
            "exchange_order_id": "bad-gen",
            "owner_id": "svc",
            "side": "SELL",
            "position_generation": "bad",
            "quantity": "bad",
            "order_type": "STOP_MARKET",
            "stop_type": "ATR_BASED",
        },
        {
            "symbol": "BTCUSDT",
            "status": "ACTIVE",
            "exchange_order_id": "wrong-side",
            "owner_id": "svc",
            "side": "BUY",
            "position_generation": 1,
            "quantity": "1",
            "order_type": "STOP_MARKET",
            "stop_type": "ATR_BASED",
        },
    ]
    covered, evidence = engine._assess_protection_coverage(positions, protections, {})
    assert covered is False
    reasons = {item["reason"] for item in evidence["unprotected_symbols"]}
    assert "VENUE_POSITION_SYMBOL_UNKNOWN" in reasons
    assert "VENUE_POSITION_QUANTITY_UNKNOWN" in reasons
    assert "STOP_LOSS_QUANTITY_UNCOVERED" in reasons

    engine._env_mode = SimpleNamespace(value="testnet")
    engine._symbol_precision = {"DUST": {"min_quantity": 0.0}}
    covered, evidence = engine._assess_protection_coverage(
        [{"symbol": "EXTERNAL", "positionAmt": "1"}, {"symbol": "DUST", "positionAmt": "0.1"}],
        [],
        {},
    )
    assert covered is True and evidence["unprotected_symbols"] == []

    engine._env_mode = SimpleNamespace(value="paper")
    engine._symbol_precision = {"DUST": {"min_quantity": 0.0}}
    covered, evidence = engine._assess_protection_coverage(
        [{"symbol": "DUST", "positionAmt": "0.1"}], [], {"p": SimpleNamespace(instrument_id="DUST")}
    )
    assert covered is False

    engine._store = SimpleNamespace(restore_order_states=lambda: [], restore_protections=lambda: [])
    engine._outbox = SimpleNamespace(stats={"state_counts": {}})
    engine._ledger = SimpleNamespace(is_frozen=True)
    assert engine._durable_fact_status()[:2] == (False, "LEDGER_FROZEN")
    engine._ledger.is_frozen = False
    engine._outbox.stats = []
    assert engine._durable_fact_status()[1].startswith("DURABLE_FACTS_UNKNOWN:TypeError")
    engine._outbox.stats = {"state_counts": []}
    assert engine._durable_fact_status()[1].startswith("DURABLE_FACTS_UNKNOWN:TypeError")
    engine._store.restore_order_states = lambda: (_ for _ in ()).throw(RuntimeError("store"))
    assert engine._durable_fact_status()[1] == "DURABLE_FACTS_UNKNOWN:RuntimeError"


def test_engine_user_stream_readiness_and_ready_gates() -> None:
    from beidou_lifecycle.lifecycle import ModuleState

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._user_stream_runtime = []
    assert engine._user_stream_readiness()[0] is False
    engine._user_stream_runtime = {"status": "HEALTHY", "last_event_mono": "bad", "listen_key_active": True}
    engine._user_stream_projector = SimpleNamespace(sequencer=SimpleNamespace(status="HEALTHY"))
    engine._event_stream_facts = SimpleNamespace(complete=True)
    ready, evidence = engine._user_stream_readiness()
    assert ready is True and evidence["event_age_seconds"] is None
    engine._env_mode = SimpleNamespace(value="testnet")
    ready, _ = engine._user_stream_readiness()
    assert ready is True

    engine._policy_error = "bad"
    assert engine._check_ready() is False
    engine._policy_error = None
    engine._state_backend_supported = False
    assert engine._check_ready() is False
    engine._state_backend_supported = True
    engine._durable_fact_status = lambda: (False, "DURABLE", {})
    assert engine._check_ready() is False
    engine._durable_fact_status = lambda: (True, "OK", {})
    engine._lifecycle = SimpleNamespace(state=ModuleState.ACTIVE)
    engine._feed = SimpleNamespace(is_healthy=lambda: False)
    assert engine._check_ready() is False
    engine._feed = SimpleNamespace(is_healthy=lambda: True)
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.NO_NEW_RISK)
    assert engine._check_ready() is False
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._protection_owner_unknown = True
    assert engine._check_ready() is False
    engine._protection_owner_unknown = False
    engine._last_reconciliation_result = None
    assert engine._check_ready() is False
    engine._last_reconciliation_result = SimpleNamespace(matched=True)
    engine._user_stream_readiness = lambda: (True, {})
    engine._running = True
    engine._last_realtime_mono = time.monotonic()
    assert engine._check_ready() is True

    engine._check_ready = lambda: False
    engine._durable_fact_status = lambda: (False, "DURABLE", {})
    engine._can_write = True
    assert engine._check_trading_ready() == (False, "DURABLE")
    engine._durable_fact_status = lambda: (True, "OK", {})
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.NO_NEW_RISK)
    assert engine._check_trading_ready()[1] == "CONTROL_NO_NEW_RISK"
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._last_reconciliation_result = None
    assert engine._check_trading_ready() == (False, "RECONCILIATION_UNKNOWN")
    engine._last_reconciliation_result = SimpleNamespace(matched=True)
    assert engine._check_trading_ready() == (False, "RUNTIME_NOT_READY")
    engine._check_ready = lambda: True
    assert engine._check_trading_ready() == (True, "READY")
    engine._can_write = False
    assert engine._check_trading_ready() == (False, "TRADING_WRITE_DISABLED")


@pytest.mark.asyncio
async def test_engine_heartbeat_task_recovery_and_leverage_error_paths(monkeypatch) -> None:
    engine = _shell_engine()
    engine._last_reconciliation_result = SimpleNamespace(matched=True)
    engine._last_realtime_mono = time.monotonic()
    engine._last_matched_reconciliation_mono = time.monotonic()
    assert (
        engine._fresh_matched_reconciliation(max_age_seconds=60.0) is False
    )  # checked_at is absent, which is not a freshness proof
    engine._last_reconciliation_result.checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    assert engine._fresh_matched_reconciliation() is True
    engine._last_matched_reconciliation_mono = None
    assert engine._recently_matched_reconciliation() is False

    class Outbox:
        async def recover_inflight(self, **_kwargs):
            return 1

    engine._outbox = Outbox()
    engine._last_unknown_resolve = 0.0
    engine._last_recon = 0.0
    engine._resolve_unknown_outbox_intents = lambda: asyncio.sleep(0)
    engine._monitor_orphan_orders = lambda: asyncio.sleep(0)
    engine._reconcile = lambda: asyncio.sleep(0, result=True)
    engine._durable_fact_status = lambda: (True, "OK", {})
    engine._control = SimpleNamespace(
        get_status=lambda: ControlAction.NO_NEW_RISK,
        execute_action=lambda _action: (_ for _ in ()).throw(RuntimeError("interlock")),
    )
    await engine._reconciliation_segment()
    assert engine._last_recon > 0

    class FailingOutbox:
        @property
        def recover_inflight(self):
            raise RuntimeError("recovery accessor")

    engine._outbox = FailingOutbox()
    engine._last_unknown_resolve = 0.0
    await engine._reconciliation_segment()
    assert engine._last_recon > 0

    class Task:
        def __init__(self, *, done, cancelled=False, error=None):
            self._done, self._cancelled, self._error = done, cancelled, error

        def done(self):
            return self._done

        def cancelled(self):
            return self._cancelled

        def exception(self):
            return self._error

    engine._intent_tasks = {
        Task(done=False),
        Task(done=True, cancelled=True),
        Task(done=True),
        Task(done=True, error=RuntimeError("intent")),
    }
    engine._reap_intent_tasks()
    assert len(engine._intent_tasks) == 1

    calls = []
    engine._reconcile_terminal_child_parent = lambda _intent: calls.append("parent") or True

    async def terminal(_intent):
        raise RuntimeError("TERMINAL_CHILD_STATE")

    engine._place_order = terminal
    await engine._execute_claimed_intent(SimpleNamespace(intent_id="terminal"))
    assert calls == ["parent"]

    async def ordinary(_intent):
        raise RuntimeError("ordinary")

    engine._place_order = ordinary
    await engine._execute_claimed_intent(SimpleNamespace(intent_id="ordinary"))

    async def succeeds(_intent):
        return None

    engine._place_order = succeeds
    await engine._execute_claimed_intent(SimpleNamespace(intent_id="ok"))

    engine._active_algo_ids = {}
    await engine._cancel_algo_orders("p", "BTCUSDT")
    engine._active_algo_ids = {"p": {"1", "2", "3"}}
    engine._cancel_algo_order = lambda _s, aid: asyncio.sleep(
        0, result={"msg": "success"} if aid == 1 else {"code": 400}
    )
    await engine._cancel_algo_orders("p", "BTCUSDT")
    engine._active_algo_ids = {"p": {"4"}}
    engine._cancel_algo_order = lambda *_args: (_ for _ in ()).throw(RuntimeError("cancel"))
    await engine._cancel_algo_orders("p", "BTCUSDT")

    responses = iter(
        [
            {"msg": '{"code": -2028}'},
            [{"leverage": "7"}],
            {"msg": "not-json"},
            {"code": -2028},
            [],
            {"code": -2028},
            [{"leverage": "bad"}],
        ]
    )

    async def leverage_api(*_args, **_kwargs):
        return next(responses)

    engine._api_async = leverage_api
    assert await engine._ensure_leverage("BTCUSDT", 3) == 7
    assert await engine._ensure_leverage("ETHUSDT", 3) == -1
    assert await engine._ensure_leverage("SOLUSDT", 3) == -1


def test_engine_factor_bar_and_portfolio_short_paths() -> None:
    engine = _shell_engine()
    engine._factor_last_bar_by_scope = {}
    engine._factor_pending_by_scope = {}
    engine._factor_pairs_by_scope = {}
    now = datetime.now(timezone.utc)
    assert engine._advance_factor_bar("BTCUSDT", "5m", now, float("nan")) is False
    engine._advance_factor_bar("BTCUSDT", "5m", now, 100.0)
    engine._store_factor_predictions("BTCUSDT", "5m", now, 100.0, {"bad": object(), "inf": float("inf")})
    assert engine._factor_pending_by_scope[("BINANCE", "BTCUSDT", "5m", 1)] == {}
    engine._store_factor_predictions("BTCUSDT", "5m", now + timedelta(minutes=5), 100.0, {"f": 1.0})

@pytest.mark.asyncio
async def test_engine_user_stream_event_dispatch_and_restart_contracts(monkeypatch) -> None:
    import beidou_exchange.binance_usdm as binance_module
    import beidou_exchange.binance_usdm.adapter as adapter_module

    class FakeWebSocket:
        instances: ClassVar[list["FakeWebSocket"]] = []

        def __init__(self, *, base_url):
            self.base_url = base_url
            self.state_handler = None
            self.event_handler = None
            FakeWebSocket.instances.append(self)

        def on_state_change(self, callback):
            self.state_handler = callback

        async def subscribe(self, _listen_key, callback):
            self.event_handler = callback
            for event in self.events:
                await callback("listen-key", event)

        async def run(self):
            return None

        async def close(self):
            return None

    class Adapter:
        async def create_user_listen_key(self):
            return Result.success({"listenKey": "lk-1"})

        async def keepalive_user_listen_key(self, _key):
            return Result.success({})

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._env_mode = SimpleNamespace(value="testnet")
    engine._adapter = Adapter()
    engine._user_stream_runtime = {"status": "DEGRADED"}
    engine._user_stream_restart_attempts = 0
    engine._user_stream_restarting = False
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._recon = SimpleNamespace(update_event_facts=lambda _facts: None)
    engine._user_stream_projector = SimpleNamespace()
    engine._event_stream_facts = None
    engine._leverage_cache = {"BTCUSDT": 10}
    engine._update_user_stream_runtime = AutonomousEngine._update_user_stream_runtime.__get__(engine)
    engine._user_stream_fault = lambda reason, terminal=False: engine._update_user_stream_runtime(
        status="FAILED" if terminal else "DEGRADED", last_error=reason, listen_key_active=False
    )
    engine.ingest_user_order_update = lambda _update: True
    engine.ingest_user_account_update = lambda _update: True
    engine._ingest_account_config_update = lambda _data: True

    fake_update = SimpleNamespace()
    parser_result = Result.success(fake_update)
    monkeypatch.setattr(
        adapter_module.BinanceUsdmAdapter, "parse_user_order_update", staticmethod(lambda _data: parser_result)
    )
    monkeypatch.setattr(
        adapter_module.BinanceUsdmAdapter, "parse_user_account_update", staticmethod(lambda _data: parser_result)
    )
    monkeypatch.setattr(binance_module, "BinanceUsdmWebSocketClient", FakeWebSocket)
    monkeypatch.setattr(binance_module, "FSTREAM_TESTNET_URL", "wss://testnet.invalid")
    monkeypatch.setattr(binance_module, "FSTREAM_PRODUCTION_URL", "wss://prod.invalid")

    FakeWebSocket.events = [
        "not-an-object",
        {"e": "listenKeyExpired"},
        {"e": "ORDER_TRADE_UPDATE"},
        {"e": "ACCOUNT_UPDATE"},
        {"e": "ACCOUNT_CONFIG_UPDATE"},
        {"e": "ALGO_UPDATE"},
        {"e": "MARGIN_CALL"},
        {"e": "TRADE_LITE"},
        {"e": "STRATEGY_UPDATE"},
        {"e": "UNKNOWN_EVENT"},
    ]
    assert await engine._start_user_stream() is True
    ws = FakeWebSocket.instances[-1]
    ws.state_handler(None, SimpleNamespace(value="CONNECTED"), 1)
    ws.state_handler(None, SimpleNamespace(value="RECONNECTING"), 1)
    ws.state_handler(None, SimpleNamespace(value="DISCONNECTED"), 1)
    assert engine._leverage_cache["BTCUSDT"] == 10
    await engine._stop_user_stream()

    # A second dispatch in a writable non-testnet mode covers the strict
    # faulting branches for shared-account informational events.
    engine._env_mode = SimpleNamespace(value="live")
    engine._user_stream_fault = lambda reason, terminal=False: engine._update_user_stream_runtime(
        status="FAILED" if terminal else "DEGRADED", last_error=reason, listen_key_active=False
    )
    FakeWebSocket.events = [
        {"e": "ALGO_UPDATE"},
        {"e": "MARGIN_CALL"},
        {"e": "STRATEGY_UPDATE"},
        {"e": "UNKNOWN_EVENT"},
    ]
    assert await engine._start_user_stream() is True
    await engine._stop_user_stream()

    # Unsupported listen-key creation and an empty key are terminal, explicit
    # failures and never silently fall back to REST-only writes.
    engine._adapter = SimpleNamespace()
    assert await engine._start_user_stream() is False

    class EmptyKeyAdapter:
        async def create_user_listen_key(self):
            return Result.success({"listenKey": ""})

    engine._adapter = EmptyKeyAdapter()
    assert await engine._start_user_stream() is False


@pytest.mark.asyncio
async def test_engine_user_stream_ingest_wrappers_config_fault_and_keepalive(monkeypatch, tmp_path) -> None:
    import beidou_core.engine as engine_module

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = SimpleNamespace()
    engine._recon = SimpleNamespace(update_event_facts=lambda _facts: None)
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._env_mode = SimpleNamespace(value="live")
    engine._can_write = False
    engine._user_stream_runtime = None
    engine._user_stream_restart_attempts = 0
    engine._running = True
    engine._record_execution_fact_failure_env_guarded = lambda _msg: None
    engine._record_execution_fact_failure = lambda _msg: None
    engine._safe_no_new_risk = lambda _reason: None
    engine._user_stream_projector = SimpleNamespace(
        ingest=lambda _u: SimpleNamespace(
            status=engine_module.UserProjectionStatus.BLOCKED, event_id="e", reason="blocked"
        ),
        authorize_replay_baseline=lambda *_args, **_kwargs: SimpleNamespace(
            status=engine_module.UserProjectionStatus.BLOCKED, reason="replay"
        ),
        ingest_account_update=lambda _u: SimpleNamespace(
            status=engine_module.UserProjectionStatus.ERROR, event_id="a", reason="account"
        ),
        fact_snapshot=lambda: SimpleNamespace(complete=True),
    )
    assert engine.ingest_user_order_update(SimpleNamespace(event=SimpleNamespace(event_type="ORDER"))) is False
    assert engine.authorize_user_stream_replay(SimpleNamespace(), evidence_hash="h", approval_id="a") is False
    assert engine.ingest_user_account_update(SimpleNamespace()) is False

    engine._leverage_cache = {"BTCUSDT": 5}
    assert engine._ingest_account_config_update({"ac": {"s": "btcusdt", "l": "bad"}, "ai": {"j": "yes"}}) is True
    assert "BTCUSDT" not in engine._leverage_cache
    assert engine._ingest_account_config_update({"ac": None, "ai": {"j": object()}}) is True
    engine._update_user_stream_runtime(status="HEALTHY")
    assert engine._user_stream_runtime["status"] == "HEALTHY"
    engine._user_stream_fault("first")
    engine._user_stream_fault("repeat")

    # Restart: success, refused start, and guard exits all keep the retry
    # budget and stopping flag explicit.
    async def fast_sleep(_seconds):
        return None

    async def stop_stream():
        return None

    async def start_stream_ok():
        return True

    monkeypatch.setattr(engine_module.asyncio, "sleep", fast_sleep)
    engine._stop_user_stream = stop_stream
    engine._start_user_stream = start_stream_ok
    engine._user_stream_stopping = False
    engine._user_stream_restarting = False
    await engine._restart_user_stream_after_fault("fault")
    assert engine._user_stream_restart_attempts == 0

    async def start_stream_fail():
        return False

    engine._start_user_stream = start_stream_fail
    engine._user_stream_restart_attempts = 0
    engine._user_stream_stopping = False
    await engine._restart_user_stream_after_fault("fault")
    assert engine._user_stream_restart_attempts == 1
    engine._user_stream_stopping = True
    await engine._restart_user_stream_after_fault("fault")

    class FailedResult:
        def is_success(self):
            return False

    class KeepaliveAdapter:
        def __init__(self, result):
            self.result = result
            self.calls = 0

        async def keepalive_user_listen_key(self, _key):
            self.calls += 1
            return self.result

    sleep_calls = {"count": 0}

    async def one_cycle_sleep(_seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] >= 1:
            engine._user_stream_stopping = True

    monkeypatch.setattr(engine_module.asyncio, "sleep", one_cycle_sleep)
    engine._user_stream_stopping = False
    engine._adapter = SimpleNamespace()
    await engine._user_stream_keepalive_loop("lk")


def test_engine_execution_binding_and_rule_snapshot_fail_closed_matrix() -> None:
    intent = SimpleNamespace(
        instrument_id="BTCUSDT",
        side=__import__("beidou_shared.types", fromlist=["OrderSide"]).OrderSide.BUY,
        order_type=__import__("beidou_shared.types", fromlist=["OrderType"]).OrderType.LIMIT,
        time_in_force=__import__("beidou_shared.types", fromlist=["TimeInForce"]).TimeInForce.GTC,
        quantity=SimpleNamespace(amount="1"),
        price=SimpleNamespace(amount="100"),
    )
    validate = AutonomousEngine._validate_slices_against_intent
    valid = [("1", "100", "LIMIT", "GTC", "cid")]
    assert validate(intent, valid, order_symbol="ETHUSDT", side="BUY", client_id="cid")[1] == "SYMBOL_MISMATCH"
    assert validate(intent, valid, order_symbol="BTCUSDT", side="SELL", client_id="cid")[1] == "SIDE_MISMATCH"
    for quantity, expected in (("bad", "APPROVED_QUANTITY_UNKNOWN"), ("0", "APPROVED_QUANTITY_UNKNOWN")):
        broken = SimpleNamespace(**{**vars(intent), "quantity": SimpleNamespace(amount=quantity)})
        assert validate(broken, valid, order_symbol="BTCUSDT", side="BUY", client_id="cid")[1] == expected
    cases = [
        ([("bad", "100", "LIMIT", "GTC", "cid")], "SLICE_QUANTITY_UNKNOWN"),
        ([("0", "100", "LIMIT", "GTC", "cid")], "SLICE_QUANTITY_INVALID"),
        ([("2", "100", "LIMIT", "GTC", "cid")], "TOTAL_QUANTITY_EXCEEDS_APPROVAL"),
        ([("1", "100", "MARKET", "GTC", "cid")], "ORDER_TYPE_MISMATCH"),
        ([("1", "100", "LIMIT", "FOK", "cid")], "TIME_IN_FORCE_MISMATCH"),
        ([("1", None, "LIMIT", "GTC", "cid")], "LIMIT_PRICE_UNKNOWN"),
        ([("1", "101", "LIMIT", "GTC", "cid")], "LIMIT_PRICE_MISMATCH"),
        ([("1", "bad", "LIMIT", "GTC", "cid")], "LIMIT_PRICE_UNKNOWN"),
    ]
    for slices, expected in cases:
        assert validate(intent, slices, order_symbol="BTCUSDT", side="BUY", client_id="cid")[1] == expected
    market = SimpleNamespace(
        **{
            **vars(intent),
            "order_type": __import__("beidou_shared.types", fromlist=["OrderType"]).OrderType.MARKET,
            "price": None,
        }
    )
    assert (
        validate(market, [("1", "100", "MARKET", "GTC", "cid")], order_symbol="BTCUSDT", side="BUY", client_id="cid")[1]
        == "MARKET_PRICE_UNEXPECTED"
    )
    assert (
        validate(intent, [("1", "100", "LIMIT", "GTC", "other")], order_symbol="BTCUSDT", side="BUY", client_id="cid")[
            1
        ]
        == "CLIENT_ORDER_ID_MISMATCH"
    )
    assert (
        validate(
            intent,
            [("0.5", "100", "LIMIT", "GTC", "cid-1"), ("0.5", "100", "LIMIT", "GTC", "bad")],
            order_symbol="BTCUSDT",
            side="BUY",
            client_id="cid",
        )[1]
        == "SLICE_CLIENT_ORDER_ID_INVALID"
    )
    assert (
        validate(
            intent,
            [("0.5", "100", "LIMIT", "GTC", "cid-1"), ("0.5", "100", "LIMIT", "GTC", "cid-1")],
            order_symbol="BTCUSDT",
            side="BUY",
            client_id="cid",
        )[1]
        == "DUPLICATE_SLICE_CLIENT_ORDER_ID"
    )
    assert validate(intent, [], order_symbol="BTCUSDT", side="BUY", client_id="cid")[1] == "SLICE_TOTAL_UNKNOWN"

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._adapter = None
    assert engine._sync_adapter_rule_snapshots(None) is False
    engine._adapter = SimpleNamespace(
        _reference_data=SimpleNamespace(instruments={}),
        sync_rule_snapshots=lambda: (_ for _ in ()).throw(RuntimeError("sync")),
    )
    assert engine._sync_adapter_rule_snapshots({"symbols": [{"symbol": "BTCUSDT"}]}) is False

    order = SimpleNamespace(
        protection_id="p",
        owner_id="owner",
        position_generation=1,
        instrument_id="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        quantity=SimpleNamespace(amount="0.1234"),
        trigger_price=SimpleNamespace(amount="99.1234"),
    )
    params = AutonomousEngine._protection_algo_params(
        order, symbol="BTCUSDT", side="SELL", precision={"quantity": 2, "price": 2}
    )
    assert params["quantity"] == "0.12" and params["triggerPrice"] == "99.12"
    for field, value in (("quantity", "bad"), ("trigger_price", "bad")):
        bad_order = SimpleNamespace(**{**vars(order), field: SimpleNamespace(amount=value)})
        with pytest.raises(ValueError):
            AutonomousEngine._protection_algo_params(
                bad_order, symbol="BTCUSDT", side="SELL", precision={"quantity": 2, "price": 2}
            )


@pytest.mark.asyncio
async def test_engine_unknown_intent_resolution_and_adjudication_paths(monkeypatch) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._outbox = SimpleNamespace(get_unknown_intents=lambda: (_ for _ in ()).throw(RuntimeError("db")))
    assert await engine._resolve_unknown_outbox_intents() == 0

    resolved = []

    class Outbox:
        def get_unknown_intents(self):
            return [
                "bad",
                {},
                {"intent_id": "missing"},
                {"intent_id": "found", "client_order_id": "cid", "symbol": "BTCUSDT"},
                {
                    "intent_id": "absent",
                    "client_order_id": "cid2",
                    "symbol": "BTCUSDT",
                    "risk_approval_id": "a",
                    "risk_nonce": "n",
                },
                {"intent_id": "partial", "client_order_id": "cid3", "symbol": "BTCUSDT"},
            ]

        def restore_execution_plan(self, intent_id):
            if intent_id == "found":
                return SimpleNamespace(children=[SimpleNamespace(client_order_id="cid-1")])
            if intent_id == "partial":
                raise RuntimeError("plan")
            return None

        def resolve_unknown(self, intent_id, *, exchange_order_found):
            resolved.append((intent_id, exchange_order_found))

    engine._outbox = Outbox()
    engine._adjudicate_unknown_slice = lambda intent_id, *_args, **_kwargs: asyncio.sleep(
        0, result={"found": "FOUND", "absent": "ABSENT", "partial": "INCONCLUSIVE"}.get(intent_id, "INCONCLUSIVE")
    )
    engine._risk_sm = SimpleNamespace(rearm_for_retry=lambda _aid: None)
    engine._approval = SimpleNamespace(rearm_nonce=lambda _nonce: None)
    assert await engine._resolve_unknown_outbox_intents() == 2
    assert resolved == [("found", True), ("absent", False)]

    engine._adjudicate_unknown_slice = AutonomousEngine._adjudicate_unknown_slice.__get__(engine)
    transitions = []
    engine._outbox = SimpleNamespace(
        transition_execution_child=lambda *args, **kwargs: transitions.append((args, kwargs))
    )
    engine._store = SimpleNamespace(save_order_state=lambda **_kwargs: None)
    engine._order_trackers = {}
    engine._order_symbols = {}
    engine._active_order_ids = set()
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(0, result={"code": -2013, "msg": "Unknown order"})
    assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "cid", 0) == "ABSENT"
    assert transitions
    for raw in (
        None,
        [],
        {"code": -1003, "msg": "rate"},
        {"orderId": "1"},
        {"orderId": "1", "clientOrderId": "other", "symbol": "BTCUSDT"},
        {"orderId": "1", "clientOrderId": "cid", "symbol": "BTCUSDT", "status": "BAD", "executedQty": "1"},
        {"orderId": "1", "clientOrderId": "cid", "symbol": "BTCUSDT", "status": "NEW", "executedQty": "NaN"},
    ):
        engine._api_async = lambda *_args, raw=raw, **_kwargs: asyncio.sleep(0, result=raw)
        assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "cid", None) == "INCONCLUSIVE"

    engine._store = SimpleNamespace()
    engine._record_execution_fact_failure_env_guarded = lambda _reason: None
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(
        0,
        result={
            "orderId": "1",
            "clientOrderId": "cid",
            "symbol": "BTCUSDT",
            "status": "NEW",
            "executedQty": "0",
            "side": "BUY",
            "type": "LIMIT",
            "origQty": "1",
            "price": "100",
        },
    )
    assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "cid", None) == "INCONCLUSIVE"

    engine._store = SimpleNamespace(save_order_state=lambda **_kwargs: None)
    engine._book_venue_terminal_fill = lambda *_args, **_kwargs: asyncio.sleep(0, result=True)
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(
        0,
        result={
            "orderId": "2",
            "clientOrderId": "cid",
            "symbol": "BTCUSDT",
            "status": "FILLED",
            "executedQty": "1",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "1",
            "price": "0",
        },
    )
    assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "cid", 1) == "FOUND"
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(
        0,
        result={
            "orderId": "3",
            "clientOrderId": "cid",
            "symbol": "BTCUSDT",
            "status": "NEW",
            "executedQty": "0",
            "side": "BUY",
            "type": "LIMIT",
            "origQty": "1",
            "price": "100",
        },
    )
    assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "cid", 2) == "FOUND"


@pytest.mark.asyncio
async def test_engine_small_durable_and_readiness_failure_matrix() -> None:
    from beidou_safety.execution.ledger import ImmutableLedger

    engine = AutonomousEngine.__new__(AutonomousEngine)

    class Adapter:
        async def request(self, *_args, **_kwargs):
            return Result.success({"ok": True})

    engine._adapter = Adapter()
    assert await engine._api_async_safe("/health") == ({"ok": True}, True)
    assert (
        await engine.enqueue_reduce_only_market(
            symbol="BTCUSDT",
            side="BUY",
            quantity=0,
            correlation_id=None,
            policy_id="p",
            policy_version="v",
            policy_signature="s",
        )
        is False
    )

    engine._env_mode = SimpleNamespace(value="live")
    engine._can_write = True
    engine._control = SimpleNamespace(
        get_status=lambda: ControlAction.RESUME,
        execute_action=lambda _action: None,
    )
    with pytest.raises(RuntimeError, match="PROTECTION_CONFIG_UNKNOWN"):
        engine._require_protection_config("BTCUSDT", SimpleNamespace(metadata={"blocked": True}, stop_pct=0))
    assert engine._protection_config_unknown is True

    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME, execute_action=lambda _action: None)
    engine._safe_no_new_risk("test")
    engine._control = SimpleNamespace(
        get_status=lambda: ControlAction.RESUME,
        execute_action=lambda _action: (_ for _ in ()).throw(RuntimeError("control")),
    )
    engine._safe_no_new_risk("control-error")

    engine._order_trackers = {"o": SimpleNamespace(apply=lambda _event: None)}
    engine._active_order_ids = {"o"}
    recorded: list[str] = []
    engine._record_execution_fact_failure_env_guarded = lambda reason: recorded.append(reason)
    engine._store = SimpleNamespace(save_order_state=lambda *_args, **_kwargs: None)
    engine._mark_order_unknown("o", "BTCUSDT", "read failed")
    assert not engine._active_order_ids and recorded == ["read failed"]
    engine._store = SimpleNamespace(save_order_state=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("db")))
    engine._mark_order_unknown("missing", "BTCUSDT", "write failed")
    assert "UNKNOWN_PERSISTENCE_FAILED:OSError" in recorded[-1]

    # Restore a valid durable journal row and exercise both migration and
    # reconstruction guards.
    posting = {
        "posting_id": "p1",
        "account_id": "default",
        "account_type": "CASH",
        "venue_id": "BINANCE",
        "instrument_id": "BTCUSDT",
        "amount": "1",
        "currency": "USDT",
        "decimals": 8,
        "side": "DEBIT",
        "description": "",
    }
    posting2 = {**posting, "posting_id": "p2", "account_type": "POSITION_COST", "side": "CREDIT"}
    durable = {
        "transaction_id": "tx-1",
        "transaction_type": "FILL",
        "postings": [posting, posting2],
        "source_event_id": "fill-1",
        "correlation_id": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "is_correction": False,
        "reverses_transaction_id": "",
        "metadata": "{}",
    }
    engine._store = SimpleNamespace(restore_ledger_transactions=lambda: [durable], restore_ledger_entries=lambda: [])
    engine._ledger = ImmutableLedger()
    engine._restore_durable_ledger()
    assert engine._ledger.transaction_count == 1
    engine._store = SimpleNamespace(restore_ledger_transactions=lambda: [], restore_ledger_entries=lambda: [{}])
    with pytest.raises(RuntimeError, match="migration is required"):
        engine._restore_durable_ledger()
    engine._store = SimpleNamespace(
        restore_ledger_transactions=lambda: [{"transaction_id": "bad", "postings": [{}]}],
        restore_ledger_entries=lambda: [],
    )
    engine._ledger = ImmutableLedger()
    with pytest.raises(RuntimeError, match="Durable ledger reconstruction failed"):
        engine._restore_durable_ledger()


def test_engine_assess_coverage_and_readiness_remaining_branches() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = SimpleNamespace(value="live")
    engine._position_projection = {"BTCUSDT": {"position_generation": "bad"}}
    engine._position_generation = {"BTCUSDT": "bad"}
    engine._symbol_precision = {"BTCUSDT": {"min_quantity": "bad"}}
    ok, evidence = engine._assess_protection_coverage(
        [
            {"symbol": "", "positionAmt": "1"},
            {"symbol": "BTCUSDT", "positionAmt": "NaN"},
            {"symbol": "BTCUSDT", "positionAmt": "1"},
        ],
        [
            {
                "symbol": "BTCUSDT",
                "status": "ACTIVE",
                "exchange_order_id": "a",
                "owner_id": "",
                "side": "SELL",
                "position_generation": "1",
                "quantity": "bad",
                "order_type": "STOP_MARKET",
            },
        ],
        {},
    )
    assert ok is False
    assert {item["reason"] for item in evidence["unprotected_symbols"]} >= {
        "VENUE_POSITION_SYMBOL_UNKNOWN",
        "VENUE_POSITION_QUANTITY_UNKNOWN",
        "STOP_LOSS_QUANTITY_UNCOVERED",
    }

    engine._can_write = True
    engine._policy_error = "bad-policy"
    engine._state_backend_supported = True
    assert engine._check_ready() is False
    assert engine._check_trading_ready() == (False, "bad-policy")
    engine._policy_error = None
    engine._state_backend_supported = False
    assert engine._check_ready() is False
    assert engine._check_trading_ready() == (False, "STATE_BACKEND_UNSUPPORTED")


def test_engine_constructor_postgres_fallback_recovery_and_activation_branches(tmp_path, monkeypatch) -> None:
    import beidou_core.engine as engine_module
    import beidou_infra.outbox as pg_outbox_module
    import beidou_infra.postgres_store as pg_store_module
    from beidou_research.factors.factor import FactorRecord

    settings = TypedSettings(
        environment=Environment.TESTNET,
        database=DatabaseConfig(url="postgresql://user:pass@invalid/beidou"),
        exchange=ExchangeConfig(rest_base_url="https://testnet.invalid", ws_base_url="wss://testnet.invalid"),
        infrastructure=InfrastructureConfig(health_host="127.0.0.1", health_port=0),
    )

    class Store:
        def __init__(self) -> None:
            self.saved_events = []

        def restore_position_projection(self):
            return []

        def restore_protections(self):
            return [
                {"status": "INACTIVE", "owner_id": "beidou-autopilot", "exchange_order_id": "x"},
                {"status": "ACTIVE", "owner_id": "other", "position_id": "other", "exchange_order_id": "y"},
                {
                    "status": "ACTIVE",
                    "owner_id": "beidou-autopilot",
                    "position_id": "pos",
                    "symbol": "BTCUSDT",
                    "exchange_order_id": "algo-1",
                    "position_generation": "2",
                },
                {
                    "status": "ACTIVE",
                    "owner_id": "beidou-autopilot",
                    "position_id": "missing",
                    "symbol": "ETHUSDT",
                    "exchange_order_id": "",
                    "position_generation": "bad",
                },
            ]

        def restore_ledger_transactions(self):
            return []

        def restore_ledger_entries(self):
            return []

        def restore_trading_pool_state(self):
            return []

        def save_trading_pool_event(self, *args, **kwargs):
            self.saved_events.append((args, kwargs))

        def restore_order_states(self):
            return [{"order_id": "old", "filled_qty": "0.25"}]

        def restore_fill_events(self):
            return [
                {
                    "fill_event_id": "committed",
                    "order_id": "old",
                    "cumulative_qty": "0.2",
                    "processing_state": "COMMITTED",
                },
                {"fill_event_id": "pending", "order_id": "old", "cumulative_qty": "0.3", "processing_state": "PENDING"},
                {"fill_event_id": "", "order_id": "", "cumulative_qty": "0", "processing_state": "PENDING"},
            ]

        def restore_user_stream_projection(self, *_args):
            return None

    class Outbox:
        def __init__(self, **_kwargs):
            pass

        def recover_inflight(self):
            return 1

        def restore_pending_approvals(self):
            return [{"bad": True}]

    store = Store()
    monkeypatch.setattr(engine_module.ConfigProvider, "load", lambda _self, environment="": settings)
    monkeypatch.setattr(engine_module.PersistentStore, "get_instance", classmethod(lambda _cls, _path: store))
    # Patch the engine's dependency binding, rather than mutating the shared
    # class ``__new__``.  Special-method restoration can leave
    # ``object.__new__`` installed explicitly and make later real instances
    # reject their database-path argument.
    monkeypatch.setattr(engine_module, "IntentOutbox", lambda *a, **k: Outbox())
    monkeypatch.setattr(pg_store_module.PostgresPersistentStore, "get_instance", classmethod(lambda _cls, _url: store))
    monkeypatch.setattr(pg_outbox_module, "PostgresIntentOutbox", Outbox)
    monkeypatch.setattr(
        engine_module.PolicyLoader,
        "load",
        lambda _self, _pid: SimpleNamespace(validate_risk_parameters=lambda: (False, "missing"), parameters={}),
    )
    real_credential = engine_module.Credential
    monkeypatch.setattr(
        engine_module,
        "Credential",
        lambda **kwargs: real_credential(**kwargs, expires_at=datetime.now(timezone.utc) + timedelta(days=1)),
    )
    monkeypatch.setattr(
        engine_module.RiskApprovalSignerImpl,
        "restore_replay_state",
        lambda _self: (_ for _ in ()).throw(OSError("approval log")),
    )
    monkeypatch.setenv("BEIDOU_FENCING_TOKEN", "bad-token")
    monkeypatch.setenv("BEIDOU_CHAOS_ENABLED", "true")
    monkeypatch.setattr(FactorRecord, "has_authorized_active_evidence", lambda _self: True)

    engine = engine_module.AutonomousEngine(["BTCUSDT"], mode="paper")
    assert engine._state_backend_supported is False
    assert engine._state_backend_error == "FENCING_TOKEN_UNKNOWN"
    assert engine._chaos_engine is not None
    assert engine._active_algo_ids["pos"] == {"algo-1"}
    assert engine._pending_fill_retry == {"pending"}

    # A configured authority failure must fall back to an isolated diagnostic
    # store and remain blocked; this also exercises the explicit unsupported
    # database URL branch without touching the real workspace database.
    monkeypatch.setattr(
        pg_store_module.PostgresPersistentStore,
        "get_instance",
        classmethod(lambda _cls, _url: (_ for _ in ()).throw(OSError("postgres down"))),
    )
    fallback = engine_module.AutonomousEngine(["BTCUSDT"], mode="paper")
    assert fallback._state_backend_supported is False
    assert fallback._state_backend_error == "OSError"


@pytest.mark.asyncio
async def test_engine_execution_planner_market_cost_algorithm_and_binding_matrix(monkeypatch) -> None:
    import beidou_core.engine as engine_module
    from beidou_exchange.core.rule_snapshot import InstrumentRuleSnapshot
    from beidou_safety.execution.algorithms import ExecutionAlgorithmType, ExecutionPlan, OrderSlice
    from beidou_shared.types import OrderSide, OrderType, Price, Quantity, TimeInForce

    class Outbox:
        def __init__(self) -> None:
            self.actions: list[tuple] = []

        def reject(self, *args, **kwargs):
            self.actions.append(("reject", args, kwargs))

        def mark_unknown(self, *args, **kwargs):
            self.actions.append(("unknown", args, kwargs))

    class Feed:
        def __init__(self, book=None, features=None, book_error=None, feature_error=None) -> None:
            self.book, self.features = book, features
            self.book_error, self.feature_error = book_error, feature_error

        async def async_fetch_orderbook(self, *_args):
            if self.book_error:
                raise self.book_error
            return self.book

        async def async_update_features(self, *_args):
            if self.feature_error:
                raise self.feature_error
            return self.features or {}

    class CostModel:
        def __init__(self, error: Exception | None = None) -> None:
            self.error = error

        def estimate_order(self, *_args, **_kwargs):
            if self.error:
                raise self.error
            return SimpleNamespace(total_fee_bps=3.0)

    class Algo:
        algorithm_type = ExecutionAlgorithmType.TWAP

        def __init__(self, *, canceled=False, empty=False, bad_slice=False) -> None:
            self.canceled, self.empty, self.bad_slice = canceled, empty, bad_slice

        def plan(self, ctx, order_id):
            if self.canceled:
                return ExecutionPlan(algorithm=self.algorithm_type, is_canceled=True, cancel_reason="cancelled")
            if self.empty:
                return ExecutionPlan(algorithm=self.algorithm_type, slices=[])
            return ExecutionPlan(
                algorithm=self.algorithm_type,
                slices=[
                    OrderSlice(
                        slice_id="slice-0",
                        parent_order_id=order_id,
                        quantity=ctx.total_quantity,
                        price=ctx.limit_price,
                        order_type=OrderType.LIMIT,
                        time_in_force=TimeInForce.GTC,
                        algorithm=self.algorithm_type,
                        sequence_number=0,
                        invariants_check_passed=not self.bad_slice,
                    )
                ],
                total_estimated_cost_bps=ctx.predicted_cost_bps,
                estimated_completion_seconds=1.0,
            )

    intent = SimpleNamespace(
        intent_id="planner",
        instrument_id=__import__("beidou_shared.types", fromlist=["InstrumentId"]).InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(amount="1"),
        price=Price(amount="100"),
        time_in_force=TimeInForce.GTC,
        reduce_only=False,
        close_position=False,
        net_alpha_bps=1.0,
        correlation_id=None,
        idempotency_key="idem",
    )
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._outbox = Outbox()
    engine._symbol_precision = {"BTCUSDT": {"min_quantity": 0.1, "min_notional": 1}}
    engine._exec_quality_history = {}
    engine._apply_fee_tier = lambda *_args: None
    engine._adapter = SimpleNamespace(
        get_rule_snapshot=lambda _symbol: InstrumentRuleSnapshot.from_exchange_info(
            "BTCUSDT",
            {
                "symbol": "BTCUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.1", "stepSize": "0.1"},
                    {"filterType": "PRICE_FILTER", "tickSize": "1"},
                ],
            },
        )
    )

    engine._feed = Feed(book=None, book_error=OSError("book"), feature_error=OSError("features"))
    engine._cost_model = CostModel(error=RuntimeError("cost"))
    engine._exec_selector = SimpleNamespace(select=lambda _ctx: Algo())
    engine._quantize_slice_quantity = lambda _symbol, qty: qty
    reduce_intent = SimpleNamespace(
        **{**vars(intent), "reduce_only": True, "order_type": OrderType.MARKET, "price": None}
    )
    planned = await engine._plan_execution(reduce_intent, "BTCUSDT", "cid")
    assert planned is not None and planned[0][0][0] == "1"

    engine._feed = Feed(book={"bids": [["99", "2"]], "asks": [["101", "3"]]})
    negative = SimpleNamespace(**{**vars(intent), "net_alpha_bps": -1.0})
    assert await engine._plan_execution(negative, "BTCUSDT", "cid-neg") is None

    engine._cost_model = CostModel()
    engine._symbol_precision = {"BTCUSDT": {"min_quantity": 0}}
    assert await engine._plan_execution(intent, "BTCUSDT", "cid-min") is None
    engine._symbol_precision = {"BTCUSDT": {"min_quantity": 0.1, "min_notional": 1}}
    engine._exec_selector = SimpleNamespace(select=lambda _ctx: None)
    assert await engine._plan_execution(intent, "BTCUSDT", "cid-none") is None
    for algo in (Algo(canceled=True), Algo(empty=True)):
        engine._exec_selector = SimpleNamespace(select=lambda _ctx, algo=algo: algo)
        assert await engine._plan_execution(intent, "BTCUSDT", "cid-plan") is None

    engine._exec_selector = SimpleNamespace(select=lambda _ctx: Algo(bad_slice=True))
    assert await engine._plan_execution(intent, "BTCUSDT", "cid-bad-slice") is None
    engine._exec_selector = SimpleNamespace(select=lambda _ctx: Algo())
    engine._quantize_slice_quantity = lambda _symbol, _qty: None
    assert await engine._plan_execution(intent, "BTCUSDT", "cid-quantize") is None
    engine._quantize_slice_quantity = lambda _symbol, qty: qty
    monkeypatch.setattr(
        engine_module.SliceInvariantChecker, "validate_plan", staticmethod(lambda *_args: (False, "bad"))
    )
    assert await engine._plan_execution(intent, "BTCUSDT", "cid-invariant") is None
    monkeypatch.setattr(engine_module.SliceInvariantChecker, "validate_plan", staticmethod(lambda *_args: (True, "ok")))
    engine._validate_slices_against_intent = lambda *_args, **_kwargs: (False, "binding")
    assert await engine._plan_execution(intent, "BTCUSDT", "cid-binding") is None
    engine._validate_slices_against_intent = AutonomousEngine._validate_slices_against_intent
    final = await engine._plan_execution(intent, "BTCUSDT", "cid-ok")
    assert final is not None and len(final[0]) == 1


@pytest.mark.asyncio
async def test_engine_submit_slice_rule_quantity_price_ack_and_duplicate_matrix() -> None:
    from beidou_shared.types import (
        AccountId,
        AccountRef,
        InstrumentId,
        OrderSide,
        OrderStatus,
        OrderType,
        Price,
        Quantity,
        TimeInForce,
        VenueId,
    )

    class Snap:
        def __init__(
            self,
            *,
            known=True,
            stale=False,
            digest="h",
            quantity="1",
            price="100",
            min_qty="0.1",
            min_notional="1",
            quantity_error=None,
            price_error=None,
        ):
            self.is_known, self.is_stale, self.digest = known, stale, digest
            self.qty_precision, self.price_precision = 3, 2
            self.rule_version = 1
            self.step_size, self.tick_size = "0.1", "0.01"
            self.min_qty, self.min_notional = min_qty, min_notional
            self.quantity, self.price = quantity, price
            self.quantity_error, self.price_error = quantity_error, price_error

        def compute_hash(self):
            return self.digest

        def quantize_quantity(self, _text):
            if self.quantity_error:
                raise ValueError(self.quantity_error)
            return self.quantity

        def quantize_price(self, _text, *, side):
            if self.price_error:
                raise ValueError(self.price_error)
            return self.price

    class Adapter:
        def __init__(self, snap=None, response=None, query=None, exception=None):
            self.snap, self.response, self.query, self.exception = snap, response, query, exception

        def get_rule_snapshot(self, _symbol):
            return self.snap

        async def create_order(self, _request):
            if self.exception:
                raise self.exception
            return self.response

        async def query_order_by_client_id(self, *_args):
            if isinstance(self.query, Exception):
                raise self.query
            return self.query

    class Store:
        def __init__(self):
            self.states = []

        def save_order_state(self, *args, **kwargs):
            self.states.append((args, kwargs))

    def intent(*, reduce_only=False, order_type=OrderType.LIMIT, price="100"):
        return SimpleNamespace(
            intent_id="slice-intent",
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("default")),
            instrument_id=InstrumentId("BTCUSDT"),
            side=OrderSide.BUY,
            order_type=order_type,
            quantity=Quantity(amount="1"),
            price=Price(amount=price) if price is not None else None,
            time_in_force=TimeInForce.GTC,
            client_order_id="cid",
            correlation_id=None,
            reduce_only=reduce_only,
            close_position=False,
        )

    def make(*, snap=None, response=None, query=None, exception=None):
        e = AutonomousEngine.__new__(AutonomousEngine)
        e._adapter = Adapter(snap=snap, response=response, query=query, exception=exception)
        e._symbol_precision = {}
        e._rule_snapshot_hashes = {}
        e._rule_change_detected = set()
        e._close_order_ids = set()
        e._order_trackers = {}
        e._order_symbols = {}
        e._order_count = 0
        e._active_order_ids = set()
        e._owned_order_ids = set()
        e._store = Store()
        e._can_write = True
        e._verify_intent_at_send = lambda *_args, **_kwargs: asyncio.sleep(0, result=True)
        e._process_fill = lambda *_args, **_kwargs: asyncio.sleep(0)
        return e

    ack = SimpleNamespace(
        raw_response={"orderId": "venue-1", "status": "NEW", "reduceOnly": False, "stopPrice": "0"},
        status=OrderStatus.NEW,
    )
    e = make(snap=Snap(), response=ack)
    result = await e._submit_order_slice(
        intent(), {"quantity": "1", "price": "100", "newClientOrderId": "cid"}, "BTCUSDT", "BUY", "LIMIT", True
    )
    assert result["orderId"] == "venue-1" and e._active_order_ids == {"venue-1"}

    for snap, expected in (
        (Snap(known=False), "VENUE_RULE_SNAPSHOT_UNKNOWN_OR_STALE"),
        (Snap(stale=True), "VENUE_RULE_SNAPSHOT_UNKNOWN_OR_STALE"),
    ):
        e = make(snap=snap, response=ack)
        assert (
            await e._submit_order_slice(
                intent(), {"quantity": "1", "price": "100", "newClientOrderId": "cid"}, "BTCUSDT", "BUY", "LIMIT", False
            )
        )["reason"] == expected

    e = make(snap=Snap(digest="new"), response=ack)
    e._rule_snapshot_hashes["BTCUSDT"] = "old"
    assert (
        await e._submit_order_slice(
            intent(), {"quantity": "1", "price": "100", "newClientOrderId": "cid"}, "BTCUSDT", "BUY", "LIMIT", False
        )
    )["reason"] == "VENUE_RULE_SNAPSHOT_CHANGED"

    for snap, expected in (
        (Snap(quantity_error="bad"), "QUANTITY_QUANTIZATION_REJECTED"),
        (Snap(quantity="0.9"), "PLANNED_QUANTITY_NOT_VENUE_EXACT"),
        (Snap(min_qty="2"), "FINAL_QUANTITY_OUTSIDE_APPROVAL_OR_MIN_QTY"),
        (Snap(price_error="bad"), "PRICE_QUANTIZATION_REJECTED"),
        (Snap(price="99"), "PLANNED_PRICE_NOT_VENUE_EXACT"),
        (Snap(min_notional="1000"), "FINAL_ORDER_BELOW_MIN_NOTIONAL"),
    ):
        e = make(snap=snap, response=ack)
        outcome = await e._submit_order_slice(
            intent(), {"quantity": "1", "price": "100", "newClientOrderId": "cid"}, "BTCUSDT", "BUY", "LIMIT", False
        )
        assert outcome["reason"] == expected

    e = make(snap=Snap(), response=ack)
    e._verify_intent_at_send = lambda *_args, **_kwargs: asyncio.sleep(0, result=False)
    assert (
        await e._submit_order_slice(
            intent(), {"quantity": "1", "price": "100", "newClientOrderId": "cid"}, "BTCUSDT", "BUY", "LIMIT", True
        )
    )["reason"] == "FINAL_APPROVAL_CONSUMPTION_FAILED"

    e = make(snap=Snap(), response=SimpleNamespace(raw_response={}, status=OrderStatus.REJECTED))
    assert (
        await e._submit_order_slice(
            intent(), {"quantity": "1", "price": "100", "newClientOrderId": "cid"}, "BTCUSDT", "BUY", "LIMIT", False
        )
    )["reason"] == "ADAPTER_REJECTED_WITHOUT_ACK"
    e = make(snap=Snap(), response=SimpleNamespace(raw_response={}, status=OrderStatus.UNKNOWN))
    assert (
        await e._submit_order_slice(
            intent(), {"quantity": "1", "price": "100", "newClientOrderId": "cid"}, "BTCUSDT", "BUY", "LIMIT", False
        )
    )["reason"] == "ADAPTER_ACK_UNKNOWN"
    e = make(snap=Snap(), response=None, exception=TypeError("bad request"))
    assert (
        await e._submit_order_slice(
            intent(), {"quantity": "1", "price": "100", "newClientOrderId": "cid"}, "BTCUSDT", "BUY", "LIMIT", False
        )
    )["reason"].startswith("ADAPTER_REQUEST_INVALID")
    e = make(snap=Snap(), response=None, exception=OSError("offline"))
    assert (
        await e._submit_order_slice(
            intent(), {"quantity": "1", "price": "100", "newClientOrderId": "cid"}, "BTCUSDT", "BUY", "LIMIT", False
        )
    )["reason"].startswith("ADAPTER_EXCEPTION")

    duplicate = {
        "orderId": "existing",
        "status": "NEW",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "origQty": "1",
        "price": "100",
        "clientOrderId": "cid",
    }
    e = make(
        snap=Snap(),
        response=SimpleNamespace(raw_response={"code": -4141}, status=OrderStatus.REJECTED),
        query=Result.success(duplicate),
    )
    recovered = await e._submit_order_slice(
        intent(), {"quantity": "1", "price": "100", "newClientOrderId": "cid"}, "BTCUSDT", "BUY", "LIMIT", False
    )
    assert recovered["orderId"] == "existing"
    e = make(
        snap=Snap(),
        response=SimpleNamespace(raw_response={"code": -4141}, status=OrderStatus.REJECTED),
        query=Result.failure("query"),
    )
    assert (
        await e._submit_order_slice(
            intent(), {"quantity": "1", "price": "100", "newClientOrderId": "cid"}, "BTCUSDT", "BUY", "LIMIT", False
        )
    )["reason"] == "DUPLICATE_QUERY_UNKNOWN"
    e = make(
        snap=Snap(), response=SimpleNamespace(raw_response={"code": -1, "msg": "rejected"}, status=OrderStatus.REJECTED)
    )
    assert (
        await e._submit_order_slice(
            intent(), {"quantity": "1", "price": "100", "newClientOrderId": "cid"}, "BTCUSDT", "BUY", "LIMIT", False
        )
    )["_submit_outcome"] == "REJECTED"


@pytest.mark.asyncio
async def test_engine_place_order_paper_matching_and_executor_gate_matrix() -> None:
    from beidou_shared.types import InstrumentId, OrderSide, OrderType, Price, Quantity, TimeInForce

    class Outbox:
        def __init__(self):
            self.actions = []

        def reject(self, *args, **kwargs):
            self.actions.append(("reject", args, kwargs))

        def ack(self, *args, **kwargs):
            self.actions.append(("ack", args, kwargs))

        def renew_lease(self, *_args, **_kwargs):
            raise RuntimeError("lease")

        def dead_letter(self, *args, **kwargs):
            self.actions.append(("dead", args, kwargs))

    class Store:
        def __init__(self):
            self.states = []

        def save_order_state(self, *args, **kwargs):
            self.states.append((args, kwargs))

    class Matching:
        taker_fee_bps = 4.0

        def __init__(self, outcome):
            self.outcome = outcome

        def match(self, *_args):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

        def realized_cost_bps(self, *_args):
            return 5.0

    class Shadow:
        def __init__(self, *, fail: bool = False):
            self.fills, self.observations = [], []
            self.fail = fail

        def record_fill_to_ledger(self, *args, **kwargs):
            if self.fail:
                raise OSError("ledger")
            self.fills.append((args, kwargs))

        def record_execution_observation(self, **kwargs):
            self.observations.append(kwargs)

    def build(outcome, *, ticker=None, shadow_fail: bool = False):
        e = AutonomousEngine.__new__(AutonomousEngine)
        e._can_write = False
        e._state_backend_supported = True
        e._env_mode = SimpleNamespace(value="paper")
        e._control = SimpleNamespace(
            should_accept=lambda _intent: True, get_status=lambda: ControlAction.RESUME, version=1
        )
        e._outbox = Outbox()
        e._store = Store()
        e._feed = SimpleNamespace(get_last_ticker=lambda _symbol: ticker or {"bidPrice": "99", "askPrice": "101"})
        e._paper_matching = Matching(outcome)
        e._shadow_runner = Shadow(fail=shadow_fail)
        e._ledger = SimpleNamespace()
        e._paper_fills = []
        e._order_trackers = {}
        e._active_order_ids = set()
        e._owned_order_ids = set()
        e._order_count = 0
        e._intent_retry_count = {}
        e._verify_intent_at_send = lambda *_args, **_kwargs: asyncio.sleep(0, result=True)
        return e

    def make_intent(name="paper"):
        return SimpleNamespace(
            intent_id=name,
            instrument_id=InstrumentId("BTCUSDT"),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(amount="1"),
            price=Price(amount="100"),
            time_in_force=TimeInForce.GTC,
            client_order_id=f"cid-{name}",
            idempotency_key=f"idem-{name}",
            reduce_only=False,
            close_position=False,
        )

    for outcome, expected in (
        (("FILLED", 1.0, 100.0, 2.0), "ack"),
        (("PARTIALLY_FILLED", 0.4, 100.0, 2.0), "ack"),
        (("REJECTED", 0.0, 0.0, 2.0), "reject"),
        (("QUEUED", 0.0, 0.0, 2.0), "ack"),
    ):
        e = build(outcome)
        await e._place_order(make_intent(outcome[0].lower()))
        assert any(action[0] == expected for action in e._outbox.actions)
        assert e._paper_fills

    e = build(("FILLED", 1.0, 100.0, 2.0), ticker={"bidPrice": "0", "askPrice": "0"})
    await e._place_order(make_intent("bad-market"))
    assert e._outbox.actions[0][1][1] == "PAPER_MARKET_DATA_UNKNOWN"
    e = build(RuntimeError("matcher"))
    await e._place_order(make_intent("bad-matcher"))
    assert e._outbox.actions[0][1][1] == "PAPER_MATCHING_UNKNOWN"
    e = build(("FILLED", 1.0, 100.0, 2.0), shadow_fail=True)
    await e._place_order(make_intent("ledger-failure"))
    assert e._paper_fills[0]["status"] == "FILLED"

    e = build(("FILLED", 1.0, 100.0, 2.0))
    e._control = SimpleNamespace(should_accept=lambda _intent: False, get_status=lambda: ControlAction.LOCK, version=2)
    await e._place_order(make_intent("control"))
    assert e._outbox.actions[0][1][1].startswith("CONTROL_GATE_")
    e = build(("FILLED", 1.0, 100.0, 2.0))
    e._can_write = True
    e._state_backend_supported = False
    await e._place_order(make_intent("backend"))
    assert e._outbox.actions[0][1][1] == "STATE_BACKEND_UNSUPPORTED"
    e = build(("FILLED", 1.0, 100.0, 2.0))
    e._verify_intent_at_send = lambda *_args, **_kwargs: asyncio.sleep(0, result=False)
    await e._place_order(make_intent("approval"))
    assert e._outbox.actions[0][1][1] == "FINAL_APPROVAL_INVALID"
    e = build(("FILLED", 1.0, 100.0, 2.0))
    e._intent_retry_count["dead"] = 50
    await e._place_order(make_intent("dead"))
    assert e._outbox.actions[0][0] == "dead"


@pytest.mark.asyncio
async def test_engine_terminal_fill_booking_success_failure_and_partial_commit_matrix() -> None:
    class Store:
        def __init__(self, committed=True):
            self.committed = committed

        def save_fill_event(self, *_args, **_kwargs):
            return True

        def get_fill_event(self, _fill_id):
            return {"processing_state": "COMMITTED" if self.committed else "PENDING"}

    def build(store=None):
        e = AutonomousEngine.__new__(AutonomousEngine)
        e._store = store
        e._close_order_ids = set()
        e._order_trackers = {}
        e._order_symbols = {}
        e._active_order_ids = set()
        e._pending_fill_retry = set()
        e._consume_cumulative_fill = lambda *_args, **_kwargs: (0.25, 100.0, "fill-1")
        e._record_partial_fill_to_ledger = lambda *_args, **_kwargs: True
        e._mark_fill_retryable = lambda oid, fid: e._pending_fill_retry.add(f"{oid}:{fid}")
        e._mark_order_unknown = lambda *_args, **_kwargs: None
        return e

    e = build(None)
    assert await e._book_venue_terminal_fill("1", "BTCUSDT", {"executedQty": "bad"}) is False
    assert await e._book_venue_terminal_fill("1", "BTCUSDT", {"executedQty": "0"}) is False
    e = build(SimpleNamespace())
    assert await e._book_venue_terminal_fill("1", "BTCUSDT", {"executedQty": "1", "status": "FILLED"}) is True
    e = build(Store())
    e._process_fill = lambda *_args: asyncio.sleep(0)
    assert (
        await e._book_venue_terminal_fill(
            "1", "BTCUSDT", {"executedQty": "1", "status": "FILLED", "clientOrderId": "x-emg-y"}
        )
        is True
    )
    e = build(Store())
    e._process_fill = lambda *_args: (_ for _ in ()).throw(RuntimeError("fill"))
    assert await e._book_venue_terminal_fill("1", "BTCUSDT", {"executedQty": "1", "status": "FILLED"}) is False
    e = build(Store())
    assert await e._book_venue_terminal_fill("2", "BTCUSDT", {"executedQty": "0.25", "status": "CANCELED"}) is True
    e = build(Store(committed=False))
    assert await e._book_venue_terminal_fill("2", "BTCUSDT", {"executedQty": "0.25", "status": "EXPIRED"}) is False
    e = build(Store())
    e._record_partial_fill_to_ledger = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger"))
    assert await e._book_venue_terminal_fill("2", "BTCUSDT", {"executedQty": "0.25", "status": "CANCELED"}) is False
    e = build(Store())
    assert await e._book_venue_terminal_fill("3", "BTCUSDT", {"executedQty": "1", "status": "NEW"}) is False
