"""`research validate` scores all four layers, not only the signal (2026-09-08 audit).

The live book is signal -> sleeve -> exit overlay -> book guards.  `guards=` reached exactly one of
sixteen `run_backtest` call sites and `apply_exits` exactly one, neither in `validate`, so the report
the registry cites - and whose sha256 the startup gate pins - described the first layer alone.
Measured on the shipped configuration before this change: OOS 1.7662 cited against 1.8492 for the book
the loop holds.  The gap was favourable, which is a fact about today's parameters and not about the
pipeline; the same silence covers a gap of either sign.

`--no-guards` / `--no-exits` reproduce the older reports, and are recorded as `null` rather than
omitted so the gate can tell "this run applied none" from "this report predates the field".
"""

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


def _validate(root: Path, out: Path, *extra: str) -> dict:
    result = CliRunner().invoke(
        main,
        [
            "research", "validate",
            "--strategy", "tsmom",
            "--root", str(root),
            "--symbols", ",".join(SYMBOLS),
            "--out", str(out),
            "--no-funding",
            "--params", '{"horizons": [5, 20, 50], "crowding_window": 0}',
            "--grid", "{}",
            "--folds", "3",
            "--min-train", "300",
            "--purge", "5",
            "--cpcv-groups", "4",
            "--min-history", "0",
            *extra,
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    newest = max(out.glob("tsmom-validation-*.json"), key=lambda p: p.stat().st_mtime)
    return json.loads(newest.read_text())


def test_the_report_records_the_two_layers_the_loop_applies(tmp_path: Path, august_dir: Path) -> None:
    root, out = tmp_path / "data", tmp_path / "reports"
    _store_from_fixtures(august_dir, root)

    report = _validate(root, out)

    # the shipped profile: daily_loss_pause -0.05, stop_loss 6 / take_profit 6
    assert report["book_guards"]["daily_loss_pause"] == -0.05
    assert report["book_guards"]["max_gross"] == 2.0
    assert report["exits"]["stop_loss"] == 6.0 and report["exits"]["take_profit"] == 6.0
    # the guard replay's own instruments reach the artefact, so "liquidation is unreachable" is a
    # measurement in the report rather than an argument in a comment
    guards = report["full_sample"]["guards"]
    assert guards["replayed"] is True
    assert guards["liquidation_touches"] == 0 and guards["min_margin_buffer"] > 1.0


def test_the_older_shape_is_reproducible_and_says_so(tmp_path: Path, august_dir: Path) -> None:
    root, out = tmp_path / "data", tmp_path / "reports"
    _store_from_fixtures(august_dir, root)

    report = _validate(root, out, "--no-guards", "--no-exits")

    assert report["book_guards"] is None and report["exits"] is None
    assert "guards" not in report["full_sample"]


def test_running_the_layers_changes_the_number_and_the_trial_it_is_charged_as(
    tmp_path: Path, august_dir: Path, isolated_trials_ledger: Path
) -> None:
    """Same params, same data, different construction: two trials, not one replay (D-024)."""
    root, out = tmp_path / "data", tmp_path / "reports"
    _store_from_fixtures(august_dir, root)

    layered = _validate(root, out, "--out", str(out))
    bare = _validate(root, out, "--no-guards", "--no-exits")

    assert layered["full_sample"]["annualized_sharpe"] != bare["full_sample"]["annualized_sharpe"]
    digests = {
        json.loads(line)["overlay_digest"] for line in isolated_trials_ledger.read_text().splitlines() if line.strip()
    }
    assert len(digests) == 2, "a guarded run and a bare one must not dedupe onto one ledger row"


def test_the_report_prices_the_book_at_the_declared_slippage_levels(tmp_path: Path, august_dir: Path) -> None:
    """`cost_stress` scales the fee too; this block holds it fixed and varies only what the loop measures."""
    root, out = tmp_path / "data", tmp_path / "reports"
    _store_from_fixtures(august_dir, root)

    report = _validate(root, out)

    levels = report["slippage_stress"]
    assert set(levels) == {"slip2", "slip5.5", "slip9.2"}, levels
    assert levels["slip2"] > levels["slip9.2"], "more slippage cannot help"
    # the shipped assumption is one of the levels, so the block contains its own baseline
    assert levels["slip2"] == report["cost_stress"]["x1"]


def test_the_report_carries_the_other_execution_convention_as_a_comparator(tmp_path: Path, august_dir: Path) -> None:
    """`open_to_close` drops the close->open gap on every held bar; live holds through it."""
    root, out = tmp_path / "data", tmp_path / "reports"
    _store_from_fixtures(august_dir, root)

    report = _validate(root, out)

    other = report["execution_comparison"]
    assert other["execution"] == "close_to_close"
    assert other["annualized_sharpe"] is not None
    assert other["annualized_sharpe"] != report["full_sample"]["annualized_sharpe"]
