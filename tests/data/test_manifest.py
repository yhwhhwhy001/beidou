"""The dataset manifest, written against the drift it exists to catch."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from beidou_data.manifest import build_manifest, manifest_problems


def _root(tmp_path: Path, *, refreshes: int, freq: str, symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")) -> Path:
    root = tmp_path / "data"
    (root / "klines" / "BTCUSDT").mkdir(parents=True, exist_ok=True)
    index = pd.date_range("2024-01-01", periods=refreshes, freq=freq, tz="UTC")
    table = pd.DataFrame(True, index=index, columns=list(symbols))
    table.to_parquet(root / "membership.parquet")
    (root / "universe.json").write_text(
        json.dumps({"symbols": list(symbols), "source": "pool-refresh", "selected_at_ms": 1}), encoding="utf-8"
    )
    frame = pd.DataFrame({"open_time": [0, 3_600_000], "close": [1.0, 2.0]})
    frame.to_parquet(root / "klines" / "BTCUSDT" / "1h.parquet")
    return root


def test_a_manifest_of_unchanged_data_reports_no_problem(tmp_path: Path) -> None:
    root = _root(tmp_path, refreshes=30, freq="D")
    recorded = build_manifest(root).to_dict()
    assert manifest_problems(recorded, build_manifest(root)) == []


def test_the_p12_drift_is_named_rather_than_merely_detected(tmp_path: Path) -> None:
    """A monthly table rebuilt as daily is the 2026-09-04 incident; the manifest must say which field moved."""
    root = _root(tmp_path, refreshes=6, freq="MS")
    recorded = build_manifest(root).to_dict()

    index = pd.date_range("2024-01-01", periods=180, freq="D", tz="UTC")
    pd.DataFrame(True, index=index, columns=["BTCUSDT", "ETHUSDT"]).to_parquet(root / "membership.parquet")

    problems = manifest_problems(recorded, build_manifest(root))
    assert problems, "a table rebuilt at a different cadence must not pass silently"
    message = problems[0]
    assert "membership.refreshes: 6 -> 180" in message
    assert "membership.median_gap_hours" in message, "the cadence change is the fact P12 needed"


def test_a_changed_universe_selection_is_caught(tmp_path: Path) -> None:
    root = _root(tmp_path, refreshes=30, freq="D")
    recorded = build_manifest(root).to_dict()
    (root / "universe.json").write_text(
        json.dumps({"symbols": ["BTCUSDT", "SOLUSDT"], "source": "live-refresh", "selected_at_ms": 2}), encoding="utf-8"
    )
    problems = manifest_problems(recorded, build_manifest(root))
    assert problems and "universe.fingerprint" in problems[0]


def test_a_result_with_no_manifest_reports_unknown_provenance_not_agreement(tmp_path: Path) -> None:
    """ "No manifest" and "manifest matches" are different facts and must not collapse into each other."""
    root = _root(tmp_path, refreshes=30, freq="D")
    problems = manifest_problems(None, build_manifest(root))
    assert problems and "cannot be checked" in problems[0]


def test_the_digest_ignores_mtime_so_a_rewrite_of_identical_data_is_not_drift(tmp_path: Path) -> None:
    root = _root(tmp_path, refreshes=30, freq="D")
    recorded = build_manifest(root).to_dict()
    table = pd.read_parquet(root / "membership.parquet")
    table.to_parquet(root / "membership.parquet")  # same content, new mtime
    assert manifest_problems(recorded, build_manifest(root)) == []
