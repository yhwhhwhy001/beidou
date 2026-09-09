"""Daily on-chain metrics (#31), and the backfill that decides which of them may reach live.

This repository holds no external API key, so the question the plan's "one alignment contract per
external API" note asks had to be answered first: is there a FREE, point-in-time, checkable on-chain
history at all?  There is one - the Coin Metrics **Community** API, no key, no registration - and
everything below was measured against it on 2026-09-09 rather than inferred from its documentation.

The sibling column #28 (token unlocks) was researched the same day and the answer was the opposite;
`docs/RESEARCH_LOG.md` carries it, and the one-line version is that the only free unlock schedule is a
single continuously-overwritten document whose already-PAST values were measured changing by 5.75x
between two dated captures.  That column does not reach live, for #19's reason.

**1. COVERAGE, and it is thin.**  138 assets carry `AdrActCnt` at 1d with `community: true`; 100 carry
`TxCnt` and `SplyCur`; 32 carry `BlkCnt`.  Against the 528 TRADING USDT perpetuals, **49 map to a
community asset - 9.3%**.  Spot's 362/528 made "missing" the common case; here it is overwhelmingly
the case, so the cross-section this column can rank is a fifth of the board at best and the mapping is
carried as data rather than derived at read time.

**2. THE NAME IS NOT A RULE**, one market over from where `spot.py` found it.  Coin Metrics splits
Avalanche into `avaxc`/`avaxp`/`avaxx` and publishes no `avax`, so AVAXUSDT has no single on-chain
asset - an honest absence, not a mapping bug.  ERC-20 deployments carry an `_eth` suffix, which is how
1000SHIBUSDT reaches `shib_eth`.  SOLUSDT has no community asset at all.  So a candidate is PROPOSED
from the perpetual's name and CONFIRMED against the catalog's own listing, exactly as `map_to_spot`
does, because a string rule would silently pair a perpetual with a different chain's numbers.

**3. THE BACKFILL IS PUBLISHED PER CELL, and that is the only reason this column is admissible.**
`FlowInExNtv`/`FlowOutExNtv` rows come back with a `<metric>-status` of `flash` and a
`<metric>-status-time`.  BTC's inflow for **2024-03-01** carries a status time of **2026-04-09**: a
value describing a day two years earlier, last written 769 days after that day ended.  ETH's inflow
for the same day carries 2024-03-02.  Same metric, same day, two assets, availability differing by
769 days - so the revision lag is a property of the CELL, not of the feed, and no single declared
offset can be right for it.  `AdrActCnt`, `TxCnt`, `SplyCur`, `BlkCnt` and `FeeTotNtv` carry no status
field at all.  Hence `REFUSED_METRICS`: the flow columns are refused on the source's own testimony,
and `revision_evidence` is the runnable form of that refusal rather than a sentence in this docstring.

**4. AVAILABILITY IS OBSERVED, THEN ROUNDED UP TO SOMETHING ARITHMETIC.**  `AssetEODCompletionTime`
reports, per asset-day, the instant Coin Metrics finished that day: over 2026-08-24..2026-09-08, BTC
26.30-28.16 h and ETH 26.62-29.78 h after the day's OPEN (n=16 each), i.e. 2.3-5.8 h after it closed.
`EventTimeContract` requires the boundary to be what must be TRUE rather than an observed latency, and
here the two disagree in the dangerous direction: "+1 day" is a whole-day boundary that the measured
completion has NEVER met, so declaring it would carry 2.3-5.8 hours of look-ahead every single day.
`available_offset_ms` is therefore **two** days - the smallest whole-day boundary the measurement has
never crossed, with 18 h of margin - and the measured completion stays evidence, not the boundary.

**5. THERE IS A SECOND, INDEPENDENT WITNESS, so the declared offset is falsifiable.**  Coin Metrics'
`TxCnt` and blockchain.info's `n-transactions` chart are two independent computations of the same
quantity for BTC.  Measured over the 355 days both cover (2025-09-09..2026-09-08): median relative
difference **0.598%**, p95 1.80%, max 2.60% at the declared offset of zero; median **10.7%** and
10.6% at one day either side.  At the 5% tolerance this module declares, that is 355/355 at the
declared offset against 97/354 and 97/355 at the rivals - a PASS with both rivals refuted.  At 2% the
declared offset itself fails (346/355), so the tolerance is a measured number and not a preference.
blockchain.info is the WITNESS only; it is never stored and never becomes a panel column.

**6. AND SO EXACTLY ONE COLUMN IS ADMITTED TODAY, NOT THREE.**  Running the code below against the live
endpoints returns PASS with `compared_columns=('onchain_tx_count',)` - the witness publishes
transactions and nothing else - so `admits_live_signal` refuses `onchain_active_addresses` and
`onchain_supply` on alignment's fourth refusal: a verification says nothing about a column it never
compared.  That is the right answer and it is worth stating loudly, because the tempting reading of
point 5 is "the feed is verified".  The FEED is not the unit; the column is.  Admitting the other two
needs a second free source that publishes them, and none was found - so they are downloaded, stored and
kept out of live, which is a different state from "not collected".

**7. MISSING IS MISSING.**  A perpetual with no on-chain leg, and a bar before its asset's first
available day, read NaN.  Not zero, and not the previous day carried forward past its availability -
`spot.py` note 7 is the same lesson with a two-year-old halted price behind it.

Run against the live endpoints on 2026-09-09 after the suite was green, which is how point 6 and the
catalog's pagination were found; `covered_assets` had been reading one page of 100 against the 138 that
had been measured by hand, and 38 assets would have joined the 479 perpetuals with no on-chain leg
without anything raising.
"""

from __future__ import annotations

import time
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np
import pandas as pd

from beidou_data.alignment import PASS, EventTimeContract, Stamp, UndeclaredColumn, Verification
from beidou_data.store import MetricsStore

COMMUNITY_BASE_URL = "https://community-api.coinmetrics.io"
WITNESS_BASE_URL = "https://api.blockchain.info"
DAY_MS = 86_400_000
ONCHAIN_KIND = "onchain"  # the `kind` a MetricsStore is opened with; see `store_kind` below
_RETRIES = 4  # 1s, 2s, 4s; the community tier throttles, so backing off works (metrics_archive's rule)

# Coin Metrics metric -> the canonical column name this repository uses.  Renamed rather than passed
# through so that a column can never be confused with the vendor's own spelling, which is the discipline
# `metrics.py` applies to `create_time`: one name per quantity, chosen here.
ADMITTED_METRICS: dict[str, str] = {
    "AdrActCnt": "onchain_active_addresses",
    "TxCnt": "onchain_tx_count",
    "SplyCur": "onchain_supply",
}

# Point 3.  These are the columns whose values the source itself marks as revisable, and they are named
# rather than omitted: a later author reaching for exchange flows should find the refusal and its
# measurement here, not an empty space that looks like nobody thought of it.
REFUSED_METRICS: dict[str, str] = {
    "FlowInExNtv": "onchain_exchange_inflow",
    "FlowOutExNtv": "onchain_exchange_outflow",
}

PANEL_COLUMNS: tuple[str, ...] = tuple(ADMITTED_METRICS.values())
STATUS_SUFFIX = "_status_time_ms"

# The witness publishes this one quantity, under the canonical name, because `verify_stamp_offset`
# compares columns present in BOTH frames by name.  Only TxCnt has an independent free witness today.
WITNESS_COLUMN = ADMITTED_METRICS["TxCnt"]

# Point 5: above the measured 2.60% worst same-day disagreement (92% of margin) and roughly four times
# below the 10.7% median one-day-off disagreement.  Not a preference - 2% fails on the declared offset
# and 10% would start admitting the rivals.
WITNESS_TOLERANCE = 0.05


class AssetNotCovered(LookupError):
    """Coin Metrics publishes no community asset for this perpetual.  An absence, not a failure."""


ONCHAIN = EventTimeContract(
    name="onchain",
    period_ms=DAY_MS,
    # `archive`/`rest` are the dataclass's names for "the source we store" and "the independent
    # witness", after the metrics pair that motivated it.  Here they are Coin Metrics and
    # blockchain.info, and both stamp a daily value with the UTC day's OPEN - which is not assumed:
    # point 5 is the measurement that separates that convention from the two one-day rivals.
    archive=Stamp("time", 0, "the UTC day OPEN (Coin Metrics)"),
    rest=Stamp("x", 0, "the UTC day OPEN (blockchain.info charts)"),
    available_offset_ms=2 * DAY_MS,
    measured=(
        "2026-09-09: Coin Metrics TxCnt vs blockchain.info n-transactions for BTC over 355 shared "
        "days - median relative difference 0.598%, p95 1.80%, max 2.60% at the declared offset of 0, "
        "against medians of 10.7% and 10.6% one day either side.  Availability from "
        "AssetEODCompletionTime: BTC 26.30-28.16 h and ETH 26.62-29.78 h after the day open over "
        "16 days, so the declared +2 day boundary has 18 h of margin and +1 day would have none."
    ),
)

# Keyed by column, like `alignment.CONTRACTS`, and deliberately a SEPARATE registry rather than an
# update of that one.  Two reasons, both worth a reader's time.  First,
# `test_every_metrics_column_that_can_reach_the_panel_is_declared` asserts
# `set(CONTRACTS) == set(VALUE_COLUMNS)` - "no gaps, no strays" - so a second feed's columns cannot be
# added there without weakening a guard that exists to catch a missing metrics contract.  Second, the
# alternative (mutating `alignment.CONTRACTS` at import time from here) makes a module-level dict
# depend on import order, and a governance gate whose answer changes with import order is worse than a
# second registry.  The cost is `admits_live_signal` below, which repeats four refusals; the test
# `test_the_two_registries_refuse_for_the_same_reasons` holds the two against each other so neither
# can move alone.  The change that would delete the copy is a `contracts` parameter on alignment's
# function, and that is an edit to a module this task was told to leave alone.
CONTRACTS: dict[str, EventTimeContract] = dict.fromkeys(PANEL_COLUMNS, ONCHAIN)


def contract_for(column: str) -> EventTimeContract:
    try:
        return CONTRACTS[column]
    except KeyError:
        raise UndeclaredColumn(
            f"{column!r} has no event-time contract; declare one in beidou_data.onchain.CONTRACTS "
            "and verify it before any signal reads it (RISK-G3)"
        ) from None


@dataclass(frozen=True)
class RevisionEvidence:
    """When the source last WROTE the cells it served, against when the contract says they were due.

    The fifth refusal, and the one no amount of stamp arithmetic can reach.  A verified offset says the
    stored value describes the day it claims to; it says nothing about whether that value existed on
    the day a backtest would have read it.  Coin Metrics answers the second question itself, per cell,
    and this is that answer folded into a verdict.

    `late` counts cells whose `-status-time` falls AFTER `contract.available_from(event)`.  One is
    enough to refuse the column: the defect is not statistical, it is that today's history is not the
    history that was readable then.
    """

    column: str
    rows: int
    stamped: int
    late: int
    worst_lag_ms: int
    worst_day_ms: int | None

    @property
    def clean(self) -> bool:
        return self.late == 0

    @property
    def reason(self) -> str:
        if self.rows == 0:
            return f"{self.column}: no rows to judge"
        if self.stamped == 0:
            return f"{self.column}: {self.rows} rows, none carries a revision stamp"
        if self.clean:
            return f"{self.column}: {self.stamped}/{self.rows} stamped, none written after its declared availability"
        day = pd.Timestamp(self.worst_day_ms, unit="ms", tz="UTC").date() if self.worst_day_ms is not None else "?"
        return (
            f"{self.column}: {self.late}/{self.stamped} cells were written after their declared "
            f"availability, worst {self.worst_lag_ms / DAY_MS:.1f} days late (day {day})"
        )


def revision_evidence(frame: pd.DataFrame, column: str, contract: EventTimeContract = ONCHAIN) -> RevisionEvidence:
    """Hold a stored column's revision stamps against the availability its contract declares."""
    stamp_column = column + STATUS_SUFFIX
    rows = len(frame)
    if rows == 0 or stamp_column not in frame.columns:
        return RevisionEvidence(column, rows, 0, 0, 0, None)
    written = pd.to_numeric(frame[stamp_column], errors="coerce")
    events = pd.to_numeric(frame["open_time"], errors="coerce")
    known = written.notna() & events.notna()
    if not bool(known.any()):
        return RevisionEvidence(column, rows, 0, 0, 0, None)
    due = events[known].astype("int64") + contract.available_offset_ms
    lag = written[known].astype("int64") - due
    late = lag > 0
    if not bool(late.any()):
        return RevisionEvidence(column, rows, int(known.sum()), 0, 0, None)
    worst = lag[late].idxmax()
    return RevisionEvidence(column, rows, int(known.sum()), int(late.sum()), int(lag[worst]), int(events[worst]))


def admits_live_signal(
    column: str, verification: Verification | None, revision: RevisionEvidence | None = None
) -> tuple[bool, str]:
    """RISK-G3 for an on-chain column: alignment's four refusals, plus the backfill one.

    The first four are `alignment.admits_live_signal`'s, over this module's registry (see `CONTRACTS`
    for why the registry is separate).  The fifth is what point 3 measured: a column can have a
    perfectly verified stamp offset and still be unusable, because the VALUE at that stamp was written
    later than the contract says it was available.  A missing `revision` argument is refused for the
    same reason a missing `verification` is - not-yet-shown and shown-false are both "no".
    """
    try:
        contract = contract_for(column)
    except UndeclaredColumn as exc:
        return False, f"RISK-G3: {exc}"
    if verification is None:
        return False, f"RISK-G3: {column} is under the {contract.name} contract but has no verification on record"
    if verification.verdict != PASS:
        return False, f"RISK-G3: {contract.name} is {verification.verdict} - {verification.reason}"
    if column not in verification.compared_columns:
        return False, f"RISK-G3: the {contract.name} verification never compared {column} itself"
    if revision is None:
        return False, f"RISK-G3: {column} has no revision evidence on record"
    if not revision.clean:
        return False, f"RISK-G3: {revision.reason}"
    return True, f"{contract.name}: {verification.reason}; {revision.reason}"


@dataclass(frozen=True)
class AssetMapping:
    """One perpetual's on-chain asset, or the recorded fact that it has none.

    Shaped after `SpotMapping` on purpose: the pairing is a venue fact that research and the live loop
    must resolve identically, so it is stored rather than re-derived (D-040).  There is no multiplier
    here - counts of addresses and transactions are not quoted in anything.
    """

    perp: str
    asset: str | None

    @property
    def exists(self) -> bool:
        return self.asset is not None


def onchain_candidates(perp: str) -> list[str]:
    """Coin Metrics assets this perpetual COULD be, most likely first.

    Identity first, then the ERC-20 deployment (`_eth`), then the same two with a leading unit multiple
    stripped - `1000SHIBUSDT` is quoted in thousands of a token Coin Metrics calls `shib_eth`, while a
    leading number can also belong to the token's own name.  The multiple itself is dropped rather than
    carried: every metric here is a count or a native supply, so the perpetual's quote unit does not
    enter.  Getting that wrong for a price column is `spot.py` note 2; here it simply does not apply,
    which is worth stating so nobody adds a multiplier by analogy.
    """
    if not perp.endswith("USDT"):
        return []
    base = perp[: -len("USDT")].lower()
    candidates = [base, base + "_eth"]
    digits = len(base) - len(base.lstrip("0123456789"))
    if 0 < digits < len(base):
        stripped = base[digits:]
        candidates += [stripped, stripped + "_eth"]
        if len(stripped) > 1 and stripped[0] in ("m", "b"):
            candidates += [stripped[1:], stripped[1:] + "_eth"]
    return candidates


def map_to_asset(perp: str, covered: Container[str]) -> AssetMapping:
    """The first candidate the catalog actually lists, or a mapping that records the absence."""
    for candidate in onchain_candidates(perp):
        if candidate in covered:
            return AssetMapping(perp=perp, asset=candidate)
    return AssetMapping(perp=perp, asset=None)


def map_universe(perps: Iterable[str], covered: Container[str]) -> dict[str, AssetMapping]:
    return {perp: map_to_asset(perp, covered) for perp in perps}


def store_kind(kind: str = ONCHAIN_KIND) -> str:
    """The `kind` to open a `MetricsStore` with.

    A function rather than a bare constant so the reason survives: this feed reuses `MetricsStore`
    unchanged - one parquet per key, append-and-dedupe on `open_time` - because its merge rule is
    already the right one and a second copy would be a second place for it to drift, which is exactly
    what `KlineStore(kind="spot_klines")` decided one feed earlier.  The key is the COIN METRICS ASSET,
    never the perpetual: one asset backing two perpetuals is stored once, and a wrong mapping cannot be
    laundered into looking like wrong data.
    """
    return kind


def _day_open_ms(stamp: str) -> int:
    return int(pd.Timestamp(stamp[:10]).tz_localize("UTC").timestamp() * 1000)


def parse_timeseries(payload: Mapping[str, Any], *, metrics: Mapping[str, str] | None = None) -> pd.DataFrame:
    """One `/timeseries/asset-metrics` page in the canonical shape, revision stamps carried alongside.

    `open_time` is the UTC day open in milliseconds, so this feed lands on the same key every other
    store here uses and `MetricsStore` needs no changes to hold it.

    The `<metric>-status-time` fields are parsed into `<column>_status_time_ms` and kept rather than
    dropped.  Dropping them is the tempting simplification - they are not values, and no signal reads
    them - and it would throw away the only evidence that distinguishes a column that was readable when
    it claims to have been from one that was written two years late (point 3).  A cell with no status
    field gets NaT, which is the source saying "final", and a NaN VALUE stays NaN: a day the source did
    not publish is absent, never zero.
    """
    columns = dict(metrics) if metrics is not None else {**ADMITTED_METRICS, **REFUSED_METRICS}
    rows = list(payload.get("data") or [])
    frame = pd.DataFrame(
        {
            "open_time": pd.Series([_day_open_ms(str(row["time"])) for row in rows], dtype="int64"),
            "asset": pd.Series([str(row["asset"]) for row in rows], dtype="object"),
        }
    )
    for metric, column in columns.items():
        frame[column] = pd.to_numeric(pd.Series([row.get(metric) for row in rows]), errors="coerce").astype(float)
        stamps = pd.Series([row.get(f"{metric}-status-time") for row in rows], dtype="object")
        parsed = pd.to_datetime(stamps, utc=True, errors="coerce", format="ISO8601")
        frame[column + STATUS_SUFFIX] = parsed.dt.as_unit("ms").astype("int64").where(parsed.notna())
    return frame.sort_values("open_time", ignore_index=True)


def witness_frame(chart: Mapping[str, Any]) -> pd.DataFrame:
    """A blockchain.info chart in the shape `verify_stamp_offset` wants for the `rest` side.

    The value lands under the canonical column name because `_agreement` compares columns present in
    BOTH frames BY NAME; under the vendor's own name it would compare nothing and the check would
    report a perfect score over an empty intersection, which is the column-blind hole `Verification`
    was extended to close one feed earlier.
    """
    values = list(chart.get("values") or [])
    return pd.DataFrame(
        {
            ONCHAIN.rest.column: pd.Series([int(point["x"]) * 1000 for point in values], dtype="int64"),
            WITNESS_COLUMN: pd.Series([float(point["y"]) for point in values], dtype=float),
        }
    )


def to_contract_frame(frame: pd.DataFrame, stamp: Stamp) -> pd.DataFrame:
    """A stored frame put back into one source's own stamp convention, for `verify_stamp_offset`.

    Thin on purpose: `alignment.restamp` already states what this costs (the arithmetic goes back in
    exactly as declared, so a restamped frame cannot re-observe the venue), and this wrapper exists
    only to drop the status columns, which are metadata about the values rather than values and would
    otherwise be compared as if they were.
    """
    keep = [c for c in frame.columns if not c.endswith(STATUS_SUFFIX) and c != "asset"]
    out = frame[keep].drop(columns=["open_time"])
    out[stamp.column] = frame["open_time"].astype("int64") + stamp.offset_ms
    return out


def align_daily_to_bars(daily: pd.DataFrame, bars: pd.DatetimeIndex, *, interval_ms: int) -> pd.DataFrame:
    """For each bar, the latest day that was AVAILABLE by the bar's own close; else NaN.

    The same searchsorted shape as `metrics.align_to_bars`, keyed on a different instant, and the
    difference is the whole point.  There a bucket became readable when it CLOSED, because the venue
    serves it immediately; here a day becomes readable when the vendor has finished computing it, which
    point 4 measured at 26-30 hours and this contract rounds up to two whole days.  Keying on the day's
    close instead would hand a bar a number that did not exist for another day and a half.

    A bar with nothing available yet gets NaN, never the nearest value - `metrics.align_to_bars`'s
    sentence, and `spot.py` note 7 is what it costs when somebody helpfully fills instead.
    """
    columns = [c for c in daily.columns if c in PANEL_COLUMNS]
    if daily.empty or not columns:
        return pd.DataFrame(index=bars, columns=list(PANEL_COLUMNS), dtype=float)
    ordered = daily.sort_values("open_time")
    available = ordered["open_time"].to_numpy(dtype="int64") + ONCHAIN.available_offset_ms
    # `as_unit("ms").view("int64")`, not a division: a pandas 2.x DatetimeIndex carries its own
    # resolution, so dividing assumes a unit where a conversion states it.  `align_to_bars` returned
    # NaN for every bar the first time that was got wrong.
    bar_closes = np.asarray(bars.as_unit("ms").view("int64")) + int(interval_ms)
    position = pd.Series(available).searchsorted(bar_closes, side="right") - 1
    frame = pd.DataFrame(index=bars, columns=columns, dtype=float)
    for column in columns:
        values = ordered[column].to_numpy(dtype=float)
        frame[column] = [values[i] if i >= 0 else float("nan") for i in position]
    # Constant width, whatever the caller happened to download.  `align_spot_to_perp_bars` ends the
    # same way and for the same reason: a symbol that is missing must occupy the SAME columns as one
    # that is present, or the absence changes the panel's shape instead of its values.
    return frame.reindex(columns=list(PANEL_COLUMNS))


class CommunityClient:
    """The Coin Metrics community tier: no key, paged, and it says which metrics are free.

    Shaped after `MetricsArchiveClient` - injectable transport, retry on 5xx only, an absence reported
    rather than raised - because those three were learned once already and a second downloader should
    not learn them again.
    """

    def __init__(
        self,
        base_url: str = COMMUNITY_BASE_URL,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        backoff: float = 1.0,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport, follow_redirects=True)
        # A seam, not a knob.  Exercising the retry with the production backoff costs 1+2+4 seconds of
        # real sleeping per test, and `suite_duration.py` names "a sleep" as exactly the step change
        # its ceiling exists to catch.  Tests pass 0; nothing else should.
        self._backoff = backoff

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CommunityClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        delay = self._backoff
        for attempt in range(_RETRIES):
            response = self._client.get(path, params=dict(params))
            if response.status_code < 500 or attempt == _RETRIES - 1:
                response.raise_for_status()
                payload = response.json()
                assert isinstance(payload, dict)
                return payload
            if delay:
                time.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")  # pragma: no cover

    def covered_assets(self, metric: str = "AdrActCnt", frequency: str = "1d") -> set[str]:
        """Assets whose `metric` is free at `frequency`.

        Filtered on the catalog's own `community` flag rather than on presence: the catalog lists what
        Coin Metrics computes, and a paid asset appears there and then answers the timeseries endpoint
        with an empty page.  Confirming against the wrong field is how a coverage number comes back
        three times too large and every missing asset looks like a download failure.

        Paged, and this one was found by running the shipped code against the live endpoint after the
        tests were green: the catalog paginates at 100 with no `page_size` and the first version read
        only the first page, so `AdrActCnt` came back as 100 assets against the 138 that were measured
        by hand.  The 38 it dropped would not have raised anything - they would simply have joined the
        479 perpetuals with "no on-chain leg", which is the failure shape this whole module is about.
        """
        params = {"metrics": metric}
        covered: set[str] = set()
        while True:
            payload = self._get("/v4/catalog-v2/asset-metrics", params)
            covered |= {
                str(row["asset"])
                for row in payload.get("data") or []
                for entry in row.get("metrics") or []
                for freq in entry.get("frequencies") or []
                if freq.get("frequency") == frequency and freq.get("community")
            }
            token = payload.get("next_page_token")
            if not token:
                return covered
            params = {**params, "next_page_token": str(token)}

    def asset_metrics(
        self, asset: str, metrics: Sequence[str], *, start: str, end: str, frequency: str = "1d"
    ) -> pd.DataFrame:
        """Every page of one asset's daily metrics in `[start, end]`, canonical and concatenated.

        Paged with the server's own `next_page_token`.  Deciding "was that the last page?" by counting
        rows against the page size is the version of this that breaks silently: the community tier caps
        a page below whatever is asked for, so a full-looking page is not evidence of more and a short
        one is not evidence of the end.  The token is the only thing that knows.
        """
        params = {
            "assets": asset,
            "metrics": ",".join(metrics),
            "frequency": frequency,
            "start_time": start,
            "end_time": end,
            "page_size": "10000",
        }
        columns = {m: c for m, c in {**ADMITTED_METRICS, **REFUSED_METRICS}.items() if m in metrics}
        frames: list[pd.DataFrame] = []
        while True:
            payload = self._get("/v4/timeseries/asset-metrics", params)
            frames.append(parse_timeseries(payload, metrics=columns))
            token = payload.get("next_page_token")
            if not token:
                break
            params = {**params, "next_page_token": str(token)}
        joined = pd.concat(frames, ignore_index=True) if frames else parse_timeseries({}, metrics=columns)
        return joined.drop_duplicates("open_time", keep="last").sort_values("open_time", ignore_index=True)


class WitnessClient:
    """blockchain.info's chart API, used for exactly one thing: falsifying the declared day offset.

    Separate from `CommunityClient` and deliberately tiny.  This source is never stored and never
    reaches a panel - it has had no contract written for it and no revision evidence collected, so by
    this module's own rule it may not reach live.  Its whole job is to be a second, independently
    computed opinion about a quantity Coin Metrics also publishes, which is what turns "the declared
    offset matches" into a check that can fail (point 5).
    """

    def __init__(
        self,
        base_url: str = WITNESS_BASE_URL,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> WitnessClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def transactions_per_day(self, timespan: str = "1year") -> pd.DataFrame:
        response = self._client.get(
            "/charts/n-transactions", params={"timespan": timespan, "format": "json", "sampled": "false"}
        )
        response.raise_for_status()
        payload = response.json()
        assert isinstance(payload, dict)
        return witness_frame(payload)


@dataclass(frozen=True)
class SyncResult:
    """What a sync stored, and what it could not - with the reason, not just the count.

    `failures` carries `type: message` per asset rather than a tally.  A run that reports "3 failed"
    and nothing else is the exact shape `ddcb216` had just finished removing from `research mine`,
    where a hole survived two rounds because the summary counted failures without naming them.
    """

    stored: dict[str, int]
    failures: dict[str, str]


def sync_onchain(
    client: CommunityClient,
    store: MetricsStore,
    mappings: Mapping[str, AssetMapping],
    *,
    start: str,
    end: str,
    metrics: Sequence[str] | None = None,
) -> SyncResult:
    """Fetch each mapped asset once and append it.  Counted per ASSET, not per perpetual.

    Keyed by asset because two perpetuals can share one (`store_kind`'s note), so a per-perpetual loop
    would download the same asset twice and then report a coverage number that double-counts it.

    One asset's failure is recorded against that asset and skipped, never raised: the store is the
    watermark, so a re-run resumes exactly where it stopped, and ending the whole run throws away every
    other asset's work.  `metrics_archive.sync_metrics` learned this on a six-hour job.
    """
    wanted = list(metrics if metrics is not None else ADMITTED_METRICS)
    assets = sorted({mapping.asset for mapping in mappings.values() if mapping.asset is not None})
    stored: dict[str, int] = {}
    failures: dict[str, str] = {}
    for asset in assets:
        try:
            frame = client.asset_metrics(asset, wanted, start=start, end=end)
        except Exception as exc:
            failures[asset] = f"{type(exc).__name__}: {exc}"
            continue
        if not frame.empty:
            stored[asset] = int(store.append(asset, frame))
    return SyncResult(stored, failures)
