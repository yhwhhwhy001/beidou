"""一键启动模块的纯本地单元测试。"""

from __future__ import annotations

from pathlib import Path

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
