"""Behavior tests for the side-effect-free legacy shell wrapper."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _sandboxed_wrapper(tmp_path: Path) -> Path:
    wrapper = tmp_path / "start_beidou.sh"
    shutil.copy2(ROOT / "start_beidou.sh", wrapper)
    launcher = tmp_path / ".venv" / "bin" / "beidou"
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/bin/sh\nprintf "delegated:%s\\n" "$*"\n', encoding="utf-8")
    launcher.chmod(0o755)
    return wrapper


def test_wrapper_requires_explicit_start_mode_and_symbols(tmp_path: Path) -> None:
    wrapper = _sandboxed_wrapper(tmp_path)

    missing_all = subprocess.run(  # noqa: S603 - test-owned executable path
        [str(wrapper)], capture_output=True, text=True, check=False
    )
    missing_symbols = subprocess.run(  # noqa: S603 - test-owned executable path
        [str(wrapper), "start", "--mode", "safety_only"], capture_output=True, text=True, check=False
    )

    assert missing_all.returncode == 64
    assert missing_symbols.returncode == 64
    assert "delegated:" not in missing_all.stdout + missing_symbols.stdout


def test_wrapper_execs_only_the_canonical_launcher(tmp_path: Path) -> None:
    wrapper = _sandboxed_wrapper(tmp_path)

    result = subprocess.run(  # noqa: S603 - test-owned executable path
        [str(wrapper), "start", "--mode", "safety_only", "--symbols", "EXPLICIT_SYMBOL"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "delegated:start --mode safety_only --symbols EXPLICIT_SYMBOL"
