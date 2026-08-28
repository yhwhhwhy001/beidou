"""PKG-11~19: Strategy & Data 测试。数据集、交易池、特征、成本、融合、组合。"""

from datetime import datetime, timezone

from beidou_data.datasets import DatasetManager
from beidou_data.feature_store import FeatureStore, FeatureVector
from beidou_data.trading_pool import PoolLifecycle, TradingPoolManager
from beidou_data.trading_pool_lifecycle import discover_startup_candidates
from beidou_shared.types import (
    InstrumentId,
    MonetaryValue,
    Quantity,
    SchemaVersion,
    StrategyId,
    VenueId,
    VenueInstrument,
)
from beidou_strategy.alpha import AlphaComponentType, AlphaSignal, SignalDirection
from beidou_strategy.alpha.signal_fusion import SignalFuser
from beidou_strategy.portfolio import PortfolioTarget, PositionOwnership
from beidou_strategy.portfolio.optimizer import PortfolioOptimizerImpl
from beidou_strategy.state.cost_model import CostModel
from beidou_strategy.state.market_state import (
    DirectionState,
    MarketStateVector,
    QualityState,
    StressState,
)


class TestDatasetManager:
    def test_create_and_retrieve(self):
        dm = DatasetManager()
        dm.create_version("btc_1m", None, instrument_ids=frozenset({InstrumentId("BTCUSDT")}), sample_count=1000)
        assert dm.get_latest("btc_1m") is not None


class TestTradingPool:
    def test_startup_candidates_are_discovered_without_fixed_symbols(self):
        exchange_info = {
            "symbols": [
                {"symbol": "ETHUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
                {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
                {"symbol": "BTCUSD", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USD"},
                {"symbol": "ADAUSDT", "status": "BREAK", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
                {
                    "symbol": "ETHUSDT_220930",
                    "status": "TRADING",
                    "contractType": "CURRENT_QUARTER",
                    "quoteAsset": "USDT",
                },
            ]
        }

        assert discover_startup_candidates(exchange_info, max_instruments=1) == ["BTCUSDT"]
        assert discover_startup_candidates(exchange_info, max_instruments=10) == ["BTCUSDT", "ETHUSDT"]
        assert discover_startup_candidates(
            exchange_info,
            max_instruments=10,
            preserve_exchange_order=True,
        ) == ["ETHUSDT", "BTCUSDT"]

    def test_lifecycle(self):
        mgr = TradingPoolManager()
        vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        mgr.add_instrument(vi, 100000, 1000)
        mgr.promote(vi, PoolLifecycle.ACTIVE)
        assert len(mgr.get_active()) == 1

    def test_capacity_check(self):
        mgr = TradingPoolManager()
        vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        mgr.add_instrument(vi, 100000, 1000)
        from beidou_shared.types import ResultStatus

        mgr.promote(vi, PoolLifecycle.ACTIVE)
        assert mgr.check_capacity(vi, 500) == ResultStatus.SUCCESS
        assert mgr.check_capacity(vi, 2000) == ResultStatus.ERROR

    def test_quarantine(self):
        mgr = TradingPoolManager()
        vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("ETHUSDT"))
        mgr.add_instrument(vi, 50000, 500)
        mgr.quarantine(vi, "Suspicious activity")
        assert len(mgr.get_active()) == 0


class TestFeatureStore:
    def test_store_and_retrieve(self):
        fs = FeatureStore()
        fv = FeatureVector(
            name="rsi_14",
            values={"value": 65.5},
            timestamp=datetime.now(timezone.utc),
            instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("BINANCE"),
            version=SchemaVersion("2.0.0"),
        )
        fs.store(fv)
        assert fs.get_latest("rsi_14", InstrumentId("BTCUSDT")) is not None


class TestMarketState:
    def test_crisis_overrides_direction(self):
        state = MarketStateVector(
            direction=DirectionState(regime="TRENDING_UP", probability=0.8, uncertainty=0.2),
            stress=StressState(level="CRISIS", probability=0.9),
            quality=QualityState(tier="GOOD"),
            venue_id=VenueId("BINANCE"),
            instrument_id=InstrumentId("BTCUSDT"),
        )
        assert state.should_override_direction()

    def test_normal_does_not_override(self):
        state = MarketStateVector(
            direction=DirectionState(regime="TRENDING_UP", probability=0.8, uncertainty=0.2),
            stress=StressState(level="NORMAL", probability=0.1),
            quality=QualityState(tier="GOOD"),
            venue_id=VenueId("BINANCE"),
            instrument_id=InstrumentId("BTCUSDT"),
        )
        assert not state.should_override_direction()


class TestCostModel:
    def test_total_cost(self):
        model = CostModel()
        model.set_fee_tier(VenueId("BINANCE"), "vip1", 2.0, 4.0)
        est = model.estimate(InstrumentId("BTCUSDT"), VenueId("BINANCE"), 10000, 10000000, 1.0, 0.02)
        assert est.total_cost_bps(is_taker=True) > 0

    def test_funding_rate_warning(self):
        model = CostModel()
        assert model.funding_rate_warning(0.002) == "HIGH_FUNDING_RATE"
        assert model.funding_rate_warning(0.0003) == "NORMAL"


class TestSignalFusion:
    def test_fuse_agreement(self):
        fuser = SignalFuser()
        s1 = AlphaSignal(
            strategy_id=StrategyId("s1"),
            component_type=AlphaComponentType.ENTRY,
            direction=SignalDirection.LONG,
            strength=0.8,
            confidence=0.9,
            instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("BINANCE"),
            model_version=SchemaVersion("2.0.0"),
        )
        s2 = AlphaSignal(
            strategy_id=StrategyId("s2"),
            component_type=AlphaComponentType.ENTRY,
            direction=SignalDirection.LONG,
            strength=0.6,
            confidence=0.7,
            instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("BINANCE"),
            model_version=SchemaVersion("2.0.0"),
        )
        fused = fuser.fuse([s1, s2])
        assert fused.direction == SignalDirection.LONG
        assert not fused.conflict_detected

    def test_detect_conflict(self):
        fuser = SignalFuser()
        s1 = AlphaSignal(
            strategy_id=StrategyId("s1"),
            component_type=AlphaComponentType.ENTRY,
            direction=SignalDirection.LONG,
            strength=0.8,
            confidence=0.9,
            instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("BINANCE"),
            model_version=SchemaVersion("2.0.0"),
        )
        s2 = AlphaSignal(
            strategy_id=StrategyId("s2"),
            component_type=AlphaComponentType.ENTRY,
            direction=SignalDirection.SHORT,
            strength=0.7,
            confidence=0.8,
            instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("BINANCE"),
            model_version=SchemaVersion("2.0.0"),
        )
        fused = fuser.fuse([s1, s2])
        assert fused.conflict_detected


class TestPortfolioOptimizer:
    def test_capital_allocation_equal(self):
        opt = PortfolioOptimizerImpl()
        alloc = opt.allocate_capital([StrategyId("s1"), StrategyId("s2")], MonetaryValue(amount="100000"))
        assert float(alloc[StrategyId("s1")].amount) == 50000

    def test_exit_protection_shared_position(self):
        opt = PortfolioOptimizerImpl()
        t = PortfolioTarget(
            strategy_id=StrategyId("s1"),
            instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("BINANCE"),
            target_quantity=Quantity(amount="0.1"),
            target_notional=MonetaryValue(amount="5000"),
            capital_budget=MonetaryValue(amount="10000"),
            ownership=PositionOwnership.SHARED,
            takeover_strategy=StrategyId("s2"),
        )
        remaining = opt.exit_protection(StrategyId("s1"), [t])
        assert len(remaining) == 1
        assert remaining[0].strategy_id == StrategyId("s2")
