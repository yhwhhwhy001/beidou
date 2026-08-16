"""Rebuild config/write-capability-registry.json against the current tree.

M00-E01 governance tool. The registry binds every executable or
exchange-sensitive path to a typed governance record. Mechanical facts
(source digests, terminal write call sites, declared entrypoints, network
imports) are recomputed from the live tree; governance decisions (capability,
status, owner, call_graph) are preserved from the existing registry, with
explicit fail-closed defaults for newly discovered paths.

New paths MUST NOT silently inherit permissive defaults. Unknown paths are
emitted as HARD_HOLD with WRITE_CAPABILITY_REGISTRY_INCOMPLETE and listed in
the printed review set; an operator reviews and re-runs validation.

Usage:
    python -m scripts.rebuild_write_registry
    .venv/bin/python scripts/rebuild_write_registry.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beidou_launcher.write_registry import (
    compute_governance_digest,
    discover_declared_entrypoints,
    discover_governed_source_digests,
    discover_network_imports,
    discover_sensitive_entry_paths,
    discover_terminal_write_calls,
    load_registry,
)

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "write-capability-registry.json"

# Governance decisions for paths introduced after the initial registry was
# authored. Each decision is deliberate: capability + status + owner +
# call_graph, following the semantics of the closest existing family.
_NEW_ENTRY_DECISIONS: dict[str, dict[str, str]] = {
    # M00-F07: launchd 实装由 wrapper 托管执行，委托 canonical launcher。
    "deploy/beidou_launchd_wrapper.sh": {
        "kind": "shell",
        "capability": "DELEGATE_TO_LAUNCHER",
        "status": "DELEGATE_ONLY",
        "owner": "Runtime Owner",
        "call_graph": "launchd -> governed wrapper -> canonical launcher (beidou start)",
        "expected_rejection": "CANONICAL_LAUNCHER_REQUIRED",
    },
    # M22: PITR 启用脚本写宿主机 PG 配置（本地运维迁移族）。
    "scripts/enable_pitr.sh": {
        "kind": "shell",
        "capability": "DATABASE_MIGRATION",
        "status": "OFFLINE_ONLY",
        "owner": "Storage Owner",
        "call_graph": "operator -> PostgreSQL WAL archiving configuration",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
    },
    # R-M03-1: 阈值重标定研究脚本（离线回测扫描族）。
    "scripts/recalibrate_r_m03_1.py": {
        "kind": "script",
        "capability": "OFFLINE_REPLAY",
        "status": "OFFLINE_ONLY",
        "owner": "Research Owner",
        "call_graph": "operator -> research recalibration evidence (M08 kernel)",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
    },
    # 本工具自身（CI/operator 治理重建，纯源码读取）。
    "scripts/rebuild_write_registry.py": {
        "kind": "script",
        "capability": "SOURCE_READ_ONLY",
        "status": "OFFLINE_ONLY",
        "owner": "Security Owner",
        "call_graph": "CI/operator -> registry rebuild -> source scan only",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
    },
}

# New terminal write call sites introduced by M19-F01 (authorized resume
# relocation) and audited dynamic boundaries.
_NEW_TERMINAL_DECISIONS: dict[str, dict[str, str]] = {
    "beidou_control/plane.py::ControlPlane.execute_authorized_resume::execute_action[RESUME]": {
        "capability": "CONTROL_RESUME_AUTHORITY_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "CONTROL_AUTHORITY_REQUIRED",
        "owner": "Control Owner",
        "call_graph": "control plane authorized resume -> ControlPlane RESUME -> supervisor terminal interlock",
    },
    "beidou_core/engine.py::AutonomousEngine._policy_float_audited::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_WRITE_BOUNDARY_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Execution Owner",
        "call_graph": "engine audited policy read -> dynamic write boundary",
    },
    "beidou_research/factors/factor.py::FactorPromotionGate.validate_evidence::getattr[DYNAMIC]": {
        "capability": "DYNAMIC_WRITE_BOUNDARY_REQUIRED",
        "status": "HARD_HOLD",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        "owner": "Research Owner",
        "call_graph": "factor promotion evidence validation -> dynamic write boundary",
    },
}

# Stale terminal sources superseded by relocated call sites in main.
_STALE_TERMINAL_SOURCES = {
    "beidou_control/api.py::ControlPlaneAPI.resume_trading::execute_action[RESUME]",
}


def _entry_id(path: str) -> str:
    stem = Path(path).stem.upper().replace("-", "_").replace(".", "_")
    return f"ENTRY-{stem[:40]}"


def _terminal_id(source: str) -> str:
    owner = source.split("::", 1)[0].split("/", 1)[0].upper()
    tail = source.split("::")[-1].split("[", 1)[0]
    tail = "".join(ch if ch.isalnum() else "-" for ch in tail).strip("-").upper()
    return f"WRITE-{owner}-{tail[:44]}"


def _rebuild_oracle_findings(registry: dict[str, Any]) -> list[str]:
    """Rebind independent-oracle findings to governed records.

    Uses the independent oracle's own scanner (scripts/verify_write_registry.py)
    so the registry stays honest to an implementation the primary scanner does
    not share. Previous bindings are kept when still allowed; new findings are
    bound to the first allowed governance record. Findings with no allowed
    record are reported for manual governance work.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_write_registry import expected_governance_ids, scan_repository

    governed = {
        record.get("id"): record
        for section in ("entries", "terminal_write_paths", "network_imports")
        for record in registry.get(section, [])
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    }
    old = {declaration["identity"]: declaration for declaration in registry.get("independent_oracle_findings", [])}
    declarations: list[dict[str, str]] = []
    unbindable: list[str] = []
    for finding in scan_repository(ROOT):
        identity = f"{finding.path}:{finding.line}:{finding.kind}:{finding.detail}"
        allowed = expected_governance_ids(finding, root=ROOT, registry=registry)
        previous = old.get(identity)
        if previous and previous.get("governance_id") in allowed:
            declarations.append(previous)
            continue
        if not allowed:
            unbindable.append(identity)
            print(f"[review] oracle finding has no allowed governance: {identity}")
            continue
        governance_id = sorted(allowed)[0]
        record = governed[governance_id]
        declarations.append(
            {
                "identity": identity,
                "governance_id": governance_id,
                "owner": record.get("owner", ""),
                "status": record.get("status", ""),
                "negative_test": record.get("negative_test", ""),
            }
        )
    registry["independent_oracle_findings"] = sorted(declarations, key=lambda declaration: declaration["identity"])
    return unbindable


def main() -> int:
    registry = load_registry(REGISTRY_PATH)

    # --- mechanical facts, recomputed from the live tree ---
    registry["governed_source_digests"] = discover_governed_source_digests(ROOT)
    registry["declared_entrypoints"] = discover_declared_entrypoints(ROOT)

    # --- network imports: keep governance records, refresh occurrences ---
    discovered_network = discover_network_imports(ROOT)
    raw_network = registry.get("network_imports", [])
    if isinstance(raw_network, dict):
        # 之前一次损坏运行把 dict 写进了 JSON；从 codex 基线恢复治理记录。
        import subprocess

        baseline = subprocess.run(
            ["git", "show", "9e5279d:config/write-capability-registry.json"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        raw_network = json.loads(baseline).get("network_imports", [])
    old_network = {item["source"]: item for item in raw_network}
    new_network: list[dict[str, Any]] = []
    for source, occurrences in sorted(discovered_network.items()):
        if source in old_network:
            record = dict(old_network[source])
            record["occurrences"] = occurrences
            new_network.append(record)
            continue
        print(f"[review] new network import without governance decision: {source}")
        network_id = f"NETWORK-{source.split('::')[-1].upper().replace('.', '-')[:44]}"
        new_network.append(
            {
                "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
                "id": network_id,
                "negative_test": (
                    "tests/architecture/test_registry_record_contracts.py"
                    f"::test_network_record_is_behaviorally_bound[{network_id}]"
                ),
                "occurrences": occurrences,
                "owner": "Runtime Owner",
                "purpose": "UNREVIEWED_NETWORK_IMPORT",
                "source": source,
                "status": "HARD_HOLD",
            }
        )
    registry["network_imports"] = new_network

    # --- entries ---
    old_entries = {entry["path"]: entry for entry in registry.get("entries", [])}
    discovered_paths = discover_sensitive_entry_paths(ROOT)
    new_entries: list[dict[str, Any]] = []
    for path in sorted(discovered_paths):
        if path in old_entries:
            new_entries.append(old_entries[path])
            continue
        decision = _NEW_ENTRY_DECISIONS.get(path)
        if decision is None:
            decision = {
                "kind": "shell" if path.endswith(".sh") else "script",
                "capability": "TERMINAL_WRITE_BOUNDARY",
                "status": "HARD_HOLD",
                "owner": "Runtime Owner",
                "call_graph": "UNREVIEWED: newly discovered sensitive path",
                "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
            }
            print(f"[review] new sensitive path without governance decision: {path}")
        entry_id = _entry_id(path)
        new_entries.append(
            {
                "call_graph": decision["call_graph"],
                "capability": decision["capability"],
                "expected_rejection": decision["expected_rejection"],
                "id": entry_id,
                "kind": decision["kind"],
                "negative_test": (
                    "tests/architecture/test_registry_record_contracts.py"
                    f"::test_entry_record_is_behaviorally_bound[{entry_id}]"
                ),
                "owner": decision["owner"],
                "path": path,
                "status": decision["status"],
            }
        )
    registry["entries"] = new_entries

    # --- terminal write paths ---
    discovered_terminal = discover_terminal_write_calls(ROOT)
    old_terminal = {item["source"]: item for item in registry.get("terminal_write_paths", [])}
    new_terminal: list[dict[str, Any]] = []
    for source, occurrences in sorted(discovered_terminal.items()):
        if source in _STALE_TERMINAL_SOURCES:
            print(f"[review] dropping stale terminal source: {source}")
            continue
        if source in old_terminal:
            record = dict(old_terminal[source])
            record["occurrences"] = occurrences
            new_terminal.append(record)
            continue
        decision = _NEW_TERMINAL_DECISIONS.get(source)
        if decision is None:
            decision = {
                "capability": "TERMINAL_WRITE_BOUNDARY",
                "status": "HARD_HOLD",
                "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
                "owner": "Runtime Owner",
                "call_graph": "UNREVIEWED: newly discovered terminal write path",
            }
            print(f"[review] new terminal write path without governance decision: {source}")
        terminal_id = _terminal_id(source)
        new_terminal.append(
            {
                "call_graph": decision["call_graph"],
                "capability": decision["capability"],
                "expected_rejection": decision["expected_rejection"],
                "id": terminal_id,
                "negative_test": (
                    "tests/architecture/test_registry_record_contracts.py"
                    f"::test_terminal_record_is_behaviorally_bound[{terminal_id}]"
                ),
                "occurrences": occurrences,
                "owner": decision["owner"],
                "source": source,
                "status": decision["status"],
            }
        )
    registry["terminal_write_paths"] = new_terminal

    # --- declared entrypoint records: sync command to the live tree ---
    declared = registry["declared_entrypoints"]
    records = registry.get("declared_entrypoint_records", {})
    for declaration, command in declared.items():
        record = records.get(declaration)
        if record is None:
            print(f"[review] entrypoint {declaration} has no governance record")
            continue
        if record.get("command") != command:
            print(f"[sync] {declaration}: {record.get('command')} -> {command}")
            record["command"] = command
    registry["declared_entrypoint_records"] = records

    # --- independent oracle findings: rebind against the live tree ---
    unbindable = _rebuild_oracle_findings(registry)
    if unbindable:
        print(f"[review] {len(unbindable)} oracle findings lack an allowed governance record")

    # --- governance digest over the updated payload ---
    registry.pop("governance_digest", None)
    registry["governance_digest"] = compute_governance_digest(registry)

    REGISTRY_PATH.write_text(
        json.dumps(registry, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {REGISTRY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
