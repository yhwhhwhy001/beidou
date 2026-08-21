"""Deterministic boundary coverage for data and universe lifecycle contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import beidou_data.canonical_bars as canonical_bars
from beidou_data.canonical_bars import BarEventType, CanonicalBarBuilder
from beidou_data.contracts import (
    CanonicalMarketEvent,
    DQCheck,
    DQSnapshot,
    DQStatus,
    EventType,
    PITUniverseSnapshot,
    UniverseEntry,
)
from beidou_data.datasets import DatasetManager
from beidou_data.feature_store import FeatureStore, FeatureVector
from beidou_data.klines import OHLCV
from beidou_data.orderbook import (
    OrderBookDiff,
    OrderBookLevel,
    OrderBookManager,
    OrderBookSnapshot,
    WarmingWindow,
)
from beidou_data.quality import AutoRepair, CrossSourceValidator, DataQualityGate
from beidou_data.trading_pool import PoolLifecycle, TradingPoolManager
from beidou_data.trading_pool_lifecycle import InstrumentScore, PoolStatus, TradingPool
from beidou_shared.types import (
    DataQualityTier,
    InstrumentId,
    Price,
    Quantity,
    SchemaVersion,
    VenueId,
    VenueInstrument,
)


def _vi(symbol: str = "BTCUSDT") -> VenueInstrument:
    return VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId(symbol))


def _bar(
    open_time: datetime,
    *,
    closed: bool = True,
    symbol: str = "BTCUSDT",
    close_price: str = "101",
) -> OHLCV:
    vi = _vi(symbol)
    return OHLCV(
        venue_instrument=vi,
        interval="1m",
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open=Price(amount="100"),
        high=Price(amount="102"),
        low=Price(amount="99"),
        close=Price(amount=close_price),
        volume=Quantity(amount="1"),
        is_closed=closed,
    )


def test_canonical_builder_emits_gap_late_duplicate_and_hash_events() -> None:
    builder = CanonicalBarBuilder("1m")
    bars = iter(
        [
            _bar(datetime(2026, 1, 1, tzinfo=timezone.utc)),
            _bar(datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc)),
            _bar(datetime(2026, 1, 1, 0, 11, tzinfo=timezone.utc)),
            _bar(datetime(2026, 1, 1, 0, 11, tzinfo=timezone.utc)),
        ]
    )
    builder._generator = SimpleNamespace(  # type: ignore[assignment]
        process_tick=lambda *_args: next(bars),
        _interval_delta=lambda: timedelta(minutes=1),
    )

    first = builder.process_tick(
        _vi(), Price(amount="100"), Quantity(amount="1"), datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    )
    gap = builder.process_tick(
        _vi(), Price(amount="100"), Quantity(amount="1"), datetime(2026, 1, 1, 0, 11, tzinfo=timezone.utc)
    )
    late = builder.process_tick(
        _vi(), Price(amount="100"), Quantity(amount="1"), datetime(2026, 1, 1, 0, 14, tzinfo=timezone.utc)
    )
    duplicate = builder.process_tick(
        _vi(), Price(amount="100"), Quantity(amount="1"), datetime(2026, 1, 1, 0, 14, tzinfo=timezone.utc)
    )

    assert first is not None and first.event_type == BarEventType.CLOSED
    assert gap is not None and gap.event_type == BarEventType.GAP
    assert late is not None and late.event_type == BarEventType.LATE
    assert duplicate is not None and duplicate.event_type == BarEventType.DUPLICATE
    assert first.event_hash and late.event_hash
    assert len(first.event_hash) == 64
    assert builder.bar_count() == 3
    assert builder.gap_count() == 1
    assert len(builder.events) == 3
    assert len(builder.gaps) == 1
    assert len(builder.closed_bars()) == 1
    assert builder.any_unclosed_or_gapped()


def test_canonical_builder_tracks_unclosed_duplicate_and_registry_reset() -> None:
    builder = CanonicalBarBuilder("1m")
    bar = _bar(datetime(2026, 1, 1, tzinfo=timezone.utc), closed=False)
    gap_bar = _bar(datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc), closed=False)
    bars = iter([bar, gap_bar, gap_bar])
    builder._generator = SimpleNamespace(  # type: ignore[assignment]
        process_tick=lambda *_args: next(bars),
        _interval_delta=lambda: timedelta(minutes=1),
    )
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    builder.process_tick(_vi(), Price(amount="100"), Quantity(amount="1"), timestamp)
    builder.process_tick(_vi(), Price(amount="100"), Quantity(amount="1"), timestamp)
    duplicate = builder.process_tick(_vi(), Price(amount="100"), Quantity(amount="1"), timestamp)
    assert duplicate is not None and duplicate.event_type == BarEventType.DUPLICATE
    assert builder.any_unclosed_or_gapped()

    canonical_bars.reset_bar_builders()
    one = canonical_bars.get_canonical_bar_builder("5m")
    assert one is canonical_bars.get_canonical_bar_builder("5m")
    canonical_bars.reset_bar_builders()
    assert canonical_bars.get_canonical_bar_builder("5m") is not one


def test_canonical_builder_handles_no_completed_tick_and_invalid_interval() -> None:
    builder = CanonicalBarBuilder("1m")
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert builder.process_tick(_vi(), Price(amount="100"), Quantity(amount="1"), timestamp) is None
    assert builder.events == []
    with pytest.raises(ValueError):
        CanonicalBarBuilder("invalid")


def test_orderbook_manager_handles_snapshot_sequences_updates_and_resync() -> None:
    empty = OrderBookSnapshot("BTCUSDT", "BINANCE", [], [])
    assert empty.best_bid() == 0.0
    assert empty.best_ask() == float("inf")
    assert empty.mid_price() == float("inf")
    zero_ask = OrderBookSnapshot("BTCUSDT", "BINANCE", [OrderBookLevel(100.0, 1.0)], [OrderBookLevel(0.0, 1.0)])
    assert zero_ask.spread_bps() == 0.0

    manager = OrderBookManager(max_depth=2)
    diff = OrderBookDiff("BTCUSDT", 1, 0, [], [])
    assert manager.needs_resync()
    assert manager.apply_diff(diff) is False
    manager.apply_snapshot(
        OrderBookSnapshot(
            "BTCUSDT",
            "BINANCE",
            [OrderBookLevel(100.0, 1.0)],
            [OrderBookLevel(101.0, 1.0)],
            sequence=10,
        )
    )
    assert not manager.needs_resync()
    assert manager.apply_diff(OrderBookDiff("BTCUSDT", 10, 9, [], [])) is True
    assert manager.apply_diff(OrderBookDiff("BTCUSDT", 12, 10, [], [])) is False
    assert manager.apply_diff(OrderBookDiff("BTCUSDT", 11, 10, [], [])) is True

    updated = manager.get_snapshot()
    assert updated is not None and updated.sequence == 11
    assert manager.apply_diff(
        OrderBookDiff(
            "BTCUSDT",
            12,
            11,
            [OrderBookLevel(0.0, 0.0), OrderBookLevel(99.0, 0.0), OrderBookLevel(102.0, 2.0)],
            [OrderBookLevel(0.0, 0.0), OrderBookLevel(101.0, 0.0), OrderBookLevel(98.0, 1.0)],
        )
    )
    updated = manager.get_snapshot()
    assert updated is not None and updated.sequence == 12
    assert [x.price for x in updated.bids] == [102.0, 100.0]
    assert [x.price for x in updated.asks] == [98.0]


def test_warming_window_requires_time_events_and_sequence_validation() -> None:
    window = WarmingWindow("BTCUSDT", min_warmup_seconds=10, min_events=2)
    assert not window.can_trade()
    window.start(100.0)
    window.record_event(105.0)
    assert not window.is_warmed
    window.record_event(110.0)
    assert window.is_warmed
    assert not window.can_trade()
    window.sequence_validated = True
    assert window.can_trade()
    window.start(200.0)
    assert window.events_received == 0 and not window.is_warmed


def test_data_quality_gate_cross_source_and_repair_boundaries() -> None:
    empty = DataQualityGate()
    assert empty.overall_tier() == DataQualityTier.FAIL
    assert not empty.is_safe_for_trading()
    assert not empty.is_safe_for_research()

    gate = DataQualityGate()
    gate.add_freshness_check(1, 5)
    gate.add_sequence_check(2, 2)
    gate.add_outlier_check(0, 0, 0)
    gate.add_outlier_check(10, 0, 1)
    gate.add_clock_skew_check(1)
    assert gate.overall_tier() == DataQualityTier.CONDITIONAL
    assert gate.is_safe_for_research() and not gate.is_safe_for_trading()

    failed = DataQualityGate()
    failed.add_freshness_check(5, 5)
    failed.add_sequence_check(1, 2)
    failed.add_clock_skew_check(5000)
    assert failed.overall_tier() == DataQualityTier.FAIL

    validator = CrossSourceValidator()
    validator.add_source("rest", {"price": 100})
    validator.add_source("ws", {"price": 100})
    assert validator.validate_consistency("price").tier == DataQualityTier.PASS
    validator.add_source("ws", {"price": 101})
    assert validator.validate_consistency("price").tier == DataQualityTier.FAIL

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    series = [{"ts": now}, {"ts": now + timedelta(milliseconds=1000)}, {"ts": now + timedelta(milliseconds=4000)}]
    same, gaps = AutoRepair.repair_gap(series, "ts", 1000)
    assert same is series and len(gaps) == 1
    assert AutoRepair.repair_gap([], "ts", 1000) == ([], [])
    assert AutoRepair.repair_gap([{"ts": 1}, {"ts": 2}], "ts", 1)[1] == []


def _feature(
    ts: datetime, *, available: datetime | None = None, tier: str = "PASS", version: str = "1"
) -> FeatureVector:
    return FeatureVector(
        name="rsi",
        values={"value": 1.0},
        timestamp=ts,
        instrument_id=InstrumentId("BTCUSDT"),
        venue_id=VenueId("BINANCE"),
        version=SchemaVersion(version),
        available_at=available,
        data_quality_tier=tier,
    )


def test_feature_store_bounds_consistency_pit_and_safe_counts() -> None:
    store = FeatureStore()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(201):
        store.store(_feature(start + timedelta(minutes=i), available=start + timedelta(minutes=i)))
    assert len(store._features["rsi:BTCUSDT"]) == 100
    assert store.get_latest("rsi", InstrumentId("BTCUSDT")) is not None
    assert (
        len(
            store.get_range(
                "rsi", InstrumentId("BTCUSDT"), start + timedelta(minutes=100), start + timedelta(minutes=200)
            )
        )
        == 100
    )
    assert store.check_consistency("rsi", InstrumentId("BTCUSDT"))
    store.store(_feature(start + timedelta(minutes=300), available=start + timedelta(minutes=300), version="2"))
    assert not store.check_consistency("rsi", InstrumentId("BTCUSDT"))

    assert store.get_as_of(
        "rsi", InstrumentId("BTCUSDT"), start + timedelta(minutes=150)
    ).timestamp == start + timedelta(minutes=150)
    store.store(_feature(start + timedelta(minutes=301), available=start + timedelta(minutes=301), tier="UNKNOWN"))
    store.store(_feature(start + timedelta(minutes=302), available=None, tier="BLOCK"))
    assert store.get_as_of(
        "rsi", InstrumentId("BTCUSDT"), start + timedelta(minutes=302)
    ).timestamp == start + timedelta(minutes=300)
    assert store.count_safe_for_trading() == 101
    assert (
        FeatureVector(
            "x", {}, start, InstrumentId("BTCUSDT"), VenueId("BINANCE"), SchemaVersion("1"), data_quality_tier="BLOCK"
        ).is_safe_for_trading
        is False
    )


def _high_score(symbol: str = "BTCUSDT", value: float = 0.9) -> InstrumentScore:
    return InstrumentScore(
        instrument_id=symbol,
        spread_score=value,
        depth_score=value,
        volume_score=value,
        stability_score=value,
        capacity_score=value,
    )


def test_trading_pool_restores_state_persists_failures_and_capacity() -> None:
    restored = TradingPool(
        initial_state=[
            {"instrument_id": "", "status": "ACTIVE"},
            {
                "instrument_id": "ETHUSDT",
                "status": "INVALID",
                "score_detail": {"observing_since": "bad", "scores": ["bad", 0.8]},
            },
        ]
    )
    assert restored._pool["ETHUSDT"].status is PoolStatus.OBSERVING
    assert [s.overall for s in restored._pool["ETHUSDT"].scores] == [0.8]

    def fail_sink(_event: dict) -> None:
        raise RuntimeError("disk unavailable")

    pool = TradingPool(event_sink=fail_sink, max_instruments=1)
    entry = pool.add("BTCUSDT")
    assert pool.seed_historical_observation("BTCUSDT", 0.7, evidence={"days": "bad"}) is False
    assert pool.seed_historical_observation("UNKNOWN", 0.9, evidence={"days": 2}) is False
    pool.score("UNKNOWN", _high_score("UNKNOWN"))
    pool.set_score_weights({"spread": "bad", "depth": 0.2, "volume": 0.2, "stability": 0.2, "capacity": 0.2})
    entry.observing_since = datetime.now(timezone.utc) - timedelta(hours=48)
    pool.score("BTCUSDT", _high_score())
    assert pool.try_promote("BTCUSDT")
    assert pool.activate("BTCUSDT")
    assert pool.active_instruments() == ["BTCUSDT"]
    assert pool.is_tradable("BTCUSDT")
    pool.set_max_position_notional("BTCUSDT", 100.0)
    pool.update_capacity("BTCUSDT", 150.0)
    assert not pool.is_tradable("BTCUSDT")
    pool.update_capacity("BTCUSDT", -1.0)
    assert pool._pool["BTCUSDT"].capacity_used_pct == 0.0
    pool.set_max_position_notional("UNKNOWN", 10.0)


def test_trading_pool_promotion_guards_activation_limits_and_recovery() -> None:
    pool = TradingPool(max_instruments=0)
    assert not pool.try_promote("UNKNOWN")
    entry = pool.add("BTCUSDT")
    assert not pool.try_promote("BTCUSDT")
    entry.observing_since = datetime.now(timezone.utc) - timedelta(hours=48)
    entry.scores.append(InstrumentScore("BTCUSDT", overall=0.5))
    assert not pool.try_promote("BTCUSDT")
    entry.scores[-1].spread_score = float("nan")
    entry.scores[-1].overall = 0.7
    assert not pool.try_promote("BTCUSDT")

    entry.status = PoolStatus.PROMOTED
    assert not pool.activate("UNKNOWN")
    assert not pool.activate("BTCUSDT")  # capacity is zero
    pool.quarantine("BTCUSDT", "manual")
    assert not pool.is_tradable("BTCUSDT")
    pool.quarantine("UNKNOWN", "ignored")

    recovering = TradingPool()
    rec = recovering.add("ETHUSDT")
    rec.status = PoolStatus.QUARANTINED
    rec.scores = [_high_score("ETHUSDT") for _ in range(2)]
    for score in rec.scores:
        score.compute_overall()
    assert not recovering.try_promote("ETHUSDT")
    good = _high_score("ETHUSDT")
    good.compute_overall()
    rec.scores.append(good)
    assert not recovering.try_promote("ETHUSDT")
    assert rec.status is PoolStatus.OBSERVING


def test_trading_pool_restores_timestamps_and_rejects_promotion_states() -> None:
    observed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    promoted = datetime(2026, 1, 2, tzinfo=timezone.utc)
    pool = TradingPool(
        initial_state=[
            {
                "instrument_id": "SOLUSDT",
                "status": PoolStatus.PROMOTED.value,
                "score_detail": {
                    "observing_since": observed.isoformat(),
                    "promoted_at": promoted.isoformat(),
                    "quarantine_reason": "old",
                    "historical_seed": {"days": 2},
                    "scores": [0.7],
                },
            }
        ]
    )
    entry = pool._pool["SOLUSDT"]
    assert entry.observing_since == observed
    assert entry.promoted_at == promoted
    assert entry.quarantine_reason == "old"
    assert entry.historical_seed == {"days": 2}

    entry.status = PoolStatus.ACTIVE
    entry.observing_since = datetime.now(timezone.utc) - timedelta(hours=48)
    assert pool.try_promote("SOLUSDT") is False
    entry.status = PoolStatus.OBSERVING
    entry.scores.clear()
    assert pool.try_promote("SOLUSDT") is False
    entry.scores.append(InstrumentScore("SOLUSDT", overall=1.5))
    assert pool.try_promote("SOLUSDT") is False


def test_data_contracts_datasets_and_legacy_pool_cover_hash_and_capacity_edges() -> None:
    event = CanonicalMarketEvent(
        symbol="BTCUSDT",
        event_type=EventType.TICK,
        timestamp=607.0,
        bucket_start=600.0,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=2.0,
    )
    assert len(event.compute_hash()) == 64
    assert CanonicalMarketEvent.floor_to_bucket(607.0) == 600.0
    checks = [
        DQCheck(check_id=check_id, name=check_id, status=DQStatus.PASS) for check_id in DQSnapshot.REQUIRED_CHECKS
    ]
    snapshot = DQSnapshot("BTCUSDT", checks=checks, warmup_complete=True)
    assert snapshot.has_warmup_data() and len(snapshot.compute_hash()) == 64
    universe = PITUniverseSnapshot(
        universe_id="u1",
        entries=[
            UniverseEntry(
                "BTCUSDT", funding_rate=0.0, open_interest=0.0, capacity_score=0.0, dq_ok=True, is_executable=True
            ),
            UniverseEntry("ETHUSDT", exclude_reason="blocked"),
        ],
    )
    assert universe.any_unknown_critical() == []
    assert len(universe.compute_hash()) == 64

    datasets = DatasetManager()
    assert datasets.get_latest("missing") is None
    first = datasets.create_version("bars", None, sample_count=1)
    second = datasets.create_version("bars", first.version, sample_count=2)
    assert datasets.get_latest("bars") is second
    assert datasets.get_version("bars", first.version) is first
    assert datasets.get_version("bars", SchemaVersion("missing")) is None
    assert datasets.compute_checksum(b"bars")

    manager = TradingPoolManager()
    vi = _vi()
    assert manager.add_instrument(vi, 100.0, 10.0).value == "SUCCESS"
    assert manager.add_instrument(vi, 100.0, 10.0).value == "ERROR"
    assert manager.promote(_vi("UNKNOWN"), PoolLifecycle.ACTIVE).value == "UNKNOWN"
    assert manager.quarantine(_vi("UNKNOWN")).value == "UNKNOWN"
    assert manager.check_capacity(_vi("UNKNOWN"), 1).value == "UNKNOWN"
    assert manager.check_capacity(vi, 11).value == "ERROR"
    assert manager.promote(vi, PoolLifecycle.EXIT_ONLY).value == "SUCCESS"
    assert manager.check_capacity(vi, 1).value == "ERROR"
    manager.promote(vi, PoolLifecycle.ACTIVE)
    manager.update_capacity(vi, 100.0)
    assert manager.check_capacity(vi, 1).value == "ERROR"
    manager.reset_capacity(vi)
    manager.update_capacity(_vi("UNKNOWN"), 10.0)
    manager.reset_capacity(_vi("UNKNOWN"))


def test_kline_generator_rejects_replays_updates_and_revises_slots_bars() -> None:
    from beidou_data.klines import KLineGenerator

    generator = KLineGenerator("5m")
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    vi = _vi()
    assert generator.update(100.0, 1.0, timestamp, symbol="btcusdt") is None
    assert generator.get_current_bar(vi) is not None
    assert (
        generator.process_tick(vi, Price(amount="99"), Quantity(amount="1"), timestamp - timedelta(seconds=1)) is None
    )
    assert generator.rejected_ticks == 1
    closed = generator.process_tick(
        vi, Price(amount="101"), Quantity(amount="1"), timestamp + timedelta(minutes=5, seconds=1)
    )
    assert closed is not None and generator.get_klines(vi)
    generator._current.clear()
    assert (
        generator.process_tick(vi, Price(amount="99"), Quantity(amount="1"), timestamp - timedelta(seconds=1)) is None
    )
    assert generator.rejected_ticks == 2
    revised = generator.revise(vi, closed.open_time, _bar(closed.open_time, close_price="103"))
    assert revised.revision_number == closed.revision_number + 1
    with pytest.raises(ValueError):
        generator.revise(vi, timestamp - timedelta(days=1), _bar(timestamp))

    hourly = KLineGenerator("1h")
    hourly.process_tick(vi, Price(amount="100"), Quantity(amount="1"), timestamp.replace(minute=30))
    assert hourly.get_current_bar(vi) is not None
