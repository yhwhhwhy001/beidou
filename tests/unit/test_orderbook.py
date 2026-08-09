from beidou_data.orderbook import OrderBookDiff, OrderBookLevel, OrderBookManager, OrderBookSnapshot


def _manager() -> OrderBookManager:
    manager = OrderBookManager()
    manager.apply_snapshot(
        OrderBookSnapshot(
            instrument_id="BTCUSDT",
            venue_id="BINANCE",
            bids=[OrderBookLevel(100.0, 1.0)],
            asks=[OrderBookLevel(101.0, 1.0)],
            sequence=10,
        )
    )
    return manager


def test_small_sequence_gap_requires_resync_without_mutating_book() -> None:
    manager = _manager()
    diff = OrderBookDiff(
        instrument_id="BTCUSDT",
        sequence=12,
        prev_sequence=10,
        bid_updates=[OrderBookLevel(100.0, 2.0)],
        ask_updates=[],
    )

    assert manager.apply_diff(diff) is False
    snapshot = manager.get_snapshot()
    assert snapshot is not None
    assert snapshot.sequence == 10
    assert snapshot.best_bid() == 100.0


def test_contiguous_sequence_updates_book() -> None:
    manager = _manager()
    diff = OrderBookDiff(
        instrument_id="BTCUSDT",
        sequence=11,
        prev_sequence=10,
        bid_updates=[OrderBookLevel(100.0, 2.0)],
        ask_updates=[],
    )

    assert manager.apply_diff(diff) is True
    snapshot = manager.get_snapshot()
    assert snapshot is not None
    assert snapshot.sequence == 11
    assert snapshot.bids[0].quantity == 2.0


def test_previous_sequence_mismatch_requires_resync() -> None:
    manager = _manager()
    diff = OrderBookDiff(
        instrument_id="BTCUSDT",
        sequence=11,
        prev_sequence=9,
        bid_updates=[],
        ask_updates=[],
    )

    assert manager.apply_diff(diff) is False
    assert manager.get_snapshot() is not None
    assert manager.get_snapshot().sequence == 10
