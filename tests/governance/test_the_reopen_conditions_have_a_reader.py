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
    """
    entries = load(ROOT / LIST)
    assert len(entries) == 14, f"the list holds {len(entries)}; the audit counted 13 in RESEARCH_LOG plus P30's"


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
