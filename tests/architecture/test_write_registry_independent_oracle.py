"""Second-implementation checks for write-registry completeness."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.verify_write_registry import load_registry, scan_repository, verify_coverage

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
        "write:\n\t$(CURL) --data risk=1 https://offline.invalid/write\n",
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
    }
    assert verify_coverage(tmp_path, fake_coverage)


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


def test_independent_oracle_rejects_duplicate_registry_keys(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(
        '{"independent_oracle_findings": [], "independent_oracle_findings": []}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="INDEPENDENT_ORACLE_DUPLICATE_KEY"):
        load_registry(registry)
