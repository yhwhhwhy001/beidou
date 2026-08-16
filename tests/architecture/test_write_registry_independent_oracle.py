"""Second-implementation checks for write-registry completeness."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.verify_write_registry import (
    expected_governance_ids,
    load_registry,
    scan_repository,
    scan_source_digests,
    verify_coverage,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "write-capability-registry.json"


def test_independent_oracle_detects_primary_scanner_bypass_shapes(tmp_path: Path) -> None:
    (tmp_path / "payload.py").write_text(
        """
from venue import create_order as imported_send
from engine import AutonomousEngine as E
import functools
import asyncio
import httpx as hx
import socket
from httpx import post as direct_send
import urllib.request

async def bypass(client, holder, name, operations):
    first = client.create_order
    second = first
    await second()
    await getattr(client, name)()
    holder.send = client.cancel_order
    await operations['send']()
    await functools.partial(client.request, 'POST', '/write')()
    callback(client.create_order)
    setattr(holder, 'send', client.cancel_order)
    await imported_send()
    network = hx.AsyncClient()
    await network.delete('https://offline.invalid/write')
    await direct_send('https://offline.invalid/write')
    request = urllib.request.Request('https://offline.invalid/write', method='POST')
    urllib.request.urlopen(request)
    await asyncio.open_connection('offline.invalid', 443)
    socket.socket()
    send = hx.post
    relay = send
    relay('https://offline.invalid/write', data=b'x')
    hidden = (lambda: getattr(client, 'create_' + 'order'))()
    await hidden()
    table = vars(hx)
    callback(table.get('post'))
    object.__getattribute__(hx, 'post')('https://offline.invalid/write', data=b'x')
    E()
""",
        encoding="utf-8",
    )
    (tmp_path / "payload.sh").write_text(
        '#!/bin/sh\ncurl \\\n  --data "risk=1" https://offline.invalid/write\n',
        encoding="utf-8",
    )
    (tmp_path / "payload.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>ProgramArguments</key><array>
<string>/usr/bin/env</string><string>curl</string><string>--data</string><string>risk=1</string>
</array></dict></plist>
""",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text(
        "HTTP = curl\nwrite:\n\t$(HTTP) --data risk=1 https://offline.invalid/write\n",
        encoding="utf-8",
    )
    (tmp_path / "workflow.yml").write_text(
        "jobs:\n  write:\n    steps:\n      - run: curl --data x https://offline.invalid/write\n",
        encoding="utf-8",
    )

    findings = scan_repository(tmp_path)
    kinds = {finding.kind for finding in findings}

    assert {
        "DIRECT_HTTP_WRITE",
        "DIRECT_URLLIB_REQUEST",
        "MAKE_HTTP_WRITE",
        "NETWORK_TRANSPORT_CALL",
        "NETWORK_STREAM_CALL",
        "PLIST_HTTP_WRITE",
        "RUNTIME_CONSTRUCTOR",
        "SHELL_HTTP_WRITE",
        "TERMINAL_CALL",
        "UNRESOLVED_WRITE_CALLABLE",
    } <= kinds
    fake_coverage = {
        "entries": [{"path": "payload.py", "status": "HARD_HOLD"}],
        "terminal_write_paths": [{"source": "payload.py::bogus"}],
        "independent_oracle_findings": [],
        "governed_source_digests": scan_source_digests(tmp_path),
    }
    assert verify_coverage(tmp_path, fake_coverage)

    exact_identity = f"{findings[0].path}:{findings[0].line}:{findings[0].kind}:{findings[0].detail}"
    raw_allowlist = {**fake_coverage, "independent_oracle_findings": [exact_identity]}
    assert "INDEPENDENT_ORACLE_DECLARATIONS_INVALID" in verify_coverage(tmp_path, raw_allowlist)

    false_governance = {
        **fake_coverage,
        "independent_oracle_findings": [
            {
                "identity": exact_identity,
                "governance_id": "missing",
                "owner": "Security Owner",
                "status": "HARD_HOLD",
                "negative_test": (
                    "tests/architecture/test_write_registry_independent_oracle.py::"
                    "test_independent_oracle_detects_primary_scanner_bypass_shapes"
                ),
            }
        ],
    }
    assert any(
        issue.startswith("INDEPENDENT_ORACLE_GOVERNANCE_MISSING:")
        for issue in verify_coverage(tmp_path, false_governance)
    )


def test_independent_oracle_accepts_current_registry() -> None:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))

    assert verify_coverage(ROOT, registry) == []


def test_oracle_finding_cannot_be_rebound_to_unrelated_same_file_record() -> None:
    registry = load_registry(REGISTRY)
    finding = next(
        item
        for item in scan_repository(ROOT)
        if item.path == "beidou_core/alerts.py" and item.kind == "DIRECT_URLLIB_REQUEST"
    )
    allowed = expected_governance_ids(finding, root=ROOT, registry=registry)

    assert "WRITE-ALERT-WEBHOOK-REQUEST" in allowed
    assert "NETWORK-ALERTS-URLLIB" not in allowed
    declaration = next(
        item
        for item in registry["independent_oracle_findings"]
        if item["identity"] == f"{finding.path}:{finding.line}:{finding.kind}:{finding.detail}"
    )
    network_record = next(item for item in registry["network_imports"] if item["id"] == "NETWORK-ALERTS-URLLIB")
    declaration.update(
        governance_id=network_record["id"],
        owner=network_record["owner"],
        status=network_record["status"],
        negative_test=network_record["negative_test"],
    )

    assert any(
        issue.startswith("INDEPENDENT_ORACLE_GOVERNANCE_SCOPE_MISMATCH:") for issue in verify_coverage(ROOT, registry)
    )


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


def test_independent_oracle_rejects_duplicate_registry_keys(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(
        '{"independent_oracle_findings": [], "independent_oracle_findings": []}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="INDEPENDENT_ORACLE_DUPLICATE_KEY"):
        load_registry(registry)
