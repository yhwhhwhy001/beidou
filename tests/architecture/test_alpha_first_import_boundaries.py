"""Fail-closed import boundaries for the first Alpha composition slice.

The baseline still contains the legacy launcher/safety/exchange graph.  This
oracle deliberately scans only the new Alpha slice and its neutral contracts;
it must not silently become green by discovering no files or by maintaining
an allow-list for a forbidden edge.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ALPHA_ROOTS = (
    ROOT / "beidou_strategy" / "alpha",
    ROOT / "apps" / "alpha_app",
)
CONTRACT_FILES = (
    ROOT / "beidou_shared" / "contracts" / "experiment.py",
    ROOT / "beidou_shared" / "contracts" / "alpha_execution.py",
)

# These are concrete runtime or side-effect domains.  Alpha may use its
# existing pure strategy implementation and the shared value types, but may
# not import any of these packages as a shortcut to a composition root.
FORBIDDEN_PREFIXES = (
    "beidou_autonomy",
    "beidou_certification",
    "beidou_chaos",
    "beidou_control",
    "beidou_core",
    "beidou_exchange",
    "beidou_execution",
    "beidou_infra",
    "beidou_launcher",
    "beidou_observability",
    "beidou_policy",
    "beidou_production",
    "beidou_safety",
    "beidou_safety_min",
    "beidou_security",
    "beidou_simulator",
)

ALLOWED_THIRD_PARTY = {"typing_extensions"}


@dataclass(frozen=True)
class ImportRecord:
    path: Path
    line: int
    imported: str
    classification: str


def _module_files(roots: tuple[Path, ...] | list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return sorted(set(files))


def _top_level(name: str) -> str:
    return name.split(".", 1)[0]


def _is_forbidden(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_PREFIXES)


def _stdlib_or_allowed(name: str) -> bool:
    top = _top_level(name)
    return top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY


def _records_for_file(path: Path, *, contract: bool) -> list[ImportRecord]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    records: list[ImportRecord] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative imports are constrained to the package being
                # scanned and cannot cross a concrete domain boundary.
                imports = [f"<relative:{node.level}>.{node.module or ''}"]
            elif node.module:
                imports = [node.module]
            else:
                imports = []
        else:
            continue
        for imported in imports:
            if imported.startswith("<relative:"):
                classification = "relative"
            elif _is_forbidden(imported):
                classification = "forbidden"
            elif imported == "beidou_shared" or imported.startswith("beidou_shared."):
                classification = "shared"
            elif imported == "beidou_strategy" or imported.startswith("beidou_strategy."):
                classification = "alpha-implementation"
            elif _stdlib_or_allowed(imported):
                classification = "stdlib"
            else:
                classification = "unknown"
            if contract and classification not in {"relative", "shared", "stdlib"}:
                classification = "contract-forbidden"
            records.append(ImportRecord(path, node.lineno, imported, classification))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "__import__":
            records.append(ImportRecord(path, node.lineno, "__import__", "dynamic"))
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {"import_module", "find_spec"}:
            records.append(ImportRecord(path, node.lineno, node.func.attr, "dynamic"))
    return records


def scan_imports(
    roots: tuple[Path, ...] | list[Path], *, contract_files: tuple[Path, ...] = ()
) -> tuple[list[Path], list[ImportRecord]]:
    files = _module_files([*roots, *contract_files])
    if not files:
        raise AssertionError("ALPHA_IMPORT_INVENTORY_EMPTY")
    contract_set = set(contract_files)
    records = [record for path in files for record in _records_for_file(path, contract=path in contract_set)]
    return files, records


def _write_evidence_artifact(name: str, payload: object) -> None:
    evidence_dir = os.environ.get("BEIDOU_EVIDENCE_DIR", "").strip()
    if not evidence_dir:
        return
    path = Path(evidence_dir) / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_alpha_inventory_is_non_vacuous_and_bound_to_expected_roots() -> None:
    """The Alpha slice and both composition/contract roots must be present."""
    missing = [str(path) for path in (*ALPHA_ROOTS, *CONTRACT_FILES) if not path.exists()]
    assert not missing, f"ALPHA_EXPECTED_PATH_MISSING:{missing}"
    files, records = scan_imports(ALPHA_ROOTS, contract_files=CONTRACT_FILES)
    assert len(files) >= 4, f"ALPHA_IMPORT_INVENTORY_TOO_SMALL:{len(files)}"
    _write_evidence_artifact(
        "module-inventory.json",
        {
            "roots": [str(path.relative_to(ROOT)) for path in (*ALPHA_ROOTS, *CONTRACT_FILES)],
            "files": [str(path.relative_to(ROOT)) for path in files],
            "file_count": len(files),
            "import_record_count": len(records),
        },
    )


def test_alpha_import_graph_has_no_forbidden_or_unclassified_edges() -> None:
    """Every import in the slice is classified; concrete runtime edges fail."""
    _, records = scan_imports(ALPHA_ROOTS, contract_files=CONTRACT_FILES)
    violations = [
        record
        for record in records
        if record.classification in {"forbidden", "unknown", "dynamic", "contract-forbidden"}
    ]
    assert not violations, "ALPHA_IMPORT_BOUNDARY_VIOLATIONS:\n" + "\n".join(
        f"{record.path.relative_to(ROOT)}:{record.line}: {record.classification}: {record.imported}"
        for record in violations
    )


def test_forbidden_edge_mutation_is_rejected(tmp_path: Path) -> None:
    """A concrete forbidden import cannot be hidden by the scanner."""
    mutated = tmp_path / "alpha_mutation.py"
    mutated.write_text("from beidou_safety.execution import OrderIntent\n", encoding="utf-8")
    _, records = scan_imports([mutated])
    assert any(record.classification == "forbidden" for record in records)
    _write_evidence_artifact(
        "forbidden-edge-mutations.json",
        {
            "mutation": "from beidou_safety.execution import OrderIntent",
            "detected": [
                {
                    "imported": record.imported,
                    "classification": record.classification,
                    "line": record.line,
                }
                for record in records
            ],
            "status": "PASS",
        },
    )


def test_empty_inventory_mutation_is_rejected(tmp_path: Path) -> None:
    """An empty or missing package is a hard failure, never a vacuous pass."""
    with pytest.raises(AssertionError, match="ALPHA_IMPORT_INVENTORY_EMPTY"):
        scan_imports([tmp_path / "missing-alpha"])


def test_neutral_contracts_do_not_construct_runtime_clients() -> None:
    """Contracts are data/protocol definitions, not composition roots."""
    for path in CONTRACT_FILES:
        assert path.exists(), f"CONTRACT_MISSING:{path}"
        source = path.read_text(encoding="utf-8")
        assert "os.environ" not in source
        assert "socket" not in source
        assert "httpx" not in source
        assert "requests" not in source
        assert "_global_registry" not in source


def test_alpha_composition_rollback_keeps_legacy_entrypoint_explicit() -> None:
    """Disabling the new route leaves the characterized launcher untouched."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    launcher = (ROOT / "beidou_launcher" / "cli.py").read_text(encoding="utf-8")
    assert 'beidou = "beidou_launcher.cli:main"' in pyproject
    assert "apps.alpha_app" not in pyproject
    assert "apps.alpha_app" not in launcher
