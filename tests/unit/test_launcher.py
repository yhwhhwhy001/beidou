"""北斗唯一一键启动模块的 fail-closed 专项测试。"""

from __future__ import annotations

import asyncio
import json
import signal
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus, StartupReport
from beidou_launcher.registry import EXPECTED_ALPHA_COMPONENTS, EXPECTED_FACTORS, REQUIRED_PACKAGES
from beidou_launcher.state import InstanceLock


def test_registry_is_complete() -> None:
    assert len(REQUIRED_PACKAGES) == 19
    assert len(EXPECTED_ALPHA_COMPONENTS) == 8
    assert EXPECTED_FACTORS == EXPECTED_ALPHA_COMPONENTS


def test_blocking_semantics() -> None:
    failure = CheckResult("x", "x", CheckStatus.FAIL, CheckSeverity.P0, "blocked")
    warning = CheckResult("y", "y", CheckStatus.WARN, CheckSeverity.P1, "warn")
    report = StartupReport("paper", ["BTCUSDT"], 9090, "abc", checks=[failure, warning])
    assert failure.is_blocking is True
    assert warning.is_blocking is False
    assert report.passed is False
    assert report.blockers == [failure]


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
    scripts = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts == {
        "beidou": "beidou_launcher.cli:main",
        "北斗": "beidou_launcher.cli:main",
        "bd": "beidou_launcher.cli:main",
    }


def _runtime_engine() -> Any:
    from beidou_launcher.registry import REQUIRED_ENGINE_ATTRIBUTES

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

    engine = SimpleNamespace()
    for attr in REQUIRED_ENGINE_ATTRIBUTES:
        setattr(engine, attr, object())
    engine._alpha_graph = Graph()
    engine._factor_registry = SimpleNamespace(_factors={item: FactorRecord() for item in EXPECTED_FACTORS})
    engine._trading_pool = Pool()
    engine._strategy_risk = StrategyRisk()
    engine._autopilot_strategy_id = "autopilot"
    engine._lifecycle = SimpleNamespace(state=SimpleNamespace(value="ACTIVE"))
    engine._control = SimpleNamespace(get_status=lambda: SimpleNamespace(value="RESUME"))
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
    protected = SimpleNamespace(instrument_id="BTCUSDT", stop_loss=object(), take_profits=[object()])
    engine._protection = SimpleNamespace(all_positions=lambda: {"pos-BTCUSDT": protected})
    engine._recon = Reconciliation()
    engine._alerts = SimpleNamespace(get_active_incidents=lambda: [])
    return engine


@pytest.mark.parametrize(
    ("algo_ids", "expected_status"),
    [
        (["algo-1"], CheckStatus.FAIL),
        (["algo-1", "algo-2"], CheckStatus.PASS),
        (["algo-1", "algo-2", "algo-3"], CheckStatus.PASS),
    ],
)
def test_protection_requires_exact_exchange_orders(algo_ids: list[str], expected_status: CheckStatus) -> None:
    from beidou_launcher.runtime import collect_runtime_checks

    engine = _runtime_engine()
    checks, _ = collect_runtime_checks(
        engine=engine,
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
        exchange_algo_snapshot={"ok": True, "by_symbol": {"BTCUSDT": algo_ids}, "observed_at": time.time()},
        exchange_account_snapshot={"ok": True, "account": engine._last_account, "observed_at": time.time()},
    )
    result = next(item for item in checks if item.check_id == "runtime.safety.protection_coverage")
    assert result.status == expected_status


def test_stop_requires_fresh_matching_process_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from beidou_launcher.state import stop_running_instance

    runtime_dir = tmp_path / ".beidou"
    runtime_dir.mkdir()
    (runtime_dir / "beidou.pid").write_text("321", encoding="utf-8")
    (runtime_dir / "supervisor-state.json").write_text(
        json.dumps({"pid": 321, "updated_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "beidou_launcher.state.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="python -m beidou_launcher"),
    )
    monkeypatch.setattr("beidou_launcher.state.os.kill", lambda pid, sig: sent.append((pid, sig)))
    ok, message = stop_running_instance(tmp_path)
    assert ok is True
    assert "已验证" in message
    assert sent == [(321, signal.SIGTERM)]


def test_stop_rejects_pid_state_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from beidou_launcher.state import stop_running_instance

    runtime_dir = tmp_path / ".beidou"
    runtime_dir.mkdir()
    (runtime_dir / "beidou.pid").write_text("321", encoding="utf-8")
    (runtime_dir / "supervisor-state.json").write_text(
        json.dumps({"pid": 999, "updated_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "beidou_launcher.state.os.kill",
        lambda _pid, _sig: (_ for _ in ()).throw(AssertionError("must not signal")),
    )
    ok, message = stop_running_instance(tmp_path)
    assert ok is False
    assert "不一致" in message


def test_nonwrite_exchange_interlock_blocks_mutations(tmp_path: Path) -> None:
    from beidou_launcher.supervisor import BeidouSupervisor

    calls: list[tuple[str, str]] = []

    async def original_async(
        path: str, method: str = "GET", signed: bool = False, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        del signed, params
        calls.append((method, path))
        return {"ok": True}

    def original_sync(
        path: str, method: str = "GET", signed: bool = False, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        del signed, params
        calls.append((method, path))
        return {"ok": True}

    supervisor = BeidouSupervisor(project_root=tmp_path, mode="paper", symbols=["BTCUSDT"], port=9090)
    supervisor.engine = SimpleNamespace(_can_write=False, _api_async=original_async, _api=original_sync)
    supervisor._install_exchange_write_interlock()
    assert asyncio.run(supervisor.engine._api_async("/time")) == {"ok": True}
    assert asyncio.run(supervisor.engine._api_async("/order", method="POST"))["error"] == -3
    assert supervisor.engine._api("/order", method="DELETE")["error"] == -3
    assert calls == [("GET", "/time")]


def test_reconciliation_mismatch_blocks_runtime() -> None:
    from beidou_launcher.runtime import collect_runtime_checks

    engine = _runtime_engine()
    facts = SimpleNamespace(timestamp=datetime.now(timezone.utc))
    engine._recon = SimpleNamespace(
        reconcile=lambda _account, _venue: SimpleNamespace(
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
        exchange_account_snapshot={"ok": True, "account": engine._last_account, "observed_at": time.time()},
    )
    result = next(item for item in checks if item.check_id == "runtime.safety.reconciliation")
    assert result.status == CheckStatus.FAIL
    assert result.is_blocking is True
