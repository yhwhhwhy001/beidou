"""Coverage-gap tests for beidou_launcher.supervisor.

Targets only the lines left uncovered by the existing suite:
``resolve_trading_pool_symbols`` error paths, the producer-only constructor
guard, the producer write interlock, ``_is_g5_producer_ready`` branches,
``trading_readiness`` control-state fallback, ``_classify_repairable``
non-dict/empty-gap evidence, the stuck-marker refresh inside ``_monitor`` and
the producer-only ``run`` path.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_control.plane import ControlPlane
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_launcher import supervisor as supervisor_module
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus
from beidou_launcher.supervisor import BeidouSupervisor, resolve_trading_pool_symbols


@pytest.fixture(autouse=True)
def _restore_cwd():
    original = Path.cwd()
    yield
    os.chdir(original)


def _supervisor(
    tmp_path: Path, *, mode: str = "testnet", symbols: list[str] | None = None, **kwargs
) -> BeidouSupervisor:
    return BeidouSupervisor(
        project_root=tmp_path,
        mode=mode,
        symbols=symbols if symbols is not None else ["BTCUSDT"],
        port=19099,
        **kwargs,
    )


def _settings(rest_url: str = "https://pool.invalid", max_instruments: object = 5) -> SimpleNamespace:
    return SimpleNamespace(
        exchange=SimpleNamespace(rest_base_url=rest_url),
        production=SimpleNamespace(max_instruments=max_instruments),
    )


# ---------------------------------------------------------------------------
# resolve_trading_pool_symbols error paths
# ---------------------------------------------------------------------------


def test_resolve_trading_pool_symbols_missing_exchange_attribute() -> None:
    with pytest.raises(RuntimeError, match="TRADING_POOL_ENDPOINT_UNKNOWN"):
        asyncio.run(resolve_trading_pool_symbols(SimpleNamespace()))


def test_resolve_trading_pool_symbols_empty_rest_url() -> None:
    with pytest.raises(RuntimeError, match="TRADING_POOL_ENDPOINT_UNKNOWN"):
        asyncio.run(resolve_trading_pool_symbols(_settings(rest_url="")))


def test_resolve_trading_pool_symbols_non_integer_capacity() -> None:
    with pytest.raises(RuntimeError, match="TRADING_POOL_CAPACITY_UNKNOWN"):
        asyncio.run(resolve_trading_pool_symbols(_settings(max_instruments="abc")))


def test_resolve_trading_pool_symbols_zero_capacity() -> None:
    with pytest.raises(RuntimeError, match="TRADING_POOL_CAPACITY_UNKNOWN"):
        asyncio.run(resolve_trading_pool_symbols(_settings(max_instruments=0)))


def test_resolve_trading_pool_symbols_exchange_info_failure(monkeypatch) -> None:
    import beidou_exchange.binance_usdm.adapter as adapter_module
    import beidou_exchange.binance_usdm.rest_client as rest_client_module

    class Result:
        def is_success(self) -> bool:
            return False

    class Client:
        def __init__(self, **kwargs) -> None:
            pass

        def close(self) -> None:
            pass

    class Adapter:
        def __init__(self, *, rest_client) -> None:
            pass

        async def request(self, method, path):
            return Result()

    monkeypatch.setattr(rest_client_module, "BinanceRESTClient", Client)
    monkeypatch.setattr(adapter_module, "BinanceUsdmAdapter", Adapter)

    with pytest.raises(RuntimeError, match="TRADING_POOL_EXCHANGE_INFO_UNKNOWN"):
        asyncio.run(resolve_trading_pool_symbols(_settings()))


def test_resolve_trading_pool_symbols_empty_candidates(monkeypatch) -> None:
    import beidou_data.trading_pool_lifecycle as tpl_module
    import beidou_exchange.binance_usdm.adapter as adapter_module
    import beidou_exchange.binance_usdm.rest_client as rest_client_module

    class Result:
        def __init__(self) -> None:
            self.data = {"symbols": []}

        def is_success(self) -> bool:
            return True

    class Client:
        def __init__(self, **kwargs) -> None:
            pass

        def close(self) -> None:
            pass

    class Adapter:
        def __init__(self, *, rest_client) -> None:
            pass

        async def request(self, method, path):
            return Result()

    monkeypatch.setattr(rest_client_module, "BinanceRESTClient", Client)
    monkeypatch.setattr(adapter_module, "BinanceUsdmAdapter", Adapter)
    monkeypatch.setattr(tpl_module, "discover_startup_candidates", lambda *a, **k: [])

    with pytest.raises(RuntimeError, match="TRADING_POOL_EMPTY"):
        asyncio.run(resolve_trading_pool_symbols(_settings()))


# ---------------------------------------------------------------------------
# Constructor guard and write interlock
# ---------------------------------------------------------------------------


def test_producer_constructor_requires_testnet(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="G5_PRODUCER_TESTNET_ONLY"):
        BeidouSupervisor(
            project_root=tmp_path,
            mode="paper",
            symbols=["BTCUSDT"],
            port=19099,
            producer_only=True,
        )


def test_producer_write_interlock_allows_only_listen_key(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, producer_only=True)
    calls: list[tuple[str, str]] = []

    def original_sync(path, method="GET", signed=False, params=None):
        calls.append(("sync", path))
        return {"ok": True}

    async def original_async(path, method="GET", signed=False, params=None):
        calls.append(("async", path))
        return {"ok": True}

    supervisor.engine = SimpleNamespace(
        _api_async=original_async,
        _api=original_sync,
        _adapter=None,
    )
    supervisor._install_exchange_write_interlock()

    blocked = supervisor.engine._api("/futures/order", method="POST")
    assert blocked["code"] == -3

    allowed = supervisor.engine._api(Endpoint.LISTEN_KEY, method="POST")
    assert allowed == {"ok": True}
    assert calls == [("sync", Endpoint.LISTEN_KEY)]


# ---------------------------------------------------------------------------
# _is_g5_producer_ready branches
# ---------------------------------------------------------------------------


def _ready_engine(**overrides) -> SimpleNamespace:
    base: dict = {
        "_running": True,
        "_control": SimpleNamespace(get_status=lambda: SimpleNamespace(value="NO_NEW_RISK")),
        "_state_backend_supported": True,
        "_last_reconciliation_result": SimpleNamespace(matched=True, checked_at=datetime.now(timezone.utc)),
        "_user_stream_readiness": lambda: (True, {}),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_g5_producer_ready_guards_and_success(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, producer_only=True)
    supervisor.report.supervisor_state = "RUNNING"
    supervisor.report.checks = []

    supervisor.engine = _ready_engine()
    assert supervisor._is_g5_producer_ready() is True

    supervisor.report.supervisor_state = "STARTING"
    assert supervisor._is_g5_producer_ready() is False
    supervisor.report.supervisor_state = "RUNNING"

    supervisor.engine = _ready_engine(_running=False)
    assert supervisor._is_g5_producer_ready() is False

    supervisor.engine = _ready_engine(_control=SimpleNamespace(get_status=lambda: SimpleNamespace(value="RESUME")))
    assert supervisor._is_g5_producer_ready() is False

    supervisor.engine = _ready_engine(_state_backend_supported=False)
    assert supervisor._is_g5_producer_ready() is False

    supervisor.engine = _ready_engine(_last_reconciliation_result=None)
    assert supervisor._is_g5_producer_ready() is False

    supervisor.engine = _ready_engine(
        _last_reconciliation_result=SimpleNamespace(matched=False, checked_at=datetime.now(timezone.utc))
    )
    assert supervisor._is_g5_producer_ready() is False

    supervisor.engine = _ready_engine(
        _last_reconciliation_result=SimpleNamespace(matched=True, checked_at="not-a-datetime")
    )
    assert supervisor._is_g5_producer_ready() is False

    old = datetime.now(timezone.utc) - timedelta(seconds=200)
    supervisor.engine = _ready_engine(_last_reconciliation_result=SimpleNamespace(matched=True, checked_at=old))
    assert supervisor._is_g5_producer_ready() is False

    supervisor.engine = _ready_engine(_user_stream_readiness=lambda: (False, {}))
    assert supervisor._is_g5_producer_ready() is False

    supervisor.engine = _ready_engine()
    supervisor.report.checks = [CheckResult("runtime.block", "block", CheckStatus.FAIL, CheckSeverity.P0, "blocked")]
    assert supervisor._is_g5_producer_ready() is False


# ---------------------------------------------------------------------------
# trading_readiness control-state fallback
# ---------------------------------------------------------------------------


class _Health:
    def __init__(self) -> None:
        self.trading_readiness = None
        self.metrics = None
        self.readiness = None
        self.liveness = None
        self.exit = None
        self.status = None
        self.factors = None
        self._metrics_collector = lambda: {}

    def set_metrics_collector(self, fn) -> None:
        self.metrics = fn

    def set_readiness_check(self, fn) -> None:
        self.readiness = fn

    def set_trading_readiness(self, fn) -> None:
        self.trading_readiness = fn

    def set_liveness_check(self, fn) -> None:
        self.liveness = fn

    def set_exit_readiness(self, fn) -> None:
        self.exit = fn

    def set_status_info(self, fn) -> None:
        self.status = fn

    def set_factor_provider(self, fn) -> None:
        self.factors = fn


def test_trading_readiness_reports_control_state_without_blockers(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, mode="paper")
    health = _Health()
    supervisor.engine = SimpleNamespace(
        _health=health,
        _control=SimpleNamespace(get_status=lambda: SimpleNamespace(value="PAUSED")),
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="ACTIVE")),
        _active_order_ids=set(),
        _recon=None,
        _factor_registry=None,
        _get_status_info=lambda: {},
    )
    supervisor._resume_authorized = False
    supervisor.report.checks = []
    supervisor._install_health_callbacks()
    assert health.trading_readiness() == (False, "CONTROL_PAUSED")


# ---------------------------------------------------------------------------
# _classify_repairable evidence edge cases
# ---------------------------------------------------------------------------


def test_classify_repairable_rejects_non_dict_evidence(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    blocker = CheckResult(
        "runtime.safety.protection_gap_detail",
        "gap",
        CheckStatus.FAIL,
        CheckSeverity.P0,
        "gap",
        evidence=["not", "a", "dict"],  # type: ignore[arg-type]
    )
    assert supervisor._classify_repairable([blocker]) is False


def test_classify_repairable_rejects_empty_gaps(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    blocker = CheckResult(
        "runtime.safety.protection_gap_detail",
        "gap",
        CheckStatus.FAIL,
        CheckSeverity.P0,
        "gap",
        evidence={"gaps": []},
    )
    assert supervisor._classify_repairable([blocker]) is False


# ---------------------------------------------------------------------------
# _monitor stuck-marker refresh
# ---------------------------------------------------------------------------


def test_monitor_refreshes_engine_stuck_marker(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor.monitor_interval = 0.001
    stuck_calls: list[str] = []
    supervisor.engine = SimpleNamespace(
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="LOCKED")),
        _control=SimpleNamespace(get_status=lambda: SimpleNamespace(value="NO_NEW_RISK")),
        _update_stuck_marker=lambda: stuck_calls.append("stuck"),
    )
    supervisor.writer = SimpleNamespace(write=lambda _report: None)
    supervisor._runtime_checks = lambda: []  # type: ignore[method-assign]
    supervisor._merge_monitoring_checks = lambda checks: checks  # type: ignore[method-assign]
    supervisor._refresh_exchange_account_snapshot = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
    supervisor._refresh_position_mode = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
    supervisor._refresh_exchange_algo_snapshot = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
    supervisor._record_g7_certification_evidence = lambda _checks: None  # type: ignore[method-assign]
    supervisor._apply_debounce_action = lambda *_args: asyncio.sleep(0)  # type: ignore[method-assign]
    supervisor._health_debounce.feed = lambda _value, **_kwargs: None
    supervisor._is_trading_ready = lambda: False  # type: ignore[method-assign]
    supervisor._g7_tracker = SimpleNamespace(
        set_durable_window_state=lambda **_kwargs: None,
        feed=lambda _checks: None,
        feed_recovery_context=lambda *_args: None,
    )

    async def scenario() -> int:
        supervisor._engine_task = asyncio.create_task(asyncio.sleep(0.01))
        return await supervisor._monitor()

    assert asyncio.run(scenario()) == 5
    assert stuck_calls and all(call == "stuck" for call in stuck_calls)


# ---------------------------------------------------------------------------
# producer-only run path
# ---------------------------------------------------------------------------


class _ProducerRunHealth:
    def __init__(self) -> None:
        self._port = 0
        self._metrics_collector = lambda: {}

    def __getattr__(self, name: str):
        if name.startswith("set_"):
            return lambda _fn: None
        raise AttributeError(name)


class _ProducerRunEngine:
    def __init__(self, *, symbols, mode, producer_only: bool = False) -> None:
        self.symbols = symbols
        self.mode = mode
        self.producer_only = producer_only
        self._health = _ProducerRunHealth()
        self._control = ControlPlane()
        self._api_async = lambda *_args, **_kwargs: asyncio.sleep(0, result={})
        self._api = lambda *_args, **_kwargs: {}
        self._adapter = SimpleNamespace(request=lambda *_args, **_kwargs: asyncio.sleep(0, result=None))
        self._factor_registry = None
        self._lifecycle = SimpleNamespace(state=SimpleNamespace(value="ACTIVE"))
        self._running = True

    async def run(self) -> None:
        return None


def _prepare_run_supervisor(tmp_path: Path) -> BeidouSupervisor:
    tmp_path.mkdir(parents=True, exist_ok=True)
    supervisor = _supervisor(tmp_path, symbols=[], producer_only=True)
    supervisor.writer = SimpleNamespace(write=lambda _report: None, state_path=tmp_path / "state.json")
    supervisor.lock = SimpleNamespace(acquire=lambda: (True, "locked"), release=lambda: None)
    return supervisor


def test_run_producer_path_resolves_symbols_and_defers_resume(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supervisor_module, "run_g5_producer_preflight", lambda *_args: ([], None))
    monkeypatch.setattr(supervisor_module, "inspect_engine_wiring", lambda *_args: [])

    async def resolve(_settings) -> list[str]:
        return ["BTCUSDT"]

    monkeypatch.setattr(supervisor_module, "resolve_trading_pool_symbols", resolve)
    monkeypatch.setattr("beidou_core.engine.AutonomousEngine", _ProducerRunEngine)

    supervisor = _prepare_run_supervisor(tmp_path)
    supervisor._wait_for_startup = lambda: asyncio.sleep(0, result=True)  # type: ignore[method-assign]

    async def monitored() -> int:
        return 7

    supervisor._monitor = monitored  # type: ignore[method-assign]

    assert asyncio.run(supervisor.run()) == 7
    assert supervisor._resume_authorized is False
    assert supervisor.report.symbols == ["BTCUSDT"]
    assert supervisor.engine.producer_only is True
