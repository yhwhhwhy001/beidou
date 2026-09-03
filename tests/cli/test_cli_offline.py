from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from click.testing import CliRunner

from beidou_cli import main
from beidou_data.store import KlineStore

SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")


def _store_from_fixtures(august_dir: Path, root: Path) -> None:
    store = KlineStore(root)
    for symbol in SYMBOLS:
        frame = pd.read_parquet(august_dir / symbol / "1h.parquet")
        frame["close_time"] = frame["open_time"] + 3_600_000 - 1
        store.append(symbol, "1h", frame)


def test_help_has_all_groups() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for group in ("data", "research", "live", "report"):
        assert group in result.output


def test_research_backtest_and_validate_offline(tmp_path: Path, august_dir: Path) -> None:
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    out = tmp_path / "reports"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "research",
            "backtest",
            "--strategy",
            "tsmom",
            "--root",
            str(root),
            "--symbols",
            ",".join(SYMBOLS),
            "--out",
            str(out),
            "--no-funding",
            "--params",
            '{"vol_window": 100}',
            "--min-history",
            "0",
        ],
    )
    assert result.exit_code == 0, result.output
    reports = list(out.glob("tsmom-backtest-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text())
    assert payload["summary"]["bars"] > 500 and payload["symbols"] == list(SYMBOLS)
    result = runner.invoke(
        main,
        [
            "research",
            "validate",
            "--strategy",
            "tsmom",
            "--root",
            str(root),
            "--symbols",
            ",".join(SYMBOLS),
            "--out",
            str(out),
            "--no-funding",
            "--grid",
            '{"vol_window": [100, 200], "entry_threshold": [0.2, 0.3]}',
            "--folds",
            "3",
            "--min-train",
            "300",
            "--purge",
            "5",
            "--cpcv-groups",
            "4",
            "--min-history",
            "0",
        ],
    )
    assert result.exit_code == 0, result.output
    validation = json.loads(next(out.glob("tsmom-validation-*.json")).read_text())
    assert validation["verdict"] in {"PASS", "WEAK_PASS", "FAIL"}
    assert validation["grid_size"] == 4
    assert "sha256=" in result.output


def test_data_status_offline(tmp_path: Path, august_dir: Path) -> None:
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    result = CliRunner().invoke(main, ["data", "status", "--root", str(root)])
    assert result.exit_code == 0
    assert "BTCUSDT" in result.output
