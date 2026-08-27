"""Per-record behavioral bindings for executable and write registries."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pytest

from beidou_launcher.write_registry import (
    compute_governance_digest,
    discover_declared_entrypoints,
    discover_network_imports,
    discover_sensitive_entry_paths,
    discover_terminal_write_calls,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "config" / "write-capability-registry.json"
REGISTRY = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

ENTRY_RECORDS = {item["id"]: item for item in REGISTRY["entries"]}
TERMINAL_RECORDS = {item["id"]: item for item in REGISTRY["terminal_write_paths"]}
NETWORK_RECORDS = {item["id"]: item for item in REGISTRY["network_imports"]}
DECLARATION_RECORDS = REGISTRY["declared_entrypoint_records"]


@lru_cache(maxsize=1)
def _discovered_entry_paths() -> set[str]:
    """Avoid rescanning the repository once per parameterized entry record."""

    return discover_sensitive_entry_paths(ROOT)


@lru_cache(maxsize=1)
def _discovered_terminal_calls() -> dict[str, int]:
    """Share the terminal-call inventory across the negative-test gate."""

    return discover_terminal_write_calls(ROOT)


@lru_cache(maxsize=1)
def _discovered_network_imports() -> dict[str, int]:
    """Share the network-import inventory across the negative-test gate."""

    return discover_network_imports(ROOT)


@lru_cache(maxsize=1)
def _discovered_entrypoints() -> dict[str, str]:
    """Share the declared-entrypoint inventory across the negative-test gate."""

    return discover_declared_entrypoints(ROOT)


def _pytest_identity(identity: str) -> str:
    return "".join(
        character if character.isascii() and (character.isalnum() or character in "_-") else f"u{ord(character):04x}"
        for character in identity
    )


def _expected_entry_rejection(status: str, capability: str) -> str:
    if status == "DELEGATE_ONLY":
        return "CANONICAL_LAUNCHER_REQUIRED"
    if status != "HARD_HOLD":
        return "EXTERNAL_WRITE_NOT_AUTHORIZED"
    if capability.startswith("LEGACY") or capability in {
        "BUILD_AND_RUNTIME_ALIAS_SURFACE",
        "GIT_BRANCH_MUTATION",
        "RUNTIME_ACTIVATION",
    }:
        return "NONCANONICAL_ENTRYPOINT_HELD"
    return "WRITE_CAPABILITY_REGISTRY_INCOMPLETE"


@pytest.mark.parametrize("record_id", sorted(ENTRY_RECORDS), ids=sorted(ENTRY_RECORDS))
def test_entry_record_is_behaviorally_bound(record_id: str) -> None:
    assert REGISTRY["governance_digest"] == compute_governance_digest(REGISTRY)
    record = ENTRY_RECORDS[record_id]
    assert record["path"] in _discovered_entry_paths()
    assert record["expected_rejection"] == _expected_entry_rejection(record["status"], record["capability"])
    if record["status"] in {"READ_ONLY", "OFFLINE_ONLY"}:
        matching = [
            terminal for terminal in TERMINAL_RECORDS.values() if terminal["source"].startswith(record["path"] + "::")
        ]
        assert all(terminal["status"] == "READ_ONLY" for terminal in matching)
    if record["status"] == "HARD_HOLD":
        assert record["capability"] not in {"EXCHANGE_READ_ONLY", "SOURCE_READ_ONLY"}


@pytest.mark.parametrize("record_id", sorted(TERMINAL_RECORDS), ids=sorted(TERMINAL_RECORDS))
def test_terminal_record_is_behaviorally_bound(record_id: str) -> None:
    assert REGISTRY["governance_digest"] == compute_governance_digest(REGISTRY)
    record = TERMINAL_RECORDS[record_id]
    discovered = _discovered_terminal_calls()
    assert discovered[record["source"]] == record["occurrences"]
    assert record["status"] in {"HARD_HOLD", "READ_ONLY"}
    expected = "EXTERNAL_WRITE_NOT_AUTHORIZED"
    if record["status"] == "HARD_HOLD":
        expected = (
            "CONTROL_AUTHORITY_REQUIRED"
            if record["capability"] == "CONTROL_RESUME_AUTHORITY_REQUIRED"
            else "WRITE_CAPABILITY_REGISTRY_INCOMPLETE"
        )
    assert record["expected_rejection"] == expected
    if record["capability"] == "USER_STREAM_SESSION_WRITE_REQUIRED":
        assert "listen_key" in record["source"]
    if record["capability"] == "CONTROL_RESUME_AUTHORITY_REQUIRED":
        assert "execute_action" in record["source"]


@pytest.mark.parametrize("record_id", sorted(NETWORK_RECORDS), ids=sorted(NETWORK_RECORDS))
def test_network_record_is_behaviorally_bound(record_id: str) -> None:
    assert REGISTRY["governance_digest"] == compute_governance_digest(REGISTRY)
    record = NETWORK_RECORDS[record_id]
    assert _discovered_network_imports()[record["source"]] == record["occurrences"]
    path, module = record["source"].split("::", 1)
    source = (ROOT / path).read_text(encoding="utf-8")
    assert module.split(".", 1)[0] in source
    assert record["status"] in {"HARD_HOLD", "READ_ONLY"}
    assert record["expected_rejection"] == (
        "WRITE_CAPABILITY_REGISTRY_INCOMPLETE" if record["status"] == "HARD_HOLD" else "EXTERNAL_WRITE_NOT_AUTHORIZED"
    )


@pytest.mark.parametrize(
    "declaration",
    sorted(DECLARATION_RECORDS),
    ids=[_pytest_identity(declaration) for declaration in sorted(DECLARATION_RECORDS)],
)
def test_declaration_record_is_behaviorally_bound(declaration: str) -> None:
    assert REGISTRY["governance_digest"] == compute_governance_digest(REGISTRY)
    record = DECLARATION_RECORDS[declaration]
    assert _discovered_entrypoints()[declaration] == record["command"]
    assert record["expected_rejection"] not in {"", "NONE", "PASS"}
