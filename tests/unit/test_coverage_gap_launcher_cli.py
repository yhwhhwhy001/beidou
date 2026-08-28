"""Coverage-gap tests for the ``g5-producer`` branch of beidou_launcher.cli."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

import beidou_launcher.cli as cli_module


@pytest.fixture(autouse=True)
def _restore_cwd():
    original = Path.cwd()
    yield
    os.chdir(original)


def _prepare_cli_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "find_project_root", lambda: tmp_path)


def test_g5_producer_rejects_non_testnet_mode(tmp_path, monkeypatch) -> None:
    _prepare_cli_root(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["g5-producer", "--mode", "paper"])
    assert result.exit_code == 1
    assert "仅允许 --mode testnet" in result.output


def test_g5_producer_requires_explicit_confirmation(tmp_path, monkeypatch) -> None:
    _prepare_cli_root(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["g5-producer", "--mode", "testnet"])
    assert result.exit_code == 1
    assert "--confirm-g5-producer" in result.output


def test_g5_producer_sets_environment_markers(tmp_path, monkeypatch) -> None:
    _prepare_cli_root(tmp_path, monkeypatch)
    # ``main`` mutates os.environ directly; pre-seed the producer markers via
    # ``setenv`` so monkeypatch records the original value and restores it after
    # the test instead of leaking ``BEIDOU_G5_PRODUCER=1`` into the whole suite.
    monkeypatch.setenv("BEIDOU_G5_PRODUCER", "")
    monkeypatch.setenv("BEIDOU_TERMINAL_WRITE_HOLD", "")
    monkeypatch.setenv("BEIDOU_ENV", "")
    runner = CliRunner()
    captured: dict = {}

    class Supervisor:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def run(self) -> int:
            return 7

    monkeypatch.setattr(cli_module, "BeidouSupervisor", Supervisor)

    result = runner.invoke(
        cli_module.main,
        ["g5-producer", "--mode", "testnet", "--confirm-g5-producer"],
    )

    assert result.exit_code == 7
    assert captured["producer_only"] is True
    assert captured["mode"] == "testnet"
    assert cli_module.os.environ["BEIDOU_G5_PRODUCER"] == "1"
    assert cli_module.os.environ["BEIDOU_TERMINAL_WRITE_HOLD"] == "hard"
