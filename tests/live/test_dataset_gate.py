"""D-041: the dataset manifest is checked at startup, not merely written.

`manifest_problems` existed and had no caller: `research validate` wrote a manifest into every report
and nothing ever read one back, so the stale-evidence pointer it was built to catch could not be caught.
These tests cover the wiring and, more importantly, the severity split - the gate has to refuse a
membership rebuild without refusing the daily sync, or it will be turned off within a week.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from beidou_alpha.registry import Registry, StrategyEntry
from beidou_data.manifest import build_manifest
from beidou_data.store import FundingStore, KlineStore
from beidou_live.composition import load_registry
from beidou_live.config import registry_dataset_problems
from beidou_live.reports import daily_markdown, daily_payload, weekly_markdown, weekly_payload
from beidou_live.state import StateStore

ROOT = Path(__file__).resolve().parents[2]


def _data_root(tmp_path: Path, *, refreshes: int = 30, symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")) -> Path:
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    index = pd.date_range("2024-01-01", periods=refreshes, freq="D", tz="UTC")
    pd.DataFrame(True, index=index, columns=list(symbols)).to_parquet(root / "membership.parquet")
    (root / "universe.json").write_text(
        json.dumps({"symbols": list(symbols), "source": "pool-refresh", "selected_at_ms": 1}), encoding="utf-8"
    )
    KlineStore(root).append("BTCUSDT", "1h", pd.DataFrame({"open_time": [0, 3_600_000], "close": [1.0, 2.0]}))
    FundingStore(root).append(
        "BTCUSDT", pd.DataFrame({"funding_time": [0], "funding_rate": [0.0001], "mark_price": [100.0]})
    )
    return root


def _registry_citing(report: Path) -> Registry:
    entry = StrategyEntry("tsmom", evidence={"report": str(report), "sha256": "a" * 64, "verdict": "PASS"})
    return Registry(version=1, ensemble_method="mean", turnover_penalty=0.0, strategies=(entry,))


def _report(tmp_path: Path, data_root: Path, *, with_manifest: bool = True) -> Path:
    payload: dict[str, object] = {"kind": "validation", "verdict": "PASS"}
    if with_manifest:
        payload["dataset"] = build_manifest(data_root).to_dict()
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_membership_rebuild_blocks_the_strategy_that_cites_it(tmp_path: Path) -> None:
    """P12's shape, now refused at startup instead of discovered afterwards."""
    root = _data_root(tmp_path, refreshes=6)
    registry = _registry_citing(_report(tmp_path, root))

    index = pd.date_range("2024-01-01", periods=180, freq="D", tz="UTC")
    pd.DataFrame(True, index=index, columns=["BTCUSDT", "ETHUSDT"]).to_parquet(root / "membership.parquet")

    check = registry_dataset_problems(registry, data_root=root)
    assert check.blocking and check.blocking[0].startswith("tsmom: ")
    assert "membership.refreshes: 6 -> 180" in check.blocking[0]


def test_the_daily_sync_appending_bars_does_not_block(tmp_path: Path) -> None:
    """The archive grows every day; a gate that refuses on that gets --allow-unvalidated forever."""
    root = _data_root(tmp_path)
    registry = _registry_citing(_report(tmp_path, root))
    KlineStore(root).append("ETHUSDT", "1h", pd.DataFrame({"open_time": [0], "close": [1.0]}))

    check = registry_dataset_problems(registry, data_root=root)
    assert not check.blocking
    assert check.advisory and "klines.symbols: 1 -> 2" in check.advisory[0]


def test_a_report_with_no_dataset_block_is_advisory_so_nothing_in_flight_is_blocked(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    registry = _registry_citing(_report(tmp_path, root, with_manifest=False))

    check = registry_dataset_problems(registry, data_root=root)
    assert not check.blocking
    assert check.advisory and "no dataset manifest recorded" in check.advisory[0]


def test_a_missing_report_is_left_to_the_evidence_gate_rather_than_reported_twice(tmp_path: Path) -> None:
    """`registry_evidence_problems` already says the report is missing; saying it again is noise."""
    root = _data_root(tmp_path)
    registry = _registry_citing(tmp_path / "gone.json")

    check = registry_dataset_problems(registry, data_root=root)
    assert not check.blocking and not check.advisory


def test_a_disabled_strategy_is_not_checked(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    entry = StrategyEntry("tsmom", enabled=False, evidence={"report": str(tmp_path / "gone.json")})
    registry = Registry(version=1, ensemble_method="mean", turnover_penalty=0.0, strategies=(entry,))

    check = registry_dataset_problems(registry, data_root=root)
    assert not check.blocking and not check.advisory


@pytest.mark.skipif(not (ROOT / ".beidou" / "data").exists(), reason="live data root is not in this checkout")
def test_the_shipped_registry_is_not_blocked_on_the_machine_that_runs_the_loop() -> None:
    """The operator-facing guard: adding this gate must not stop what is already running.

    Skipped in worktrees and CI, where `.beidou/data` does not exist - it is a statement about the
    machine holding the archive, which is the only place the answer means anything.
    """
    registry = load_registry(ROOT / "config" / "alpha_registry.yaml")
    check = registry_dataset_problems(registry, data_root=ROOT / ".beidou" / "data")
    assert not check.blocking, check.blocking


def _dataset_block() -> dict[str, list[str]]:
    return {
        "blocking": [],
        "advisory": ["flow: no dataset manifest recorded: this result's data provenance cannot be checked"],
    }


def test_the_daily_report_carries_the_dataset_check(tmp_path: Path) -> None:
    """Where a human actually reads it.  A check only the startup gate sees is invisible after startup."""
    store = StateStore(tmp_path / "live")
    payload = daily_payload(store, "2025-09-02", dataset=_dataset_block())
    assert payload["dataset"] == _dataset_block()
    assert "no dataset manifest recorded" in daily_markdown(payload)


def test_the_weekly_report_carries_the_dataset_check(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live")
    payload = weekly_payload(store, "2025-09-02", dataset=_dataset_block())
    assert payload["dataset"] == _dataset_block()
    assert "no dataset manifest recorded" in weekly_markdown(payload)


def test_a_report_written_without_a_dataset_check_still_renders(tmp_path: Path) -> None:
    """Older payloads on disk have no `dataset` key; rendering one must not raise."""
    store = StateStore(tmp_path / "live")
    payload = daily_payload(store, "2025-09-02")
    assert payload["dataset"] == {"blocking": [], "advisory": []}
    assert daily_markdown(payload)
