"""Independent negative contracts for governed executable entrypoints."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "write-capability-registry.json"


def _registry() -> dict[str, object]:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def test_delegating_entrypoints_reject_write_mode_injection() -> None:
    for module in ("apps.strategy_engine", "apps.safety_executor"):
        result = subprocess.run(  # noqa: S603 - fixed interpreter and repository-local modules
            [sys.executable, "-m", module, "--mode=testnet", "--symbols", "BTCUSDT"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert result.returncode != 0
        assert "fixes mode=" in result.stderr


def test_offline_and_read_only_entries_have_no_terminal_write_calls() -> None:
    registry = _registry()
    terminal_records = registry["terminal_write_paths"]  # type: ignore[index]
    for entry in registry["entries"]:  # type: ignore[index]
        if entry["status"] not in {"OFFLINE_ONLY", "READ_ONLY"}:
            continue
        prefix = entry["path"] + "::"
        assert all(
            terminal["status"] == "READ_ONLY"
            for terminal in terminal_records
            if terminal["source"].startswith(prefix)
        ), entry["id"]


def test_legacy_delivery_and_activation_entries_are_hard_held() -> None:
    registry = _registry()
    entries = {entry["id"]: entry for entry in registry["entries"]}  # type: ignore[index]
    held = {
        "ENTRY-LAUNCHD-PLIST",
        "ENTRY-DELIVERY-COLLECT-EVIDENCE",
        "ENTRY-DELIVERY-INIT-BRANCH",
        "ENTRY-DELIVERY-PREFLIGHT",
        "ENTRY-DELIVERY-RUN-G0",
        "ENTRY-TOOL-STRATEGY-LIVE",
    }
    for entry_id in held:
        assert entries[entry_id]["status"] == "HARD_HOLD"


def test_declarative_entrypoint_surfaces_are_explicitly_governed() -> None:
    registry = _registry()
    entries = {entry["path"]: entry for entry in registry["entries"]}  # type: ignore[index]

    assert entries["docker-compose.yml"]["status"] == "HARD_HOLD"
    assert entries["docker-compose.yml"]["capability"] == "DATABASE_MIGRATION"
    assert entries[".github/workflows/ci.yml"]["capability"] == "CI_AUTOMATION"
    assert entries[".pre-commit-config.yaml"]["capability"] == "DEVELOPER_HOOKS"


def test_declared_entrypoints_have_specific_rejection_contracts() -> None:
    registry = _registry()
    declarations = registry["declared_entrypoints"]  # type: ignore[index]
    records = registry["declared_entrypoint_records"]  # type: ignore[index]

    assert set(records) == set(declarations)
    for declaration, command in declarations.items():
        record = records[declaration]
        pytest_identity = "".join(
            character
            if character.isascii() and (character.isalnum() or character in "_-")
            else f"u{ord(character):04x}"
            for character in declaration
        )
        assert record["command"] == command
        assert record["expected_rejection"] not in {"", "PASS", "NONE"}
        assert record["negative_test"] == (
            "tests/architecture/test_registry_record_contracts.py::"
            "test_declaration_record_is_behaviorally_bound["
            f"{pytest_identity}]"
        )


def test_g5_runner_uses_producer_preflight_and_remains_hard_held() -> None:
    registry = _registry()
    entry = next(item for item in registry["entries"] if item["id"] == "ENTRY-G5-RUNNER")  # type: ignore[index]
    source = (ROOT / entry["path"]).read_text(encoding="utf-8")

    assert entry["status"] == "HARD_HOLD"
    assert "run_g5_producer_preflight" in source
    assert "--confirm-testnet" in source
    assert "--symbol" in source


def test_canonical_cli_help_probe_has_no_runtime_side_effects(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "beidou_launcher", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert result.returncode == 0
    assert not (tmp_path / ".beidou").exists()
    assert not (tmp_path / "evidence").exists()
