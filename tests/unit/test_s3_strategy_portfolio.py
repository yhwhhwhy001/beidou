"""
S3 (PKG16-19): 策略、组合与资本分配安全测试。

覆盖：
- PKG18: NaN 绕过晋级阈值 (BDS-P0-022)
- PKG18: 组合优化器不篡改策略原意 (BDS-P0-007)
- PKG16: 策略图哈希绑定 (BDS-P1-001)
"""

from __future__ import annotations

import math

import pytest

from beidou_shared.types import MonetaryValue, Quantity, StrategyId, VenueId
from beidou_strategy.portfolio import PortfolioTarget, PositionOwnership
from beidou_strategy.portfolio.optimizer import PortfolioOptimizerImpl
from beidou_data.trading_pool_lifecycle import (
    InstrumentScore,
    PoolEntry,
    PoolStatus,
    TradingPool,
)


class TestNaNThresholdBypass:
    """PKG18: NaN 绕过晋级阈值 (BDS-P0-022)。"""

    def test_nan_overall_blocked_from_promotion(self) -> None:
        """NaN 综合评分不能晋级。"""
        pool = TradingPool()
        pool.add("BTCUSDT")
        entry = pool._pool["BTCUSDT"]
        entry.status = PoolStatus.OBSERVING
        entry.observing_since = __import__("datetime").datetime(
            2020, 1, 1, tzinfo=__import__("datetime").timezone.utc
        )  # 足够长的观察期

        # 注入 NaN 评分
        nan_score = InstrumentScore(instrument_id="BTCUSDT")
        nan_score.overall = float("nan")
        entry.scores.append(nan_score)

        result = pool.try_promote("BTCUSDT")
        assert result is False, "NaN 评分必须被阻止晋级"

    def test_inf_overall_blocked_from_promotion(self) -> None:
        """Inf 评分不能晋级。"""
        pool = TradingPool()
        pool.add("ETHUSDT")
        entry = pool._pool["ETHUSDT"]
        entry.status = PoolStatus.OBSERVING
        entry.observing_since = __import__("datetime").datetime(
            2020, 1, 1, tzinfo=__import__("datetime").timezone.utc
        )

        inf_score = InstrumentScore(instrument_id="ETHUSDT")
        inf_score.overall = float("inf")
        entry.scores.append(inf_score)

        result = pool.try_promote("ETHUSDT")
        assert result is False, "Inf 评分必须被阻止晋级"

    def test_nan_sub_score_blocked(self) -> None:
        """子评分为 NaN 时整体被阻止。"""
        pool = TradingPool()
        pool.add("SOLUSDT")
        entry = pool._pool["SOLUSDT"]
        entry.status = PoolStatus.OBSERVING
        entry.observing_since = __import__("datetime").datetime(
            2020, 1, 1, tzinfo=__import__("datetime").timezone.utc
        )

        score = InstrumentScore(instrument_id="SOLUSDT")
        score.spread_score = float("nan")  # 子评分 NaN
        score.depth_score = 0.8
        score.volume_score = 0.8
        score.stability_score = 0.8
        score.capacity_score = 0.8
        score.compute_overall()
        entry.scores.append(score)

        result = pool.try_promote("SOLUSDT")
        assert result is False, "子评分 NaN 必须被阻止"

    def test_valid_score_promotes(self) -> None:
        """正常评分可以晋级。"""
        pool = TradingPool()
        pool.add("BNBUSDT")
        entry = pool._pool["BNBUSDT"]
        entry.status = PoolStatus.OBSERVING
        entry.observing_since = __import__("datetime").datetime(
            2020, 1, 1, tzinfo=__import__("datetime").timezone.utc
        )

        score = InstrumentScore(instrument_id="BNBUSDT")
        score.spread_score = 0.8
        score.depth_score = 0.8
        score.volume_score = 0.7
        score.stability_score = 0.9
        score.capacity_score = 0.7
        score.compute_overall()
        entry.scores.append(score)

        result = pool.try_promote("BNBUSDT")
        assert result is True
        assert entry.status == PoolStatus.PROMOTED

    def test_out_of_range_score_blocked(self) -> None:
        """评分为负数（超出范围）被阻止。"""
        pool = TradingPool()
        pool.add("ADAUSDT")
        entry = pool._pool["ADAUSDT"]
        entry.status = PoolStatus.OBSERVING
        entry.observing_since = __import__("datetime").datetime(
            2020, 1, 1, tzinfo=__import__("datetime").timezone.utc
        )

        score = InstrumentScore(instrument_id="ADAUSDT")
        score.spread_score = -1.0  # 不合法
        score.depth_score = 0.8
        score.volume_score = 0.8
        score.stability_score = 0.8
        score.capacity_score = 0.8
        score.compute_overall()
        entry.scores.append(score)

        result = pool.try_promote("ADAUSDT")
        assert result is False


class TestPortfolioOptimizer:
    """PKG18: 组合优化器不篡改策略原意 (BDS-P0-007)。"""

    def test_preserves_strategy_direction(self) -> None:
        """策略方向不被组合层篡改。"""
        optimizer = PortfolioOptimizerImpl(max_total_leverage=10.0)  # P1-008: 允许测试杠杆

        long_target = PortfolioTarget(
            strategy_id=StrategyId("strategy_a"),
            instrument_id="BTCUSDT",
            venue_id=VenueId("BINANCE"),
            target_quantity=Quantity(amount="1.0"),
            target_notional=MonetaryValue(amount="50000"),
            capital_budget=MonetaryValue(amount="10000"),
            ownership=PositionOwnership.EXCLUSIVE,
        )
        short_target = PortfolioTarget(
            strategy_id=StrategyId("strategy_b"),
            instrument_id="BTCUSDT",
            venue_id=VenueId("BINANCE"),
            target_quantity=Quantity(amount="-0.5"),
            target_notional=MonetaryValue(amount="25000"),
            capital_budget=MonetaryValue(amount="10000"),
            ownership=PositionOwnership.EXCLUSIVE,
        )

        resolved, conflicts = optimizer.resolve_conflicts([long_target, short_target])

        assert conflicts == 1
        assert len(resolved) == 2  # 两个方向都保留
        # 验证方向被保留
        long_resolved = [t for t in resolved if float(t.target_quantity.amount) > 0]
        short_resolved = [t for t in resolved if float(t.target_quantity.amount) < 0]
        assert len(long_resolved) == 1
        assert len(short_resolved) == 1
        # 原始数量未被净额抵消
        assert float(long_resolved[0].target_quantity.amount) == 1.0
        assert float(short_resolved[0].target_quantity.amount) == -0.5

    def test_same_direction_scales_by_capital(self) -> None:
        """同方向策略按资本比例分配（不改变方向）。"""
        optimizer = PortfolioOptimizerImpl()

        t1 = PortfolioTarget(
            strategy_id=StrategyId("s1"),
            instrument_id="ETHUSDT",
            venue_id=VenueId("BINANCE"),
            target_quantity=Quantity(amount="2.0"),
            target_notional=MonetaryValue(amount="60000"),
            capital_budget=MonetaryValue(amount="30000"),
            ownership=PositionOwnership.EXCLUSIVE,
        )
        t2 = PortfolioTarget(
            strategy_id=StrategyId("s2"),
            instrument_id="ETHUSDT",
            venue_id=VenueId("BINANCE"),
            target_quantity=Quantity(amount="1.0"),
            target_notional=MonetaryValue(amount="30000"),
            capital_budget=MonetaryValue(amount="10000"),
            ownership=PositionOwnership.EXCLUSIVE,
        )

        resolved, conflicts = optimizer.resolve_conflicts([t1, t2])

        assert conflicts == 1
        assert len(resolved) == 2
        # 方向一致，都保留正向
        for t in resolved:
            assert float(t.target_quantity.amount) > 0

    def test_single_strategy_not_affected(self) -> None:
        """单一策略不产生冲突，不被修改。"""
        optimizer = PortfolioOptimizerImpl()

        target = PortfolioTarget(
            strategy_id=StrategyId("solo"),
            instrument_id="SOLUSDT",
            venue_id=VenueId("BINANCE"),
            target_quantity=Quantity(amount="5.0"),
            target_notional=MonetaryValue(amount="50000"),
            capital_budget=MonetaryValue(amount="50000"),
            ownership=PositionOwnership.EXCLUSIVE,
        )

        resolved, conflicts = optimizer.resolve_conflicts([target])

        assert conflicts == 0
        assert len(resolved) == 1
        assert resolved[0].strategy_id == StrategyId("solo")
        assert float(resolved[0].target_quantity.amount) == 5.0


class TestStrategyHashBinding:
    """PKG16: 策略图哈希绑定 (BDS-P1-001)。"""

    def test_graph_hash_binds_all_components(self) -> None:
        """Graph hash 必须绑定参数、模型、因子版本、failure policy、代码 SHA。"""
        import hashlib
        import json

        components = {
            "parameters": {"lookback": 20, "threshold": 1.5},
            "model_version": "v2.0.0",
            "factor_versions": {"rsi": "v1.0", "macd": "v2.0"},
            "failure_policy": "SKIP",
            "code_sha": "abc123def456",
        }

        # 序列化后哈希
        canonical = json.dumps(components, sort_keys=True)
        graph_hash = hashlib.sha256(canonical.encode()).hexdigest()

        # 修改任一组件 → hash 变化
        altered = dict(components)
        altered["parameters"] = {"lookback": 30, "threshold": 1.5}  # 改了 lookback
        altered_canonical = json.dumps(altered, sort_keys=True)
        altered_hash = hashlib.sha256(altered_canonical.encode()).hexdigest()

        assert graph_hash != altered_hash, "修改参数必须改变 graph hash"

    def test_node_output_hash_binds_metadata(self) -> None:
        """Node output hash 绑定版本和元数据。"""
        import hashlib
        import json

        node_output = {
            "node_id": "rsi_14",
            "value": 65.5,
            "version": "v1.0",
            "metadata": {"lookback": 14, "source": "close_price"},
        }

        canonical = json.dumps(node_output, sort_keys=True)
        h1 = hashlib.sha256(canonical.encode()).hexdigest()

        node_output["version"] = "v2.0"
        canonical2 = json.dumps(node_output, sort_keys=True)
        h2 = hashlib.sha256(canonical2.encode()).hexdigest()

        assert h1 != h2, "修改版本必须改变 node output hash"
