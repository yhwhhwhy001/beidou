"""Legacy applications may only delegate to the canonical launcher."""

from __future__ import annotations

import importlib
import sys

import pytest

import beidou_launcher.cli as launcher_cli


@pytest.mark.parametrize(
    ("module_name", "expected_args"),
    [
        ("apps.autopilot.__main__", ["start", "--symbols", "EXPLICIT_SYMBOL"]),
        (
            "apps.strategy_engine.__main__",
            ["start", "--mode", "paper", "--symbols", "EXPLICIT_SYMBOL"],
        ),
        (
            "apps.safety_executor.__main__",
            ["start", "--mode", "safety_only", "--symbols", "EXPLICIT_SYMBOL"],
        ),
    ],
)
def test_legacy_entrypoint_delegates_without_constructing_runtime(
    module_name: str,
    expected_args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(sys, "argv", [module_name, "--symbols", "EXPLICIT_SYMBOL"])
    monkeypatch.setattr(launcher_cli.main, "main", lambda **kwargs: captured.append(kwargs))

    importlib.import_module(module_name).main()

    assert captured == [
        {
            "args": expected_args,
            "prog_name": f"python -m {module_name.removesuffix('.__main__')}",
            "standalone_mode": True,
        }
    ]


@pytest.mark.parametrize(
    "module_name",
    ["apps.strategy_engine.__main__", "apps.safety_executor.__main__"],
)
def test_fixed_safe_legacy_entrypoints_reject_mode_override(module_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", [module_name, "--mode", "testnet", "--symbols", "EXPLICIT_SYMBOL"])

    with pytest.raises(SystemExit, match="fixes mode="):
        importlib.import_module(module_name).main()
