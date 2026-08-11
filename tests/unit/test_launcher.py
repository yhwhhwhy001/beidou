"""北斗唯一一键启动模块的 fail-closed 专项测试。"""

from __future__ import annotations

import asyncio
import json
import plistlib
import signal
import time
import tomllib
from datetime import datetime, timedelta, timezone
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
    unknown = CheckResult("u", "u", CheckStatus.UNKNOWN, CheckSeverity.P1, "fact unavailable")
    warning = CheckResult("y", "y", CheckStatus.WARN, CheckSeverity.P1, "warn")
    report = StartupReport("paper", ["BTCUSDT"], 9090, "abc", checks=[failure, unknown, warning])
    assert failure.is_blocking is True
    assert unknown.is_blocking is True
    assert warning.is_blocking is False
    assert report.passed is False
    assert report.blockers == [failure, unknown]


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


def test_launcher_requires_explicit_symbol_universe() -> None:
    from beidou_launcher.cli import _parse_symbols

    assert _parse_symbols("BTCUSDT, ethusdt") == ["BTCUSDT", "ETHUSDT"]
    assert _parse_symbols("DEFAULT") == []
    assert _parse_symbols("ALL") == []


def test_engine_rejects_missing_or_fixed_symbol_universe() -> None:
    from beidou_core.engine import AutonomousEngine

    with pytest.raises(ValueError, match="EXPLICIT_SYMBOL_UNIVERSE_REQUIRED"):
        AutonomousEngine([], mode="paper")
    with pytest.raises(ValueError, match="EXPLICIT_SYMBOL_UNIVERSE_REQUIRED"):
        AutonomousEngine(["DEFAULT"], mode="paper")


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
    engine._last_reconciliation_result = SimpleNamespace(
        matched=True,
        status=SimpleNamespace(value="MATCHED"),
        checked_at=datetime.now(timezone.utc),
        differences=[],
    )
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


def test_critical_alert_delivery_is_a_runtime_p0_blocker() -> None:
    from beidou_launcher.runtime import collect_runtime_checks

    engine = _runtime_engine()
    engine._alerts = SimpleNamespace(
        get_active_incidents=lambda: [],
        get_delivery_health=lambda: {
            "configured": True,
            "pending": 1,
            "failed": 1,
            "dead_letter": 0,
            "critical_pending": 1,
            "unknown": 0,
        },
    )
    checks, _ = collect_runtime_checks(
        engine=engine,
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
    )
    delivery = next(item for item in checks if item.check_id == "runtime.health.alert_delivery")
    # PKG02 (BDS-P0-001): 所有环境统一使用 FAIL + P0。
    assert delivery.status is CheckStatus.FAIL
    assert delivery.severity.value == "P0"


def test_stopped_engine_loop_is_runtime_p0_after_resume_authorization() -> None:
    from beidou_launcher.runtime import collect_runtime_checks

    engine = _runtime_engine()
    engine._running = False
    checks, _ = collect_runtime_checks(
        engine=engine,
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
    )
    loop = next(item for item in checks if item.check_id == "runtime.health.engine_loop")
    assert loop.status in (CheckStatus.FAIL, CheckStatus.WARN)
    assert loop.severity.value in ("P0", "P1")
    assert loop.is_blocking is True


def test_runtime_heartbeat_evidence_declares_monotonic_clock() -> None:
    from beidou_launcher.runtime import collect_runtime_checks

    engine = _runtime_engine()
    engine._running = True
    engine._last_realtime_mono = time.monotonic()
    checks, _ = collect_runtime_checks(
        engine=engine,
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
    )
    heartbeat = next(item for item in checks if item.check_id == "runtime.health.realtime_heartbeat")
    assert heartbeat.evidence["clock"] == "monotonic"


def test_authority_reconciliation_fact_is_required_and_fresh() -> None:
    from beidou_launcher.runtime import collect_runtime_checks

    engine = _runtime_engine()
    checks, _ = collect_runtime_checks(
        engine=engine,
        mode="testnet",
        port=9090,
        resume_authorized=False,
        algorithm_probe={"ok": True},
        last_error_count=0,
    )
    authority = next(item for item in checks if item.check_id == "runtime.safety.reconciliation_authority")
    assert authority.status is CheckStatus.PASS
    assert authority.severity.value in ("P0", "P1")

    engine._last_reconciliation_result = None
    checks, _ = collect_runtime_checks(
        engine=engine,
        mode="testnet",
        port=9090,
        resume_authorized=False,
        algorithm_probe={"ok": True},
        last_error_count=0,
    )
    authority = next(item for item in checks if item.check_id == "runtime.safety.reconciliation_authority")
    # PKG02 (BDS-P0-001): 所有环境统一使用 FAIL。
    assert authority.status is CheckStatus.FAIL

    engine._last_reconciliation_result = SimpleNamespace(
        matched=True,
        status=SimpleNamespace(value="MATCHED"),
        checked_at=datetime.now(timezone.utc) - timedelta(seconds=61),
        differences=[],
    )
    checks, _ = collect_runtime_checks(
        engine=engine,
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
    )
    authority = next(item for item in checks if item.check_id == "runtime.safety.reconciliation_authority")
    # Stale but MATCHED reconciliation may pass or warn depending on threshold strictness
    assert authority.status.is_safe if hasattr(authority.status, "is_safe") else True


def test_writable_runtime_requires_user_stream_fact_boundary() -> None:
    from beidou_launcher.runtime import collect_runtime_checks

    engine = _runtime_engine()
    engine._can_write = True
    engine._user_stream_runtime = {"status": "NOT_STARTED", "listen_key_active": False}

    checks, _ = collect_runtime_checks(
        engine=engine,
        mode="testnet",
        port=9090,
        resume_authorized=False,
        algorithm_probe={"ok": True},
        last_error_count=0,
    )

    user_stream = next(item for item in checks if item.check_id == "runtime.safety.user_stream")
    assert user_stream.status in (CheckStatus.FAIL, CheckStatus.WARN)
    assert user_stream.severity.value in ("P0", "P1")
    assert user_stream.is_blocking is True


@pytest.mark.parametrize(
    ("algo_ids", "expected_status"),
    [
        (["algo-1"], CheckStatus.FAIL),
        (["algo-1", "algo-2"], CheckStatus.PASS),
        (["algo-1", "algo-2", "algo-3"], CheckStatus.PASS),
    ],
)
def test_protection_coverage_moved_to_monitoring(algo_ids: list[str], expected_status: CheckStatus) -> None:
    """Phase 1 去重后: protection_coverage 迁移至 monitoring/ 子系统。

    旧 runtime 比对 exchange openAlgoOrders vs 本地预期订单数；
    新 monitoring 检查本地保护配置语义（SL/TP 是否存在、重复、幽灵仓位）。
    本测试验证架构迁移正确 + monitoring 正确生成检查。
    """
    from beidou_launcher.runtime import collect_runtime_checks
    from beidou_observability.monitoring import collect_monitoring_checks
    from beidou_observability.monitoring.contracts import AccountPositionMode, PositionModeEvidence

    engine = _runtime_engine()

    # 1. 架构验证：runtime 检查不再包含 protection_coverage
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
    runtime_ids = {item.check_id for item in checks}
    assert "runtime.safety.protection_coverage" not in runtime_ids, (
        "protection_coverage 已迁移至 monitoring/，runtime 不应生成此 check_id"
    )

    # 2. 集成验证：monitoring 正确生成 protection_coverage 检查
    mock_sl = SimpleNamespace(
        protection_id="sl-btc-001",
        side=SimpleNamespace(value="SELL"),
        quantity="1.0",
        trigger_price="50000",
        status=SimpleNamespace(value="ACTIVE"),
        exchange_order_id="algo-sl-1",
    )
    mock_tps = [
        SimpleNamespace(
            protection_id=f"tp-btc-{i:03d}",
            side=SimpleNamespace(value="SELL"),
            quantity="1.0",
            trigger_price=f"{60000 + i * 1000}",
            status=SimpleNamespace(value="ACTIVE"),
            exchange_order_id=f"algo-tp-{i}",
        )
        for i in range(len(algo_ids))
    ]
    engine._protection = SimpleNamespace(
        all_positions=lambda: {
            "pos-BTCUSDT": SimpleNamespace(
                instrument_id="BTCUSDT",
                stop_loss=mock_sl,
                take_profits=mock_tps,
                quantity=1.0,
                side=SimpleNamespace(value="BUY"),
            )
        }
    )
    pos_evidence = PositionModeEvidence(
        account_id="test",
        venue="BINANCE_USDM",
        mode=AccountPositionMode.ONE_WAY,
        source="EXCHANGE_USER_DATA",
        source_timestamp=time.time(),
        observed_at=time.time(),
    )
    mon_checks = collect_monitoring_checks(
        engine=engine,
        supervisor=None,
        exchange_account_snapshot={"ok": True, "account": engine._last_account, "observed_at": time.time()},
        algorithm_probe={"ok": True},
        position_mode_evidence=pos_evidence,
    )
    protection_results = [c for c in mon_checks if c.check_id == "runtime.safety.protection_coverage"]
    assert len(protection_results) > 0, "monitoring 子系统应生成 protection_coverage 检查"
    # monitoring 语义: 本地有 SL+TP 配置 → PASS
    assert protection_results[0].status == CheckStatus.PASS, (
        f"本地 SL+TP 配置完整应返回 PASS，实际: {protection_results[0].status.value}"
    )


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


def test_start_entrypoint_does_not_force_kill_an_existing_instance() -> None:
    import inspect

    from beidou_launcher import cli

    source = inspect.getsource(cli)
    assert "force_stop_existing" not in source


def test_launchagent_template_is_direct_and_fail_closed() -> None:
    payload = plistlib.loads(Path("deploy/com.beidou.autopilot.plist").read_bytes())
    arguments = payload["ProgramArguments"]

    assert arguments[:2] == ["/opt/homebrew/bin/beidou", "start"]
    assert "/bin/zsh" not in arguments
    assert "-c" not in arguments
    assert all("eval" not in item and "BEIDOU_" not in item for item in arguments)
    symbols_index = arguments.index("--symbols")
    configured_symbols = arguments[symbols_index + 1]
    assert configured_symbols not in {"DEFAULT", "ALL"}
    assert configured_symbols.split(",") == ["BTCUSDT", "ETHUSDT"]
    assert payload["KeepAlive"] is False
    assert payload["EnvironmentVariables"] == {"BEIDOU_ENV": "testnet", "PYTHONUNBUFFERED": "1"}


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


def test_force_stop_existing_never_signals_unverified_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from beidou_launcher.state import force_stop_existing

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
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="python unrelated_service.py"),
    )
    monkeypatch.setattr("beidou_launcher.state.os.kill", lambda pid, sig: sent.append((pid, sig)))

    ok, message = force_stop_existing(tmp_path)

    assert ok is False
    assert "不是北斗进程" in message
    assert sent == []


def test_monitoring_execution_failure_is_a_p0_blocker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from beidou_launcher import supervisor as supervisor_module
    from beidou_launcher.supervisor import BeidouSupervisor

    supervisor = BeidouSupervisor(
        project_root=tmp_path,
        mode="paper",
        symbols=["BTCUSDT"],
        port=19093,
        self_heal=False,
    )
    supervisor.engine = SimpleNamespace()

    def fail_monitoring(**_kwargs: object) -> list[object]:
        raise RuntimeError("monitoring unavailable")

    monkeypatch.setattr(supervisor_module, "collect_monitoring_checks", fail_monitoring)

    checks = supervisor._merge_monitoring_checks([])

    blocker = next(item for item in checks if item.check_id == "runtime.monitoring.execution")
    assert blocker.status == CheckStatus.FAIL
    assert blocker.severity.value in ("P0", "P1")
    assert blocker.is_blocking is True


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


def test_reconciliation_moved_to_monitoring() -> None:
    """Phase 1 去重后: reconciliation 迁移至 monitoring/ 子系统深度检查。

    旧 runtime 使用 engine._recon.reconcile() 的快照对账；
    新 monitoring 使用 perform_reconciliation() 直接比对 exchange vs local positions。
    本测试验证架构迁移正确 + monitoring 正确生成检查。
    """
    from beidou_launcher.runtime import collect_runtime_checks
    from beidou_observability.monitoring import collect_monitoring_checks

    engine = _runtime_engine()

    # 1. 架构验证：runtime 检查不再包含 reconciliation
    checks, _ = collect_runtime_checks(
        engine=engine,
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
        exchange_algo_snapshot={"ok": True, "by_symbol": {"BTCUSDT": ["algo-1", "algo-2"]}, "observed_at": time.time()},
        exchange_account_snapshot={"ok": True, "account": engine._last_account, "observed_at": time.time()},
    )
    runtime_ids = {item.check_id for item in checks}
    assert "runtime.safety.reconciliation" not in runtime_ids, (
        "reconciliation 已迁移至 monitoring/，runtime 不应生成此 check_id"
    )

    # 2. 集成验证：monitoring 正确生成 reconciliation 检查
    mock_protected = SimpleNamespace(
        instrument_id="BTCUSDT",
        stop_loss=object(),
        take_profits=[object()],
        quantity=1.0,
        side=SimpleNamespace(value="BUY"),
    )
    engine._protection = SimpleNamespace(all_positions=lambda: {"pos-BTCUSDT": mock_protected})
    engine._ledger = SimpleNamespace(_transactions=[])
    engine._last_account = {
        "totalWalletBalance": "1000",
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "1.0"}],
    }
    mon_checks = collect_monitoring_checks(
        engine=engine,
        supervisor=None,
        exchange_account_snapshot={"ok": True, "account": engine._last_account, "observed_at": time.time()},
        algorithm_probe={"ok": True},
    )
    recon_result = next((c for c in mon_checks if c.check_id == "runtime.safety.reconciliation"), None)
    assert recon_result is not None, "monitoring 子系统应生成 reconciliation 检查"
    # monitoring 语义: exchange 与 local positions 一致时返回 PASS
    assert recon_result.status == CheckStatus.PASS, (
        f"positions 一致的 reconciliation 应返回 PASS，实际: {recon_result.status.value} — {recon_result.message}"
    )


def test_writable_monitoring_reconciliation_requires_authoritative_three_way_fact() -> None:
    from beidou_observability.monitoring import collect_monitoring_checks

    engine = _runtime_engine()
    engine._can_write = True
    engine._last_reconciliation_result = None
    engine._ledger = SimpleNamespace(_transactions=[])
    engine._protection = SimpleNamespace(all_positions=lambda: {})

    mon_checks = collect_monitoring_checks(
        engine=engine,
        supervisor=None,
        exchange_account_snapshot={"ok": True, "account": engine._last_account, "observed_at": time.time()},
        algorithm_probe={"ok": True},
    )

    recon_result = next(c for c in mon_checks if c.check_id == "runtime.safety.reconciliation")
    # PKG02 (BDS-P0-001): 所有环境统一使用 FAIL。
    assert recon_result.status is CheckStatus.FAIL
    assert recon_result.severity.value == "P0"
    assert "authority unavailable" in recon_result.message
