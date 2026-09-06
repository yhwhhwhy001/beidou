from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from beidou_alpha.mining import Funding, Ratio, Ret, Squash, Vol
from beidou_alpha.mining.search import Candidate
from beidou_alpha.signals import SIGNALS, get_signal
from beidou_cli import main
from beidou_cli.research_cmd import _resolve_mined
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
    # D-024: the report is reproducible from itself and records every trial it charged
    assert validation["folds"] == 3 and validation["min_train"] == 300 and validation["purge"] == 5
    assert validation["grid"] == {"vol_window": [100, 200], "entry_threshold": [0.2, 0.3]}
    assert len(validation["trial_sharpes"]) == 4 and validation["ledger"]["ledger_rows"] == 0
    # D-024: the report says which portfolio construction produced these numbers, so a later band or
    # half-life change in the profile is detectable rather than silent
    assert validation["portfolio"]["no_trade_rel_band"] is not None
    assert validation["portfolio"]["vol_target"] > 0 and validation["portfolio"]["max_weight"] > 0
    assert "noise_null" in validation["multiple_testing"]
    # an exact replay of the same grid on the same data is not charged twice (ledger dedupe)
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
    replay = json.loads(sorted(out.glob("tsmom-validation-*.json"))[-1].read_text())
    assert replay["ledger"]["ledger_rows"] == 4 and replay["ledger"]["ledger_trials"] == 0
    assert replay["multiple_testing"]["n_trials"] == validation["multiple_testing"]["n_trials"]
    result = runner.invoke(
        main,
        [
            "research",
            "decompose",
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
            "--folds",
            "3",
            "--min-train",
            "300",
            "--purge",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    decomposition = json.loads(next(out.glob("decompose-tsmom-*.json")).read_text())
    assert set(decomposition["variants"]) == {
        "full",
        "constant_long",
        "sign_only",
        "long_only",
        "short_only",
        "equal_notional",
    }
    assert "signal_over_construction" in decomposition["increments"] and "increments:" in result.output


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
    # D-024: the report says which registry it decided against, so a later reader can tell if it expired
    fingerprint = overlay["registry"]
    assert len(fingerprint["digest"]) == 64 and fingerprint["ensemble_method"] == "mean"
    assert set(fingerprint["strategies"]) == set(overlay["strategies"])
    tsmom_params = fingerprint["strategies"]["tsmom"]["params"]
    assert tsmom_params["horizons"] == [5, 20, 50] and tsmom_params["vol_window"] == 100  # what this run used
    assert tsmom_params["conviction_mode"] == "score"  # an unwritten default is filled in, not left absent
    assert overlay["portfolio"]["max_weight"] > 0
    assert f"registry digest: {fingerprint['digest']}" in result.output
    assert "recommendation:" in result.output


def test_research_book_offline(tmp_path: Path, august_dir: Path) -> None:
    """research book writes the D-018 evidence report and charges the sleeve's standalone trial once."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    out = tmp_path / "reports"
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "version: 1\n"
        "ensemble: {method: mean}\n"
        "strategies:\n"
        "  - {id: tsmom, enabled: true, params: {vol_window: 100, horizons: [5, 20, 50], horizon_weights: [0.2, 0.3, 0.5]}}\n"
        "  - {id: xsmom, enabled: false, params: {horizons: [5, 20, 50], horizon_weights: [0.2, 0.3, 0.5]}}\n",
        encoding="utf-8",
    )
    args = [
        "research",
        "book",
        "--main",
        "tsmom",
        "--sleeve",
        "xsmom",
        "--sleeve-params",
        '{"entry_threshold": 0.2}',
        "--root",
        str(root),
        "--symbols",
        ",".join(SYMBOLS),
        "--universe",
        "static",
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
        "--purge",
        "5",
        "--cpcv-groups",
        "4",
        "--prior-trials",
        "3",
    ]
    runner = CliRunner()
    result = runner.invoke(main, args)
    assert result.exit_code == 0, result.output
    report = json.loads(next(out.glob("book-tsmom-xsmom-*.json")).read_text())
    assert report["book_verdict"] in {"ACCEPT", "REJECT"}
    assert report["sleeve"]["params"]["entry_threshold"] == 0.2
    decision = report["universes"]["static"]
    assert set(decision["by_fraction"]) == {"0.3333", "0.2000", "0.5000"}
    assert decision["sleeve_standalone"]["multiple_testing"]["n_trials"] == 4  # 3 declared + this one
    assert decision["sleeve_standalone"]["verdict"] in {"PASS", "WEAK_PASS", "FAIL"}
    assert "BOOK VERDICT" in result.output and "robustness universe not evaluated" in result.output
    ledger = (out / "trials.jsonl").read_text().splitlines()
    assert len(ledger) == 1 and json.loads(ledger[0])["strategy"] == "xsmom"
    # an exact replay is not a second trial
    result = runner.invoke(main, args)
    assert result.exit_code == 0, result.output
    assert "exact replay" in result.output
    assert len((out / "trials.jsonl").read_text().splitlines()) == 1
    replay = json.loads(sorted(out.glob("book-tsmom-xsmom-*.json"))[-1].read_text())
    assert replay["universes"]["static"]["sleeve_standalone"]["multiple_testing"]["n_trials"] == 4


def test_validate_reserves_a_holdout_tail(tmp_path: Path) -> None:
    """KILL-006: --holdout-months cuts the tail before folds and records the reservation in the report."""
    import numpy as np

    root = tmp_path / "data"
    store = KlineStore(root)
    step = 3_600_000
    bars = 24 * 30 * 6  # six months of hourly bars, so a one-month tail still leaves a training set
    base = 1_700_000_000_000 // step * step - bars * step
    rng = np.random.default_rng(11)
    for symbol in SYMBOLS:
        close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.004, bars)))
        opens = [base + i * step for i in range(bars)]
        store.append(
            symbol,
            "1h",
            pd.DataFrame(
                {
                    "open_time": opens,
                    "open": close,
                    "high": close * 1.001,
                    "low": close * 0.999,
                    "close": close,
                    "volume": np.full(bars, 1000.0),
                    "close_time": [o + step - 1 for o in opens],
                }
            ),
        )
    out = tmp_path / "reports"
    args = [
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
        '{"horizons": [5, 20, 50], "vol_window": 100}',
        "--grid",
        "{}",
        "--folds",
        "3",
        "--min-train",
        "1000",
        "--purge",
        "5",
        "--cpcv-groups",
        "4",
        "--min-history",
        "0",
    ]
    runner = CliRunner()
    assert runner.invoke(main, args).exit_code == 0
    full = json.loads(sorted(out.glob("tsmom-validation-*.json"))[-1].read_text())
    assert full["holdout"] is None, "no reservation unless it is asked for"

    result = runner.invoke(main, [*args, "--holdout-months", "1"])
    assert result.exit_code == 0, result.output
    report_path = sorted(out.glob("tsmom-validation-*.json"))[-1]
    held = json.loads(report_path.read_text())
    assert held["holdout"]["months"] == 1 and held["holdout"]["bars_reserved"] > 600
    assert held["range"]["bars"] < full["range"]["bars"], "the reserved tail is genuinely absent from the run"
    assert pd.Timestamp(held["range"]["end"]) <= pd.Timestamp(held["holdout"]["start"])
    assert "Holdout" in report_path.with_suffix(".md").read_text()

    # asking for more than the data can spare is refused rather than silently ignored
    refused = runner.invoke(main, [*args, "--holdout-months", "600"])
    assert refused.exit_code != 0 and "leaves no training data" in str(refused.output) + str(refused.exception)


def test_cost_flag_and_grid_table(tmp_path: Path, august_dir: Path) -> None:
    """KILL-013 asks for a >40% cost share to be flagged; the plan asks the parameter grid to be visible."""
    from beidou_cli.research_cmd import _cost_flag, _grid_table

    assert _cost_flag(0.39) == "" and _cost_flag(None) == ""
    flagged = _cost_flag(0.55)
    assert "55%" in flagged and "KILL-013" in flagged

    params = {"a": {"window": 24, "scale": 0.05}, "b": {"window": 48, "scale": 0.05}}
    rows = _grid_table(params, {"a": 1.2, "b": 0.4})
    assert rows[0].startswith("window=24") and "sharpe=1.20" in rows[0], rows
    assert rows[1].startswith("window=48"), "ranked by Sharpe, best first"
    assert "scale" not in rows[0], "a parameter that does not vary is noise in the table"
    assert _grid_table({"only": {"window": 24}}, {"only": None}) == ["single configuration: sharpe=n/a"]

    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    out = tmp_path / "reports"
    result = CliRunner().invoke(
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
            '{"vol_window": [100, 200]}',
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
    markdown = sorted(out.glob("tsmom-validation-*.md"))[-1].read_text()
    assert "Grid (full-sample Sharpe per configuration)" in markdown
    assert "vol_window=100" in markdown and "vol_window=200" in markdown
    assert "parameter_neighbourhood" in markdown


def _mine(root: Path, out: Path, *extra: str) -> tuple[int, str, dict]:
    """Run `research mine` on the August fixtures and read the report back.

    The funding flags are NOT fixed here: F-1's whole subject is what happens when they disagree with the
    panel, so a helper that pinned them would make that case unreachable from the tests.
    """
    result = CliRunner().invoke(
        main,
        [
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
            *extra,
        ],
    )
    reports = sorted(out.glob("mine-shortlist-*.json"))
    payload = json.loads(reports[-1].read_text()) if reports else {}
    return result.exit_code, result.output, payload


def test_mine_records_the_parameters_of_its_own_run(tmp_path: Path, august_dir: Path) -> None:
    """DL-P17-04: the report rebuilds its own command line, which P14's could not (D-024 applied to mine)."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    out = tmp_path / "reports"
    code, output, payload = _mine(root, out, "--no-funding", "--no-include-funding")
    assert code == 0, output

    run = payload["run"]
    assert run["funding"] is False
    assert run["include_funding"] is False
    assert run["execution"] == "open_to_close"
    assert run["max_complexity"] == 10
    assert run["max_lookback"] == 200
    assert run["min_history_bars"] == 24  # the resolved value, not the raw option
    assert run["baseline"] is None
    assert run["root"] == str(root)
    # The two dataclasses that decide every number in the table.
    assert run["costs"]["use_funding"] is False
    assert "vol_target" in run["portfolio"]

    # Every enumerated candidate is accounted for exactly once, which is what makes declared_trials honest.
    outcomes = payload["outcomes"]
    assert outcomes["scored"] + outcomes["errored"] + outcomes["never_traded"] == len(payload["candidates"])
    assert outcomes["scored"] > 0  # or the identity above holds vacuously
    # The achievable form of pre-registered rule 2: every counted expression lands in exactly one bucket.
    # `scored == evaluated` as frozen cannot hold, because `evaluated` fires before the caps drop anything.
    assert outcomes["accounted"] == outcomes["evaluated"] == payload["evaluated"]
    assert outcomes["dropped_by_caps"] > 0  # --max-lookback 200 drops most of them, so this is not vacuous
    assert run["ranked_by"] == "sharpe"  # no baseline given
    assert payload["declared_trials"] == payload["evaluated"]
    assert payload["baseline"] is None
    # Stamped: two runs differing only in their flags must not overwrite each other's evidence.
    assert sorted(out.glob("mine-shortlist-*.json"))[-1].name != "mine-shortlist.json"


def test_mine_compares_every_candidate_against_a_named_baseline(tmp_path: Path, august_dir: Path) -> None:
    """DL-P17-05: the question is a second uncorrelated book, so the marginal is the quantity to rank on.

    The baseline here is a mined id rather than ``tsmom``, for two reasons: this fixture is 720 bars and
    tsmom's registry horizons reach 720, so it produces no decisions at all on it; and a mined baseline
    also exercises the ``_resolve_mined`` call, without which ``_entry`` raises a bare ``KeyError``.
    """
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    out = tmp_path / "reports"
    # A price-only tree on purpose: this fixture carries OHLCV alone, so quote_volume and
    # taker_buy_quote arrive as all-NaN frames and any flow candidate scores nothing.
    mock_candidate = Candidate.of(Squash(Ratio(Ret(24), Vol(48)), 1.0))
    baseline_id, mock_hash, mock_expr = f"mined_{mock_candidate.hash}", mock_candidate.hash, str(mock_candidate.expr)
    code, output, payload = _mine(root, out, "--no-funding", "--no-include-funding", "--baseline", baseline_id)
    assert code == 0, output

    assert payload["run"]["baseline"] == baseline_id
    assert payload["baseline"]["strategy"] == baseline_id
    # KILL-P17-06's mitigation: naming the strategy is not enough, because the registry moves and a
    # marginal measured against one configuration is a different number from the same strategy under
    # another, with nothing in the artefact to separate them.
    assert payload["baseline"]["params"] == {"entry_threshold": 0.2, "expression": mock_expr, "hash": mock_hash}
    assert payload["baseline"]["sharpe"] is not None
    assert payload["baseline"]["max_drawdown"] is not None
    assert "NOT the risk-budgeted" in payload["baseline"]["marginal"]

    scored = [row for row in payload["candidates"] if row.get("sharpe") is not None]
    assert scored
    for row in scored:
        assert "baseline_correlation" in row
        assert "baseline_marginal_sharpe" in row
        assert row["baseline_bars"] > 0
    # The error rows must stay clean: `scored` filters on sharpe, so a baseline key there would be a lie.
    for row in payload["candidates"]:
        if "error" in row:
            assert "baseline_correlation" not in row
    assert "| marginal | corr |" in sorted(out.glob("mine-shortlist-*.md"))[-1].read_text()

    # Pre-registered rule 6: with a baseline the shortlist ranks on the MARGINAL, not the full-sample
    # Sharpe.  Ranking on the absolute number is what produces a false "the space is empty" verdict - the
    # best absolute candidate is usually the one most correlated with the book already running.
    assert payload["run"]["ranked_by"] == "baseline_marginal_sharpe"
    by_marginal = [row["hash"] for row in sorted(scored, key=lambda row: -row["baseline_marginal_sharpe"])]
    by_sharpe = [row["hash"] for row in sorted(scored, key=lambda row: -row["sharpe"])]
    printed = [
        line.split("|")[1].strip()
        for line in sorted(out.glob("mine-shortlist-*.md"))[-1].read_text().splitlines()[2:]
        if line.startswith("|")
    ]
    assert printed and printed == by_marginal[: len(printed)]
    assert printed != by_sharpe[: len(printed)], (
        "this fixture no longer separates the two rankings, so the assertion above proves nothing"
    )


def test_mine_narrows_the_space_instead_of_searching_a_family_the_panel_cannot_answer(
    tmp_path: Path, august_dir: Path, fixtures_dir: Path
) -> None:
    """F-1: the carry family is neither searched nor charged when there is nothing to search.

    The flags here are the ones an operator gets by DEFAULT: --funding and --include-funding are both on,
    and the August store has no funding archive.  Three weaker guards would all pass this and let the
    whole family run on constants:

      * checking the CLI flag - it says funding was requested, not that any arrived;
      * checking ``panel.funding is not None`` - ``_load`` returns an all-zero frame, not None;
      * refusing outright - which the contract explicitly did not ask for, and which would make `mine`
        unrunnable on every OHLCV-only panel in this suite.

    What the guard must do instead is narrow, and say in the artefact that it narrowed.  Without that,
    the report records ``include_funding: true`` over 42 expressions that read nothing but zeros while
    still being charged to ``--prior-trials`` - KILL-027 standing inside the guard written to stop it.
    """
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    out = tmp_path / "reports"
    code, output, payload = _mine(root, out, "--funding")
    assert code == 0, output
    assert "narrowing the search space" in output

    run = payload["run"]
    assert run["include_funding"] is False  # what was searched
    assert run["include_funding_requested"] is True  # what was asked for
    assert run["funding_symbols"] == 0  # the evidence for the difference

    # Neither searched nor charged: the trial count is the pre-carry space, pinned to the recorded
    # fixture rather than to a literal, so widening the miner cannot quietly widen this assertion.
    baseline = json.loads((fixtures_dir / "mining_baseline_hashes.json").read_text(encoding="utf-8"))
    assert payload["evaluated"] == baseline["evaluated"]
    assert payload["declared_trials"] == baseline["evaluated"]
    assert not any("funding(" in row["expression"] for row in payload["candidates"])


def _store_with_balanced_flow(august_dir: Path, root: Path) -> None:
    """The August bars plus a quote volume whose taker flow is exactly balanced.

    ``takerbuy(w)`` is the taker-buy SHARE minus 0.5, so a store where buys are exactly half of volume
    makes every flow candidate score a constant zero: below ``entry_threshold``, so the weights are all
    zero, the net is all zero, and ``sharpe`` returns None.  That is the "enumerated but never traded"
    state - a candidate charged to ``declared_trials`` that leaves no row in the table and raises no
    error - and it is the only way to exercise it deterministically.
    """
    store = KlineStore(root)
    for symbol in SYMBOLS:
        frame = pd.read_parquet(august_dir / symbol / "1h.parquet")
        frame["close_time"] = frame["open_time"] + 3_600_000 - 1
        frame["quote_volume"] = frame["volume"] * frame["close"]
        frame["taker_buy_quote"] = frame["quote_volume"] * 0.5
        store.append(symbol, "1h", frame)


def test_mine_counts_the_candidates_that_enumerate_but_never_trade(tmp_path: Path, august_dir: Path) -> None:
    """The third bucket, exercised non-zero - the accounting identity is vacuous while it stays at 0.

    Before the `outcomes` block these rows were in `candidates` with a null Sharpe and in no count at
    all: `scored` filtered them out of the table and the only stdout line counted evaluation errors, so
    a family that enumerated and never traded was discoverable only by re-deriving it from the array,
    while every one of its members was billed to `--prior-trials`.
    """
    root = tmp_path / "data"
    _store_with_balanced_flow(august_dir, root)
    out = tmp_path / "reports"
    code, output, payload = _mine(root, out, "--no-funding", "--no-include-funding")
    assert code == 0, output

    outcomes = payload["outcomes"]
    assert outcomes["never_traded"] > 0
    assert outcomes["scored"] + outcomes["errored"] + outcomes["never_traded"] == len(payload["candidates"])
    assert outcomes["scored"] > 0
    assert "enumerated but never traded" in output

    # They are exactly the rows with neither a Sharpe nor an error, and they are billed all the same.
    silent = [row for row in payload["candidates"] if "error" not in row and row.get("sharpe") is None]
    assert len(silent) == outcomes["never_traded"]
    assert all("takerbuy(" in row["expression"] for row in silent)
    assert payload["declared_trials"] == payload["evaluated"]


def test_mine_refuses_a_baseline_that_consumes_funding_the_panel_does_not_have(
    tmp_path: Path, august_dir: Path
) -> None:
    """D-023 holds for `targets` but not for `evaluate`, so the research path needs its own refusal.

    tsmom's registry params carry `crowding_window: 72`, so `needs_funding` is True.  Without this the
    baseline book would be computed with the modifier silently inert - E-040 / KILL-027 - and every
    candidate's marginal Sharpe would be measured against a book nobody validated.
    """
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    out = tmp_path / "reports"
    code, output, _ = _mine(root, out, "--funding", "--baseline", "tsmom")
    assert code == 1
    assert "consumes funding under its registry params" in output


def test_a_mined_carry_id_still_resolves_to_a_signal(tmp_path: Path, august_dir: Path) -> None:
    """`_resolve_mined` re-derives an id from a bare `enumerate_candidates()`, carry hashes included.

    It resolves only because `include_funding` defaults True in the library.  Flip that default and every
    carry hash becomes one that `research correlate` reports as gone - silently, because the docstring's
    promise ("a hash that no longer enumerates is reported as gone") reads identically either way.

    Using such an id as a `--baseline` is a separate matter and is refused on a settlement-free panel by
    the test above: a mined carry tree consumes funding exactly as tsmom's modifier does.
    """
    mined_id = f"mined_{Candidate.of(Squash(Ratio(Funding(24), Vol(48)), 1.0)).hash}"
    _resolve_mined(mined_id)  # raises ClickException if the hash no longer enumerates
    assert get_signal(mined_id).needs_funding({}) is True


def test_mine_refuses_to_write_a_report_whose_arithmetic_does_not_close(
    tmp_path: Path, august_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accounting identity is enforced, not merely reported.

    A defensive check can only be exercised by breaking what it checks, so the enumerator is patched to
    over-report `evaluated` by one.  Without this the `raise` could be deleted and nothing would notice
    until a real run silently produced an artefact that bills more trials than it accounts for - and
    `--prior-trials` is the number the whole promotion gate is scaled by.
    """
    from beidou_cli import research_cmd

    real = research_cmd.enumerate_candidates

    def inflated(**kwargs: object) -> object:
        result = real(**kwargs)  # type: ignore[arg-type]
        return dataclasses.replace(result, evaluated=result.evaluated + 1)

    monkeypatch.setattr(research_cmd, "enumerate_candidates", inflated)
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    code, output, _ = _mine(root, tmp_path / "reports", "--no-funding", "--no-include-funding")
    assert code == 1
    assert "does not account for itself" in output


def test_every_command_can_address_a_mined_candidate(
    tmp_path: Path, august_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_entry` is the one chokepoint every strategy id passes through, so resolution belongs there.

    It used to be wired into `correlate` alone, so `research validate --strategy mined_<hash>` raised a
    bare KeyError - the wall an operator hits the moment the shortlist hands them something worth
    validating, which is exactly what a shortlist is for.

    The registry entry is removed first, and that is not ceremony: `register` mutates a process-global
    dict, every `research mine` in this file registers all 267 candidates into it, and without this the
    test passes on another test's side effect.  Measured - it did, until the deletion was added.
    """
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    mined_id = f"mined_{Candidate.of(Squash(Ratio(Ret(24), Vol(48)), 1.0)).hash}"
    monkeypatch.delitem(SIGNALS, mined_id, raising=False)
    result = CliRunner().invoke(
        main,
        [
            "research",
            "backtest",
            "--strategy",
            mined_id,
            "--root",
            str(root),
            "--symbols",
            ",".join(SYMBOLS),
            "--out",
            str(tmp_path / "reports"),
            "--no-funding",
            "--min-history",
            "24",
        ],
    )
    assert result.exit_code == 0, result.output
    assert sorted((tmp_path / "reports").glob(f"{mined_id}-backtest-*.json"))
