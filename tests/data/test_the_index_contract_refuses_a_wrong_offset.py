"""#29: the index-price contract, and the four ways it has to say no.

Every fixture here reproduces a shape MEASURED against the venue on 2026-09-09 and none of them reach
the network - the numbers are in `beidou_data.index_price`'s docstring with the day they were taken.
The tests that carry the weight are the ones that must FAIL: a declaration wrong by one bucket either
way, a sample of columns that cannot tell offsets apart, and a column asking to reach live without a
verification.  A contract only ever exercised on the happy path is decoration.

What this file does NOT do is check that the venue still behaves as measured; that is
`scratchpad/verify_live.py`'s job and it needs the network.  The split is deliberate: the rule belongs
in a test that always runs, the observation belongs where a 503 cannot turn it red.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import numpy as np
import pandas as pd
import pytest

from beidou_data.alignment import (
    FAIL,
    PASS,
    UNVERIFIABLE,
    Stamp,
    UndeclaredColumn,
    Verification,
    admits_live_signal,
    contract_for,
    verify_stamp_offset,
)
from beidou_data.binance_public import drop_unclosed
from beidou_data.index_price import (
    INDEX_DEGENERATE_FIELDS,
    INDEX_PRICE,
    INDEX_VALUE_COLUMNS,
    IndexPriceClient,
    align_index_to_perp_bars,
    index_archive_path,
    index_columns_for_live,
    index_contract,
    parse_index_archive_csv,
    parse_index_rest_rows,
    verify_index_contract,
)

HOUR_MS = 3_600_000
START = pd.Timestamp("2026-09-05 00:00", tz="UTC").value // 1_000_000
ARCHIVE_HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"
)


def _prices(count: int, seed: int = 7) -> list[float]:
    """A random walk.  Values that MOVE are the whole precondition for telling offsets apart."""
    rng = np.random.default_rng(seed)
    return [float(x) for x in 79_000.0 + np.cumsum(rng.normal(0.0, 60.0, count))]


def _rows(count: int, *, start: int = START, prices: list[float] | None = None) -> list[list[object]]:
    """Venue-shaped kline rows: 12 fields, zeros in the four volume slots, `count` = 3600 (measured)."""
    values = prices if prices is not None else _prices(count)
    return [
        [
            start + i * HOUR_MS,
            f"{values[i]:.8f}",
            f"{values[i] + 40:.8f}",
            f"{values[i] - 40:.8f}",
            f"{values[i] + 5:.8f}",
            "0",
            start + (i + 1) * HOUR_MS - 1,
            "0",
            3600,
            "0",
            "0",
            "0",
        ]
        for i in range(count)
    ]


def _csv(rows: list[list[object]], *, header: bool) -> str:
    body = "\n".join(",".join(str(field) for field in row) for row in rows)
    return f"{ARCHIVE_HEADER}\n{body}\n" if header else f"{body}\n"


def _pair(count: int = 24) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The archive and the REST page for one day, as the two independent renderings they are."""
    rows = _rows(count)
    return parse_index_archive_csv(_csv(rows, header=True)), parse_index_rest_rows(rows)


# --------------------------------------------------------------------------------------------------
# The declared offset, and the two it must refute.


def test_the_measured_offset_of_zero_passes_and_names_the_columns_it_compared() -> None:
    archive, rest = _pair()
    verification = verify_index_contract(archive, rest)
    assert verification.verdict == PASS, verification.reason
    assert verification.matched == verification.compared == 24
    assert verification.compared_columns == INDEX_VALUE_COLUMNS
    assert verification.uncompared_columns == ()
    # The evidence is the pair, not the first number: both rivals refuted on the same sample.
    assert {rival.rest_offset_ms for rival in verification.rivals} == {-HOUR_MS, HOUR_MS}
    assert all(rival.matched == 0 for rival in verification.rivals), verification.rivals


@pytest.mark.parametrize("wrong_offset_ms", [-HOUR_MS, HOUR_MS])
def test_a_declaration_wrong_by_one_bucket_either_way_fails(wrong_offset_ms: int) -> None:
    """The point of the whole module.  An off-by-one here is a full bar of look-ahead in one direction
    and a wasted bar in the other, and neither raises anything on its own."""
    archive, rest = _pair()
    wrong = replace(index_contract("1h"), rest=Stamp("open_time", wrong_offset_ms, "WRONG"))
    verification = verify_stamp_offset(wrong, archive, rest, value_columns=INDEX_VALUE_COLUMNS)
    assert verification.verdict == FAIL
    assert verification.matched == 0


def test_the_zero_volume_columns_cannot_carry_the_evidence_and_the_check_says_so() -> None:
    """Note 2, made executable.

    An index has no trades, so the archive writes four literal-zero columns.  Zero equals zero at every
    offset, so a verification resting on them refutes nothing - and the failure mode is not a wrong
    number but a confident one.  `verify_stamp_offset` answers UNVERIFIABLE; it must never answer PASS.
    """
    rows = _rows(24)
    archive = pd.DataFrame({"open_time": [row[0] for row in rows], "volume": 0.0, "quote_volume": 0.0})
    rest = archive.copy()
    verification = verify_stamp_offset(index_contract("1h"), archive, rest, value_columns=("volume", "quote_volume"))
    assert verification.verdict == UNVERIFIABLE
    assert "do not move enough" in verification.reason
    # And this is why they are not carried at all: the parser drops them before anyone can be tempted.
    parsed = parse_index_archive_csv(_csv(rows, header=True))
    assert not set(INDEX_DEGENERATE_FIELDS) & set(parsed.columns)


# --------------------------------------------------------------------------------------------------
# RISK-G3's gate.  `index_columns_for_live` is the first production caller of `admits_live_signal`.


def test_the_declared_columns_are_registered_under_the_index_contract() -> None:
    for column in INDEX_VALUE_COLUMNS:
        assert contract_for(column) is INDEX_PRICE
    # Namespaced on purpose: `CONTRACTS` is one flat dict keyed by bare column name, so an index feed
    # registering "close" would hand its contract to the perpetual's own close.
    with pytest.raises(UndeclaredColumn):
        contract_for("close")


def test_a_column_reaches_live_only_with_a_passing_verification_that_compared_it() -> None:
    archive, rest = _pair()
    admitted, refused = index_columns_for_live(verify_index_contract(archive, rest))
    assert admitted == INDEX_VALUE_COLUMNS
    assert refused == {}


@pytest.mark.parametrize(
    "verification",
    [
        None,
        Verification(FAIL, "the declared offset holds on 0/23 rows", 23, 0),
        Verification(UNVERIFIABLE, "only 4 comparable overlapping rows", 4, 4),
    ],
    ids=["never-verified", "refuted", "unverifiable"],
)
def test_no_verification_and_a_failed_one_are_both_refusals(verification: Verification | None) -> None:
    """Not-yet-shown and shown-false are both "no" - the obligation is to have SHOWN the offset."""
    admitted, refused = index_columns_for_live(verification)
    assert admitted == ()
    assert set(refused) == set(INDEX_VALUE_COLUMNS)
    assert all(reason.startswith("RISK-G3") for reason in refused.values())


def test_a_verification_that_never_compared_this_column_does_not_admit_it() -> None:
    """The column-blind hole.  A PASS earned by `index_close` says nothing about `index_high`."""
    passing_but_partial = Verification(PASS, "24/24", 24, 24, (), compared_columns=("index_close",))
    admitted, refused = index_columns_for_live(passing_but_partial)
    assert admitted == ("index_close",)
    assert set(refused) == {"index_open", "index_high", "index_low"}
    assert "never compared" in refused["index_high"]


def test_an_unimported_feed_fails_closed_rather_than_open() -> None:
    """Registration happens in `index_price`, so forgetting the import can only make the gate STRICTER.

    Stated as a test because the reverse arrangement is the tempting one: if `alignment` imported every
    feed, a circular import or a dropped line would leave the column DECLARED but unverified, and the
    direction of that failure is the one nobody notices.
    """
    allowed, reason = admits_live_signal("index_price_of_a_feed_nobody_declared", None)
    assert not allowed
    assert "no event-time contract" in reason


# --------------------------------------------------------------------------------------------------
# Causality and missingness.


def test_a_perp_bar_reads_the_index_bar_with_the_same_open_time_and_never_a_later_one() -> None:
    archive, _ = _pair(6)
    bars = pd.DatetimeIndex(pd.to_datetime(archive["open_time"].to_numpy(), unit="ms", utc=True))
    aligned = align_index_to_perp_bars(archive, bars)
    assert list(aligned.columns) == list(INDEX_VALUE_COLUMNS)
    # Same-bar, exactly: bar i carries index bar i, and in particular not bar i+1.
    assert np.allclose(aligned["index_close"].to_numpy(), archive["index_close"].to_numpy())
    shifted = archive["index_close"].to_numpy()[1:]
    assert not np.allclose(aligned["index_close"].to_numpy()[:-1], shifted)


def test_a_hole_stays_a_hole_and_is_never_filled_forward_or_with_zero() -> None:
    """ "缺失必须表现为缺失".  A forward fill is the helpful change that turns a gap into a fake basis."""
    archive, _ = _pair(6)
    gapped = archive.drop(index=[2, 3]).reset_index(drop=True)
    bars = pd.DatetimeIndex(pd.to_datetime(archive["open_time"].to_numpy(), unit="ms", utc=True))
    aligned = align_index_to_perp_bars(gapped, bars)
    missing = aligned["index_close"].to_numpy()[2:4]
    assert np.isnan(missing).all(), missing
    # Not zero, and not the last value before the hole.
    assert not (missing == 0).any()
    assert not np.allclose(missing, archive["index_close"].to_numpy()[1])
    assert aligned["index_close"].notna().sum() == 4


def test_a_symbol_with_no_index_at_all_yields_a_full_column_of_nan_rather_than_an_empty_frame() -> None:
    """Absence is the common case for a fresh listing; it must be a shaped NaN column, not a raise."""
    bars = pd.DatetimeIndex(pd.to_datetime([START + i * HOUR_MS for i in range(5)], unit="ms", utc=True))
    aligned = align_index_to_perp_bars(parse_index_rest_rows([]), bars)
    assert list(aligned.columns) == list(INDEX_VALUE_COLUMNS)
    assert len(aligned) == 5
    assert aligned.isna().all().all()


def test_the_bucket_still_in_progress_is_droppable_because_close_time_survives_the_parse() -> None:
    """Measured 2026-09-09: the newest REST bucket came back with `count` 2364 of 3600 - a partial
    bucket whose `close` is the index price NOW, not at the bucket's close.  The archive never contains
    one; REST always can, so `close_time` has to survive parsing for `drop_unclosed` to have a key."""
    rows = _rows(3)
    frame = parse_index_rest_rows(rows)
    assert "close_time" in frame.columns
    now = int(frame["close_time"].iloc[-1]) - 1  # the last bucket has not closed yet
    closed = drop_unclosed(frame, now)
    assert len(closed) == 2
    assert int(closed["open_time"].iloc[-1]) == START + HOUR_MS


# --------------------------------------------------------------------------------------------------
# The archive's own traps.


def test_a_headerless_archive_day_is_parsed_identically_to_a_headered_one() -> None:
    """Measured: 2021-06-19 ships no header while 2019, 2024, 2025 and 2026 do.

    `pd.read_csv`'s default would take the first BAR as the column names - not a crash, a silently
    short day with numeric column names, landing in the middle of a backfill.
    """
    rows = _rows(24)
    headered = parse_index_archive_csv(_csv(rows, header=True))
    headerless = parse_index_archive_csv(_csv(rows, header=False))
    pd.testing.assert_frame_equal(headered, headerless)
    assert len(headerless) == 24, "the first bar must not have been eaten as a header row"


def test_the_archive_and_the_rest_page_agree_field_for_field() -> None:
    """The two renderings are the same numbers; if the parsers disagreed, note 1 would be untestable."""
    archive, rest = _pair()
    pd.testing.assert_frame_equal(archive, rest)


def test_the_archive_path_keys_on_pair_and_the_daily_layout() -> None:
    assert index_archive_path("BTCUSDT", "1h", "2026-09-05") == (
        "/data/futures/um/daily/indexPriceKlines/BTCUSDT/1h/BTCUSDT-1h-2026-09-05.zip"
    )


def test_the_client_sends_pair_rather_than_symbol() -> None:
    """`?symbol=` answers 400 `-1102 Mandatory parameter 'pair' was not sent` - measured 2026-09-09."""
    sent: dict[str, object] = {}

    class Recording(IndexPriceClient):
        def __init__(self) -> None:  # no base __init__: nothing here opens a connection
            self._max_retries = 0
            self._page_limit = 1500

        def get(self, path: str, params: object = None) -> object:
            sent.update({"path": path, "params": params})
            return _rows(3)

    frame = Recording().klines("BTCUSDT", "1h", start_ms=START, limit=3)
    assert sent["path"] == "/fapi/v1/indexPriceKlines"
    assert sent["params"] == {"pair": "BTCUSDT", "interval": "1h", "limit": 3, "startTime": START}
    assert list(frame["index_close"]) == [float(row[4]) for row in _rows(3)]


def test_an_unknown_interval_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="unsupported metrics period"):
        index_contract("7m")


# --------------------------------------------------------------------------------------------------
# The command.  #29 had a contract and a verification and no way to run either; a command that nothing
# drives would be the same hole one level out, which is what happened to `governance canary` on
# 2026-09-09 - it shipped broken on its first real run and no test noticed.
# --------------------------------------------------------------------------------------------------


BARS = 30
# The newest bucket is half-finished, which is what the venue actually serves: at 09:19Z the 1h bucket
# came back with `count` 2364 of an eventual 3600 and a `close` that was the index AT THAT MOMENT.
NOW_MS = START + (BARS - 1) * HOUR_MS + HOUR_MS // 2


def _index_transport(asked: list[dict[str, str]]) -> httpx.MockTransport:
    rows = _rows(BARS)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": NOW_MS})
        params = dict(request.url.params)
        asked.append(params)
        if params["pair"] == "BADUSDT":
            # The venue's own answer for a pair it does not list; 400 is not retryable, so it lands here.
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
        after = int(params.get("startTime", 0))
        return httpx.Response(200, json=[row for row in rows if int(row[0]) >= after])  # type: ignore[arg-type]

    return httpx.MockTransport(handler)


def _run_index(root, monkeypatch, asked, symbols="BTCUSDT"):  # type: ignore[no-untyped-def]
    from click.testing import CliRunner

    import beidou_cli.data_cmd as data_cmd

    def client(url: str) -> IndexPriceClient:
        built = IndexPriceClient(url)
        built._client = httpx.Client(base_url=url, transport=_index_transport(asked))
        return built

    monkeypatch.setattr(data_cmd, "IndexPriceClient", client)
    return CliRunner().invoke(
        data_cmd.data.commands["index"], ["--root", str(root), "--symbols", symbols, "--start", "2026-09"]
    )


def test_the_command_stores_only_closed_bars_and_resumes_from_the_store(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Two claims the operator has to be able to read off one run, and neither is free.

    The bucket in progress must not land in the store - it is a price at a moment dressed as a bar, and
    `close_time` survives the parse only so `drop_unclosed` can tell the two apart.  And the second run
    must ask from the watermark rather than re-downloading the history: the store IS the resume point,
    which is what makes a daily job cheap enough to schedule.
    """
    from beidou_data.store import MetricsStore

    asked: list[dict[str, str]] = []
    first = _run_index(tmp_path, monkeypatch, asked)

    assert first.exit_code == 0, first.output
    assert f"+{BARS - 1} bars" in first.output, first.output
    store = MetricsStore(tmp_path, kind="index_klines_1h")
    assert store.last_open_time("BTCUSDT") == START + (BARS - 2) * HOUR_MS
    # The interval is part of the store's `kind`: MetricsStore keys on the symbol alone, so 1h and 4h
    # bars sharing a directory would merge into one file on a key that cannot tell them apart.
    assert (tmp_path / "index_klines_1h" / "BTCUSDT.parquet").exists()

    second = _run_index(tmp_path, monkeypatch, asked)

    assert second.exit_code == 0, second.output
    assert "no closed bar the store did not already hold" in second.output
    assert asked[0].get("startTime") == str(pd.Timestamp("2026-09-01", tz="UTC").value // 1_000_000)
    assert asked[-1]["startTime"] == str(START + (BARS - 2) * HOUR_MS + 1)
    assert store.last_open_time("BTCUSDT") == START + (BARS - 2) * HOUR_MS


def test_one_unlistable_pair_does_not_end_a_universe_wide_run(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`metrics_archive.sync_metrics` learned this on a six-hour job: the store is the watermark, so
    ending the run for one bad symbol throws away every other symbol's work.  The failure is NAMED,
    because a run that says "1 failed" and nothing else is the shape `ddcb216` had just removed.
    """
    from beidou_data.store import MetricsStore

    result = _run_index(tmp_path, monkeypatch, [], symbols="BADUSDT,BTCUSDT")

    assert result.exit_code == 0, result.output
    assert "BADUSDT: ! HTTPStatusError" in result.output
    assert MetricsStore(tmp_path, kind="index_klines_1h").symbols() == ["BTCUSDT"]


def test_the_ingest_admits_no_column_to_live_and_says_so(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Downloading a column and admitting it are different acts, and this command only does the first.

    `verify_index_contract` needs the daily archive BESIDE this REST page - two independent renderings -
    and this command fetches one, so the honest line is that four columns are stored and none may reach
    live.  Printing nothing would leave the reader to assume the ingest settled it.
    """
    result = _run_index(tmp_path, monkeypatch, [])

    assert result.exit_code == 0, result.output
    assert f"live gate: 0/{len(INDEX_VALUE_COLUMNS)} index columns may reach live" in result.output
    for column in INDEX_VALUE_COLUMNS:
        assert f"REFUSED {column}" in result.output
    assert "no verification on record" in result.output


def test_the_command_refuses_to_guess_a_universe_when_the_store_is_empty(tmp_path) -> None:
    from click.testing import CliRunner

    import beidou_cli.data_cmd as data_cmd

    result = CliRunner().invoke(data_cmd.data.commands["index"], ["--root", str(tmp_path)])

    assert result.exit_code != 0
    assert "holds no 1h klines" in result.output
