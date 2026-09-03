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
            '{"vol_window": 100, "horizons": [5, 20, 50]}',
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
            "--params",
            '{"horizons": [5, 20, 50]}',
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


def test_research_pit_universe_and_overlay_offline(tmp_path: Path, august_dir: Path) -> None:
    """--universe pit reads the membership table; research overlay writes the D-017 evidence report."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    index = pd.DatetimeIndex(pd.read_parquet(august_dir / "BTCUSDT" / "1h.parquet")["open_time"], name="open_time")
    stamps = pd.to_datetime(index.to_numpy(), unit="ms", utc=True)
    membership = pd.DataFrame(
        [[True, True, True, False], [True, True, False, True]],
        index=pd.DatetimeIndex([stamps[0], stamps[len(stamps) // 2]]),
        columns=list(SYMBOLS),
    )
    membership.to_parquet(root / "membership.parquet")
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
            "--universe",
            "pit",
            "--out",
            str(out),
            "--no-funding",
            "--params",
            '{"vol_window": 100, "horizons": [5, 20, 50]}',
            "--min-history",
            "0",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(next(out.glob("tsmom-backtest-*.json")).read_text())
    assert payload["universe_mode"] == "pit" and set(payload["symbols"]) == set(SYMBOLS)
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "version: 1\n"
        "ensemble: {method: mean}\n"
        "strategies:\n"
        "  - {id: tsmom, enabled: true, params: {vol_window: 100, horizons: [5, 20, 50], horizon_weights: [0.2, 0.3, 0.5]}}\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        main,
        [
            "research",
            "overlay",
            "--root",
            str(root),
            "--symbols",
            ",".join(SYMBOLS),
            "--registry",
            str(registry),
            "--out",
            str(out),
            "--no-funding",
            "--min-history",
            "0",
            "--folds",
            "3",
            "--min-train",
            "300",
            "--exits-grid",
            '{"stop_loss": [0.0, 2.0], "take_profit": [0.0, 4.0]}',
            "--throttle-grid",
            '{"start": [0.02], "stop": [0.10], "floor": [0.5]}',
        ],
    )
    assert result.exit_code == 0, result.output
    overlay = json.loads(next(out.glob("overlay-*.json")).read_text())
    assert overlay["baseline"]["oos_sharpe"] is not None
    assert [row["kind"] for row in overlay["candidates"]] == ["exits", "exits", "exits", "throttle"]
    assert set(overlay["recommendation"]) == {"exits", "throttle"}
    assert "recommendation:" in result.output
