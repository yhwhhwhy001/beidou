"""Compatibility facade for launcher preflight and wiring checks."""

from __future__ import annotations

import os
from pathlib import Path

from .manifest import HEALTH_PORT
from .models import StartupReport
from .preflight import current_commit, run_preflight
from .registry import inspect_engine_wiring


def find_project_root(start: Path | None = None) -> Path:
    explicit = os.environ.get("BEIDOU_PROJECT_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    current = (start or Path.cwd()).resolve()
    package_root = Path(__file__).resolve().parents[1]
    for candidate in (current, *current.parents, package_root):
        if (candidate / "pyproject.toml").is_file() and (candidate / "beidou_core").exists():
            return candidate
    raise FileNotFoundError("Unable to locate the Beidou project root")


class PreflightChecker:
    """Backward-compatible wrapper around the authoritative preflight checks."""

    def __init__(
        self,
        mode: str,
        symbols: list[str],
        port: int = HEALTH_PORT,
        project_root: Path | None = None,
    ) -> None:
        self.mode = mode
        self.symbols = symbols
        self.port = port
        self.root = project_root or find_project_root()

    def run(self) -> StartupReport:
        checks, _settings = run_preflight(self.root, self.mode, self.port)
        return StartupReport(
            mode=self.mode,
            symbols=self.symbols,
            port=self.port,
            commit=current_commit(self.root),
            checks=checks,
            phase="PREFLIGHT",
            supervisor_state="PREFLIGHT",
        )


__all__ = [
    "PreflightChecker",
    "current_commit",
    "find_project_root",
    "inspect_engine_wiring",
    "run_preflight",
]
