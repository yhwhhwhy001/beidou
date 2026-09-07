"""DL-K1's other half: which commands pay, and the fact that they cannot pay into a different book.

`research validate` and `research book` charged the ledger.  `research backtest` and `research overlay`
did not, and they evaluate configurations exactly as much - that is E-15's finding, and it is why the
protocol in the report §7.1.4 says every configuration evaluated is a trial rather than every
configuration *validated*.  A search whose exploratory arm is free is a search whose denominator is
wrong in the one direction that flatters it.

C-P6 bounds this: it changes what future runs cost.  Nothing here reaches back into the 145 archived
rows, so tsmom's N=125 is the number it was.
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


def _backtest_args(root: Path, out: Path, *, vol_window: int = 100) -> list[str]:
    return [
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
        f'{{"vol_window": {vol_window}, "horizons": [5, 20, 50], "crowding_window": 0}}',
        "--min-history",
        "0",
    ]


def _rows(ledger: Path) -> list[dict]:
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_backtest_is_a_trial(  # T-K1-2
    tmp_path: Path, august_dir: Path, isolated_trials_ledger: Path
) -> None:
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)

    result = CliRunner().invoke(main, _backtest_args(root, tmp_path / "reports"))

    assert result.exit_code == 0, result.output
    rows = _rows(isolated_trials_ledger)
    assert len(rows) == 1
    assert rows[0]["strategy"] == "tsmom"
    assert rows[0]["construction_digest"] and rows[0]["symbol_set_hash"]


def test_the_same_backtest_twice_is_still_one_trial(
    tmp_path: Path, august_dir: Path, isolated_trials_ledger: Path
) -> None:
    """D-024 is unchanged by any of this: an exact replay on the same data is one trial."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    runner = CliRunner()

    assert runner.invoke(main, _backtest_args(root, tmp_path / "r1")).exit_code == 0
    assert runner.invoke(main, _backtest_args(root, tmp_path / "r2")).exit_code == 0

    assert len(_rows(isolated_trials_ledger)) == 1


def test_scanning_a_second_parameter_value_is_a_second_trial(
    tmp_path: Path, august_dir: Path, isolated_trials_ledger: Path
) -> None:
    """The thing that was free: try five vol windows by hand, charge nothing, report the best."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    runner = CliRunner()

    assert runner.invoke(main, _backtest_args(root, tmp_path / "r1", vol_window=100)).exit_code == 0
    assert runner.invoke(main, _backtest_args(root, tmp_path / "r2", vol_window=120)).exit_code == 0

    assert len(_rows(isolated_trials_ledger)) == 2


def test_pointing_the_reports_elsewhere_does_not_buy_a_fresh_ledger(  # T-K1-3
    tmp_path: Path, august_dir: Path, isolated_trials_ledger: Path
) -> None:
    """KILL-Q5 exactly: `--out` used to be the accounting flag as well as the reporting one."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    runner = CliRunner()

    assert runner.invoke(main, _backtest_args(root, tmp_path / "reports", vol_window=100)).exit_code == 0
    assert runner.invoke(main, _backtest_args(root, tmp_path / "scratch", vol_window=120)).exit_code == 0

    assert len(_rows(isolated_trials_ledger)) == 2
    assert not (tmp_path / "scratch" / "trials.jsonl").exists()
    assert not (tmp_path / "reports" / "trials.jsonl").exists()


def test_the_report_says_which_ledger_it_was_charged_to(
    tmp_path: Path, august_dir: Path, isolated_trials_ledger: Path
) -> None:
    """A run that dodges the ledger has to be identifiable afterwards, not only preventable beforehand."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)

    assert CliRunner().invoke(main, _backtest_args(root, tmp_path / "reports")).exit_code == 0

    report = json.loads(next((tmp_path / "reports").glob("tsmom-backtest-*.json")).read_text())
    assert report["ledger"]["path"] == str(isolated_trials_ledger)


# --- DL-K2: the search pays for itself ------------------------------------------------------------


def _mine_args(root: Path, out: Path, *extra: str) -> list[str]:
    return [
        "research",
        "mine",
        "--strategy",
        "tsmom",
        "--root",
        str(root),
        "--symbols",
        ",".join(SYMBOLS),
        "--out",
        str(out),
        "--max-lookback",
        "200",
        "--min-history",
        "24",
        "--top",
        "3",
        "--no-funding",
        "--no-include-funding",
        *extra,
    ]


def test_a_mine_charges_every_candidate_it_kept(  # T-K2-1
    tmp_path: Path, august_dir: Path, isolated_trials_ledger: Path
) -> None:
    """The 514 in P20's verdict was hand-copied off the terminal.  This is where that stops."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    out = tmp_path / "reports"

    result = CliRunner().invoke(main, _mine_args(root, out))

    assert result.exit_code == 0, result.output
    payload = json.loads(sorted(out.glob("mine-shortlist-*.json"))[-1].read_text())
    rows = _rows(isolated_trials_ledger)
    assert len(rows) == len(payload["candidates"])
    assert {row["strategy"] for row in rows} == {"mined"}
    assert {row["param_key"] for row in rows} == {c["hash"] for c in payload["candidates"]}


def test_re_running_the_same_search_does_not_charge_it_twice(
    tmp_path: Path, august_dir: Path, isolated_trials_ledger: Path
) -> None:
    """A search is a set of hypotheses, not an event.  Looking at the same 514 again examines nothing new."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    runner = CliRunner()

    assert runner.invoke(main, _mine_args(root, tmp_path / "r1")).exit_code == 0
    first = len(_rows(isolated_trials_ledger))
    assert runner.invoke(main, _mine_args(root, tmp_path / "r2")).exit_code == 0

    assert first > 0
    assert len(_rows(isolated_trials_ledger)) == first


def test_a_wider_search_charges_only_what_is_new(
    tmp_path: Path, august_dir: Path, isolated_trials_ledger: Path
) -> None:
    """Why mine rows carry no `search_space_version`.

    A candidate examined in a 267-wide search and again in a 514-wide one is one hypothesis looked at
    twice, not two.  Stamping the space version onto mine rows would charge 267 + 514 for a family of
    514 - inflating N in the direction that looks rigorous and is simply wrong.
    """
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    runner = CliRunner()

    narrow = runner.invoke(main, _mine_args(root, tmp_path / "r1", "--grids", '{"include_panel_nodes": false}'))
    assert narrow.exit_code == 0, narrow.output
    after_narrow = len(_rows(isolated_trials_ledger))
    wide = runner.invoke(main, _mine_args(root, tmp_path / "r2"))
    assert wide.exit_code == 0, wide.output

    wide_payload = json.loads(sorted((tmp_path / "r2").glob("mine-shortlist-*.json"))[-1].read_text())
    assert 0 < after_narrow < len(wide_payload["candidates"])
    # The union, not the sum: every narrow candidate is also a wide one.
    assert len(_rows(isolated_trials_ledger)) == len(wide_payload["candidates"])
    assert all(row["search_space_version"] == "" for row in _rows(isolated_trials_ledger))


def test_validating_a_mined_candidate_reads_the_search_that_found_it(  # T-K2-2
    tmp_path: Path, august_dir: Path, isolated_trials_ledger: Path
) -> None:
    """The number that had to be hand-carried from the mine's last line into `--prior-trials`."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    out = tmp_path / "reports"
    runner = CliRunner()
    assert runner.invoke(main, _mine_args(root, out)).exit_code == 0
    payload = json.loads(sorted(out.glob("mine-shortlist-*.json"))[-1].read_text())
    candidate = next(c for c in payload["candidates"] if c.get("sharpe") is not None)

    result = runner.invoke(
        main,
        [
            "research",
            "validate",
            "--strategy",
            f"mined_{candidate['hash']}",
            "--root",
            str(root),
            "--symbols",
            ",".join(SYMBOLS),
            "--out",
            str(out),
            "--no-funding",
            "--min-history",
            "24",
            "--folds",
            "3",
            "--min-train",
            "300",
            "--purge",
            "5",
            "--cpcv-groups",
            "4",
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(sorted(out.glob("mined_*-validation-*.json"))[-1].read_text())
    # No --prior-trials was passed, and the denominator is the search anyway.
    assert report["prior_trials_declared"] == 0
    assert report["multiple_testing"]["ledger_trials"] == len(payload["candidates"])
    assert report["multiple_testing"]["n_trials"] >= len(payload["candidates"])


def test_the_weekly_check_sees_book_reports_not_only_validations(tmp_path: Path) -> None:
    """DL-K3's own coverage, asserted rather than assumed."""
    from beidou_cli.live_cmd import _validations_since

    directory = tmp_path / "research"
    directory.mkdir()
    (directory / "tsmom-validation-x.json").write_text(
        json.dumps({"kind": "validation", "strategy": "tsmom", "generated_at": "2026-09-08T00:00:00+00:00"}),
        encoding="utf-8",
    )
    (directory / "book-tsmom-mined_abc-x.json").write_text(
        json.dumps(
            {
                "kind": "book",
                "main": {"strategy": "tsmom"},
                "sleeve": {"strategy": "mined_abc"},
                "generated_at": "2026-09-08T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (directory / "mine-shortlist-x.json").write_text(
        json.dumps({"kind": "mine", "generated_at": "2026-09-08T00:00:00+00:00"}), encoding="utf-8"
    )

    found = _validations_since(directory, 0)

    # The sleeve is the thing being promoted, so it is the strategy the ordering is checked against.
    assert sorted(row["strategy"] for row in found) == ["mined_abc", "tsmom"]
