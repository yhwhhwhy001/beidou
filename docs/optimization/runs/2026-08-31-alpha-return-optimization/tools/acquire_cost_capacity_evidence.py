"""Acquire bounded public evidence for research cost, impact, and capacity calibration.

This tool is deliberately outside the trading runtime.  It reads only public
Binance USD-M market-data endpoints and the already-frozen August K-line
artifacts.  It never reads an account, submits an order, searches Alpha
candidates, or claims fill calibration.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import httpx
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
START = datetime(2026, 8, 1, tzinfo=timezone.utc)
END = datetime(2026, 8, 31, tzinfo=timezone.utc)
START_MS = int(START.timestamp() * 1000)
END_MS = int(END.timestamp() * 1000)
BOOK_DEPTH_BASE = "https://data.binance.vision/data/futures/um/daily/bookDepth"
LOGICAL_API_BASE = "https://fapi.binance.com/fapi/v1"
API_TRANSPORT_BASE = "https://d2ukl3c6tymv7q.cloudfront.net/fapi/v1"
API_TRANSPORT_HEADERS = {"Host": "fapi.binance.com"}
DATASET_ROOT = REPOSITORY_ROOT / "artifacts/datasets/alpha-return-2026-08-01_2026-08-31-v1"
OUTPUT_ROOT = (
    REPOSITORY_ROOT
    / "artifacts/analysis/alpha-return-2026-08-01_2026-08-31-v1/governance/cost-capacity"
)
METRIC_POLICY = Path(
    "/Users/maguannan/beidou-authorization/BD-AF-P3-T07/metric-owner-policy.json"
)
FACTOR_POLICY = REPOSITORY_ROOT / "config/factor_mining_policy.yaml"
DEPTH_PERCENT = Decimal("0.20")
EXPECTED_SNAPSHOTS_PER_DAY = 2_880
TARGET_NOTIONALS = (10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"INVALID_DECIMAL:{field}") from exc
    if not result.is_finite():
        raise ValueError(f"NON_FINITE_DECIMAL:{field}")
    return result


def parse_checksum(payload: bytes, expected_filename: str) -> str:
    try:
        parts = payload.decode("ascii").strip().split()
    except UnicodeDecodeError as exc:
        raise ValueError("CHECKSUM_FORMAT_INVALID") from exc
    if len(parts) != 2 or len(parts[0]) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in parts[0]):
        raise ValueError("CHECKSUM_FORMAT_INVALID")
    if parts[1] != expected_filename or Path(parts[1]).name != parts[1]:
        raise ValueError("CHECKSUM_FILENAME_MISMATCH")
    return parts[0].lower()


@dataclass(frozen=True, slots=True)
class DepthSample:
    timestamp: str
    bid_notional_20bps: Decimal
    ask_notional_20bps: Decimal

    @property
    def two_sided_notional_20bps(self) -> Decimal:
        return min(self.bid_notional_20bps, self.ask_notional_20bps)


def parse_book_depth_archive(payload: bytes, expected_csv_name: str) -> tuple[DepthSample, ...]:
    """Parse the official 30-second depth archive and retain the +/-20 bps bands."""

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
            files = [entry for entry in bundle.infolist() if not entry.is_dir()]
            if len(files) != 1 or files[0].filename != expected_csv_name:
                raise ValueError("BOOK_DEPTH_MEMBER_MISMATCH")
            if files[0].file_size > 100_000_000:
                raise ValueError("BOOK_DEPTH_MEMBER_TOO_LARGE")
            with bundle.open(files[0]) as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                if reader.fieldnames != ["timestamp", "percentage", "depth", "notional"]:
                    raise ValueError("BOOK_DEPTH_HEADER_INVALID")
                grouped: dict[str, dict[str, Decimal]] = {}
                for row in reader:
                    percentage = _decimal(row.get("percentage"), "percentage")
                    if abs(percentage) != DEPTH_PERCENT:
                        continue
                    timestamp = str(row.get("timestamp", ""))
                    try:
                        parsed = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=timezone.utc
                        )
                    except ValueError as exc:
                        raise ValueError("BOOK_DEPTH_TIMESTAMP_INVALID") from exc
                    if parsed.strftime("%Y-%m-%d %H:%M:%S") != timestamp:
                        raise ValueError("BOOK_DEPTH_TIMESTAMP_NON_CANONICAL")
                    notional = _decimal(row.get("notional"), "notional")
                    if notional <= 0:
                        raise ValueError("BOOK_DEPTH_NOTIONAL_NON_POSITIVE")
                    side = "bid" if percentage < 0 else "ask"
                    values = grouped.setdefault(timestamp, {})
                    if side in values:
                        raise ValueError("BOOK_DEPTH_SIDE_DUPLICATE")
                    values[side] = notional
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise ValueError("BOOK_DEPTH_ARCHIVE_INVALID") from exc

    samples: list[DepthSample] = []
    for timestamp, sides in sorted(grouped.items()):
        if set(sides) != {"bid", "ask"}:
            raise ValueError("BOOK_DEPTH_SIDE_INCOMPLETE")
        samples.append(
            DepthSample(
                timestamp=timestamp,
                bid_notional_20bps=sides["bid"],
                ask_notional_20bps=sides["ask"],
            )
        )
    if not samples:
        raise ValueError("BOOK_DEPTH_ARCHIVE_EMPTY")
    return tuple(samples)


def parse_funding_payload(payload: bytes, *, expected_symbol: str) -> tuple[dict[str, object], ...]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("FUNDING_JSON_INVALID") from exc
    if not isinstance(value, list) or not value:
        raise ValueError("FUNDING_PAYLOAD_EMPTY")
    result: list[dict[str, object]] = []
    previous_time = -1
    for row in value:
        if not isinstance(row, Mapping) or row.get("symbol") != expected_symbol:
            raise ValueError("FUNDING_SYMBOL_MISMATCH")
        try:
            funding_time = int(str(row["fundingTime"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("FUNDING_TIME_INVALID") from exc
        rate = _decimal(row.get("fundingRate"), "fundingRate")
        if funding_time <= previous_time or not START_MS <= funding_time < END_MS:
            raise ValueError("FUNDING_TIME_RANGE_OR_ORDER_INVALID")
        previous_time = funding_time
        result.append({"funding_time_ms": funding_time, "funding_rate": float(rate)})
    return tuple(result)


def parse_depth_snapshot(payload: bytes, *, expected_symbol: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("DEPTH_JSON_INVALID") from exc
    if not isinstance(value, Mapping):
        raise ValueError("DEPTH_PAYLOAD_INVALID")
    bids = value.get("bids")
    asks = value.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        raise ValueError("DEPTH_SIDES_MISSING")

    def levels(raw_levels: Sequence[object], side: str) -> tuple[tuple[Decimal, Decimal], ...]:
        parsed: list[tuple[Decimal, Decimal]] = []
        for raw in raw_levels:
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != 2:
                raise ValueError(f"DEPTH_LEVEL_INVALID:{side}")
            price = _decimal(raw[0], f"{side}.price")
            quantity = _decimal(raw[1], f"{side}.quantity")
            if price <= 0 or quantity <= 0:
                raise ValueError(f"DEPTH_LEVEL_NON_POSITIVE:{side}")
            parsed.append((price, quantity))
        if side == "bids" and any(left[0] <= right[0] for left, right in pairwise(parsed)):
            raise ValueError("DEPTH_BID_ORDER_INVALID")
        if side == "asks" and any(left[0] >= right[0] for left, right in pairwise(parsed)):
            raise ValueError("DEPTH_ASK_ORDER_INVALID")
        return tuple(parsed)

    parsed_bids = levels(bids, "bids")
    parsed_asks = levels(asks, "asks")
    if parsed_bids[0][0] >= parsed_asks[0][0]:
        raise ValueError("DEPTH_BOOK_CROSSED")
    return {
        "symbol": expected_symbol,
        "last_update_id": int(value.get("lastUpdateId", 0)),
        "event_time_ms": int(value.get("E", 0)),
        "transaction_time_ms": int(value.get("T", 0)),
        "bids": parsed_bids,
        "asks": parsed_asks,
    }


def walk_book(
    *, bids: Sequence[tuple[Decimal, Decimal]], asks: Sequence[tuple[Decimal, Decimal]], notional: Decimal
) -> dict[str, float | str | None]:
    if notional <= 0:
        raise ValueError("TARGET_NOTIONAL_NON_POSITIVE")
    mid = (bids[0][0] + asks[0][0]) / Decimal(2)

    def walk(levels: Sequence[tuple[Decimal, Decimal]], *, buy: bool) -> tuple[Decimal, Decimal] | None:
        remaining = notional
        base = Decimal(0)
        quote = Decimal(0)
        for price, quantity in levels:
            available_quote = price * quantity
            used_quote = min(remaining, available_quote)
            quote += used_quote
            base += used_quote / price
            remaining -= used_quote
            if remaining <= Decimal("0.00000001"):
                average = quote / base
                adverse = (average / mid - 1) if buy else (1 - average / mid)
                return average, adverse * Decimal(10_000)
        return None

    buy = walk(asks, buy=True)
    sell = walk(bids, buy=False)
    return {
        "notional": float(notional),
        "buy_average_price": float(buy[0]) if buy else None,
        "buy_adverse_bps_vs_mid": float(buy[1]) if buy else None,
        "sell_average_price": float(sell[0]) if sell else None,
        "sell_adverse_bps_vs_mid": float(sell[1]) if sell else None,
        "status": "PASS" if buy and sell else "INSUFFICIENT_SNAPSHOT_DEPTH",
    }


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values or not 0 <= probability <= 1:
        raise ValueError("QUANTILE_INPUT_INVALID")
    ordered = sorted(float(value) for value in values)
    if any(not math.isfinite(value) for value in ordered):
        raise ValueError("QUANTILE_NON_FINITE")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def distribution(values: Sequence[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "min": min(values),
        "p01": _quantile(values, 0.01),
        "p05": _quantile(values, 0.05),
        "median": _quantile(values, 0.50),
        "p95": _quantile(values, 0.95),
        "p99": _quantile(values, 0.99),
        "max": max(values),
        "mean": sum(values) / len(values),
    }


def calibrated_market_envelope(
    *, two_sided_depth_notional: Sequence[float], hourly_quote_volume: Sequence[float], participation_rate: float
) -> dict[str, float | str]:
    if not 0 < participation_rate <= 1:
        raise ValueError("PARTICIPATION_RATE_INVALID")
    depth_p05 = _quantile(two_sided_depth_notional, 0.05)
    volume_p05 = _quantile(hourly_quote_volume, 0.05)
    participation_capacity = volume_p05 * participation_rate
    capacity = min(depth_p05, participation_capacity)
    return {
        "depth_20bps_p05_notional": depth_p05,
        "hourly_quote_volume_p05": volume_p05,
        "participation_rate_max": participation_rate,
        "participation_capacity_p05_notional": participation_capacity,
        "market_capacity_envelope_notional": capacity,
        "capacity_constraint": "DEPTH_20BPS" if depth_p05 <= participation_capacity else "HOURLY_VOLUME",
        "strategy_capacity_status": "NOT_VERIFIABLE_NO_CANDIDATE_NET_RETURN",
    }


def _dates(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor < end:
        yield cursor
        cursor += timedelta(days=1)


def _fetch(client: httpx.Client, url: str, *, params: Mapping[str, object] | None = None) -> httpx.Response:
    last_error: Exception | None = None
    headers = API_TRANSPORT_HEADERS if url.startswith(API_TRANSPORT_BASE) else None
    for attempt in range(3):
        try:
            response = client.get(url, params=params, headers=headers)
            if 300 <= response.status_code < 400:
                raise RuntimeError(f"REDIRECT_REJECTED:{response.status_code}")
            response.raise_for_status()
            return response
        except (httpx.HTTPError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
    assert last_error is not None
    raise RuntimeError(f"PUBLIC_READ_FAILED:{url}:{type(last_error).__name__}") from last_error


def _book_depth_day(
    client: httpx.Client, *, output: Path, symbol: str, day: date
) -> tuple[str, tuple[DepthSample, ...], dict[str, object]]:
    stamp = day.isoformat()
    filename = f"{symbol}-bookDepth-{stamp}.zip"
    url = f"{BOOK_DEPTH_BASE}/{symbol}/{filename}"
    checksum_url = f"{url}.CHECKSUM"
    archive_path = output / "raw/bookDepth" / symbol / filename
    checksum_path = archive_path.with_name(f"{filename}.CHECKSUM")
    if archive_path.is_file() and checksum_path.is_file():
        archive_payload = archive_path.read_bytes()
        checksum_payload = checksum_path.read_bytes()
        transport = "LOCAL_VERIFIED_REUSE"
    else:
        archive_response = _fetch(client, url)
        checksum_response = _fetch(client, checksum_url)
        archive_payload = archive_response.content
        checksum_payload = checksum_response.content
        _write_bytes(archive_path, archive_payload)
        _write_bytes(checksum_path, checksum_payload)
        transport = "HTTPS_PUBLIC_ARCHIVE"
    expected_sha256 = parse_checksum(checksum_payload, filename)
    actual_sha256 = _sha256_bytes(archive_payload)
    if expected_sha256 != actual_sha256:
        raise ValueError(f"BOOK_DEPTH_CHECKSUM_MISMATCH:{filename}")
    samples = parse_book_depth_archive(archive_payload, filename.removesuffix(".zip") + ".csv")
    if len(samples) != EXPECTED_SNAPSHOTS_PER_DAY:
        raise ValueError(f"BOOK_DEPTH_DAILY_COUNT_MISMATCH:{filename}:{len(samples)}")
    return symbol, samples, {
        "date": stamp,
        "url": url,
        "checksum_url": checksum_url,
        "archive_path": str(archive_path.relative_to(REPOSITORY_ROOT)),
        "checksum_path": str(checksum_path.relative_to(REPOSITORY_ROOT)),
        "archive_sha256": actual_sha256,
        "published_sha256": expected_sha256,
        "snapshots": len(samples),
        "transport": transport,
    }


def _load_quote_volume(symbol: str) -> tuple[float, ...]:
    path = DATASET_ROOT / "raw/api" / f"{symbol}-1h.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or len(value) != 720:
        raise ValueError(f"QUOTE_VOLUME_SOURCE_INVALID:{symbol}")
    result: list[float] = []
    previous = -1
    for row in value:
        if not isinstance(row, list) or len(row) != 12:
            raise ValueError(f"QUOTE_VOLUME_ROW_INVALID:{symbol}")
        open_time = int(str(row[0]))
        if open_time <= previous or not START_MS <= open_time < END_MS:
            raise ValueError(f"QUOTE_VOLUME_TIME_INVALID:{symbol}")
        previous = open_time
        quote_volume = float(_decimal(row[7], "quote_volume"))
        if quote_volume < 0:
            raise ValueError(f"QUOTE_VOLUME_NEGATIVE:{symbol}")
        result.append(quote_volume)
    return tuple(result)


def _policy_inputs() -> dict[str, object]:
    metric = json.loads(METRIC_POLICY.read_text(encoding="utf-8"))
    factor = yaml.safe_load(FACTOR_POLICY.read_text(encoding="utf-8"))
    metric_values = metric["cost_capacity"]["configured_values"]
    factor_values = factor["cost"]
    return {
        "metric_owner_policy": {
            "path": str(METRIC_POLICY),
            "source_sha256": _sha256_file(METRIC_POLICY),
            "policy_digest": metric["digest"]["policy_digest"],
            "status": metric["status"],
            "configured_values": metric_values,
            "interpretation": "FROZEN_POLICY_PRIOR_NOT_ACCOUNT_OR_FILL_CALIBRATION",
        },
        "factor_mining_policy": {
            "path": str(FACTOR_POLICY.relative_to(REPOSITORY_ROOT)),
            "source_sha256": _sha256_file(FACTOR_POLICY),
            "policy_version": factor["policy_version"],
            "configured_values": factor_values,
            "interpretation": "VERSIONED_DIAGNOSTIC_PRIOR",
        },
    }


def acquire(*, output: Path = OUTPUT_ROOT, max_workers: int = 8, snapshot_count: int = 5) -> dict[str, object]:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    started_at = _utc_now()
    samples_by_symbol: dict[str, list[DepthSample]] = {symbol: [] for symbol in SYMBOLS}
    archive_records: list[dict[str, object]] = []
    timeout = httpx.Timeout(30.0, connect=15.0)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        futures = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for symbol in SYMBOLS:
                for day in _dates(START.date(), END.date()):
                    futures.append(
                        executor.submit(_book_depth_day, client, output=output, symbol=symbol, day=day)
                    )
            for future in as_completed(futures):
                symbol, samples, record = future.result()
                samples_by_symbol[symbol].extend(samples)
                archive_records.append(record)

        exchange_info = _fetch(client, f"{API_TRANSPORT_BASE}/exchangeInfo")
        _write_bytes(output / "raw/api/exchangeInfo.json", exchange_info.content)
        exchange_payload = exchange_info.json()
        exchange_symbols = {
            row["symbol"]: row
            for row in exchange_payload.get("symbols", [])
            if isinstance(row, dict) and row.get("symbol") in SYMBOLS
        }
        if set(exchange_symbols) != set(SYMBOLS):
            raise ValueError("EXCHANGE_INFO_SYMBOLS_INCOMPLETE")

        funding_by_symbol: dict[str, tuple[dict[str, object], ...]] = {}
        snapshots_by_symbol: dict[str, list[dict[str, object]]] = {symbol: [] for symbol in SYMBOLS}
        for symbol in SYMBOLS:
            funding_response = _fetch(
                client,
                f"{API_TRANSPORT_BASE}/fundingRate",
                params={"symbol": symbol, "startTime": START_MS, "endTime": END_MS - 1, "limit": 1000},
            )
            _write_bytes(output / "raw/api" / f"{symbol}-fundingRate.json", funding_response.content)
            funding_by_symbol[symbol] = parse_funding_payload(
                funding_response.content, expected_symbol=symbol
            )
            for index in range(snapshot_count):
                response = _fetch(
                    client,
                    f"{API_TRANSPORT_BASE}/depth",
                    params={"symbol": symbol, "limit": 1000},
                )
                retrieved_at = _utc_now()
                raw_path = output / "raw/api" / f"{symbol}-depth-{index + 1:02d}.json"
                _write_bytes(raw_path, response.content)
                parsed = parse_depth_snapshot(response.content, expected_symbol=symbol)
                bids = parsed.pop("bids")
                asks = parsed.pop("asks")
                assert isinstance(bids, tuple) and isinstance(asks, tuple)
                best_bid = bids[0][0]
                best_ask = asks[0][0]
                mid = (best_bid + best_ask) / Decimal(2)
                snapshots_by_symbol[symbol].append(
                    {
                        **parsed,
                        "retrieved_at": retrieved_at,
                        "raw_path": str(raw_path.relative_to(REPOSITORY_ROOT)),
                        "raw_sha256": _sha256_file(raw_path),
                        "best_bid": float(best_bid),
                        "best_ask": float(best_ask),
                        "spread_bps": float((best_ask - best_bid) / mid * Decimal(10_000)),
                        "walks": [
                            walk_book(bids=bids, asks=asks, notional=Decimal(target))
                            for target in TARGET_NOTIONALS
                        ],
                    }
                )
                time.sleep(0.05)

    per_symbol: dict[str, object] = {}
    for symbol in SYMBOLS:
        depth_samples = sorted(samples_by_symbol[symbol], key=lambda item: item.timestamp)
        expected_monthly = EXPECTED_SNAPSHOTS_PER_DAY * (END.date() - START.date()).days
        if len(depth_samples) != expected_monthly or len({row.timestamp for row in depth_samples}) != expected_monthly:
            raise ValueError(f"BOOK_DEPTH_MONTHLY_COUNT_MISMATCH:{symbol}:{len(depth_samples)}")
        bid_depth = [float(row.bid_notional_20bps) for row in depth_samples]
        ask_depth = [float(row.ask_notional_20bps) for row in depth_samples]
        two_sided = [float(row.two_sided_notional_20bps) for row in depth_samples]
        impact_slope = [20.0 * 10_000.0 / value for value in two_sided]
        quote_volume = list(_load_quote_volume(symbol))
        funding_bps = [float(row["funding_rate"]) * 10_000.0 for row in funding_by_symbol[symbol]]
        spread_bps = [float(row["spread_bps"]) for row in snapshots_by_symbol[symbol]]
        per_symbol[symbol] = {
            "historical_window": {
                "start_inclusive": START.isoformat().replace("+00:00", "Z"),
                "end_exclusive": END.isoformat().replace("+00:00", "Z"),
                "depth_snapshots": len(depth_samples),
                "hourly_quote_volume_rows": len(quote_volume),
                "funding_observations": len(funding_bps),
            },
            "depth_notional_at_20bps": {
                "bid": distribution(bid_depth),
                "ask": distribution(ask_depth),
                "two_sided_min": distribution(two_sided),
                "source": "OFFICIAL_BINANCE_BOOKDEPTH_ARCHIVE_30_SECOND",
            },
            "linearized_impact_bps_per_10k_at_20bps_band": {
                **distribution(impact_slope),
                "interpretation": "LOCAL_SLOPE_FROM_20BPS_CUMULATIVE_DEPTH_NOT_EXECUTED_FILL",
            },
            "hourly_quote_volume": distribution(quote_volume),
            "funding_rate_8h_bps": distribution(funding_bps),
            "current_public_depth_snapshots": {
                "count": len(snapshots_by_symbol[symbol]),
                "spread_bps": distribution(spread_bps),
                "snapshots": snapshots_by_symbol[symbol],
                "time_alignment": "CURRENT_SNAPSHOT_NOT_AUGUST_HISTORICAL_SPREAD",
            },
            "market_capacity_envelope": calibrated_market_envelope(
                two_sided_depth_notional=two_sided,
                hourly_quote_volume=quote_volume,
                participation_rate=0.10,
            ),
            "exchange_contract": {
                "status": exchange_symbols[symbol].get("status"),
                "contract_type": exchange_symbols[symbol].get("contractType"),
                "onboard_date_ms": exchange_symbols[symbol].get("onboardDate"),
                "quote_asset": exchange_symbols[symbol].get("quoteAsset"),
                "margin_asset": exchange_symbols[symbol].get("marginAsset"),
            },
        }

    report: dict[str, object] = {
        "schema_version": "1.0",
        "report_id": "ARO-07-COST-IMPACT-CAPACITY-CALIBRATION",
        "started_at": started_at,
        "completed_at": _utc_now(),
        "status": "PASS_WITH_CONDITIONS",
        "promotion_use": "NOT_VERIFIABLE",
        "scope": {
            "venue": "BINANCE_USDM",
            "symbols": list(SYMBOLS),
            "interval": "1h",
            "historical_start_inclusive": START.isoformat().replace("+00:00", "Z"),
            "historical_end_exclusive": END.isoformat().replace("+00:00", "Z"),
            "account_or_order_reads": False,
            "candidate_search": False,
        },
        "policy_inputs": _policy_inputs(),
        "archive_verification": {
            "files": len(archive_records),
            "published_checksums_verified": len(archive_records),
            "expected_files": len(SYMBOLS) * (END.date() - START.date()).days,
            "records": sorted(archive_records, key=lambda row: (str(row["date"]), str(row["url"]))),
        },
        "per_symbol": per_symbol,
        "evidence_classification": {
            "funding": "HISTORICAL_PUBLIC_RATE_CALIBRATED",
            "depth_and_impact": "HISTORICAL_PUBLIC_DEPTH_CALIBRATED",
            "hourly_volume": "HISTORICAL_FROZEN_DATA_CALIBRATED",
            "spread": "CURRENT_PUBLIC_SNAPSHOT_ONLY",
            "fees": "FROZEN_POLICY_PRIOR_NOT_ACCOUNT_TIER_CALIBRATED",
            "fills_and_latency": "ABSENT",
            "strategy_capacity": "NOT_VERIFIABLE_WITHOUT_CANDIDATE_NET_RETURN",
        },
        "limitations": [
            "No account fee tier or private fill history was read; fee values remain frozen policy priors.",
            "Historical +/-20 bps depth is calibrated from official 30-second archives, but "
            "top-of-book spread is represented only by current public snapshots.",
            "Linearized impact is an exogenous market-depth envelope, not a realized fill or latency model.",
            "Capacity cannot pass the Metric Owner rule until a frozen candidate remains net-positive "
            "under every required cost stress and its actual notional/turnover is bound.",
            "This report authorizes no Alpha candidate, promotion, Paper/Testnet/Mainnet action, "
            "or profitability claim.",
        ],
    }
    _write_bytes(output / "calibration-report.json", _json_bytes(report))
    return report


def verify_report(path: Path = OUTPUT_ROOT / "calibration-report.json") -> dict[str, object]:
    report = json.loads(path.read_text(encoding="utf-8"))
    reasons: list[str] = []
    if report.get("status") != "PASS_WITH_CONDITIONS" or report.get("promotion_use") != "NOT_VERIFIABLE":
        reasons.append("REPORT_STATUS_INVALID")
    scope = report.get("scope", {})
    if scope.get("account_or_order_reads") is not False or scope.get("candidate_search") is not False:
        reasons.append("SCOPE_BOUNDARY_INVALID")
    archive = report.get("archive_verification", {})
    if archive.get("files") != 120 or archive.get("published_checksums_verified") != 120:
        reasons.append("ARCHIVE_CHECKSUM_COVERAGE_INVALID")
    per_symbol = report.get("per_symbol", {})
    if set(per_symbol) != set(SYMBOLS):
        reasons.append("SYMBOL_SCOPE_INVALID")
    for symbol in SYMBOLS:
        value = per_symbol.get(symbol, {})
        if value.get("historical_window", {}).get("depth_snapshots") != 86_400:
            reasons.append(f"DEPTH_COUNT_INVALID:{symbol}")
        if value.get("market_capacity_envelope", {}).get("strategy_capacity_status") != (
            "NOT_VERIFIABLE_NO_CANDIDATE_NET_RETURN"
        ):
            reasons.append(f"CAPACITY_STATUS_INVALID:{symbol}")
    return {
        "schema_version": "1.0",
        "status": "PASS" if not reasons else "FAIL",
        "reasons": reasons,
        "report_path": str(path),
        "report_sha256": _sha256_file(path),
        "verified_at": _utc_now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    acquire_parser = subcommands.add_parser("acquire")
    acquire_parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    acquire_parser.add_argument("--max-workers", type=int, default=8)
    acquire_parser.add_argument("--snapshot-count", type=int, default=5)
    verify_parser = subcommands.add_parser("verify")
    verify_parser.add_argument("--report", type=Path, default=OUTPUT_ROOT / "calibration-report.json")
    args = parser.parse_args()
    if args.command == "acquire":
        report = acquire(
            output=args.output, max_workers=args.max_workers, snapshot_count=args.snapshot_count
        )
        print(  # noqa: T201 - CLI result contract
            json.dumps({"status": report["status"], "output": str(args.output)}, sort_keys=True)
        )
        return 0
    verification = verify_report(args.report)
    print(json.dumps(verification, sort_keys=True))  # noqa: T201 - CLI result contract
    return 0 if verification["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
