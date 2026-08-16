"""交易池治理测试（M02）。

覆盖: 历史预筛选观察期证据派生（F01）、评分权重策略覆盖（F05）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from beidou_data.trading_pool_lifecycle import InstrumentScore, TradingPool


def test_seed_backdates_by_evidence_days() -> None:
    """M02-F01: 观察期起点派生自证据数据天数（非 365d 魔数）。"""
    pool = TradingPool()
    entry = pool.add("BTCUSDT")
    before = datetime.now(timezone.utc)
    assert pool.seed_historical_observation("BTCUSDT", 0.7, evidence={"days": 100}) is True
    backdated_hours = (before - entry.observing_since).total_seconds() / 3600
    assert 99 * 24 <= backdated_hours <= 101 * 24  # ~100 天,而非 365
    assert entry.historical_seed is not None
    assert entry.historical_seed["observation_backdate_hours"] == 2400.0


def test_seed_backdate_clamped_to_365_days() -> None:
    pool = TradingPool()
    entry = pool.add("BTCUSDT")
    before = datetime.now(timezone.utc)
    assert pool.seed_historical_observation("BTCUSDT", 0.7, evidence={"days": 1000}) is True
    backdated_hours = (before - entry.observing_since).total_seconds() / 3600
    assert backdated_hours <= 365 * 24 + 1


def test_seed_without_days_evidence_does_not_accelerate() -> None:
    """无数据天数证据 → 不加速观察期（保守 24h 语义,不伪造已观察）。"""
    pool = TradingPool()
    pool.add("BTCUSDT")
    assert pool.seed_historical_observation("BTCUSDT", 0.7, evidence={}) is False


def test_seed_below_threshold_rejected() -> None:
    pool = TradingPool()
    pool.add("BTCUSDT")
    assert pool.seed_historical_observation("BTCUSDT", 0.3, evidence={"days": 100}) is False


def test_score_weights_policy_override() -> None:
    """M02-F05: 有效权重覆盖默认;非法权重保持默认。"""
    pool = TradingPool()
    pool.set_score_weights({"spread": 0.5, "depth": 0.5, "volume": 0.0, "stability": 0.0, "capacity": 0.0})
    score = InstrumentScore(
        instrument_id="BTCUSDT",
        spread_score=0.8,
        depth_score=0.6,
        volume_score=0.9,
        stability_score=0.9,
        capacity_score=0.9,
    )
    score.compute_overall(pool._score_weights)
    assert score.overall == 0.8 * 0.5 + 0.6 * 0.5

    pool_bad = TradingPool()
    pool_bad.set_score_weights({"spread": 1.5, "depth": 0.0, "volume": 0.0, "stability": 0.0, "capacity": 0.0})
    assert pool_bad._score_weights is None
    pool_missing_key = TradingPool()
    pool_missing_key.set_score_weights({"spread": 0.5})
    assert pool_missing_key._score_weights is None
