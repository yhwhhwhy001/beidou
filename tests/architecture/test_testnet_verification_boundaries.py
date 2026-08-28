"""PKG-00: fail-closed boundaries for the Testnet Verification composition root."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERIFIER_ROOT = ROOT / "apps" / "testnet_verify"

FORBIDDEN_IMPORT_PREFIXES = (
    "beidou_autonomy",
    "beidou_certification",
    "beidou_chaos",
    "beidou_control",
    "beidou_core",
    "beidou_infra",
    "beidou_launcher",
    "beidou_observability",
    "beidou_policy",
    "beidou_production",
    "beidou_safety",
    "beidou_security",
    "beidou_exchange.core.signed_capability",
)

FORBIDDEN_TRANSPORT_MODULES = {"httpx", "requests", "urllib", "urllib3"}


def _python_files() -> list[Path]:
    files = sorted(VERIFIER_ROOT.rglob("*.py")) if VERIFIER_ROOT.is_dir() else []
    if not files:
        raise AssertionError("TESTNET_VERIFIER_IMPORT_INVENTORY_EMPTY")
    return files


def _import_names(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                names.append((node.lineno, node.module))
    return names


def test_verifier_inventory_is_non_vacuous() -> None:
    assert _python_files()


def test_verifier_does_not_import_frozen_runtime_or_production_signer() -> None:
    violations: list[str] = []
    for path in _python_files():
        for line, imported in _import_names(path):
            if any(imported == prefix or imported.startswith(prefix + ".") for prefix in FORBIDDEN_IMPORT_PREFIXES):
                violations.append(f"{path.relative_to(ROOT)}:{line}:{imported}")
    assert not violations, "TESTNET_VERIFIER_FORBIDDEN_IMPORTS:\n" + "\n".join(violations)


def test_verifier_does_not_construct_a_second_http_writer() -> None:
    violations: list[str] = []
    for path in _python_files():
        for line, imported in _import_names(path):
            if imported.split(".", 1)[0] in FORBIDDEN_TRANSPORT_MODULES:
                violations.append(f"{path.relative_to(ROOT)}:{line}:{imported}")
        source = path.read_text(encoding="utf-8")
        if "client.create_order(" in source or "client._request(" in source:
            violations.append(f"{path.relative_to(ROOT)}:direct-client-writer")
    assert not violations, "TESTNET_VERIFIER_SECOND_WRITER:\n" + "\n".join(violations)


@pytest.mark.parametrize("module", ["beidou_certification", "beidou_production", "beidou_chaos"])
def test_frozen_modules_remain_importable(module: str) -> None:
    assert (ROOT / module).is_dir(), f"FROZEN_MODULE_REMOVED:{module}"


def test_legacy_entrypoint_is_not_cut_over_implicitly() -> None:
    source = (ROOT / "beidou_launcher" / "cli.py").read_text(encoding="utf-8")
    assert "apps.testnet_verify" not in source


def test_verifier_does_not_reference_deprecated_sizing_helpers() -> None:
    """PKG-05-M07: 旧 adaptive_leverage/adaptive_position_pct 仅属冻结
    Engine 路径;唯一 sizing authority 是 compute_adaptive_sizing。"""
    violations: list[str] = []
    for path in _python_files():
        source = path.read_text(encoding="utf-8")
        for helper in ("adaptive_leverage", "adaptive_position_pct"):
            if helper in source:
                violations.append(f"{path.relative_to(ROOT)}:references:{helper}")
    assert not violations, "TESTNET_VERIFIER_DEPRECATED_SIZING:\n" + "\n".join(violations)
