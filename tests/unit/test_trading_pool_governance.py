"""交易池治理测试（M02）。

覆盖: 历史预筛选观察期证据派生（F01）、评分权重策略覆盖（F05）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from beidou_data.trading_pool_lifecycle import InstrumentScore, PoolStatus, TradingPool


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


def test_score_weights_sum_below_promote_threshold_rejected() -> None:
    """M02-R2 (CE-1): 权重和 <0.6 时满分也永远无法晋级 → 拒绝该权重表。"""
    pool = TradingPool()
    pool.set_score_weights({"spread": 0.1, "depth": 0.1, "volume": 0.1, "stability": 0.1, "capacity": 0.1})
    assert pool._score_weights is None


def test_score_weights_sum_above_one_rejected() -> None:
    """M02-R2 (CE-1): 权重和 >1.0 时 overall 越界触发 range 检查 → 拒绝。"""
    pool = TradingPool()
    pool.set_score_weights({"spread": 0.3, "depth": 0.3, "volume": 0.2, "stability": 0.2, "capacity": 0.2})
    assert pool._score_weights is None


def test_observation_hours_env_clamped_to_minimum(monkeypatch) -> None:
    """M02-R2 (CE-2): BEIDOU_MIN_OBSERVATION_HOURS=0 不得完全绕过观察期。"""
    import beidou_data.trading_pool_lifecycle as tpl

    monkeypatch.setenv("BEIDOU_MIN_OBSERVATION_HOURS", "0.0")
    assert tpl._default_observation_hours() == 1.0  # 钳制到 1h,而非 0
    monkeypatch.setenv("BEIDOU_MIN_OBSERVATION_HOURS", "6.0")
    assert tpl._default_observation_hours() == 6.0
    monkeypatch.setenv("BEIDOU_MIN_OBSERVATION_HOURS", "invalid")
    assert tpl._default_observation_hours() == 24.0


def test_seed_failure_leaves_no_historical_seed() -> None:
    """M02-R2 (CE-4): 校验失败路径不得留下"已种子"的假审计记录。"""
    pool = TradingPool()
    entry = pool.add("BTCUSDT")
    assert pool.seed_historical_observation("BTCUSDT", 0.7, evidence={}) is False
    assert entry.historical_seed is None


def test_historical_seed_persisted_and_restored() -> None:
    """M02-R2 (CE-4): 历史种子证据必须持久化并恢复（审计链跨重启）。"""
    events: list[dict] = []
    pool = TradingPool(event_sink=events.append)
    pool.add("BTCUSDT")
    assert pool.seed_historical_observation("BTCUSDT", 0.7, evidence={"days": 100}) is True
    assert events and isinstance(events[-1]["score_detail"].get("historical_seed"), dict)

    restored = TradingPool(initial_state=[events[-1]])
    entry = restored._pool.get("BTCUSDT")
    assert entry is not None and entry.historical_seed is not None
    assert entry.historical_seed["observation_backdate_hours"] == 2400.0


def test_pool_version_and_membership_diff_survive_restart() -> None:
    events: list[dict] = []
    pool = TradingPool(event_sink=events.append)
    entry = pool.add("BTCUSDT")
    entry.min_observation_hours = 1.0
    assert pool.seed_historical_observation("BTCUSDT", 0.9, evidence={"days": 2})
    good = InstrumentScore(
        instrument_id="BTCUSDT",
        spread_score=1.0,
        depth_score=1.0,
        volume_score=1.0,
        stability_score=1.0,
        capacity_score=1.0,
    )
    pool.score("BTCUSDT", good)
    assert pool.try_promote("BTCUSDT")
    assert pool.activate("BTCUSDT")
    active = pool.snapshot()
    assert active.membership_diff["active_added"] == ["BTCUSDT"]
    version_before = active.version
    assert events[-1]["pool_version"] == version_before

    restored = TradingPool(initial_state=[events[-1]])
    assert restored.snapshot().version >= version_before
    restored.quarantine("BTCUSDT", "test")
    quarantined = restored.snapshot()
    assert quarantined.version > version_before
    assert quarantined.membership_diff == {
        "active_added": [],
        "active_removed": ["BTCUSDT"],
        "quarantined_added": ["BTCUSDT"],
        "quarantined_removed": [],
    }


def test_quarantine_regression_clears_historical_seed() -> None:
    """M02-R2 (CE-5): QUARANTINED 回归后重新计时,历史种子证据清除。"""
    pool = TradingPool()
    entry = pool.add("BTCUSDT")
    pool.seed_historical_observation("BTCUSDT", 0.7, evidence={"days": 100})
    entry.status = PoolStatus.ACTIVE
    for _ in range(3):
        score = InstrumentScore(instrument_id="BTCUSDT")
        score.spread_score = score.depth_score = score.volume_score = 0.0
        score.stability_score = score.capacity_score = 0.0
        pool.score("BTCUSDT", score)
    assert entry.status is PoolStatus.QUARANTINED
    for _ in range(3):
        good = InstrumentScore(instrument_id="BTCUSDT")
        good.spread_score = good.depth_score = good.volume_score = 0.9
        good.stability_score = good.capacity_score = 0.9
        pool.score("BTCUSDT", good)
    assert pool.try_promote("BTCUSDT") is False  # 观察期重启计时(24h 未满)
    assert entry.status is PoolStatus.OBSERVING
    assert entry.historical_seed is None  # 审计一致:回归后不再声称 8760h 观察
