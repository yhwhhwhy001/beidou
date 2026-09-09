"""The #31 downloader: what it asks for, what it refuses to infer, and what it says when it fails.

Every response here is a `MockTransport`; nothing in this file reaches the network.  The payload shapes
are the ones measured on 2026-09-09 - including the two that are easy to get wrong from the docs alone:
the catalog marks free assets with a `community` flag rather than by listing only free ones, and the
timeseries pages with a `next_page_token` rather than by a countable page size.
"""

from __future__ import annotations

import httpx
import pandas as pd
import pytest

from beidou_data.onchain import (
    ADMITTED_METRICS,
    STATUS_SUFFIX,
    WITNESS_COLUMN,
    AssetMapping,
    CommunityClient,
    map_to_asset,
    map_universe,
    onchain_candidates,
    parse_timeseries,
    store_kind,
    sync_onchain,
)
from beidou_data.store import MetricsStore

# Measured: Coin Metrics publishes no `avax`, three Avalanche chains, and an `_eth` deployment for the
# ERC-20 that 1000SHIBUSDT is quoted in thousands of.
COVERED = {"btc", "eth", "ada", "shib_eth", "avaxc", "avaxp", "avaxx", "link", "doge"}


def _transport(pages: list[dict], *, status: int = 200) -> httpx.MockTransport:
    served = list(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": {"type": "boom"}})
        return httpx.Response(200, json=served.pop(0) if served else {"data": []})

    return httpx.MockTransport(handler)


def _row(day: str, tx: str, *, asset: str = "btc", status_time: str | None = None) -> dict:
    row = {"asset": asset, "time": f"{day}T00:00:00.000000000Z", "TxCnt": tx, "AdrActCnt": "500000"}
    if status_time is not None:
        row["TxCnt-status"] = "flash"
        row["TxCnt-status-time"] = status_time
    return row


# --------------------------------------------------------------------------------------------------
# The mapping: proposed from the name, confirmed against the catalog
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("perp", "expected"),
    [
        ("BTCUSDT", "btc"),
        ("ETHUSDT", "eth"),
        ("1000SHIBUSDT", "shib_eth"),  # the unit multiple is stripped and the ERC-20 suffix tried
        ("LINKUSDT", "link"),
        ("AVAXUSDT", None),  # three chains, no single asset - an absence, not a mapping bug
        ("SOLUSDT", None),  # no community asset at all
        ("BTCUSDC", None),  # not a USDT perpetual, so nothing is proposed
    ],
)
def test_a_perpetual_maps_to_the_asset_the_catalog_actually_lists(perp: str, expected: str | None) -> None:
    assert map_to_asset(perp, COVERED).asset == expected


def test_identity_is_proposed_before_the_digits_are_stripped() -> None:
    """`spot.py` note 2, one market over: a leading number can belong to the token's own name.

    If stripping came first, a perpetual whose base really starts with digits would be paired with a
    different asset's chain activity - and a wrong pairing here reads as a plausible number, never as
    an error, which is why the order is asserted rather than left to the catalog to sort out.
    """
    assert onchain_candidates("1000SHIBUSDT")[0] == "1000shib"
    assert map_to_asset("1000SHIBUSDT", {"1000shib", "shib_eth"}).asset == "1000shib"


def test_the_universe_records_absences_rather_than_dropping_them() -> None:
    mapped = map_universe(["BTCUSDT", "SOLUSDT"], COVERED)
    assert set(mapped) == {"BTCUSDT", "SOLUSDT"}
    assert mapped["SOLUSDT"] == AssetMapping("SOLUSDT", None) and mapped["SOLUSDT"].exists is False


# --------------------------------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------------------------------


def test_only_assets_flagged_community_are_counted_as_covered() -> None:
    """The catalog lists what Coin Metrics computes, not what it gives away.

    A paid asset appears with `community: false` and then answers the timeseries endpoint with an empty
    page, so confirming on presence instead of on the flag inflates coverage and makes every one of
    those assets look like a download failure afterwards.
    """
    catalog = {
        "data": [
            {
                "asset": "btc",
                "metrics": [{"metric": "AdrActCnt", "frequencies": [{"frequency": "1d", "community": True}]}],
            },
            {
                "asset": "paid",
                "metrics": [{"metric": "AdrActCnt", "frequencies": [{"frequency": "1d", "community": False}]}],
            },
            {
                "asset": "hourly",
                "metrics": [{"metric": "AdrActCnt", "frequencies": [{"frequency": "1h", "community": True}]}],
            },
        ]
    }
    with CommunityClient(backoff=0.0, transport=_transport([catalog])) as client:
        assert client.covered_assets() == {"btc"}


def test_the_catalog_is_paged_too_and_a_short_read_would_be_silent() -> None:
    """Found by running the shipped client against the live endpoint after the suite was green.

    The catalog paginates at 100 with no `page_size` to raise, and reading only the first page returned
    100 assets where 138 had been measured by hand.  Nothing would have raised: the 38 missing assets
    become perpetuals with "no on-chain leg", which is indistinguishable from the 479 that really have
    none.  A coverage number that can only be wrong downwards is the worst kind here, because the
    honest answer for this feed is already "mostly absent".
    """
    pages = [
        {
            "data": [
                {"asset": "btc", "metrics": [{"metric": "A", "frequencies": [{"frequency": "1d", "community": True}]}]}
            ],
            "next_page_token": "more",
        },
        {
            "data": [
                {"asset": "eth", "metrics": [{"metric": "A", "frequencies": [{"frequency": "1d", "community": True}]}]}
            ]
        },
    ]
    with CommunityClient(backoff=0.0, transport=_transport(pages)) as client:
        assert client.covered_assets() == {"btc", "eth"}


def test_every_page_is_followed_by_its_token_rather_than_by_counting_rows() -> None:
    pages = [
        {"data": [_row("2026-06-01", "600000"), _row("2026-06-02", "610000")], "next_page_token": "abc"},
        {"data": [_row("2026-06-03", "620000")]},
    ]
    with CommunityClient(backoff=0.0, transport=_transport(pages)) as client:
        frame = client.asset_metrics("btc", ["TxCnt", "AdrActCnt"], start="2026-06-01", end="2026-06-03")
    assert len(frame) == 3
    assert frame["onchain_tx_count"].tolist() == [600_000.0, 610_000.0, 620_000.0]
    assert frame["open_time"].is_monotonic_increasing


def test_a_repeated_day_across_pages_is_deduped_to_the_last_one() -> None:
    """Pages overlapping on a boundary is a server behaviour, not a caller error; the store's own
    dedupe rule (keep last) is applied here too so a frame handed anywhere else is already clean."""
    pages = [
        {"data": [_row("2026-06-01", "600000")], "next_page_token": "abc"},
        {"data": [_row("2026-06-01", "601000"), _row("2026-06-02", "610000")]},
    ]
    with CommunityClient(backoff=0.0, transport=_transport(pages)) as client:
        frame = client.asset_metrics("btc", ["TxCnt"], start="2026-06-01", end="2026-06-02")
    assert len(frame) == 2
    assert frame["onchain_tx_count"].tolist() == [601_000.0, 610_000.0]


def test_a_5xx_is_retried_and_a_4xx_is_not() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="throttled")
        return httpx.Response(200, json={"data": [_row("2026-06-01", "600000")]})

    with CommunityClient(backoff=0.0, transport=httpx.MockTransport(handler)) as client:
        assert len(client.asset_metrics("btc", ["TxCnt"], start="2026-06-01", end="2026-06-01")) == 1
    assert attempts["n"] == 3

    with (
        CommunityClient(backoff=0.0, transport=_transport([], status=400)) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        client.asset_metrics("btc", ["TxCnt"], start="2026-06-01", end="2026-06-01")


# --------------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------------


def test_the_revision_stamp_survives_parsing_and_a_missing_one_is_not_invented() -> None:
    """The measured shape: `TxCnt` carries no status, the flow metrics do.  Both must round-trip.

    Dropping the stamp is the tempting simplification - it is not a value and no signal reads it - and
    it is the only field that separates a number that was readable then from one written two years
    later.  A row without a stamp must come back NaT, never "now" and never zero.
    """
    payload = {"data": [_row("2026-06-01", "600000"), _row("2026-06-02", "610000", status_time="2028-04-09T08:42:55Z")]}
    frame = parse_timeseries(payload, metrics={"TxCnt": "onchain_tx_count"})
    stamps = frame["onchain_tx_count" + STATUS_SUFFIX]
    assert pd.isna(stamps.iloc[0])
    assert stamps.iloc[1] == pd.Timestamp("2028-04-09T08:42:55Z").value // 1_000_000


def test_a_metric_the_source_left_empty_parses_to_nan_not_zero() -> None:
    payload = {"data": [{"asset": "btc", "time": "2026-06-01T00:00:00.000000000Z", "TxCnt": None}]}
    frame = parse_timeseries(payload, metrics={"TxCnt": "onchain_tx_count"})
    assert frame["onchain_tx_count"].isna().all()


def test_the_day_open_is_the_store_key() -> None:
    frame = parse_timeseries({"data": [_row("2026-06-01", "600000")]}, metrics=ADMITTED_METRICS)
    assert frame["open_time"].iloc[0] == pd.Timestamp("2026-06-01", tz="UTC").value // 1_000_000


# --------------------------------------------------------------------------------------------------
# The sync
# --------------------------------------------------------------------------------------------------


def test_two_perpetuals_sharing_one_asset_are_downloaded_once(tmp_path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": [_row("2026-06-01", "600000", asset="eth")]})

    store = MetricsStore(tmp_path, kind=store_kind())
    mappings = {"ETHUSDT": AssetMapping("ETHUSDT", "eth"), "ETHBULLUSDT": AssetMapping("ETHBULLUSDT", "eth")}
    with CommunityClient(backoff=0.0, transport=httpx.MockTransport(handler)) as client:
        result = sync_onchain(client, store, mappings, start="2026-06-01", end="2026-06-01")

    assert len(calls) == 1, "one asset, one download - the store is keyed by asset, not by perpetual"
    assert result.stored == {"eth": 1} and result.failures == {}
    assert store.symbols() == ["eth"]


def test_a_failing_asset_is_reported_with_its_reason_and_the_others_still_land(tmp_path) -> None:
    """`ddcb216`'s lesson, applied before it can be repeated: a count is not a report.

    A run that says "1 failed" and nothing else is how the DL-D4 hole survived two rounds.  The failure
    has to name the asset AND the exception, and the other assets have to be stored anyway - the store
    is the watermark, so ending the run throws away every other asset's work for one bad symbol.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "assets=bad" in str(request.url):
            return httpx.Response(500, text="upstream")
        return httpx.Response(200, json={"data": [_row("2026-06-01", "600000", asset="btc")]})

    store = MetricsStore(tmp_path, kind=store_kind())
    mappings = {"BTCUSDT": AssetMapping("BTCUSDT", "btc"), "BADUSDT": AssetMapping("BADUSDT", "bad")}
    with CommunityClient(backoff=0.0, transport=httpx.MockTransport(handler)) as client:
        result = sync_onchain(client, store, mappings, start="2026-06-01", end="2026-06-01")

    assert result.stored == {"btc": 1}
    assert set(result.failures) == {"bad"}
    assert "HTTPStatusError" in result.failures["bad"] and "500" in result.failures["bad"]


def test_a_perpetual_with_no_asset_is_never_requested(tmp_path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    store = MetricsStore(tmp_path, kind=store_kind())
    with CommunityClient(backoff=0.0, transport=httpx.MockTransport(handler)) as client:
        result = sync_onchain(client, store, {"SOLUSDT": AssetMapping("SOLUSDT", None)}, start="a", end="b")
    assert calls == [] and result.stored == {} and result.failures == {}


def test_the_store_is_the_metrics_store_under_its_own_kind(tmp_path) -> None:
    """Reuse, not a fourth copy of append-and-dedupe.  A re-run must not duplicate a stored day."""
    store = MetricsStore(tmp_path, kind=store_kind())
    frame = parse_timeseries({"data": [_row("2026-06-01", "600000")]}, metrics=ADMITTED_METRICS)
    assert store.append("btc", frame) == 1
    assert store.append("btc", frame) == 1
    assert store.last_open_time("btc") == pd.Timestamp("2026-06-01", tz="UTC").value // 1_000_000
    assert (tmp_path / "onchain" / "btc.parquet").exists()


# --------------------------------------------------------------------------------------------------
# The command, because a command nothing drives is the defect of 2026-09-09
# --------------------------------------------------------------------------------------------------


DAYS = 20
FIRST_DAY = pd.Timestamp("2026-06-01", tz="UTC")


def _tx(i: int) -> float:
    """A count that MOVES 20% a day, which is the whole precondition for refuting the one-day rivals.

    A flat series agrees with itself at every offset and `verify_stamp_offset` answers UNVERIFIABLE for
    the honest reason that nothing was refuted; the command would then admit no column and this file
    would be testing the refusal path while believing it tested the happy one.
    """
    return round(500_000 * 1.2**i)


def _community_transport(*, status_time: str | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if "catalog" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"asset": asset, "metrics": [{"frequencies": [{"frequency": "1d", "community": True}]}]}
                        for asset in ("btc", "eth")
                    ]
                },
            )
        asset = request.url.params["assets"]
        rows = [
            _row(str((FIRST_DAY + pd.Timedelta(days=i)).date()), f"{_tx(i):.0f}", asset=asset, status_time=status_time)
            for i in range(DAYS)
        ]
        return httpx.Response(200, json={"data": rows})

    return httpx.MockTransport(handler)


def _witness_transport() -> httpx.MockTransport:
    """blockchain.info's chart: the same counts, computed independently, under its own stamp."""
    values = [{"x": int((FIRST_DAY + pd.Timedelta(days=i)).timestamp()), "y": _tx(i)} for i in range(DAYS)]
    return httpx.MockTransport(lambda request: httpx.Response(200, json={"values": values}))


def _run_onchain(root, monkeypatch, *, status_time=None, extra=None):  # type: ignore[no-untyped-def]
    """Drive the command the way the operator does - through the CLI, against two fake publishers."""
    from click.testing import CliRunner

    import beidou_cli.data_cmd as data_cmd
    from beidou_data.onchain import CommunityClient as Community
    from beidou_data.onchain import WitnessClient as Witness

    monkeypatch.setattr(
        data_cmd,
        "CommunityClient",
        lambda: Community(backoff=0.0, transport=_community_transport(status_time=status_time)),
    )
    monkeypatch.setattr(data_cmd, "WitnessClient", lambda: Witness(transport=_witness_transport()))
    return CliRunner().invoke(
        data_cmd.data.commands["onchain"],
        [
            "--root",
            str(root),
            "--symbols",
            "BTCUSDT,ETHUSDT,SOLUSDT",
            "--from",
            str(FIRST_DAY.date()),
            "--to",
            str((FIRST_DAY + pd.Timedelta(days=DAYS - 1)).date()),
            *(extra or []),
        ],
    )


def test_the_command_stores_by_asset_and_admits_exactly_the_column_the_witness_compared(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Point 6 as the operator sees it: three columns are stored, ONE may reach live.

    The tempting reading of a PASS is "the feed is verified".  The feed is not the unit - the column
    is - and the free witness publishes transactions and nothing else, so the other two are refused on
    alignment's fourth refusal.  If this command ever prints ADMITTED for all three, that reading has
    got into the code.
    """
    result = _run_onchain(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    # SOLUSDT has no community asset at all - an honest absence, reported rather than dropped.
    assert "on-chain legs: 2 assets for 2/3 perpetuals" in result.output
    assert f"btc (BTCUSDT): {DAYS} rows stored, last day 2026-06-20" in result.output
    assert "witness: PASS" in result.output
    assert f"ADMITTED {WITNESS_COLUMN}" in result.output
    for refused in ("onchain_active_addresses", "onchain_supply"):
        assert f"REFUSED  {refused}" in result.output, result.output
    assert "never compared" in result.output
    assert (tmp_path / "onchain" / "btc.parquet").exists()
    assert MetricsStore(tmp_path, kind=store_kind()).symbols() == ["btc", "eth"]


def test_a_cell_the_source_wrote_late_is_refused_by_the_command_that_just_stored_it(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The fifth refusal, driven end to end: the same PASS, and the column still does not reach live.

    Storing a column and admitting it are different acts.  A command that printed its verdict from
    anywhere but the stored cells would pass the test above and miss this one.
    """
    result = _run_onchain(tmp_path, monkeypatch, status_time="2028-09-09T00:00:00.000000000Z")

    assert result.exit_code == 0, result.output
    assert "witness: PASS" in result.output
    assert f"REFUSED  {WITNESS_COLUMN}" in result.output
    assert "written after their declared availability" in result.output


def test_the_command_admits_nothing_when_the_witness_was_skipped(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`--no-witness` is an ingest with no re-measurement, and silence there would read as consent."""
    result = _run_onchain(tmp_path, monkeypatch, extra=["--no-witness"])

    assert result.exit_code == 0, result.output
    assert "witness: skipped" in result.output
    assert "ADMITTED" not in result.output
    assert "no verification on record" in result.output


def test_the_command_refuses_to_guess_a_universe_when_the_store_is_empty(tmp_path) -> None:
    from click.testing import CliRunner

    import beidou_cli.data_cmd as data_cmd

    result = CliRunner().invoke(
        data_cmd.data.commands["onchain"], ["--root", str(tmp_path), "--from", "2026-06-01", "--to", "2026-06-02"]
    )

    assert result.exit_code != 0
    assert "holds no 1h klines" in result.output
