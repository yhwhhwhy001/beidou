from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from beidou_alpha.panel import Panel

AUGUST_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")


def load_august_panel(august_dir: Path) -> Panel:
    frames = {symbol: pd.read_parquet(august_dir / symbol / "1h.parquet") for symbol in AUGUST_SYMBOLS}
    return Panel.from_frames(frames, interval="1h")


@pytest.fixture(scope="session")
def august_panel(august_dir: Path) -> Panel:
    return load_august_panel(august_dir)


@pytest.fixture(scope="session")
def august_baseline(august_dir: Path) -> dict:
    return json.loads((august_dir / "baseline-report.json").read_text(encoding="utf-8"))
