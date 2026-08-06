"""一键启动模块的纯本地单元测试。"""

from __future__ import annotations

import json
import signal
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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
            return SimpleNamespace(
                max_drawdown_pct=20.0,
                max_daily_loss_pct=5.0,
                max_position_notional=500000.0,
                max_leverage=3.0,
                risk_per_trade_pct=1.0,
                max_consecutive_losses=5,
            )

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

    class Reconciliation:
        @staticmethod
        def reconcile(_account_id: object, _venue_id: object) -> object:
            facts = SimpleNamespace(timestamp=datetime.now(timezone.utc))
            return SimpleNamespace(
                status=SimpleNamespace(value="MATCHED"),
                differences=[],
                exchange_facts=facts,
                system_facts=facts,
            )

    protected_position = SimpleNamespace(
        instrument_id="BTCUSDT",
        stop_loss=object(),
        take_profits=[object()],
    )
    engine = SimpleNamespace()
    for attr in REQUIRED_ENGINE_ATTRIBUTES:
        setattr(engine, attr, object())
    engine._alpha_graph = Graph()
    engine._factor_registry = SimpleNamespace(
        _factors={item: FactorRecord() for item in EXPECTED_FACTORS}
    )
    engine._trading_pool = Pool()
    engine._strategy_risk = StrategyRisk()
    engine._autopilot_strategy_id = "autopilot"
    engine._lifecycle = SimpleNamespace(state=SimpleNamespace(value="ACTIVE"))
    engine._feed = Feed()
    engine._health = SimpleNamespace(_thread=Thread())
    engine._tick_count = 1
    engine._last_realtime = time.time()
    engine._last_nearline = time.time()
    engine._last_recon = time.time()
    engine._error_count = 0
    engine._last_account = {
        "totalWalletBalance": "1000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}],
    }
    engine._protection = SimpleNamespace(
        all_positions=lambda: {"pos-recovered-BTCUSDT": protected_position}
    )
    engine._active_algo_ids = {
        "pos-recovered-BTCUSDT": {str(item) for item in range(server_algo_count)}
    }
    engine._recon = Reconciliation()
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
        exchange_algo_snapshot={
            "ok": True,
            "by_symbol": {"BTCUSDT": ["algo-1"]},
            "observed_at": time.time(),
        },
        exchange_account_snapshot={
            "ok": True,
            "account": _runtime_engine(server_algo_count=1)._last_account,
            "observed_at": time.time(),
        },
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
        exchange_algo_snapshot={
            "ok": True,
            "by_symbol": {"BTCUSDT": ["algo-1", "algo-2"]},
            "observed_at": time.time(),
        },
        exchange_account_snapshot={
            "ok": True,
            "account": _runtime_engine(server_algo_count=2)._last_account,
            "observed_at": time.time(),
        },
    )
    protection = next(item for item in checks if item.check_id == "runtime.safety.protection_coverage")
    assert protection.status == CheckStatus.PASS
    assert protection.evidence["missing"] == []

    checks, _ = collect_runtime_checks(
        engine=_runtime_engine(server_algo_count=3),
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
        exchange_algo_snapshot={
            "ok": True,
            "by_symbol": {"BTCUSDT": ["algo-1", "algo-2", "algo-3"]},
            "observed_at": time.time(),
        },
        exchange_account_snapshot={
            "ok": True,
            "account": _runtime_engine(server_algo_count=3)._last_account,
            "observed_at": time.time(),
        },
    )
    protection = next(item for item in checks if item.check_id == "runtime.safety.protection_coverage")
    assert protection.status == CheckStatus.FAIL
    assert protection.evidence["missing"] == ["BTCUSDT"]


def test_stop_requires_fresh_matching_process_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from beidou_bootstrap.state import stop_running_instance

    runtime_dir = tmp_path / ".beidou"
    runtime_dir.mkdir()
    (runtime_dir / "beidou.pid").write_text("321", encoding="utf-8")
    (runtime_dir / "supervisor-state.json").write_text(
        json.dumps({"pid": 321, "updated_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )

    sent: list[tuple[int, int]] = []

    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(returncode=0, stdout="python -m beidou_bootstrap")

    def fake_kill(pid: int, sig: int) -> None:
        sent.append((pid, sig))

    monkeypatch.setattr("beidou_bootstrap.state.subprocess.run", fake_run)
    monkeypatch.setattr("beidou_bootstrap.state.os.kill", fake_kill)

    ok, message = stop_running_instance(tmp_path)
    assert ok is True
    assert "已验证" in message
    assert sent == [(321, signal.SIGTERM)]


def test_stop_rejects_pid_state_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from beidou_bootstrap.state import stop_running_instance

    runtime_dir = tmp_path / ".beidou"
    runtime_dir.mkdir()
    (runtime_dir / "beidou.pid").write_text("321", encoding="utf-8")
    (runtime_dir / "supervisor-state.json").write_text(
        json.dumps({"pid": 999, "updated_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )

    def forbidden_kill(_pid: int, _sig: int) -> None:
        raise AssertionError("PID mismatch must not send a signal")

    monkeypatch.setattr("beidou_bootstrap.state.os.kill", forbidden_kill)
    ok, message = stop_running_instance(tmp_path)
    assert ok is False
    assert "不一致" in message


def test_nonwrite_exchange_interlock_blocks_mutations(tmp_path: Path) -> None:
    import asyncio

    from beidou_bootstrap.supervisor import StartupSupervisor

    calls: list[tuple[str, str]] = []

    async def original_async(
        path: str,
        method: str = "GET",
        signed: bool = False,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del signed, params
        calls.append((method, path))
        return {"ok": True}

    def original_sync(
        path: str,
        method: str = "GET",
        signed: bool = False,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del signed, params
        calls.append((method, path))
        return {"ok": True}

    supervisor = StartupSupervisor(
        project_root=tmp_path,
        mode="paper",
        symbols=["BTCUSDT"],
        port=9090,
    )
    supervisor.engine = SimpleNamespace(
        _can_write=False,
        _api_async=original_async,
        _api=original_sync,
    )
    supervisor._install_exchange_write_interlock()

    assert asyncio.run(supervisor.engine._api_async("/time")) == {"ok": True}
    blocked = asyncio.run(
        supervisor.engine._api_async("/fapi/v1/order", method="POST", signed=True, params={})
    )
    assert blocked["error"] == -3
    assert supervisor.engine._api("/fapi/v1/order", method="DELETE")["error"] == -3
    assert calls == [("GET", "/time")]
    assert len(supervisor.engine._supervisor_blocked_writes) == 2


def test_reconciliation_mismatch_blocks_runtime() -> None:
    from beidou_bootstrap.runtime import collect_runtime_checks

    engine = _runtime_engine(server_algo_count=2)
    facts = SimpleNamespace(timestamp=datetime.now(timezone.utc))
    engine._recon = SimpleNamespace(
        reconcile=lambda _account_id, _venue_id: SimpleNamespace(
            status=SimpleNamespace(value="MISMATCHED"),
            differences=["Position mismatch"],
            exchange_facts=facts,
            system_facts=facts,
        )
    )
    checks, _ = collect_runtime_checks(
        engine=engine,
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
        exchange_algo_snapshot={
            "ok": True,
            "by_symbol": {"BTCUSDT": ["algo-1", "algo-2"]},
            "observed_at": time.time(),
        },
        exchange_account_snapshot={
            "ok": True,
            "account": engine._last_account,
            "observed_at": time.time(),
        },
    )
    reconciliation = next(item for item in checks if item.check_id == "runtime.safety.reconciliation")
    assert reconciliation.status == CheckStatus.FAIL
    assert reconciliation.is_blocking is True
    assert reconciliation.evidence["status"] == "MISMATCHED"
