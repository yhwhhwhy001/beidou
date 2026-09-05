from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from beidou_cli import main
from beidou_data.store import FundingStore, KlineStore

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


# `crowding_window: 0` in the `--params` below is not decoration.  These runs pass `--no-funding`, and
# tsmom's registry params turn the funding-reading crowding modifier on, so before E-040 was closed they
# were quietly exercising an inert modifier.  Pinning it off says what they have always actually tested.
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
            '{"vol_window": 100, "horizons": [5, 20, 50], "crowding_window": 0}',
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
            '{"horizons": [5, 20, 50], "crowding_window": 0}',
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
            '{"horizons": [5, 20, 50], "crowding_window": 0}',
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
            '{"vol_window": 100, "horizons": [5, 20, 50], "crowding_window": 0}',
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
            '{"vol_window": 100, "horizons": [5, 20, 50], "crowding_window": 0}',
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
        '{"horizons": [5, 20, 50], "vol_window": 100, "crowding_window": 0}',
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
            '{"horizons": [5, 20, 50], "crowding_window": 0}',
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


# --- E-040 / KILL-027 on the research path ---------------------------------------------------------
# `AlphaModel.targets` refuses a funding-consuming model without funding history (D-023); the research
# path did not, so under `--no-funding` tsmom's crowding modifier ran inert and the report cited
# `crowding_window: 72` for a configuration that never ran.  The registry is passed explicitly because
# that is the premise of these tests: tsmom's *registry* params are what set `crowding_window`.
REPO = Path(__file__).resolve().parents[2]
REGISTRY = str(REPO / "config" / "alpha_registry.yaml")
# What the offline fixture (720 bars) can actually score; the registry's weekly horizons cannot.
SHORT_HORIZONS = '{"vol_window": 100, "horizons": [5, 20, 50]}'

# One invocation per research command that offers `--funding/--no-funding`, minus the exemptions below.
# `--min-history 0` is per-command: `correlate` does not take it, and `overlay --min-history` rebuilds the
# model without `books=`, so on the shipped registry it dies before it can reach any guard (reported separately).
_SHORT = ["--strategy", "tsmom", "--params", SHORT_HORIZONS, "--min-history", "0"]
FUNDING_COMMANDS: dict[str, list[str]] = {
    "backtest": _SHORT,
    "validate": _SHORT,
    "diagnose": _SHORT,
    "decompose": _SHORT,
    "correlate": ["--strategies", "tsmom"],
    "overlay": [],
    "book": ["--main", "tsmom", "--sleeve", "breakout", "--universe", "static", "--min-history", "0"],
}
# `mine` takes `--funding` but ignores `--strategy`: its candidates are the strategies, and no enumerated
# candidate reads funding on this branch.  Its baseline arm is guarded where the baseline is introduced.
EXEMPT_FROM_FUNDING_GUARD = {"list", "mine"}


def _research_args(command: str, extra: list[str], root: Path, out: Path) -> list[str]:
    common = ["--root", str(root), "--symbols", ",".join(SYMBOLS), "--registry", REGISTRY, "--out", str(out)]
    return ["research", command, *extra, *common, "--no-funding"]


def test_the_funding_command_table_covers_every_command_that_takes_the_flag() -> None:
    """A new research command with `--funding` must join the table above, not arrive silently unguarded."""
    from beidou_cli import research

    with_flag = {
        name
        for name, command in research.commands.items()
        if any("--funding" in (param.opts + param.secondary_opts) for param in command.params)
    }
    assert with_flag - EXEMPT_FROM_FUNDING_GUARD == set(FUNDING_COMMANDS)


def test_backtest_does_not_exit_zero_while_the_registry_makes_tsmom_consume_funding(
    tmp_path: Path, august_dir: Path
) -> None:
    """The sharp case: this exact invocation exited 0 and reported a Sharpe of 4.34 for an inert modifier."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    out = tmp_path / "reports"
    result = CliRunner().invoke(main, _research_args("backtest", FUNDING_COMMANDS["backtest"], root, out))
    assert result.exit_code != 0, result.output
    assert "--funding" in result.output and "tsmom" in result.output, result.output
    assert not list(out.glob("*.json")), "a refused run must not leave evidence behind"


@pytest.mark.parametrize(("command", "extra"), sorted(FUNDING_COMMANDS.items()))
def test_every_research_command_refuses_a_funding_consuming_strategy_without_funding(
    command: str, extra: list[str], tmp_path: Path, august_dir: Path
) -> None:
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    out = tmp_path / "reports"
    result = CliRunner().invoke(main, _research_args(command, extra, root, out))
    # The message, not the exit code, is what proves the guard: several of these commands already fail on
    # this short fixture for unrelated reasons, and a refusal that names `--funding` cannot be one of those.
    assert "--funding" in result.output, f"{command} did not refuse:\n{result.output}"
    assert result.exit_code != 0
    assert not list(out.glob("*.json")), f"{command} wrote evidence it should have refused"


def test_the_control_arm_still_runs_without_funding(tmp_path: Path, august_dir: Path) -> None:
    """`crowding_window: 0` reads no funding, so `--no-funding` *is* the configuration it was judged on."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    out = tmp_path / "reports"
    control = '{"vol_window": 100, "horizons": [5, 20, 50], "crowding_window": 0}'
    args = _research_args("backtest", ["--strategy", "tsmom", "--params", control, "--min-history", "0"], root, out)
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert len(list(out.glob("tsmom-backtest-*.json"))) == 1


def _funding_for(august_dir: Path, root: Path, symbols: tuple[str, ...]) -> None:
    """Settlements every 8 bars for `symbols` only; the rest of the universe gets no archive at all."""
    stamps = pd.read_parquet(august_dir / "BTCUSDT" / "1h.parquet")["open_time"].to_numpy()[::8]
    store = FundingStore(root)
    for symbol in symbols:
        store.append(
            symbol,
            pd.DataFrame(
                {"funding_time": stamps, "funding_rate": [0.0001] * len(stamps), "mark_price": [1.0] * len(stamps)}
            ),
        )


def test_a_report_records_what_the_signals_required_of_funding_and_what_the_panel_carried(
    tmp_path: Path, august_dir: Path
) -> None:
    """The residual hole the guard cannot see: `--funding` against a partial archive is inert again.

    The panel carries a funding frame, so the guard is satisfied, but two of the four symbols have no
    settlement in it - and tsmom's crowding rank reads an unobserved symbol as uncrowded rather than
    failing.  Reports record the cost model's `use_funding` and nothing about the signals' own
    requirement, so nothing on disk distinguished that from a modifier that was never configured.
    """
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    _funding_for(august_dir, root, ("BTCUSDT", "ETHUSDT"))
    out = tmp_path / "reports"
    args = [
        "research",
        "backtest",
        "--strategy",
        "tsmom",
        "--root",
        str(root),
        "--symbols",
        ",".join(SYMBOLS),
        "--registry",
        REGISTRY,
        "--out",
        str(out),
        "--funding",
        "--min-history",
        "0",
        "--params",
        SHORT_HORIZONS,
    ]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    payload = json.loads(next(out.glob("tsmom-backtest-*.json")).read_text())
    assert payload["funding"] == {
        "required_by": ["tsmom"],
        "panel": True,
        "symbols_settled": 2,
        "symbols": 4,
    }
    assert payload["costs"]["use_funding"] is True  # the cost model's flag stays what it was


def test_a_control_arm_report_says_no_signal_required_funding(tmp_path: Path, august_dir: Path) -> None:
    """`required_by: []` beside `panel: false` is the reading that used to be unavailable: absent, not inert."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    out = tmp_path / "reports"
    control = '{"vol_window": 100, "horizons": [5, 20, 50], "crowding_window": 0}'
    args = _research_args("backtest", ["--strategy", "tsmom", "--params", control, "--min-history", "0"], root, out)
    assert CliRunner().invoke(main, args).exit_code == 0
    payload = json.loads(next(out.glob("tsmom-backtest-*.json")).read_text())
    assert payload["funding"] == {"required_by": [], "panel": False, "symbols_settled": 0, "symbols": 4}


def test_research_refuses_when_the_funding_archive_holds_no_settlement_at_all(tmp_path: Path, august_dir: Path) -> None:
    """`--funding` is the DEFAULT, and against an unsynced archive it is as inert as `--no-funding`.

    `FundingStore.load` returns an empty frame per symbol, so `load_panel` builds a funding frame of all
    zeros - not `None` - and neither `panel.funding is None` guard can tell that from real data.  tsmom's
    crowding rank then reads every symbol as uncrowded and the run writes the exact report E-040 is about:
    exit 0, `params.crowding_window: 72`, and a Sharpe for a modifier that consumed nothing.
    """
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)  # klines synced, funding never pulled
    out = tmp_path / "reports"
    args = [
        "research",
        "backtest",
        "--strategy",
        "tsmom",
        "--root",
        str(root),
        "--symbols",
        ",".join(SYMBOLS),
        "--registry",
        REGISTRY,
        "--out",
        str(out),
        "--funding",
        "--min-history",
        "0",
        "--params",
        SHORT_HORIZONS,
    ]
    result = CliRunner().invoke(main, args)
    assert result.exit_code != 0, result.output
    assert "no settlement" in result.output and "tsmom" in result.output, result.output
    assert not list(out.glob("*.json")), "a refused run must not leave evidence behind"


def test_partial_funding_coverage_is_announced_rather_than_left_in_the_json(tmp_path: Path, august_dir: Path) -> None:
    """Some settlement is not zero settlement, so it runs - but the operator is told, not just the file."""
    root = tmp_path / "data"
    _store_from_fixtures(august_dir, root)
    _funding_for(august_dir, root, ("BTCUSDT", "ETHUSDT"))
    out = tmp_path / "reports"
    args = [
        "research",
        "backtest",
        "--strategy",
        "tsmom",
        "--root",
        str(root),
        "--symbols",
        ",".join(SYMBOLS),
        "--registry",
        REGISTRY,
        "--out",
        str(out),
        "--funding",
        "--min-history",
        "0",
        "--params",
        SHORT_HORIZONS,
    ]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert "2/4" in result.output and "tsmom" in result.output, result.output
