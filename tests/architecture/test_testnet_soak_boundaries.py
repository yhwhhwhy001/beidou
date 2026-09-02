"""Architecture boundaries for the finite Testnet execution probe."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOAK = ROOT / "apps" / "testnet_soak"


def test_soak_has_no_exchange_writer_or_daemon_mechanism() -> None:
    sources = "\n".join(path.read_text(encoding="utf-8") for path in SOAK.glob("*.py"))

    assert ".create_order(" not in sources
    assert "BinanceRESTClient" not in sources
    assert "BinanceUsdmAdapter" not in sources
    assert "launchd" not in sources.lower()
    assert "KeepAlive" not in sources
    assert "while True" not in sources


def test_soak_runtime_delegates_to_existing_verifier() -> None:
    tree = ast.parse((SOAK / "runtime.py").read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "apps.testnet_verify.runtime"
        for alias in node.names
    }

    assert "VerificationRuntime" in imports


def test_probe_evidence_is_explicitly_not_alpha() -> None:
    source = (SOAK / "kernel.py").read_text(encoding="utf-8")

    assert "EXECUTION_PROBE" in source
    assert '"alpha_evidence": False' in source
