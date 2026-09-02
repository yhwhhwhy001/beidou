"""Subprocess proof that the first Alpha slice is genuinely offline."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _offline_script() -> str:
    return r"""
import builtins
import json
import socket
import sys

root = sys.argv[1]
sys.path.insert(0, root)
forbidden_prefixes = (
    "beidou_launcher", "beidou_safety", "beidou_execution", "beidou_exchange", "beidou_core",
)
# socket/ssl/urllib 不能按模块名拒绝: 3.12 的 pathlib 在模块级 import
# urllib.parse, email.utils 在模块级 import socket, pydantic 的导入链会把
# 它们合法带进来 —— 一个连接都没开。按名字拒绝只能证明「某个名字没出现
# 过」,还会随 Python 版本漂移。这里只拦「除联网外别无用途」的客户端库,
# 真正的不变量交给下面的建连拦截: 证明没有出过网。
forbidden_clients = ("httpx", "requests", "websockets", "aiohttp")
original_import = builtins.__import__

def deny(name, *args, **kwargs):
    if name.startswith(forbidden_clients):
        raise ImportError("NETWORK_IMPORT_DENIED:" + name)
    if name.startswith(forbidden_prefixes):
        raise ImportError("DOMAIN_IMPORT_DENIED:" + name)
    return original_import(name, *args, **kwargs)

egress_attempts = []

def block_egress(*args, **kwargs):
    egress_attempts.append("NETWORK_EGRESS_ATTEMPTED")
    raise AssertionError("NETWORK_EGRESS_ATTEMPTED")

socket.socket = block_egress
socket.create_connection = block_egress
builtins.__import__ = deny
from apps.alpha_app import BoundLocalData, OfflineAlphaApp
from beidou_shared.contracts.experiment import DatasetRef
from beidou_shared.types import InstrumentId, SchemaVersion, VenueId
from datetime import datetime, timezone

data = BoundLocalData(
    dataset=DatasetRef("fixture-local", SchemaVersion("1"), "fixture-content-hash"),
    instrument_id=InstrumentId("BTCUSDT"),
    venue_id=VenueId("LOCAL"),
    closes=tuple(100.0 + index * 0.25 for index in range(60)),
    observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
)
result = OfflineAlphaApp().evaluate(data)
loaded_forbidden = sorted(
    name for name in sys.modules
    if name.startswith(forbidden_prefixes) or name.startswith(forbidden_clients)
)
print(json.dumps({
    "row_count": result.row_count,
    "forecast_hash": result.target.forecast_hash,
    "target_weight": result.target.target_weight,
    "loaded_forbidden": loaded_forbidden,
    "network_egress_attempts": egress_attempts,
}, sort_keys=True))
"""


def _run_offline_subprocess() -> dict[str, object]:
    completed = subprocess.run(  # noqa: S603 - executable and script are test-local constants
        [sys.executable, "-c", _offline_script(), str(ROOT)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, f"offline subprocess emitted no transcript; stderr={completed.stderr!r}"
    transcript = json.loads(lines[-1])
    evidence_dir = os.environ.get("BEIDOU_EVIDENCE_DIR", "").strip()
    if evidence_dir:
        path = Path(evidence_dir) / "offline-import-transcript.json"
        path.write_text(json.dumps(transcript, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        provenance = {
            "interpreter": sys.executable,
            "python_version": sys.version,
            "repo_root": str(ROOT),
            "source_checkout": True,
            "network_policy": "DENIED",
            "status": "PASS",
        }
        (Path(evidence_dir) / "isolated-install-provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return transcript


def test_alpha_composition_processes_bound_local_data_offline() -> None:
    transcript = _run_offline_subprocess()
    assert transcript["row_count"] == 60
    assert isinstance(transcript["forecast_hash"], str) and transcript["forecast_hash"]
    assert transcript["loaded_forbidden"] == []
    assert transcript["network_egress_attempts"] == []


def test_alpha_composition_is_deterministic_for_same_bound_fixture() -> None:
    first = _run_offline_subprocess()
    second = _run_offline_subprocess()
    assert first == second


def test_alpha_composition_rejects_unbound_or_insufficient_data() -> None:
    from datetime import datetime, timezone

    from apps.alpha_app import BoundLocalData, OfflineAlphaApp
    from beidou_shared.contracts.experiment import DatasetRef
    from beidou_shared.types import InstrumentId, SchemaVersion, VenueId

    with pytest.raises(ValueError, match="LOCAL"):
        BoundLocalData(
            dataset=DatasetRef("remote", SchemaVersion("1"), "hash", source="REMOTE"),
            instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("LOCAL"),
            closes=tuple(100.0 + index for index in range(60)),
            observed_at=datetime.now(timezone.utc),
        )

    with pytest.raises(ValueError, match="51 closes"):
        OfflineAlphaApp()
        BoundLocalData(
            dataset=DatasetRef("local", SchemaVersion("1"), "hash"),
            instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("LOCAL"),
            closes=tuple(100.0 + index for index in range(50)),
            observed_at=datetime.now(timezone.utc),
        )
