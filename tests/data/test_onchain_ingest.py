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
