"""M00-E01: every executable or exchange-sensitive path is registered."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from beidou_launcher.write_registry import (
    discover_sensitive_entry_paths,
    discover_terminal_write_calls,
    load_registry,
    validate_registry,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "write-capability-registry.json"


def test_write_capability_registry_is_complete_and_valid() -> None:
    registry = load_registry(REGISTRY)

    assert validate_registry(registry, root=ROOT) == []
    assert {entry["path"] for entry in registry["entries"]} == discover_sensitive_entry_paths(ROOT)
    registered_calls = {item["source"]: item["occurrences"] for item in registry["terminal_write_paths"]}
    assert registered_calls == discover_terminal_write_calls(ROOT)


def test_registry_read_only_dry_run_is_executable() -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository-local module
        [
            sys.executable,
            "-m",
            "beidou_launcher.write_registry",
            "--root",
            str(ROOT),
            "--registry",
            str(REGISTRY),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "PASS", "issues": []}


def test_only_launcher_cli_can_be_a_future_writable_runtime_candidate() -> None:
    registry = load_registry(REGISTRY)
    candidates = [entry for entry in registry["entries"] if entry["capability"] == "WRITABLE_RUNTIME_CANDIDATE"]

    assert [(entry["id"], entry["path"]) for entry in candidates] == [("ENTRY-LAUNCHER-CLI", "beidou_launcher/cli.py")]
    assert candidates[0]["status"] == "HARD_HOLD"


def test_compatibility_apps_do_not_construct_engine_or_default_testnet() -> None:
    for relative in (
        "apps/autopilot/__main__.py",
        "apps/strategy_engine/__main__.py",
        "apps/safety_executor/__main__.py",
        "apps/research_lab/__main__.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "AutonomousEngine(" not in source
        assert 'BEIDOU_ENV"] = "testnet"' not in source


def test_noncanonical_tools_have_no_implicit_testnet_symbols_or_shell_secret_loading() -> None:
    factor_miner = (ROOT / "apps/factor_miner/__main__.py").read_text(encoding="utf-8")
    mining_cron = (ROOT / "scripts/mining_cron.sh").read_text(encoding="utf-8")
    g5_runner = (ROOT / "scripts/testnet/run_g5.py").read_text(encoding="utf-8")

    assert 'setdefault("BEIDOU_ENV", "testnet")' not in factor_miner
    assert "~/.zshrc" not in mining_cron
    assert "eval " not in mining_cron
    assert "BTCUSDT,ETHUSDT" not in mining_cron
    assert "BEIDOU_MINING_SYMBOLS" in mining_cron
    assert "api_key[-4:]" not in g5_runner


def test_legacy_start_wrapper_has_no_startup_side_effects() -> None:
    source = (ROOT / "start_beidou.sh").read_text(encoding="utf-8")

    for forbidden in (
        ".env",
        "docker",
        "mkdir",
        "source ",
        "export ",
        "risk_parameters",
        "CERT-G5",
        "BTCUSDT",
        ":-testnet",
    ):
        assert forbidden not in source
    assert source.count("exec ") == 1
