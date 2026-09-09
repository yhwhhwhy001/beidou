"""The #32 downloader and its contract check: what it refuses to infer, and what it never prints.

Every response here is a `MockTransport`; nothing in this file reaches the network and no test reads a
key from the environment.  The payload shapes are the ones measured on 2026-09-09, including the three
that are easy to get wrong from the documentation alone: ALFRED returns the whole revision history only
when both real-time bounds are widened, BLS spells a missing month "-" and an annual average "M13",
and FRED's own error path carries the API key inside the URL.
"""

from __future__ import annotations

import httpx
import pandas as pd
import pytest

from beidou_data.alignment import CONTRACTS, FAIL, PASS, UNVERIFIABLE, Stamp, contract_for
from beidou_data.macro import (
    API_KEY_ENV,
    AVAILABLE,
    BY_COLUMN,
    MACRO,
    MACRO_COLUMNS,
    MACRO_SERIES,
    MONTH_MS,
    REFERENCE_OPEN,
    REFUSED_SERIES,
    AlfredClient,
    BlsWitnessClient,
    FredApiKeyMissing,
    FredRequestFailed,
    api_key,
    month_open_ms,
    parse_bls_series,
    parse_release_ledger,
    verify_macro_contract,
)

COLUMN = "macro_nonfarm_payrolls"
SERIES = BY_COLUMN[COLUMN]
DAY_MS = 86_400_000

# Measured: the reference month, the day FRED first served the value, and the value.  Twelve months so
# the +-31-day rivals draw comparisons on the seven 31-day months (note 3) and the check is real.
MONTHLY: tuple[tuple[str, str, float], ...] = (
    ("2024-01-01", "2024-02-02", 157700.0),
    ("2024-02-01", "2024-03-08", 157808.0),
    ("2024-03-01", "2024-04-05", 158133.0),
    ("2024-04-01", "2024-05-03", 158425.0),
    ("2024-05-01", "2024-06-07", 158717.0),
    ("2024-06-01", "2024-07-05", 158923.0),
    ("2024-07-01", "2024-08-02", 159037.0),
    ("2024-08-01", "2024-09-06", 159179.0),
    ("2024-09-01", "2024-10-04", 159433.0),
    ("2024-10-01", "2024-11-01", 159445.0),
    ("2024-11-01", "2024-12-06", 159672.0),
    ("2024-12-01", "2025-01-10", 159928.0),
)


def _alfred_payload(rows: tuple[tuple[str, str, float], ...] = MONTHLY) -> dict:
    return {
        "observations": [
            {"date": date, "realtime_start": released, "realtime_end": "9999-12-31", "value": f"{value:.0f}"}
            for date, released, value in rows
        ]
    }


def _bls_payload(rows: tuple[tuple[str, str, float], ...] = MONTHLY, *, shift: int = 0) -> dict:
    data = []
    for date, _released, value in rows:
        stamp = pd.Timestamp(date) + pd.DateOffset(months=shift)
        data.append(
            {"year": str(stamp.year), "period": f"M{stamp.month:02d}", "value": f"{value:.0f}", "footnotes": [{}]}
        )
    return {"status": "REQUEST_SUCCEEDED", "Results": {"series": [{"seriesID": SERIES.bls_id, "data": data}]}}


def _transport(payloads: list[dict], *, status: int = 200) -> httpx.MockTransport:
    served = list(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error_code": status, "error_message": "boom"})
        return httpx.Response(200, json=served.pop(0) if served else {"observations": []})

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------------------------------
# The key: read from the environment, and its absence is loud
# --------------------------------------------------------------------------------------------------


def test_a_missing_key_raises_and_names_the_variable_instead_of_falling_back_to_todays_values() -> None:
    """Note 9, and the reason it is a refusal rather than a warning.

    The same endpoint answers WITHOUT a key for the current-vintage query, so a fallback would return a
    complete, plausible frame in which every value is the one written today - this module's own defect,
    shipped by this module.  The error names the variable because a silent misconfiguration is how the
    fallback gets reinvented.
    """
    with pytest.raises(FredApiKeyMissing, match=API_KEY_ENV):
        api_key({})
    with pytest.raises(FredApiKeyMissing):
        api_key({API_KEY_ENV: "   "})

    assert api_key({API_KEY_ENV: " token "}) == "token"


def test_an_upstream_error_never_carries_the_url_because_the_url_carries_the_key() -> None:
    """FRED authenticates by query parameter only; there is no header form to move it into.

    So `httpx.Response.raise_for_status` - whose message embeds the full URL - would put the key into a
    traceback, and from there into a log, a pytest report or a governance artefact.  `_get` raises its
    own error carrying the path and the status and nothing else, which is asserted both ways.
    """
    secret = "notarealkey"
    with (
        AlfredClient(secret, backoff=0.0, transport=_transport([], status=400)) as client,
        pytest.raises(FredRequestFailed) as caught,
    ):
        client.release_ledger(SERIES, start="2024-01-01", end="2024-12-01")

    message = str(caught.value)
    assert secret not in message and "api_key" not in message
    assert "/fred/series/observations" in message and "400" in message


def test_a_5xx_is_retried_and_a_4xx_is_not() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) < 3:
            return httpx.Response(503, text="throttled")
        return httpx.Response(200, json=_alfred_payload(MONTHLY[:2]))

    with AlfredClient("k", backoff=0.0, transport=httpx.MockTransport(handler)) as client:
        ledger = client.release_ledger(SERIES, start="2024-01-01", end="2024-02-01")

    assert len(seen) == 3 and len(ledger) == 2


# --------------------------------------------------------------------------------------------------
# Parsing: one row per release, and the markers that are not values
# --------------------------------------------------------------------------------------------------


def test_the_parser_keeps_one_row_per_release_and_adds_the_two_day_rounding_once() -> None:
    ledger = parse_release_ledger(_alfred_payload(MONTHLY[:3]), COLUMN)

    assert list(ledger.columns) == [REFERENCE_OPEN, "released_ms", AVAILABLE, COLUMN]
    assert (ledger[AVAILABLE] - ledger["released_ms"] == 2 * DAY_MS).all()
    assert ledger[REFERENCE_OPEN].iloc[0] == month_open_ms(2024, 1)
    assert "realtime_end" not in ledger.columns, "derived from the next release, never stored twice"


def test_a_release_that_published_nothing_is_dropped_rather_than_carried_as_a_missing_value() -> None:
    """FRED spells missing "."  A dropped row and a NaN row differ where it matters: a NaN row would
    reset the staleness clock, so a non-publication would look like a fresh print for another 120 days.
    """
    payload = _alfred_payload(MONTHLY[:2])
    payload["observations"].append(
        {"date": "2024-03-01", "realtime_start": "2024-04-05", "realtime_end": "9999-12-31", "value": "."}
    )

    ledger = parse_release_ledger(payload, COLUMN)

    assert len(ledger) == 2 and not ledger[COLUMN].isna().any()
    assert parse_release_ledger({"observations": []}, COLUMN).empty


def test_the_bls_witness_drops_annual_averages_and_its_own_missing_marker() -> None:
    """ "-" is BLS's missing and "M13" is its annual average; neither is a month the contract stamps."""
    payload = _bls_payload(MONTHLY[:3])
    payload["Results"]["series"][0]["data"] += [
        {"year": "2024", "period": "M13", "value": "158000", "footnotes": [{}]},
        {"year": "2024", "period": "M04", "value": "-", "footnotes": [{}]},
    ]

    frame = parse_bls_series(payload, COLUMN)

    assert len(frame) == 3
    assert list(frame.columns) == [MACRO.rest.column, COLUMN]
    assert frame[MACRO.rest.column].iloc[0] == month_open_ms(2024, 1)


def test_the_witness_client_asks_bls_for_the_series_under_its_own_id() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=_bls_payload(MONTHLY[:2]))

    with BlsWitnessClient(transport=httpx.MockTransport(handler)) as client:
        frame = client.series(SERIES, start_year=2024, end_year=2024)

    assert seen == [f"/publicAPI/v1/timeseries/data/{SERIES.bls_id}"]
    assert len(frame) == 2


# --------------------------------------------------------------------------------------------------
# The contract, held against the witness and against the declaration that must fail
# --------------------------------------------------------------------------------------------------


def test_the_declared_reference_period_offset_verifies_and_its_month_rivals_are_refuted() -> None:
    """Note 4, on the shape the live measurement had: 12 months, both rivals drawing real comparisons.

    The rivals only fire on the 31-day months, which is note 3 made visible - `compared` is smaller
    than the declared offset's 12 and is not zero, and that non-zero is the whole reason `period_ms` is
    31 days rather than 30.
    """
    ledger = parse_release_ledger(_alfred_payload(), COLUMN)

    result = verify_macro_contract(ledger, parse_bls_series(_bls_payload(), COLUMN), COLUMN)

    assert result.verdict == PASS, result.reason
    assert result.matched == result.compared == 12
    assert result.compared_columns == (COLUMN,)
    assert {rival.rest_offset_ms for rival in result.rivals} == {-MONTH_MS, MONTH_MS}
    for rival in result.rivals:
        assert 0 < rival.compared < 12 and rival.matched == 0, rival


def test_a_witness_shifted_by_one_calendar_month_fails_the_declaration() -> None:
    """The other direction: the contract is right and one side moved.  Correct data, wrong month.

    And the second half is the one worth keeping.  A one-month shift COSTS an overlap row - the twelve
    months line up on eleven - so a sample sized exactly at `min_overlap` stops being able to tell a
    wrong month from a thin sample and answers UNVERIFIABLE instead of FAIL.  That is the honest
    answer and it is also a trap: it means the sample has to be comfortably larger than the floor, not
    merely at it.  Both branches are asserted so neither can change silently.
    """
    ledger = parse_release_ledger(_alfred_payload(), COLUMN)
    shifted = parse_bls_series(_bls_payload(shift=1), COLUMN)

    result = verify_macro_contract(ledger, shifted, COLUMN, min_overlap=6)

    assert result.verdict == FAIL, result.reason
    assert result.matched == 0 and result.compared == 11

    thin = verify_macro_contract(ledger, shifted, COLUMN)
    assert thin.verdict == UNVERIFIABLE and "11 comparable overlapping rows" in thin.reason


def test_a_flat_series_cannot_verify_its_own_stamp_and_says_so() -> None:
    """A confirmation is not a check, and this is the failure UNRATE approaches in production.

    Measured on the live endpoints: the unemployment rate's rivals came back 6/17 and 6/18 rather than
    0, because a series with ~20 distinct values agrees with a one-month shift by coincidence.  Pushed
    to its limit here - a constant series - the answer is UNVERIFIABLE rather than PASS, which is why
    UNRATE is refused rather than merely noted.
    """
    flat = tuple((date, released, 158000.0) for date, released, _ in MONTHLY)
    ledger = parse_release_ledger(_alfred_payload(flat), COLUMN)

    result = verify_macro_contract(ledger, parse_bls_series(_bls_payload(flat), COLUMN), COLUMN)

    assert result.verdict == UNVERIFIABLE
    assert "cannot tell the declared offset apart" in result.reason


def test_the_contract_compares_the_current_vintage_because_that_is_what_the_witness_publishes() -> None:
    """The stamp check and the point-in-time check are different questions on the same ledger.

    BLS serves only its current vintage, so the comparison collapses the ledger to its newest value per
    reference period - deliberately the NAIVE frame - and asks about the STAMP.  Asserted because
    comparing a point-in-time frame against a current-vintage witness would report disagreements that
    are revisions rather than misalignments, and somebody would then loosen the tolerance.
    """
    revised = (*MONTHLY, ("2024-01-01", "2025-02-07", 157049.0))
    ledger = parse_release_ledger(_alfred_payload(revised), COLUMN)
    witness = _bls_payload(MONTHLY)
    witness["Results"]["series"][0]["data"][0]["value"] = "157049"  # BLS shows the revised number

    result = verify_macro_contract(ledger, parse_bls_series(witness, COLUMN), COLUMN)

    assert len(ledger) == 13, "the revision is a row in the ledger"
    assert result.verdict == PASS and result.compared == 12, "and it is not a row in the comparison"


@pytest.mark.parametrize("wrong_offset_ms", [MONTH_MS, -MONTH_MS])
def test_a_declaration_wrong_by_a_month_must_fail_on_correct_data(wrong_offset_ms: int) -> None:
    """The declaration is what an author writes by hand and what nothing else contradicts.

    `min_overlap` is dropped to three here, and the number it is dropped FROM is the finding.  A
    31-day offset lands on a real month open only for the seven 31-day months (note 3), so a wrong
    declaration is judged on about half the sample - six of twelve - and at the default floor of
    twelve it would come back UNVERIFIABLE.  Refusing to verify is the safe answer, but this test is
    about the declaration being REFUTED, so the sample it is refuted on is stated rather than padded.
    """
    from dataclasses import replace

    from beidou_data.alignment import verify_stamp_offset
    from beidou_data.macro import latest_vintage_frame

    wrong = replace(MACRO, rest=Stamp(MACRO.rest.column, wrong_offset_ms, "one month out"))
    ledger = parse_release_ledger(_alfred_payload(), COLUMN)

    result = verify_stamp_offset(
        wrong,
        latest_vintage_frame(ledger, COLUMN),
        parse_bls_series(_bls_payload(), COLUMN),
        value_columns=[COLUMN],
        min_overlap=3,
    )

    assert result.verdict == FAIL, result.reason
    assert result.matched == 0 and 3 <= result.compared <= 7


# --------------------------------------------------------------------------------------------------
# The pre-registration, held to the code
# --------------------------------------------------------------------------------------------------


def test_every_pre_registered_series_carries_its_publisher_pair_its_release_and_its_reason() -> None:
    """One family, chosen before it was run, and every field of the choice is asserted to be present.

    `bls_id` is required rather than optional: a series with no independent publisher cannot refute the
    reference-period stamp, so it could never be verified and could never reach live.  A field allowed
    to be empty is an invitation to add one anyway and find the refusal much later.
    """
    assert len(MACRO_SERIES) == 3
    assert {series.release for series in MACRO_SERIES} == {"Employment Situation", "Consumer Price Index"}
    for series in MACRO_SERIES:
        assert series.column.startswith("macro_"), "the flat CONTRACTS registry is global; names must be too"
        assert series.fred_id and series.bls_id and len(series.why) > 40
    assert set(REFUSED_SERIES) >= {"UNRATE", "INDPRO", "WALCL"}
    assert all(len(reason) > 40 for reason in REFUSED_SERIES.values())
    assert not set(REFUSED_SERIES) & {series.fred_id for series in MACRO_SERIES}


def test_the_macro_columns_are_registered_in_alignments_table_under_the_macro_contract() -> None:
    assert all(contract_for(column) is MACRO for column in MACRO_COLUMNS)
    assert set(MACRO_COLUMNS) <= set(CONTRACTS)
    assert MACRO.stamp_offset_ms == 0
    assert MACRO.rival_rest_offsets() == (-MONTH_MS, MONTH_MS)
    assert MACRO.period_ms == 31 * DAY_MS, "the longest month, so a rival lands on a real month open"
