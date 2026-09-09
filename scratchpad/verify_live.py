"""#29's live half: the measurement `beidou_data.index_price`'s docstring reports, re-run against the venue.

`tests/data/test_the_index_contract_refuses_a_wrong_offset.py` holds the RULE - a declaration wrong by
one bucket must fail, the zero columns must answer UNVERIFIABLE - on fixtures that never reach the
network, so a venue outage cannot turn the suite red.  This script is the other half of that split: the
OBSERVATION that the venue still behaves as note 1 and note 2 of the module docstring say it did on
2026-09-09.  It is the thing that makes those two numbers reproducible rather than remembered.

It checks exactly the two claims that are written down, and fails loudly on either:

  1.  The archive and REST agree on 24/24 buckets of BTCUSDT, ETHUSDT and SOLUSDT for 2026-09-05 at a
      stamp offset of 0, and on 0/23 at one bucket either way.
  2.  The four volume/taker columns are identically zero and match 23/23 at BOTH rivals - which is why
      `INDEX_VALUE_COLUMNS` is the four price columns and nothing else.

Claim 2 has to build its own frames.  `parse_index_archive_csv` DROPS the degenerate columns at parse
time (that is note 2's whole point), so a check that went through the canonical parser could not see the
columns it exists to indict.  The header detection is mirrored from that parser rather than shared,
because sharing it would mean exporting a private helper for one caller; if the parser's rule ever
changes, this copy is wrong and says so by disagreeing.

The day is pinned rather than "yesterday" on purpose.  A drifting day would make a failure ambiguous
between "the venue moved its stamp" and "that particular day is odd", and 2026-09-05 is the sample the
docstring's numbers were taken on - so a re-run is a comparison against a fixed record, not a new
measurement each time.  Both sources serve it indefinitely (note 7), so this stays runnable.

Reads the venue only.  Writes nothing.  Exits non-zero when a claim no longer holds.

    .venv/bin/python scratchpad/verify_live.py
"""

from __future__ import annotations

import io
import sys
import zipfile
from typing import Any

import httpx
import pandas as pd

from beidou_data.alignment import PASS, UNVERIFIABLE, Verification, verify_stamp_offset
from beidou_data.archive import parse_checksum, verify_zip
from beidou_data.index_price import (
    INDEX_ARCHIVE_COLUMNS,
    INDEX_DEGENERATE_FIELDS,
    INDEX_KLINES_PATH,
    INDEX_VALUE_COLUMNS,
    IndexPriceClient,
    index_archive_path,
    index_contract,
    parse_index_archive_csv,
    parse_index_rest_rows,
    verify_index_contract,
)

ARCHIVE_BASE_URL = "https://data.binance.vision"
DAY = "2026-09-05"
INTERVAL = "1h"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

HOUR_MS = 3_600_000
START_MS = pd.Timestamp(f"{DAY} 00:00", tz="UTC").value // 1_000_000
BUCKETS = 24  # a full UTC day at 1h; the archive ships exactly these and REST is asked for the same


def fetch_archive_csv(client: httpx.Client, symbol: str) -> str:
    """One day of the daily index archive, checksum-verified as `MetricsArchiveClient.fetch_day` does.

    The CHECKSUM is fetched rather than trusted-by-omission because a truncated zip is the failure that
    would otherwise show up as a short day - i.e. as a FAIL of claim 1, blamed on the venue's stamp.
    """
    path = index_archive_path(symbol, INTERVAL, DAY)
    response = client.get(path)
    response.raise_for_status()
    checksum = client.get(path + ".CHECKSUM")
    checksum.raise_for_status()
    verify_zip(response.content, parse_checksum(checksum.text))
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        return archive.read(archive.namelist()[0]).decode("utf-8")


def fetch_rest_rows(client: IndexPriceClient, symbol: str) -> list[list[Any]]:
    """The same day from `/fapi/v1/indexPriceKlines`, as RAW rows.

    `IndexPriceClient.klines` would hand back a canonical frame, and claim 2 needs the columns that
    parser drops - so the request is made once, here, and both frames are built from these rows.
    """
    params = {"pair": symbol, "interval": INTERVAL, "limit": BUCKETS, "startTime": START_MS}
    rows = client.get(INDEX_KLINES_PATH, params)
    assert isinstance(rows, list)
    return [list(row) for row in rows]


def _numeric(frame: pd.DataFrame) -> pd.DataFrame:
    """`open_time` plus the four degenerate columns, as numbers - the frame claim 2 compares."""
    out = pd.DataFrame({"open_time": pd.to_numeric(frame["open_time"], errors="coerce").astype("int64")})
    for column in INDEX_DEGENERATE_FIELDS:
        out[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    return out.sort_values("open_time", ignore_index=True)


def degenerate_frames(archive_csv: str, rest_rows: list[list[Any]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Both sources with the volume/taker columns KEPT.  Header detection mirrors the canonical parser."""
    stripped = archive_csv.strip()
    first_field = stripped.splitlines()[0].split(",")[0].strip()
    headerless = first_field.replace(".", "", 1).isdigit()
    archive = (
        pd.read_csv(io.StringIO(stripped), header=None, names=list(INDEX_ARCHIVE_COLUMNS))
        if headerless
        else pd.read_csv(io.StringIO(stripped))
    )
    rest = pd.DataFrame([row[:12] for row in rest_rows], columns=list(INDEX_ARCHIVE_COLUMNS))
    return _numeric(archive), _numeric(rest)


def _rivals(verification: Verification) -> str:
    return ", ".join(f"{rival.matched}/{rival.compared} at {rival.rest_offset_ms:+d} ms" for rival in verification.rivals)


def check_symbol(archive_csv: str, rest_rows: list[list[Any]], symbol: str) -> list[str]:
    """Both claims for one pair.  Returns the complaints; empty means the docstring still reads true."""
    complaints: list[str] = []

    # Claim 1: the declared offset of zero holds on every bucket, and both rivals are refuted on the
    # same sample.  The second half is the one that carries the evidence - a PASS whose rivals were
    # never scored is a naive join that happened to agree with itself.
    archive = parse_index_archive_csv(archive_csv)
    rest = parse_index_rest_rows(rest_rows)
    price = verify_index_contract(archive, rest, interval=INTERVAL)
    print(f"  {symbol} offset 0: {price.verdict} {price.matched}/{price.compared}  rivals {_rivals(price)}")
    if price.verdict != PASS:
        complaints.append(f"{symbol}: the declared offset no longer passes - {price.verdict}: {price.reason}")
    if (price.matched, price.compared) != (BUCKETS, BUCKETS):
        complaints.append(f"{symbol}: docstring says {BUCKETS}/{BUCKETS} at offset 0, measured {price.matched}/{price.compared}")
    if price.compared_columns != INDEX_VALUE_COLUMNS:
        complaints.append(f"{symbol}: compared {price.compared_columns}, expected {INDEX_VALUE_COLUMNS}")
    if {rival.rest_offset_ms for rival in price.rivals} != {-HOUR_MS, HOUR_MS}:
        complaints.append(f"{symbol}: rivals were {[r.rest_offset_ms for r in price.rivals]}, expected one bucket either way")
    for rival in price.rivals:
        if (rival.matched, rival.compared) != (0, BUCKETS - 1):
            complaints.append(
                f"{symbol}: docstring says 0/{BUCKETS - 1} at {rival.rest_offset_ms:+d} ms, "
                f"measured {rival.matched}/{rival.compared}"
            )

    # Claim 2: the four zero columns agree with themselves at every offset, so a verification resting on
    # them refutes nothing.  UNVERIFIABLE is the RIGHT answer here; a PASS would be the dangerous one.
    archive_raw, rest_raw = degenerate_frames(archive_csv, rest_rows)
    zeros = verify_stamp_offset(
        index_contract(INTERVAL), archive_raw, rest_raw, value_columns=INDEX_DEGENERATE_FIELDS
    )
    print(f"  {symbol} zero columns: {zeros.verdict} {zeros.matched}/{zeros.compared}  rivals {_rivals(zeros)}")
    if zeros.verdict != UNVERIFIABLE:
        complaints.append(f"{symbol}: the volume columns answered {zeros.verdict}, not UNVERIFIABLE - {zeros.reason}")
    for rival in zeros.rivals:
        if (rival.matched, rival.compared) != (BUCKETS - 1, BUCKETS - 1):
            complaints.append(
                f"{symbol}: docstring says the zero columns match {BUCKETS - 1}/{BUCKETS - 1} at "
                f"{rival.rest_offset_ms:+d} ms, measured {rival.matched}/{rival.compared}"
            )
    if not (archive_raw[list(INDEX_DEGENERATE_FIELDS)] == 0.0).all().all():
        complaints.append(f"{symbol}: the archive's volume/taker columns are no longer identically zero")

    return complaints


def main() -> int:
    print(f"#29 index-price contract against the venue: {DAY} {INTERVAL}, {', '.join(SYMBOLS)}")
    complaints: list[str] = []
    with (
        httpx.Client(base_url=ARCHIVE_BASE_URL, timeout=60.0, follow_redirects=True) as archive_client,
        IndexPriceClient() as rest_client,
    ):
        for symbol in SYMBOLS:
            complaints += check_symbol(fetch_archive_csv(archive_client, symbol), fetch_rest_rows(rest_client, symbol), symbol)

    if complaints:
        print("\nThe venue no longer matches what the docstring records:")
        for complaint in complaints:
            print(f"  - {complaint}")
        print("\nThe measurement moved, so the DOCSTRING is what has to change - and RISK-G3's gate is")
        print("what decides whether the columns may still reach live.  Do not edit this script to agree.")
        return 1
    print("\nBoth claims hold: 24/24 at offset 0 and 0/23 either way; the zero columns match 23/23 at both.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
