"""Second-implementation checks for write-registry completeness."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.verify_write_registry import scan_repository, verify_coverage

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "write-capability-registry.json"


def test_independent_oracle_detects_primary_scanner_bypass_shapes(tmp_path: Path) -> None:
    (tmp_path / "payload.py").write_text(
        """
from venue import create_order as imported_send
from engine import AutonomousEngine as E
import functools
import httpx

async def bypass(client, holder, name, operations):
    first = client.create_order
    second = first
    await second()
    await getattr(client, name)()
    holder.send = client.cancel_order
    await operations['send']()
    await functools.partial(client.request, 'POST', '/write')()
    await imported_send()
    await httpx.AsyncClient().post('https://offline.invalid/write')
    E()
""",
        encoding="utf-8",
    )
    (tmp_path / "payload.sh").write_text(
        '#!/bin/sh\ncurl -X "$METHOD" https://offline.invalid/write\n',
        encoding="utf-8",
    )

    findings = scan_repository(tmp_path)
    kinds = {finding.kind for finding in findings}

    assert {
        "DIRECT_HTTP_WRITE",
        "RUNTIME_CONSTRUCTOR",
        "SHELL_HTTP_WRITE",
        "TERMINAL_CALL",
        "UNRESOLVED_WRITE_CALLABLE",
    } <= kinds
    assert verify_coverage(tmp_path, {"entries": [], "terminal_write_paths": []})


def test_independent_oracle_accepts_current_registry() -> None:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))

    assert verify_coverage(ROOT, registry) == []


def test_independent_oracle_cli_is_a_separate_executable_gate() -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository-local script
        [
            sys.executable,
            "scripts/verify_write_registry.py",
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
