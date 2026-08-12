"""Task 4: factor_miner backfill 核心逻辑测试。

_fake feed 返回与 MarketDataFeed.fetch_klines 一致的解析后 dict 列表
（open_time 为 UTC datetime），验证 backfill 的 datetime→毫秒转换契约。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from beidou_research.data.backfill import backfill_symbol
from beidou_research.data.dataset_manifest import DatasetManifest
from beidou_research.data.kline_store import KlineStore

INTERVAL_MS = 3_600_000
T0 = 1_700_000_000_000


def _raw_kline(open_time_ms: int) -> dict[str, object]:
    """与 MarketDataFeed.fetch_klines 输出一致的解析后 kline dict。"""
    return {
        "open_time": datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "is_closed": True,
    }


class _FakeFeed:
    """第一页返回 3 根，之后返回空（模拟已拉尽）。"""

    def __init__(self) -> None:
        self.calls = 0

    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int | None = None,
        end_time: int | None = None,
        max_pages: int = 400,
    ) -> list[dict]:
        self.calls += 1
        if self.calls > 1:
            return []
        return [_raw_kline(T0 + i * INTERVAL_MS) for i in range(3)]


def test_backfill_writes_parquet_and_manifest(tmp_path: Path) -> None:
    feed = _FakeFeed()
    store = KlineStore(root=str(tmp_path / "klines"))
    report = backfill_symbol(
        feed,
        store,
        "BTCUSDT",
        "1h",
        start_ms=T0,
        end_ms=T0 + 10 * INTERVAL_MS,
        page_pause_seconds=0,
    )
    assert report["rows"] == 3 and not report["errors"]
    manifest_path = tmp_path / "klines" / "BTCUSDT" / "1h.manifest.json"
    manifest = DatasetManifest.read(manifest_path)
    assert manifest is not None and manifest["rows"] == 3
    assert report["manifest_hash"] == DatasetManifest.hash_of(manifest)


def test_backfill_resumes_from_store(tmp_path: Path) -> None:
    feed = _FakeFeed()
    store = KlineStore(root=str(tmp_path / "klines"))
    first = backfill_symbol(feed, store, "BTCUSDT", "1h", start_ms=0, end_ms=10**12, page_pause_seconds=0)
    assert first["rows"] == 3
    # 第二次运行：store 已有数据 → 从 last_open_time 继续（fake feed 第二次返回空）→ 不重复不报错
    second = backfill_symbol(feed, store, "BTCUSDT", "1h", start_ms=0, end_ms=10**12, page_pause_seconds=0)
    assert second["rows"] == 3 and not second["errors"]


def test_backfill_reports_feed_errors(tmp_path: Path) -> None:
    class _BrokenFeed:
        def fetch_klines(
            self,
            symbol: str,
            interval: str,
            start_time: int | None = None,
            end_time: int | None = None,
            max_pages: int = 400,
        ) -> list[dict]:
            raise RuntimeError("api down")

    store = KlineStore(root=str(tmp_path / "klines"))
    report = backfill_symbol(_BrokenFeed(), store, "BTCUSDT", "1h", start_ms=0, end_ms=10**12, page_pause_seconds=0)
    assert report["errors"] and report["rows"] == 0
