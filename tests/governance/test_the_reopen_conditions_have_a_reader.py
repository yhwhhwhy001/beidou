"""Thirteen 「重开条件」 were written and none was read.  This is the reader, and its refusals.

2026-09-09's audit counted `REFUTED` 69 times in `RESEARCH_LOG.md` and thirteen blocks naming what
would have to become true for a closed hypothesis to be reopened.  A grep for `REFUTED` and `reopen`
over `beidou_governance/` and `governance_cmd.py` returned nothing.  So a condition that came true
would never be noticed, and the hypothesis would stay closed by neglect instead of by evidence.

The concrete cost was visible the moment the reader existed: regime (#47) was closed needing "块 1 的
数据宽度（OI / 多空比 / 基差 / 清算流）", and three of those four landed between 2026-09-09 and
2026-09-10.  Whether three of four IS that width is the operator's judgement - the point is that
nobody had been told there was a judgement to make.

**The refusal this file exists to hold.**  Nine of thirteen conditions cannot be asked of a machine.
They are `NEEDS A PERSON`, they are never MET and never NOT MET, and the summary counts them out loud.
The temptation - to give each of them some checkable proxy so the list looks complete - would turn a
judgement into a predicate that happens to be answerable, which is how a gate becomes decoration.  A
list that reported "0 MET" over nine unaskable conditions would read as an all-clear.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from click.testing import CliRunner

from beidou_cli.governance_cmd import governance
from beidou_governance.reopen import (
    LIST,
    MET,
    NEEDS_A_PERSON,
    NOT_MET,
    RESOLVED,
    UNREADABLE,
    Entry,
    evaluate,
    load,
    render,
    short_legs,
    survey,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 10, tzinfo=UTC)


def _entry(check: str, **args: object) -> Entry:
    return Entry(id="x", subject="s", ruled="2026-09-09", verdict="REFUTED", condition="c", check=check, args=args)


# --- the list itself ---------------------------------------------------------------------------------


def test_every_condition_in_the_log_is_in_the_list() -> None:
    """13 blocks were counted on 2026-09-10; P30 ruled a 14th on 2026-09-12.

    The number is a ratchet, not a fact about the world: a condition written into RESEARCH_LOG and not
    into the list is the exact gap this file exists to close, and the only way to notice it is for the
    count to be pinned.  Raise it in the commit that adds the entry, never to make a red test green.

    2026-09-17 (Q-SF2) raised it 14 -> 19, and the five split into two kinds.  Three were the gap this
    test is named for: breakout, chanlun and pairs each had a reopen condition written into RESEARCH_LOG
    (block 6 s4.5, P27's second point, P28's correction) that the 2026-09-09 audit did not count, so they
    sat closed with nobody reading their terms.  The other two are a different thing and their entries say
    so out loud: meanrev and xsmom had NO condition anywhere - their rulings say only "do not re-run" - and
    what the list now carries was PROPOSED by that day's pit diagnostics, not ruled by the operator.  Both
    are `check: operator`, so they can only ever report NEEDS A PERSON; writing an unruled condition down
    is how a judgement gets queued instead of forgotten, which is what this file is for.  What would be
    wrong is letting either of those two become machine-askable before the operator has ruled it.

    2026-09-19 raised it 20 -> 21 for `risk-g11-denominator`, and that entry widens what this file holds:
    every other one is a REFUTED hypothesis waiting to be reopened, and this one is an ACCEPTED RISK waiting
    to be re-ruled.  The shape is identical - a judgement, a condition, and no reader - and so is the failure
    it prevents: the operator chose "do nothing until the freeze lifts" over three priced alternatives on
    2026-09-19, and with no entry that choice would have expired on 2026-10-13 into nobody remembering it had
    been a choice.  `check: date_after` because the date is the only part a machine can answer; the entry's
    own `condition` says in as many words that the date is necessary and not sufficient.

    2026-09-20 raised it 21 -> 22 for `drawdown-budget-denominator`, the second entry of that widened
    kind and the sibling of the one above: same freeze, same 2026-10-13, and they have to be ruled
    together.  What it holds is a ruling made AGAINST the operator's own correction - they were right
    that the drawdown they budget is the tradable money's, and the reporting side was changed to match
    (PR #93), but re-running D-035's bootstrap says re-denominating the LADDER buys nothing: about 13pp
    of CAGR for a budget still breached in 94.5% of draws.  That is exactly the shape that expires into
    nobody remembering, because the visible outcome is "nothing changed" - and the entry is what says
    the nothing was measured.

    2026-09-28 raised it 22 -> 23 for `lsr-timing`, the whole-market long/short timing book: pre-registered
    in #204, run once on its 4 ledger rows the same morning, refuted (OOS 1.28 against a gate of 1.65; the
    part beyond the market and ten trend controls t 1.78 against 2.0).  It is the plainest case of what
    this list is for, because the pre-registration wrote the reopen condition down before any number
    existed, and it is `operator` because "new information" - the loop's own recorded ratios, another
    venue's - is a judgement about where data came from, not a column a machine can count.  `ls-leaf`,
    the selection-signal ruling this hypothesis was split out of, keeps its own entry unchanged.

    2026-09-29 raised it 23 -> 25.  `news-flow` writes down a decision that had never been made, only
    left undone: news is not a data source (the time of a story cannot be pinned, and nothing reads it).
    `net-exposure-cap-g11` defers G11 with a condition a machine CAN answer, `short_leg`, because the
    condition is a shape of the live record and not a judgement.  Its first wording, "any short weight",
    was met by flow_short's sleeve alone - two bars in, both holding -0.2% to -0.5% on BNBUSDT - so it
    reads the short leg's share of gross instead.

    The same day raised it 25 -> 26 for `xs-lowvol-g12`: pre-registered in #256, stage 0 passed (correlation
    with tsmom -0.04), stage 1 refuted on its 4 ledger rows (OOS 0.54 against a gate of 0.98).  `operator`,
    because its condition is new information, the same judgement `lsr-timing` carries.
    """
    entries = load(ROOT / LIST)
    assert len(entries) == 26, (
        f"the list holds {len(entries)}; 13 from the audit, P30's, Q-SF2's five, EXP-SL1's, "
        "the two denominators (RISK-G11's and P13's drawdown budget), lsr-timing, news-flow, G11 and xs-lowvol"
    )


def test_every_entry_quotes_its_condition_and_cites_the_log() -> None:
    for entry in load(ROOT / LIST):
        assert entry.condition.strip(), f"{entry.id} carries no condition"
        assert entry.log.strip(), f"{entry.id} does not say where it came from"
        assert entry.verdict.strip(), f"{entry.id} does not say what was ruled"


def test_most_of_them_are_admitted_to_be_unaskable() -> None:
    """If this ever drops to zero, check that judgements were not quietly turned into predicates."""
    entries = load(ROOT / LIST)
    unaskable = [entry for entry in entries if entry.check == "operator"]
    assert len(unaskable) >= 8, f"only {len(unaskable)} of {len(entries)} are admitted judgements"


# --- the checks --------------------------------------------------------------------------------------


def test_an_operator_condition_is_neither_met_nor_unmet() -> None:
    status = evaluate(_entry("operator"), {})

    assert status.state == NEEDS_A_PERSON
    assert status.actionable is False


def test_a_resolved_condition_is_reported_and_not_counted_as_outstanding() -> None:
    assert evaluate(_entry("resolved"), {}).state == RESOLVED


def test_equity_below_the_bar_is_not_met() -> None:
    assert evaluate(_entry("equity_at_least", usdt=1_000_000), {"equity": 10_870.0}).state == NOT_MET


def test_equity_at_the_bar_is_met() -> None:
    assert evaluate(_entry("equity_at_least", usdt=1_000_000), {"equity": 1_000_000.0}).state == MET


def test_absent_equity_is_unreadable_rather_than_met() -> None:
    """The direction a missing fact has to fail in, which is the whole file's shape."""
    assert evaluate(_entry("equity_at_least", usdt=1_000_000), {"equity": None}).state == UNREADABLE


def test_a_partly_present_data_width_is_not_met_and_says_what_is_missing() -> None:
    status = evaluate(
        _entry("data_columns", columns=["oi", "lsr", "basis", "liquidations"]),
        {"columns": {"oi", "lsr", "basis"}},
    )

    assert status.state == NOT_MET
    assert "liquidations" in status.why


def test_a_complete_data_width_is_met() -> None:
    status = evaluate(
        _entry("data_columns", columns=["oi", "lsr"]),
        {"columns": {"oi", "lsr", "basis"}},
    )

    assert status.state == MET


def test_a_date_in_the_future_is_not_met_and_counts_down() -> None:
    status = evaluate(_entry("date_after", date="2026-10-03T00:00:00+00:00"), {"now": NOW})

    assert status.state == NOT_MET
    assert "22.0 days away" in status.why or "days away" in status.why


def test_a_date_that_has_passed_is_met() -> None:
    status = evaluate(_entry("date_after", date="2026-09-01T00:00:00+00:00"), {"now": NOW})

    assert status.state == MET


def test_a_date_that_will_not_parse_is_unreadable_rather_than_met() -> None:
    """Absent or not a date: the direction a missing fact fails in, and never a crash."""
    for date in ("", "the first of november"):
        assert evaluate(_entry("date_after", date=date), {"now": NOW}).state == UNREADABLE


def test_an_unknown_check_is_unreadable_rather_than_quietly_skipped() -> None:
    assert evaluate(_entry("whatever_i_invented"), {}).state == UNREADABLE


# --- the summary, which is the part a reader actually looks at ---------------------------------------


def test_the_summary_says_how_many_a_machine_cannot_answer() -> None:
    """A count of MET over a list that is two-thirds unaskable would read as an all-clear."""
    statuses = survey(load(ROOT / LIST), {"equity": 10_870.0, "columns": {"oi", "lsr", "basis"}, "now": NOW})

    text = render(statuses)

    assert "NEEDS A PERSON" in text
    assert "机器答不了" in text


def test_the_real_list_reads_today_without_a_met_being_invented(tmp_path: Path) -> None:
    """Against the real file and today's real facts: nothing is MET, and regime names its gap."""
    statuses = {
        s.entry.id: s
        for s in survey(load(ROOT / LIST), {"equity": 10_870.0, "columns": {"oi", "lsr", "basis"}, "now": NOW})
    }

    assert statuses["vwap-42"].state == NOT_MET
    assert statuses["regime-47"].state == NOT_MET
    assert "liquidations" in statuses["regime-47"].why
    assert statuses["carry"].state == NEEDS_A_PERSON


def test_the_command_runs_and_reopens_nothing() -> None:
    result = CliRunner().invoke(governance, ["reopen"])

    assert result.exit_code == 0, result.output
    assert "NEEDS A PERSON" in result.output


def test_a_short_leg_on_enough_bars_is_met() -> None:
    status = evaluate(_entry("short_leg", leg=0.10, share=0.20, bars=4), {"short_legs": [0.0, 0.02, 0.05, 0.30]})
    assert status.state == MET, status.why
    assert "1/4" in status.why


def test_a_sleeve_s_sliver_of_short_is_not_a_two_sided_book() -> None:
    """flow_short alone puts a short of about 2% of gross on the book; the line is the leg's share, not its sign."""
    status = evaluate(_entry("short_leg", leg=0.10, share=0.20, bars=2), {"short_legs": [0.018, 0.018]})
    assert status.state == NOT_MET, status.why


def test_a_window_shorter_than_the_line_is_not_met_however_short_the_book() -> None:
    status = evaluate(_entry("short_leg", leg=0.10, share=0.20, bars=720), {"short_legs": [0.5] * 24})
    assert status.state == NOT_MET, status.why


def test_no_bar_since_the_construction_began_is_unreadable_rather_than_unmet() -> None:
    for facts in ({}, {"short_legs": None}, {"short_legs": []}):
        assert evaluate(_entry("short_leg", leg=0.10, share=0.20, bars=720), facts).state == UNREADABLE


def test_short_legs_counts_from_the_construction_and_keeps_the_last_row_of_a_bar() -> None:
    rows = [
        {"bar_open_ms": 1, "targets": {"A": -1.0}},  # before the construction: not counted
        {"bar_open_ms": 2, "targets": {"A": 0.20, "B": -0.05}},  # rewritten below
        {"bar_open_ms": 2, "targets": {"A": 0.20, "B": 0.0}},
        {"bar_open_ms": 3, "targets": {"A": 0.30, "B": -0.10}},
        {"bar_open_ms": 4, "targets": {}},  # flat: no leg, still a bar
        {"bar_open_ms": 5},  # no targets written: not a bar this can read
    ]
    assert short_legs(rows, None) is None
    assert short_legs(rows, 2) == [0.0, 0.25, 0.0]


# --- 2026-09-29 review: a typo is not MET, and the wiring CI never ran --------------------------------


def test_a_machine_check_missing_an_arg_is_unreadable_rather_than_met() -> None:
    """The defaults once made a typo MET: `short_leg` with no args read "needs >= 100% over >= 0 bars"."""
    legs = {"short_legs": [0.5, 0.5, 0.5]}
    for args in ({}, {"legs": 0.10, "share": 0.20, "bar": 720}):
        status = evaluate(_entry("short_leg", **args), legs)
        assert status.state == UNREADABLE and "args lack" in status.why, status.why
    assert evaluate(_entry("equity_at_least"), {"equity": 1e9}).state == UNREADABLE
    assert evaluate(_entry("data_columns"), {"columns": set()}).state == UNREADABLE


def test_the_command_reads_g11_off_the_live_record_it_is_pointed_at(tmp_path: Path) -> None:
    """`reopen` reads `cycles.jsonl` under `--state-dir`, from the running construction's first traded bar."""
    (tmp_path / "governance").mkdir()
    (tmp_path / LIST).write_text(
        "entries:\n  - id: g11\n    check: short_leg\n    args: {leg: 0.10, share: 0.50, bars: 2}\n", encoding="utf-8"
    )
    live = tmp_path / ".beidou" / "live"
    live.mkdir(parents=True)
    rows = [
        {"bar_open_ms": 1, "equity": 1.0, "construction": "old", "targets": {"A": -1.0}},  # before it: not counted
        {"bar_open_ms": 2, "equity": 1.0, "construction": "new", "targets": {"A": 0.3, "B": -0.1}},  # leg 25%
        {"bar_open_ms": 3, "equity": None, "construction": "new", "targets": {"A": -1.0}},  # failed: no bar
        {"bar_open_ms": 4, "equity": 1.0, "construction": "new", "targets": {"A": 0.3}},  # long only
    ]
    (live / "cycles.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = CliRunner().invoke(governance, ["reopen", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "1/2 bars since the construction began" in result.output and "MET 1, NOT MET 0" in result.output

    (live / "cycles.jsonl").unlink()
    result = CliRunner().invoke(governance, ["reopen", "--root", str(tmp_path)])
    assert "the live record was not read" in result.output, result.output
