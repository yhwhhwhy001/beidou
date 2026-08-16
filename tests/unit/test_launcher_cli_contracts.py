from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import beidou_launcher.checks as checks_module
import beidou_launcher.cli as cli_module
import beidou_launcher.state as state_module
from beidou_launcher.checks import PreflightChecker, find_project_root
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus


@pytest.fixture(autouse=True)
def _restore_cwd():
    """M21-F01: cli.main 会 os.chdir(project_root) —— 进程级副作用。

    测试用 monkeypatch 把 root 指向 tmp_path(chdir 跟随),但 invoke
    结束后 CWD 停在 tmp_path(测试收尾时已删除),后续测试/插件以
    相对路径写文件会落到悬空目录。每个测试后恢复真实 CWD。
    """
    import os

    original = Path.cwd()
    yield
    os.chdir(original)


def _check(*, blocking: bool) -> CheckResult:
    return CheckResult(
        check_id="check",
        name="check",
        status=CheckStatus.FAIL if blocking else CheckStatus.PASS,
        severity=CheckSeverity.P0,
        message="result",
    )


def test_find_project_root_explicit_ancestor_package_fallback_and_failure(tmp_path, monkeypatch) -> None:
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    monkeypatch.setenv("BEIDOU_PROJECT_ROOT", str(explicit))
    assert find_project_root() == explicit.resolve()

    monkeypatch.delenv("BEIDOU_PROJECT_ROOT")
    project = tmp_path / "project"
    nested = project / "nested" / "path"
    nested.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='test'\n")
    (project / "beidou_core").mkdir()
    assert find_project_root(nested) == project.resolve()

    package = tmp_path / "package"
    (package / "beidou_launcher").mkdir(parents=True)
    (package / "pyproject.toml").write_text("[project]\nname='test'\n")
    (package / "beidou_core").mkdir()
    monkeypatch.setattr(checks_module, "__file__", str(package / "beidou_launcher" / "checks.py"))
    assert find_project_root(tmp_path / "outside") == package.resolve()

    (package / "pyproject.toml").unlink()
    with pytest.raises(FileNotFoundError, match="Unable to locate"):
        find_project_root(tmp_path / "outside")


def test_preflight_checker_builds_non_ready_report_from_authoritative_checks(tmp_path, monkeypatch) -> None:
    check = _check(blocking=True)
    monkeypatch.setattr(checks_module, "run_preflight", lambda root, mode, port: ([check], object()))
    monkeypatch.setattr(checks_module, "current_commit", lambda root: "commit")

    checker = PreflightChecker("testnet", ["BTCUSDT"], port=19090, project_root=tmp_path)
    report = checker.run()

    assert report.mode == "testnet"
    assert report.symbols == ["BTCUSDT"]
    assert report.port == 19090
    assert report.commit == "commit"
    assert report.checks == [check]
    assert report.phase == "PREFLIGHT"
    assert report.supervisor_state == "PREFLIGHT"
    assert not report.passed


def _prepare_cli_root(tmp_path: Path, monkeypatch) -> None:
    # The real CLI changes into the project root for the lifetime of its
    # process. Record the test process cwd so pytest restores it afterwards.
    monkeypatch.chdir(Path.cwd())
    (tmp_path / ".env").write_text(
        '# comment\nFROM_DOTENV=loaded\nPRESERVED=from-file\nINVALID_LINE\nQUOTED="quoted-value"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PRESERVED", "from-process")
    monkeypatch.setattr(cli_module, "find_project_root", lambda: tmp_path)


def test_cli_doctor_status_and_stop_actions(tmp_path, monkeypatch) -> None:
    _prepare_cli_root(tmp_path, monkeypatch)
    runner = CliRunner()

    monkeypatch.setattr(cli_module, "run_preflight", lambda root, mode, port: ([_check(blocking=False)], None))
    result = runner.invoke(cli_module.main, ["doctor", "--mode", "paper"])
    assert result.exit_code == 0
    assert '"status": "PASS"' in result.output
    assert cli_module.os.environ["BEIDOU_ENV"] == "paper"
    assert "FROM_DOTENV" not in cli_module.os.environ
    assert cli_module.os.environ["PRESERVED"] == "from-process"
    assert "QUOTED" not in cli_module.os.environ

    monkeypatch.setattr(cli_module, "run_preflight", lambda root, mode, port: ([_check(blocking=True)], None))
    assert runner.invoke(cli_module.main, ["doctor"]).exit_code == 2
    assert cli_module.os.environ["BEIDOU_ENV"] == "safety_only"

    monkeypatch.setattr(cli_module, "inspect_runtime_status", lambda root: None)
    missing = runner.invoke(cli_module.main, ["status"])
    assert missing.exit_code == 1
    assert "未发现监督器状态证据" in missing.output

    monkeypatch.setattr(cli_module, "inspect_runtime_status", lambda root: {"state": "RUNNING"})
    status = runner.invoke(cli_module.main, ["status"])
    assert status.exit_code == 0
    assert '"state": "RUNNING"' in status.output

    monkeypatch.setattr(cli_module, "stop_running_instance", lambda root: (True, "stopped"))
    stopped = runner.invoke(cli_module.main, ["stop"])
    assert stopped.exit_code == 0 and "stopped" in stopped.output
    monkeypatch.setattr(cli_module, "stop_running_instance", lambda root: (False, "not-running"))
    assert runner.invoke(cli_module.main, ["stop"]).exit_code == 1


def test_cli_start_requires_bounded_symbols_and_runs_supervisor(tmp_path, monkeypatch) -> None:
    _prepare_cli_root(tmp_path, monkeypatch)
    runner = CliRunner()
    assert runner.invoke(cli_module.main, ["start", "--symbols", "BTCUSDT,ALL"]).exit_code == 1

    captured = {}

    class Supervisor:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def run(self) -> int:
            return 7

    monkeypatch.setattr(cli_module, "BeidouSupervisor", Supervisor)
    result = runner.invoke(
        cli_module.main,
        [
            "start",
            "--mode",
            "paper",
            "--symbols",
            "BTCUSDT,btcusdt,ETHUSDT",
            "--port",
            "19090",
            "--startup-timeout",
            "30",
            "--monitor-interval",
            "1",
            "--no-self-heal",
            "--max-restarts",
            "0",
        ],
    )
    assert result.exit_code == 7
    assert "Readiness gate: MODULE_START" in result.output
    assert captured["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert captured["port"] == 19090
    assert captured["self_heal"] is False
    assert captured["max_restarts"] == 0

    class InterruptedSupervisor(Supervisor):
        async def run(self) -> int:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "BeidouSupervisor", InterruptedSupervisor)
    assert runner.invoke(cli_module.main, ["start", "--symbols", "BTCUSDT"]).exit_code == 130


def test_python_module_entrypoints_delegate_to_click_command(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(checks_module, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(state_module, "inspect_runtime_status", lambda root: {"state": "RUNNING"})
    monkeypatch.setattr(sys, "argv", ["beidou", "status"])
    with pytest.warns(RuntimeWarning, match="found in sys.modules"), pytest.raises(SystemExit) as cli_exit:
        runpy.run_module("beidou_launcher.cli", run_name="__main__")
    assert cli_exit.value.code == 0

    calls = []
    monkeypatch.setattr(cli_module, "main", lambda: calls.append("called"))
    runpy.run_module("beidou_launcher.__main__", run_name="__main__")
    assert calls == ["called"]


def test_enable_unbuffered_stdout_reconfigures_non_tty_streams(monkeypatch) -> None:
    """非 tty 流（日志文件重定向）切换行缓冲 —— 诊断输出逐行落盘。"""
    calls: list[dict] = []

    class FakeStream:
        def isatty(self) -> bool:
            return False

        def reconfigure(self, **kwargs) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(cli_module.sys, "stdout", FakeStream())
    monkeypatch.setattr(cli_module.sys, "stderr", FakeStream())

    cli_module._enable_unbuffered_stdout()

    assert calls == [{"line_buffering": True}, {"line_buffering": True}]


def test_enable_unbuffered_stdout_leaves_tty_streams_alone(monkeypatch) -> None:
    """tty 流保持默认缓冲行为。"""

    class FakeTty:
        def isatty(self) -> bool:
            return True

        def reconfigure(self, **kwargs) -> None:
            raise AssertionError("tty streams must not be reconfigured")

    monkeypatch.setattr(cli_module.sys, "stdout", FakeTty())
    monkeypatch.setattr(cli_module.sys, "stderr", FakeTty())

    cli_module._enable_unbuffered_stdout()  # 不抛异常即通过


def test_enable_unbuffered_stdout_survives_non_reconfigurable_streams(monkeypatch) -> None:
    """StringIO 等无 reconfigure 方法的流不抛异常（测试捕获器场景）。"""

    class FakeCapture:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(cli_module.sys, "stdout", FakeCapture())
    monkeypatch.setattr(cli_module.sys, "stderr", FakeCapture())

    cli_module._enable_unbuffered_stdout()  # 不抛异常即通过
