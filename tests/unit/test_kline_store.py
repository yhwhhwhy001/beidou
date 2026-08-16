from pathlib import Path

from beidou_research.data.kline_store import KlineStore


def _kline(open_time: int, close: float) -> dict:
    return {
        "open_time": open_time,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100.0,
        "is_closed": True,
    }


def test_append_deduplicates_by_open_time(tmp_path: Path) -> None:
    store = KlineStore(root=str(tmp_path))
    first = store.append("BTCUSDT", "1h", [_kline(1000, 1.0), _kline(2000, 2.0)])
    assert first == 2
    # 第二批次与第一批有重叠：open_time=2000 重复，open_time=3000 新增。
    # concat 先旧后新 + drop_duplicates(keep="last") → 重复行保留新批次值。
    second = store.append("BTCUSDT", "1h", [_kline(2000, 2.5), _kline(3000, 3.0)])
    assert second == 3
    df = store.load("BTCUSDT", "1h")
    assert list(df["open_time"]) == [1000, 2000, 3000]
    assert df.iloc[1]["close"] == 2.5


def test_load_returns_full_schema(tmp_path: Path) -> None:
    store = KlineStore(root=str(tmp_path))
    store.append("BTCUSDT", "1h", [_kline(1000, 1.0)])
    df = store.load("BTCUSDT", "1h")
    assert list(df.columns) == ["open_time", "open", "high", "low", "close", "volume", "is_closed"]


def test_has_data_and_last_open_time(tmp_path: Path) -> None:
    store = KlineStore(root=str(tmp_path))
    assert store.has_data("BTCUSDT", "1h") is False
    assert store.last_open_time("BTCUSDT", "1h") is None
    store.append("BTCUSDT", "1h", [_kline(1000, 1.0), _kline(2000, 2.0)])
    assert store.has_data("BTCUSDT", "1h", min_rows=2) is True
    assert store.last_open_time("BTCUSDT", "1h") == 2000
