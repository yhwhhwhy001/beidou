from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from beidou_alpha.panel import Panel

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
