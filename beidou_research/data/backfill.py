"""历史 K 线回填：分页拉取 → KlineStore 去重落盘 → DatasetManifest。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_research.data.dataset_manifest import (
    DATASET_MANIFEST_SCHEMA_VERSION,
    DatasetManifest,
    MarketDataProvenance,
)
from beidou_research.data.kline_store import KlineStore


def _to_open_time_ms(value: Any) -> int:
    """fetch_klines 的 open_time 是 UTC datetime；落盘前必须转为毫秒 int。

    KlineStore.append 内部执行 int(k["open_time"])，datetime 会崩，因此
    backfill 在 append 前完成转换；已为 int 的输入原样通过。
    """
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return int(value)


def _retrieved_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _feed_endpoint(feed: Any) -> str:
    """Read a public endpoint fact without granting the feed any new capability."""

    def exact_kline_endpoint(endpoint: str) -> str:
        normalized = endpoint.rstrip("/")
        candidate = f"{normalized}{Endpoint.KLINES}"
        classified = MarketDataProvenance.from_endpoint(candidate, retrieved_at="")
        if classified.source_class != "UNKNOWN":
            return candidate
        return endpoint

    try:
        public_endpoint = feed.rest_url
    except AttributeError:
        public_endpoint = ""
    if isinstance(public_endpoint, str) and public_endpoint:
        return exact_kline_endpoint(public_endpoint)

    try:
        private_endpoint = feed._rest_url
    except AttributeError:
        return ""
    return exact_kline_endpoint(private_endpoint) if isinstance(private_endpoint, str) else ""


def _safe_manifest_hash(manifest: dict[str, Any] | None) -> str:
    if manifest is None:
        return ""
    try:
        return DatasetManifest.hash_of(manifest)
    except (TypeError, ValueError):
        return ""


def _existing_provenance(
    store: KlineStore, symbol: str, interval: str
) -> tuple[dict[str, Any] | None, MarketDataProvenance | None]:
    manifest = DatasetManifest.read(store.manifest_path(symbol, interval))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != DATASET_MANIFEST_SCHEMA_VERSION:
        return manifest, None
    raw = manifest.get("provenance")
    if not isinstance(raw, dict):
        return manifest, None
    try:
        return manifest, MarketDataProvenance.from_dict(raw)
    except (TypeError, ValueError):
        return manifest, None


def backfill_symbol(
    feed: Any,
    store: KlineStore,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    max_pages: int = 400,
    page_pause_seconds: float = 0.5,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """拉取一个 (symbol, interval) 的完整历史并落盘。

    续传：store 已有数据时从 last_open_time 继续，start_ms 仅作首次起点。
    单页失败重试 3 次后记入 errors 并停止该 symbol 的拉取。
    返回 rows 为本次运行后数据集总行数（含既有数据），便于续传场景对齐 manifest。
    """
    errors: list[str] = []
    endpoint = _feed_endpoint(feed)
    incoming_provenance = MarketDataProvenance.from_endpoint(endpoint, retrieved_at=_retrieved_at())
    existing_rows = store._count_existing(symbol, interval)
    existing_manifest: dict[str, Any] | None = None
    if existing_rows > 0:
        existing_manifest, prior_provenance = _existing_provenance(store, symbol, interval)
        if prior_provenance is None:
            return {
                "symbol": symbol,
                "interval": interval,
                "pages": 0,
                "rows": existing_rows,
                "manifest_hash": _safe_manifest_hash(existing_manifest),
                "errors": ["DATASET_PROVENANCE_UNVERIFIABLE"],
            }
        assert existing_manifest is not None
        existing_frame = store.load(symbol, interval)
        content_reasons = DatasetManifest.content_verification_reasons(
            existing_frame,
            existing_manifest,
            symbol,
            interval,
        )
        if content_reasons:
            return {
                "symbol": symbol,
                "interval": interval,
                "pages": 0,
                "rows": existing_rows,
                "manifest_hash": _safe_manifest_hash(existing_manifest),
                "errors": ["DATASET_CONTENT_MISMATCH"],
                "data_integrity_reasons": list(content_reasons),
            }
        if prior_provenance.source_identity() != incoming_provenance.source_identity():
            return {
                "symbol": symbol,
                "interval": interval,
                "pages": 0,
                "rows": existing_rows,
                "manifest_hash": DatasetManifest.hash_of(existing_manifest),
                "errors": ["DATASET_SOURCE_MISMATCH"],
            }
    effective_start = store.last_open_time(symbol, interval) or start_ms
    pages = 0
    collected: list[dict[str, Any]] = []
    cursor = effective_start
    for _ in range(max_pages):
        page: Any = None
        for attempt in range(3):
            try:
                page = feed.fetch_klines(symbol, interval, start_time=cursor, end_time=end_ms, max_pages=1)
                break
            except Exception as exc:
                if attempt == 2:
                    errors.append(f"page@{cursor}:{type(exc).__name__}")
                else:
                    time.sleep(1.0 * (attempt + 1))
        if page is None and errors:
            break
        if not page:
            break
        pages += 1
        for k in page:
            k["open_time"] = _to_open_time_ms(k["open_time"])
        collected.extend(page)
        cursor = int(page[-1]["open_time"])
        if on_progress:
            on_progress(f"{symbol} {interval}: page {pages} rows {len(collected)}")
        if cursor >= end_ms or len(page) < 1000:
            break
        if page_pause_seconds > 0:
            time.sleep(page_pause_seconds)

    rows = store.append(symbol, interval, collected)
    manifest_hash = ""
    if rows > 0:
        if existing_manifest is not None and not collected:
            manifest = existing_manifest
        else:
            frame = store.load(symbol, interval)
            completed_provenance = MarketDataProvenance.from_endpoint(endpoint, retrieved_at=_retrieved_at())
            manifest = DatasetManifest.compute(frame, symbol, interval, provenance=completed_provenance)
        manifest_hash = DatasetManifest.hash_of(manifest)
        DatasetManifest.write(store.manifest_path(symbol, interval), manifest)
    return {
        "symbol": symbol,
        "interval": interval,
        "pages": pages,
        "rows": rows,
        "manifest_hash": manifest_hash,
        "errors": errors,
    }


def backfill_all(
    feed: Any,
    store: KlineStore,
    symbols: list[str],
    intervals: list[str],
    start_ms: int,
    end_ms: int,
    max_pages: int = 400,
    page_pause_seconds: float = 0.5,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for symbol in symbols:
        for interval in intervals:
            reports.append(
                backfill_symbol(
                    feed,
                    store,
                    symbol,
                    interval,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    max_pages=max_pages,
                    page_pause_seconds=page_pause_seconds,
                )
            )
    return reports
