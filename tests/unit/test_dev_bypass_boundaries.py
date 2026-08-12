"""Development bypasses must never be reachable from writable Testnet."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_bootstrap.dev import bootstrap_universe, patch_engine_for_dev


def test_factor_bypass_rejects_testnet_without_touching_engine() -> None:
    engine = SimpleNamespace()

    patch_engine_for_dev(engine, "testnet")

    assert vars(engine) == {}


@pytest.mark.asyncio
async def test_universe_bypass_rejects_testnet_without_touching_engine() -> None:
    engine = SimpleNamespace()

    await bootstrap_universe(engine, "testnet")

    assert vars(engine) == {}


def test_supervisor_never_routes_testnet_to_dev_bypass() -> None:
    supervisor_path = Path(__file__).parents[2] / "beidou_launcher" / "supervisor.py"
    tree = ast.parse(supervisor_path.read_text(encoding="utf-8"))
    bypass_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"patch_engine_for_dev", "bootstrap_universe"}
    ]

    assert len(bypass_calls) == 2
    for call in bypass_calls:
        assert len(call.args) >= 2
        assert isinstance(call.args[1], ast.Attribute)
        assert call.args[1].attr == "mode"

    source = supervisor_path.read_text(encoding="utf-8")
    assert 'if self.mode in ("paper", "research"):' in source
    assert '("paper", "research", "testnet")' not in source
