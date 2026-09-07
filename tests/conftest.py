from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from beidou_alpha.panel import Panel

# M-003's second threshold, wired here because a hook only fires from a conftest.  Imported rather
# than re-declared so there is exactly one copy of the arithmetic, and it is the one under test.
from tests.architecture.suite_duration import (  # noqa: F401 - pytest collects these by name
    pytest_configure,
    pytest_sessionfinish,
)

FIXTURES = Path(__file__).parent / "fixtures"
AUGUST_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")


def load_august_panel(august_dir: Path) -> Panel:
    frames = {symbol: pd.read_parquet(august_dir / symbol / "1h.parquet") for symbol in AUGUST_SYMBOLS}
    return Panel.from_frames(frames, interval="1h")


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def august_dir() -> Path:
    return FIXTURES / "august_2026"


@pytest.fixture(scope="session")
def august_panel(august_dir: Path) -> Panel:
    return load_august_panel(august_dir)


@pytest.fixture(scope="session")
def august_baseline(august_dir: Path) -> dict:
    return json.loads((august_dir / "baseline-report.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def isolated_trials_ledger(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the one ledger at a throwaway file for every test, without anyone having to remember.

    DL-K1 fixed the ledger's address so `--out` can no longer move it.  That is the right production
    behaviour and a live hazard for the test suite: a CLI test that runs `research book` would append
    real trials to `reports/research/trials.jsonl` and raise the DSR denominator for every strategy in
    the repository.  Autouse rather than opt-in, because the failure mode of forgetting is silent and
    permanent - the ledger is append-only by design, so a bad row cannot be taken back out.
    """
    path = tmp_path_factory.mktemp("trials-ledger") / "trials.jsonl"
    monkeypatch.setenv("BEIDOU_TRIALS_LEDGER", str(path))
    return path
