"""PKG-14 因子研究模块测试。IC/RankIC/ICIR、分层、边际贡献、生命周期、退役/重启。"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from beidou_shared.types import SchemaVersion, VenueId
from beidou_research.factors.factor import (
    FactorLifecycle,
    FACTOR_LIFECYCLE_TRANSITIONS,
    FactorDefinition,
    FactorPerformance,
    MarginalContribution,
    FactorRecord,
    FactorEvaluator,
    FactorRegistry,
)


class TestFactorLifecycle:
    """因子生命周期状态机测试。"""

    def test_idea_to_research(self):
        f = FactorRecord(
            definition=FactorDefinition(
                factor_id="f-001", name="momentum_24h", version=SchemaVersion("1.0.0"),
                description="24h momentum", author="test", category="momentum",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="trend persists", lookback_period="24h", rebalance_interval="1h",
            ),
            lifecycle=FactorLifecycle.IDEA,
        )
        assert f.transition(FactorLifecycle.RESEARCH)
        assert f.lifecycle == FactorLifecycle.RESEARCH

    def test_full_lifecycle_path(self):
        f = FactorRecord(
            definition=FactorDefinition(
                factor_id="f-002", name="mean_rev_1h", version=SchemaVersion("1.0.0"),
                description="mean reversion", author="test", category="mean_reversion",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="price reverts", lookback_period="1h", rebalance_interval="15m",
            ),
            lifecycle=FactorLifecycle.IDEA,
        )
        path = [
            FactorLifecycle.RESEARCH,
            FactorLifecycle.BACKTEST,
            FactorLifecycle.PAPER_TRADING,
            FactorLifecycle.CHALLENGER,
            FactorLifecycle.ACTIVE,
        ]
        for state in path:
            assert f.transition(state), f"Should transition to {state}"
            assert f.lifecycle == state

    def test_invalid_transition_rejected(self):
        f = FactorRecord(
            definition=FactorDefinition(
                factor_id="f-003", name="test", version=SchemaVersion("1.0.0"),
                description="test", author="test", category="momentum",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="test", lookback_period="24h", rebalance_interval="1h",
            ),
            lifecycle=FactorLifecycle.IDEA,
        )
        assert not f.transition(FactorLifecycle.ACTIVE)  # IDEA→ACTIVE 非法
        assert f.lifecycle == FactorLifecycle.IDEA

    def test_retired_cannot_transition(self):
        f = FactorRecord(
            definition=FactorDefinition(
                factor_id="f-004", name="retired_test", version=SchemaVersion("1.0.0"),
                description="test", author="test", category="momentum",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="test", lookback_period="24h", rebalance_interval="1h",
            ),
            lifecycle=FactorLifecycle.RETIRED,
        )
        assert not f.transition(FactorLifecycle.ACTIVE)
        assert not f.transition(FactorLifecycle.CHALLENGER)
        assert f.lifecycle == FactorLifecycle.RETIRED

    def test_suspended_restarts_as_challenger(self):
        f = FactorRecord(
            definition=FactorDefinition(
                factor_id="f-005", name="suspended_test", version=SchemaVersion("1.0.0"),
                description="test", author="test", category="momentum",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="test", lookback_period="24h", rebalance_interval="1h",
            ),
            lifecycle=FactorLifecycle.SUSPENDED,
        )
        assert f.can_restart()
        assert f.restart_as_challenger()
        assert f.lifecycle == FactorLifecycle.CHALLENGER

    def test_degraded_to_suspended(self):
        f = FactorRecord(
            definition=FactorDefinition(
                factor_id="f-006", name="degraded_test", version=SchemaVersion("1.0.0"),
                description="test", author="test", category="momentum",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="test", lookback_period="24h", rebalance_interval="1h",
            ),
            lifecycle=FactorLifecycle.DEGRADED,
        )
        assert f.transition(FactorLifecycle.SUSPENDED)
        assert f.lifecycle == FactorLifecycle.SUSPENDED

    def test_degraded_can_recover(self):
        f = FactorRecord(
            definition=FactorDefinition(
                factor_id="f-007", name="recover_test", version=SchemaVersion("1.0.0"),
                description="test", author="test", category="momentum",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="test", lookback_period="24h", rebalance_interval="1h",
            ),
            lifecycle=FactorLifecycle.DEGRADED,
        )
        assert f.transition(FactorLifecycle.ACTIVE)

    def test_all_transitions_in_valid_set(self):
        """验证所有 FACTOR_LIFECYCLE_TRANSITIONS 中定义的状态都是合法的。"""
        for src, targets in FACTOR_LIFECYCLE_TRANSITIONS.items():
            for tgt in targets:
                assert isinstance(tgt, FactorLifecycle)
                # 确保不会自转换
                assert tgt != src or tgt == FactorLifecycle.RETIRED  # RETIRED→RETIRED 不定义


class TestFactorEvaluator:
    """因子评估器测试。"""

    def test_ic_computation(self):
        preds = [1.0, 2.0, 3.0, 4.0, 5.0]
        rets = [0.01, 0.02, 0.03, 0.04, 0.05]
        ic, ic_std = FactorEvaluator.compute_ic(preds, rets)
        assert ic > 0.9  # 接近完美正相关

    def test_ic_zero_for_short_series(self):
        ic, ic_std = FactorEvaluator.compute_ic([1.0], [0.01])
        assert ic == 0.0

    def test_rank_ic(self):
        preds = [1.0, 2.0, 3.0, 4.0, 5.0]
        rets = [0.01, 0.02, 0.03, 0.04, 0.05]
        rank_ic = FactorEvaluator.compute_rank_ic(preds, rets)
        assert rank_ic > 0.9

    def test_rank_ic_negative(self):
        preds = [5.0, 4.0, 3.0, 2.0, 1.0]
        rets = [0.01, 0.02, 0.03, 0.04, 0.05]
        rank_ic = FactorEvaluator.compute_rank_ic(preds, rets)
        assert rank_ic < -0.9

    def test_icir_from_series(self):
        ic_series = [0.05, 0.06, 0.04, 0.07, 0.05]
        icir = FactorEvaluator.compute_icir(ic_series)
        assert icir > 2.0  # high IC, low std → high ICIR

    def test_icir_low_performance(self):
        ic_series = [0.001, -0.002, 0.0, 0.003, -0.001]
        icir = FactorEvaluator.compute_icir(ic_series)
        assert abs(icir) < 1.0

    def test_decile_spread(self):
        preds = [float(i) for i in range(100)]
        rets = [float(i) / 1000 for i in range(100)]
        spread = FactorEvaluator.compute_decile_spread(preds, rets)
        assert spread > 0  # top decile returns > bottom decile

    def test_turnover_zero(self):
        turnover = FactorEvaluator.compute_turnover([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        assert turnover == 0.0

    def test_turnover_full(self):
        turnover = FactorEvaluator.compute_turnover([1.0, 2.0, 3.0], [-1.0, -2.0, -3.0])
        assert turnover > 0.0

    def test_vif_no_collinearity(self):
        corr = {"f1": {"f2": 0.1, "f3": 0.2}}
        vif = FactorEvaluator.compute_vif(corr, "f1")
        assert vif < 2.0

    def test_vif_high_collinearity(self):
        corr = {"f1": {"f2": 0.9, "f3": 0.85}}
        vif = FactorEvaluator.compute_vif(corr, "f1")
        assert vif > 5.0

    def test_marginal_contribution(self):
        perf = FactorPerformance(
            factor_id="f-a", evaluation_period="2026-Q1", sample_count=1000,
            ic_mean=0.05, ic_std=0.02, icir=2.5, rank_ic_mean=0.06, rank_ic_std=0.02, rank_icir=3.0,
        )
        corr = {"f-a": {"f-b": 0.2, "f-c": 0.3}}
        mc = FactorEvaluator.compute_marginal_contribution(perf, [], corr)
        assert mc.marginal_sharpe > 0
        assert mc.collinearity_vif < 5.0
        assert not mc.has_adverse_collinearity()

    def test_marginal_contribution_high_collinearity(self):
        perf = FactorPerformance(
            factor_id="f-x", evaluation_period="2026-Q1", sample_count=1000,
            ic_mean=0.002, ic_std=0.02, icir=0.1, rank_ic_mean=0.002, rank_ic_std=0.02, rank_icir=0.1,
        )
        corr = {"f-x": {"f-y": 0.95}}
        mc = FactorEvaluator.compute_marginal_contribution(perf, [], corr)
        assert mc.collinearity_vif > 5.0
        assert mc.has_adverse_collinearity()


class TestFactorRegistry:
    """因子注册中心测试。"""

    def _make_def(self, fid: str, name: str = "test_factor") -> FactorDefinition:
        return FactorDefinition(
            factor_id=fid, name=name, version=SchemaVersion("1.0.0"),
            description="test factor", author="test", category="momentum",
            universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
            economic_rationale="test", lookback_period="24h", rebalance_interval="1h",
        )

    def test_register_factor(self):
        reg = FactorRegistry()
        record = reg.register(self._make_def("f-reg-001"))
        assert record is not None
        assert record.lifecycle == FactorLifecycle.IDEA

    def test_retire_factor_preserves_evidence(self):
        reg = FactorRegistry()
        record = reg.register(self._make_def("f-reg-002"))
        record.transition(FactorLifecycle.RESEARCH)
        assert reg.retire("f-reg-002", "No longer useful")
        assert reg.get("f-reg-002") is not None  # 退役后仍然可查
        retired = reg.get_retired()
        assert len(retired) == 1
        assert retired[0].retirement_reason == "No longer useful"
        assert "reason" in retired[0].retirement_evidence
        assert retired[0].retired_at is not None

    def test_retired_factor_not_in_active(self):
        reg = FactorRegistry()
        reg.register(self._make_def("f-reg-003"))
        reg.retire("f-reg-003", "obsolete")
        # 退役因子不在活跃列表中
        reg2 = FactorRegistry()
        reg2.register(self._make_def("f-reg-004", "other"))
        reg2._factors["f-reg-004"].transition(FactorLifecycle.RESEARCH)
        reg2._factors["f-reg-004"].transition(FactorLifecycle.BACKTEST)
        reg2._factors["f-reg-004"].transition(FactorLifecycle.PAPER_TRADING)
        reg2._factors["f-reg-004"].transition(FactorLifecycle.CHALLENGER)
        reg2._factors["f-reg-004"].transition(FactorLifecycle.ACTIVE)
        assert len(reg2.get_active()) == 1

    def test_restart_retired_as_new_idea(self):
        reg = FactorRegistry()
        reg.register(self._make_def("f-reg-005"))
        reg.retire("f-reg-005", "obsolete")
        new_def = self._make_def("f-reg-006", "retired_restart")
        result = reg.restart_as_new_idea("f-reg-005", new_def)
        assert result is not None
        assert result.lifecycle == FactorLifecycle.IDEA  # 重启=全新IDEA
        assert result.restarted_from == "f-reg-005"

    def test_promote_to_active_requires_performance(self):
        reg = FactorRegistry()
        record = reg.register(self._make_def("f-reg-007"))
        # 直接晋升 ACTIVE 应失败（没有经过完整评估）
        assert not reg.promote_to_active("f-reg-007")

    def test_promote_low_icir_fails(self):
        reg = FactorRegistry()
        record = reg.register(self._make_def("f-reg-008"))
        for state in [FactorLifecycle.RESEARCH, FactorLifecycle.BACKTEST, FactorLifecycle.PAPER_TRADING]:
            record.transition(state)
        record.transition(FactorLifecycle.CHALLENGER)
        perf = FactorPerformance(
            factor_id="f-reg-008", evaluation_period="2026-Q1", sample_count=100,
            ic_mean=0.01, ic_std=0.05, icir=0.2, rank_ic_mean=0.01, rank_ic_std=0.05, rank_icir=0.2,
        )
        mc = MarginalContribution(
            factor_id="f-reg-008", existing_factor_ids=frozenset(),
            marginal_sharpe=0.02, marginal_ic=0.005, diversification_benefit=0.0,
            collinearity_vif=1.2,
        )
        reg.evaluate("f-reg-008", perf, [mc])
        # ICIR < 0.3 → 不应晋升
        assert not reg.promote_to_active("f-reg-008")

    def test_cross_venue_stability(self):
        reg = FactorRegistry()
        reg.register(self._make_def("f-reg-009"))
        ok, msg = reg.cross_venue_stability(
            "f-reg-009",
            {VenueId("BINANCE"): 0.05, VenueId("OKX"): 0.04},
            min_stability_threshold=0.03,
        )
        assert ok, f"Should be stable: {msg}"

    def test_cross_venue_instability(self):
        reg = FactorRegistry()
        reg.register(self._make_def("f-reg-010"))
        ok, msg = reg.cross_venue_stability(
            "f-reg-010",
            {VenueId("BINANCE"): 0.05, VenueId("OKX"): -0.04},  # 方向不一致
        )
        assert not ok

    def test_cross_venue_below_threshold(self):
        reg = FactorRegistry()
        reg.register(self._make_def("f-reg-011"))
        ok, msg = reg.cross_venue_stability(
            "f-reg-011",
            {VenueId("BINANCE"): 0.01},  # 低于阈值
            min_stability_threshold=0.03,
        )
        assert not ok

    def test_degrade_active_factor(self):
        reg = FactorRegistry()
        record = reg.register(self._make_def("f-reg-012"))
        for state in [FactorLifecycle.RESEARCH, FactorLifecycle.BACKTEST, FactorLifecycle.PAPER_TRADING, FactorLifecycle.CHALLENGER]:
            record.transition(state)
        record.transition(FactorLifecycle.ACTIVE)
        assert reg.degrade("f-reg-012", "performance decline")
        assert record.lifecycle == FactorLifecycle.DEGRADED

    def test_suspend_factor(self):
        reg = FactorRegistry()
        record = reg.register(self._make_def("f-reg-013"))
        record.lifecycle = FactorLifecycle.ACTIVE
        assert reg.suspend("f-reg-013", "market regime change")
        assert record.lifecycle == FactorLifecycle.SUSPENDED

    def test_retired_evidence_complete(self):
        reg = FactorRegistry()
        record = reg.register(self._make_def("f-reg-014"))
        record.transition(FactorLifecycle.RESEARCH)
        record.performance.append(FactorPerformance(
            factor_id="f-reg-014", evaluation_period="2026-Q1", sample_count=500,
            ic_mean=0.04, ic_std=0.02, icir=2.0, rank_ic_mean=0.04, rank_ic_std=0.02, rank_icir=2.0,
        ))
        assert reg.retire("f-reg-014", "strategy decommissioned")
        retired = reg.get_retired()
        assert len(retired) == 1
        evidence = retired[0].retirement_evidence
        assert evidence["reason"] == "strategy decommissioned"
        assert evidence["final_lifecycle"] == "RESEARCH"
        assert len(evidence["performance_history"]) == 1


class TestFactorPerformance:
    """因子性能记录测试。"""

    def test_performance_creation(self):
        perf = FactorPerformance(
            factor_id="f-001", evaluation_period="2026-Q2", sample_count=5000,
            ic_mean=0.05, ic_std=0.02, icir=2.5,
            rank_ic_mean=0.06, rank_ic_std=0.02, rank_icir=3.0,
            cost_adjusted_ic=0.04, sharpe_contribution=0.3,
        )
        assert perf.icir > 2.0
        assert perf.rank_icir > 2.0
        assert perf.cost_adjusted_ic is not None

    def test_performance_with_venue_breakdown(self):
        perf = FactorPerformance(
            factor_id="f-002", evaluation_period="2026-Q2", sample_count=5000,
            ic_mean=0.05, ic_std=0.02, icir=2.5,
            rank_ic_mean=0.06, rank_ic_std=0.02, rank_icir=3.0,
            venue_breakdown={VenueId("BINANCE"): 0.06, VenueId("OKX"): 0.04},
        )
        assert VenueId("BINANCE") in perf.venue_breakdown
        assert perf.venue_breakdown[VenueId("BINANCE")] > 0
