from beidou_core.feed import MarketDataFeed

INTERVAL_MS = 3_600_000


def _raw_kline(open_time_ms: int) -> list:
    # Binance kline 数组 12 字段：0=openTime, 1=open, 2=high, 3=low, 4=close,
    # 5=volume, 6=closeTime, 7=quoteVolume, 8=trades, 11=isClosed。
    # closeTime 必须 > openTime（_parse_rest_kline 校验），且早于 now。
    return [
        open_time_ms,
        "100.0",
        "101.0",
        "99.0",
        "100.5",
        "10.0",
        open_time_ms + INTERVAL_MS - 1,
        "0",
        "0",
        "0",
        "0",
        True,
    ]


class _MockApi:
    def __init__(self, pages: dict[int, list[list]]) -> None:
        self._pages = pages
        self.calls: list[dict] = []

    def __call__(self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None) -> list:
        params = params or {}
        self.calls.append(dict(params))
        # 无 startTime（旧行为单请求）→ 返回第一页；未知 startTime → 回退第一页，
        # 模拟重复页以触发实现的"无进展即 break"防死循环保护。
        first_page = next(iter(self._pages.values()), [])
        start_time = params.get("startTime")
        if start_time is None:
            return first_page
        return self._pages.get(int(start_time), first_page)


def _make_feed(pages: dict[int, list[list]]) -> tuple[MarketDataFeed, _MockApi]:
    feed = MarketDataFeed.__new__(MarketDataFeed)
    api = _MockApi(pages)
    # _api 是实例方法；mypy 不允许直接赋值，但运行时注入 mock 完全等价
    feed._api = api  # type: ignore[method-assign]
    return feed, api


def test_paginates_until_end_time() -> None:
    t0 = 1_700_000_000_000
    page1 = [_raw_kline(t0 + i * INTERVAL_MS) for i in range(3)]
    page2 = [_raw_kline(t0 + 3 * INTERVAL_MS)]
    # 分页推进 = 本页最后一根 open_time（t0+2h），下一页请求以其为 startTime。
    pages = {t0: page1, t0 + 2 * INTERVAL_MS: page2}
    feed, api = _make_feed(pages)
    result = feed.fetch_klines("BTCUSDT", "1h", start_time=t0, end_time=t0 + 3 * INTERVAL_MS)
    assert len(result) == 4
    assert api.calls[0]["startTime"] == t0
    assert api.calls[1]["startTime"] == t0 + 2 * INTERVAL_MS
    assert all(k["is_closed"] is True for k in result)


def test_deduplicates_across_pages() -> None:
    t0 = 1_700_000_000_000
    page1 = [_raw_kline(t0 + i * INTERVAL_MS) for i in range(2)]
    page2 = [_raw_kline(t0 + INTERVAL_MS), _raw_kline(t0 + 2 * INTERVAL_MS)]  # 与 page1 重叠 1 根
    pages = {t0: page1, t0 + INTERVAL_MS: page2}
    feed, _ = _make_feed(pages)
    result = feed.fetch_klines("BTCUSDT", "1h", start_time=t0, end_time=t0 + 2 * INTERVAL_MS)
    assert len(result) == 3


def test_no_time_range_keeps_legacy_behavior() -> None:
    t0 = 1_700_000_000_000
    pages = {t0: [_raw_kline(t0)]}
    feed, api = _make_feed(pages)
    result = feed.fetch_klines("BTCUSDT", "1h", limit=500)
    assert len(result) == 1
    call = api.calls[0]
    assert "startTime" not in call and call["limit"] == 500


def test_max_pages_bound() -> None:
    t0 = 1_700_000_000_000
    pages = {t0: [_raw_kline(t0 + i * INTERVAL_MS) for i in range(1000)]}
    feed, api = _make_feed(pages)
    # 下一页 startTime 与第一页相同 → 无限循环风险；max_pages 必须截断
    result = feed.fetch_klines("BTCUSDT", "1h", start_time=t0, end_time=t0 + 10**12, max_pages=2)
    assert len(result) == 1000
    assert len(api.calls) == 2
