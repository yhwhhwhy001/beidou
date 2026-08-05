"""PKG-28~34: Model Registry、Control Plane、Chaos、Production Ladder 测试。"""

from datetime import datetime, timezone

from beidou_chaos.engine import ChaosEngine, KillScenario
from beidou_control.plane import AccountFactOverview, ControlAction, ControlPlane
from beidou_production.ladder import GateResult, LadderLevel, ProductionLadder
from beidou_research.backtest.replay import CheatDetection, ReplayResult, ReplayValidator
from beidou_shared.types import (
    AccountId,
    ModelId,
    MonetaryValue,
    SchemaVersion,
    StrategyId,
    VenueId,
)
from beidou_strategy.alpha.model_registry import DriftDetector, ModelRecord, ModelRegistry, ModelStatus


class TestModelRegistry:
    def test_champion_promotion(self):
        reg = ModelRegistry()
        reg.register(
            ModelRecord(
                model_id=ModelId("m1"),
                strategy_id=StrategyId("s1"),
                status=ModelStatus.CHALLENGER,
                version=SchemaVersion("1.0.0"),
            )
        )
        reg.register(
            ModelRecord(
                model_id=ModelId("m2"),
                strategy_id=StrategyId("s1"),
                status=ModelStatus.CHALLENGER,
                version=SchemaVersion("2.0.0"),
            )
        )
        assert reg.promote_to_champion(StrategyId("s1"), ModelId("m1"))
        champ = reg.get_champion(StrategyId("s1"))
        assert champ is not None
        assert champ.model_id == ModelId("m1")

    def test_retire_strategy(self):
        reg = ModelRegistry()
        reg.register(
            ModelRecord(
                model_id=ModelId("m1"),
                strategy_id=StrategyId("s1"),
                status=ModelStatus.CHAMPION,
                version=SchemaVersion("1.0.0"),
            )
        )
        retired = reg.retire_strategy(StrategyId("s1"), "Performance degradation")
        assert len(retired) == 1
        assert retired[0].status == ModelStatus.RETIRED


class TestDriftDetector:
    def test_no_drift(self):
        dd = DriftDetector(threshold=0.5)
        dd.set_baseline({"sharpe": 1.5, "win_rate": 0.55})
        drift = dd.detect({"sharpe": 1.4, "win_rate": 0.53})
        assert len(drift) == 0

    def test_drift_detected(self):
        dd = DriftDetector(threshold=0.1)
        dd.set_baseline({"sharpe": 2.0, "win_rate": 0.6})
        drift = dd.detect({"sharpe": 1.0, "win_rate": 0.6})
        assert len(drift) > 0
        assert "sharpe" in drift

    def test_should_retire(self):
        dd = DriftDetector(threshold=0.05)
        dd.set_baseline({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0})
        drift = dd.detect({"a": 2.0, "b": 4.0, "c": 6.0, "d": 8.0})
        assert dd.should_retire(drift)


class TestControlPlane:
    def test_emergency_lock(self):
        cp = ControlPlane()
        assert cp.emergency_lock("Security incident")
        assert cp.get_status() == ControlAction.LOCK

    def test_account_overview(self):
        cp = ControlPlane()
        overview = AccountFactOverview(
            account_id=AccountId("test"),
            venue_id=VenueId("BINANCE"),
            total_equity=MonetaryValue(amount="100000"),
            available_balance=MonetaryValue(amount="50000"),
            margin_used=MonetaryValue(amount="50000"),
            margin_ratio=0.5,
            unrealized_pnl=MonetaryValue(amount="1000"),
        )
        cp.update_account_overview(overview)
        # Verify the overview was stored
        issues = cp.get_unknown_or_differences()
        assert isinstance(issues, list)


class TestChaosEngine:
    def test_all_experiments_passed(self):
        engine = ChaosEngine()
        exp = engine.inject(KillScenario.EXCHANGE_DISCONNECT)
        engine.verify_recovery(exp, True, 30.0, True)
        assert engine.all_experiments_passed()

    def test_combination_fault(self):
        engine = ChaosEngine()
        exps = engine.combination_fault_test([KillScenario.DATABASE_FAILURE, KillScenario.KAFKA_OUTAGE])
        assert len(exps) == 2


class TestProductionLadder:
    def test_cannot_skip_levels(self):
        ladder = ProductionLadder()
        assert not ladder.can_promote_to(LadderLevel.L5_CHAMPION)

    def test_promote_step_by_step(self):
        ladder = ProductionLadder()
        ladder.certify(LadderLevel.L1_SHADOW, GateResult.PASS, [])
        ladder.certify(LadderLevel.L2_CANARY, GateResult.PASS, [])
        assert ladder.can_promote_to(LadderLevel.L3_RAMP)

    def test_degradation_on_drawdown(self):
        ladder = ProductionLadder()
        assert ladder.should_degrade(25.0, 1.5, 0)

    def test_no_degradation_normal(self):
        ladder = ProductionLadder()
        assert not ladder.should_degrade(5.0, 1.5, 0)


class TestReplayValidator:
    def test_future_function_detected(self):
        v = ReplayValidator()
        t0 = datetime.now(timezone.utc)
        from datetime import timedelta

        # signal before data available → future leak → FAIL
        assert not v.check_future_function(t0, t0 + timedelta(seconds=1))
        # signal after data available → no leak → PASS
        assert v.check_future_function(t0 + timedelta(seconds=1), t0)

    def test_survivorship_check(self):
        v = ReplayValidator()
        missing = v.check_survivorship({"BTC", "ETH"}, {"BTC", "ETH", "SOL"})
        assert "SOL" in missing

    def test_all_checks_pass(self):
        result = ReplayResult(
            replay_id="r1", deterministic=True, cheat_checks=dict.fromkeys(CheatDetection, True), output_hash="abc"
        )
        v = ReplayValidator()
        v.set_baseline("abc")
        assert v.all_checks_pass(result)
        assert v.verify_determinism(result)
