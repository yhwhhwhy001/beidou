"""一键启动模块的纯本地单元测试。"""

from __future__ import annotations

import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from beidou_bootstrap.models import CheckResult, CheckSeverity, CheckStatus, StartupReport
from beidou_bootstrap.registry import EXPECTED_ALPHA_COMPONENTS, EXPECTED_FACTORS, REQUIRED_PACKAGES
from beidou_bootstrap.state import InstanceLock


def test_registry_is_complete() -> None:
    assert len(REQUIRED_PACKAGES) == 19
    assert len(EXPECTED_ALPHA_COMPONENTS) == 8
    assert EXPECTED_FACTORS == EXPECTED_ALPHA_COMPONENTS


def test_blocking_semantics() -> None:
    p0_failure = CheckResult(
        check_id="x",
        name="x",
        status=CheckStatus.FAIL,
        severity=CheckSeverity.P0,
        message="blocked",
    )
    warning = CheckResult(
        check_id="y",
        name="y",
        status=CheckStatus.WARN,
        severity=CheckSeverity.P1,
        message="warn",
    )
    report = StartupReport(mode="paper", symbols=["BTCUSDT"], port=9090, commit="abc", checks=[p0_failure, warning])
    assert p0_failure.is_blocking is True
    assert warning.is_blocking is False
    assert report.passed is False
    assert report.blockers == [p0_failure]


def test_instance_lock_removes_stale_pid(tmp_path: Path) -> None:
    path = tmp_path / "beidou.pid"
    path.write_text("99999999", encoding="utf-8")
    lock = InstanceLock(path)
    ok, _ = lock.acquire()
    try:
        assert ok is True
        assert int(path.read_text(encoding="utf-8")) > 0
    finally:
        lock.release()
    assert not path.exists()


def test_console_script_aliases() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts == {
        "beidou": "beidou_bootstrap.cli:main",
        "北斗": "beidou_bootstrap.cli:main",
        "bd": "beidou_bootstrap.cli:main",
    }


def _runtime_engine(server_algo_count: int) -> Any:
    from beidou_bootstrap.registry import REQUIRED_ENGINE_ATTRIBUTES

    class Component:
        def validate(self) -> bool:
            return True

    class Graph:
        _components = {item: Component() for item in EXPECTED_ALPHA_COMPONENTS}

        def topological_order(self) -> list[str]:
            return list(self._components)

    class FactorRecord:
        lifecycle = SimpleNamespace(value="ACTIVE")

    class Pool:
        @staticmethod
        def active_count() -> int:
            return 1

    class StrategyRisk:
        @staticmethod
        def get_budget(_strategy_id: object) -> object:
            return object()

    class Feed:
        _last_ticker = {"BTCUSDT": {"lastPrice": "1"}}
        _last_orderbook = {"BTCUSDT": {"bids": [["1", "1"]], "asks": [["1", "1"]]}}

        @staticmethod
        def is_healthy() -> bool:
            return True

    class Thread:
        @staticmethod
        def is_alive() -> bool:
            return True

    protected_position = SimpleNamespace(
        instrument_id="BTCUSDT",
        stop_loss=object(),
        take_profits=[object()],
    )
    engine = SimpleNamespace()
    for attr in REQUIRED_ENGINE_ATTRIBUTES:
        setattr(engine, attr, object())
    engine._alpha_graph = Graph()
    engine._factor_registry = SimpleNamespace(_factors={item: FactorRecord() for item in EXPECTED_FACTORS})
    engine._trading_pool = Pool()
    engine._strategy_risk = StrategyRisk()
    engine._autopilot_strategy_id = "autopilot"
    engine._lifecycle = SimpleNamespace(state=SimpleNamespace(value="ACTIVE"))
    engine._feed = Feed()
    engine._health = SimpleNamespace(_thread=Thread())
    engine._tick_count = 1
    engine._last_realtime = time.time()
    engine._last_nearline = time.time()
    engine._error_count = 0
    engine._last_account = {
        "totalWalletBalance": "1000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}],
    }
    engine._protection = SimpleNamespace(all_positions=lambda: {"pos-recovered-BTCUSDT": protected_position})
    engine._active_algo_ids = {"pos-recovered-BTCUSDT": {str(item) for item in range(server_algo_count)}}
    engine._alerts = SimpleNamespace(get_active_incidents=lambda: [])
    return engine


def test_protection_coverage_requires_all_exchange_algo_orders() -> None:
    from beidou_bootstrap.runtime import collect_runtime_checks

    checks, _ = collect_runtime_checks(
        engine=_runtime_engine(server_algo_count=1),
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
    )
    protection = next(item for item in checks if item.check_id == "runtime.safety.protection_coverage")
    assert protection.status == CheckStatus.FAIL
    assert protection.evidence["missing"] == ["BTCUSDT"]

    checks, _ = collect_runtime_checks(
        engine=_runtime_engine(server_algo_count=2),
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
    )
    protection = next(item for item in checks if item.check_id == "runtime.safety.protection_coverage")
    assert protection.status == CheckStatus.PASS
    assert protection.evidence["missing"] == []
