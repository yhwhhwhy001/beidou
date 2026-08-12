"""历史 K 线回填：分页拉取 → KlineStore 去重落盘 → DatasetManifest。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from beidou_research.data.dataset_manifest import DatasetManifest
from beidou_research.data.kline_store import KlineStore


def _to_open_time_ms(value: Any) -> int:
    """fetch_klines 的 open_time 是 UTC datetime；落盘前必须转为毫秒 int。

    KlineStore.append 内部执行 int(k["open_time"])，datetime 会崩，因此
    backfill 在 append 前完成转换；已为 int 的输入原样通过。
    """
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return int(value)


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
    effective_start = store.last_open_time(symbol, interval) or start_ms
    pages = 0
    collected: list[dict[str, Any]] = []
    cursor = effective_start
    for _ in range(max_pages):
        page: Any = None
        for attempt in range(3):
            try:
                page = feed.fetch_klines(
                    symbol, interval, start_time=cursor, end_time=end_ms, max_pages=1
                )
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
        frame = store.load(symbol, interval)
        manifest = DatasetManifest.compute(frame, symbol, interval)
        manifest_hash = DatasetManifest.hash_of(manifest)
        DatasetManifest.write(store._path(symbol, interval).with_name(f"{interval}.manifest.json"), manifest)
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
