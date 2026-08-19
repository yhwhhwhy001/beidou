"""补充合约测试: 覆盖剩余低覆盖率模块。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from beidou_certification.contracts import (
    CertificationGate,
    FaultInjectionResult,
    FaultScenario,
    GateLevel,
    StagedCertification,
)
from beidou_data.canonical_bars import (
    CanonicalBarBuilder,
    get_canonical_bar_builder,
    reset_bar_builders,
)
from beidou_data.contracts import (
    DQCheck,
    DQSnapshot,
    DQStatus,
    PITUniverseSnapshot,
    UniverseEntry,
)
from beidou_exchange.core.rule_snapshot import InstrumentRuleSnapshot
from beidou_research.contracts import (
    CapacityModel,
    CostBreakdown,
    KernelParityResult,
    StatisticalTest,
    StatisticalValidationResult,
)
from beidou_safety.execution.contracts import (
    ExecutionCostSnapshot,
)

# ============================================================================
# BD-CV10: InstrumentRuleSnapshot — UNKNOWN 规则
# ============================================================================


class TestInstrumentRuleSnapshot:
    def test_unknown_rule_not_known(self):
        snap = InstrumentRuleSnapshot.unknown("BTCUSDT")
        assert not snap.is_known
        assert snap.symbol == "BTCUSDT"

    def test_known_rule_is_known(self):
        snap = InstrumentRuleSnapshot(
            symbol="BTCUSDT",
            tick_size="0.1",
            step_size="0.001",
            min_qty="0.001",
            min_notional="20.0",
            price_precision=1,
            qty_precision=3,
            observed_at=datetime.now(timezone.utc).isoformat(),
        )
        assert snap.is_known

    def test_integer_tick_and_step_are_valid_zero_precision_rules(self):
        snap = InstrumentRuleSnapshot.from_exchange_info(
            "INTEGERUSDT",
            {
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "1"},
                    {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ]
            },
        )
        assert snap.is_known
        assert snap.price_precision == 0
        assert snap.qty_precision == 0

    def test_scientific_notation_precision_and_conservative_quantization(self):
        snap = InstrumentRuleSnapshot.from_exchange_info(
            "MICROUSDT",
            {
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "1E-8"},
                    {"filterType": "LOT_SIZE", "stepSize": "1E-7", "minQty": "1E-7"},
                    {"filterType": "MIN_NOTIONAL", "notional": "0.01"},
                ]
            },
        )
        assert snap.is_known
        assert snap.price_precision == 8
        assert snap.qty_precision == 7
        assert snap.quantize_quantity("0.00000015") == "0.0000001"
        assert snap.quantize_price("1.234567899", side="BUY") == "1.23456789"
        assert snap.quantize_price("1.234567891", side="SELL") == "1.23456790"

    def test_hash_consistent(self):
        now = datetime.now(timezone.utc).isoformat()
        s1 = InstrumentRuleSnapshot(
            symbol="BTCUSDT",
            tick_size="0.1",
            step_size="0.001",
            min_qty="0.001",
            min_notional="20.0",
            price_precision=1,
            qty_precision=3,
            observed_at=now,
        )
        s2 = InstrumentRuleSnapshot(
            symbol="BTCUSDT",
            tick_size="0.1",
            step_size="0.001",
            min_qty="0.001",
            min_notional="20.0",
            price_precision=1,
            qty_precision=3,
            observed_at=now,
        )
        assert s1.compute_hash() == s2.compute_hash()

    def test_unknown_hash_differs(self):
        s1 = InstrumentRuleSnapshot.unknown("BTCUSDT")
        s2 = InstrumentRuleSnapshot(
            symbol="ETHUSDT",
            tick_size="0.01",
            step_size="0.001",
            min_qty="0.001",
            min_notional="10.0",
            price_precision=2,
            qty_precision=3,
            observed_at=datetime.now(timezone.utc).isoformat(),
        )
        assert s1.compute_hash() != s2.compute_hash()


# ============================================================================
# BD-CV11: CanonicalBarBuilder — gap/late/duplicate
# ============================================================================


class TestCanonicalBarBuilder:
    def setup_method(self):
        reset_bar_builders()

    def test_floor_to_bucket_5m(self):
        dt = datetime(2026, 1, 1, 10, 7, 30, tzinfo=timezone.utc)
        floored = CanonicalBarBuilder.floor_to_bucket(dt, 300)
        assert floored.hour == 10
        assert floored.minute == 5

    def test_get_shared_builder(self):
        b1 = get_canonical_bar_builder("5m")
        b2 = get_canonical_bar_builder("5m")
        assert b1 is b2

    def test_different_interval_different_builder(self):
        b1 = get_canonical_bar_builder("5m")
        b2 = get_canonical_bar_builder("1h")
        assert b1 is not b2

    def test_valid_intervals(self):
        assert "5m" in CanonicalBarBuilder.VALID_INTERVALS
        assert "1h" in CanonicalBarBuilder.VALID_INTERVALS
        assert "1d" in CanonicalBarBuilder.VALID_INTERVALS

    def test_invalid_interval_raises(self):
        with pytest.raises(ValueError):
            CanonicalBarBuilder("invalid")


# ============================================================================
# BD-CV12: DQSnapshot
# ============================================================================


class TestDQSnapshot:
    def test_all_required_pass_when_empty(self):
        snap = DQSnapshot(symbol="BTCUSDT")
        assert not snap.all_required_pass()

    def test_all_required_pass_when_complete(self):
        checks = [DQCheck(check_id=cid, name=cid, status=DQStatus.PASS) for cid in DQSnapshot.REQUIRED_CHECKS]
        snap = DQSnapshot(symbol="BTCUSDT", checks=checks, warmup_complete=True)
        assert snap.all_required_pass()
        assert snap.has_warmup_data()

    def test_one_fail_blocks_all(self):
        checks = [DQCheck(check_id=cid, name=cid, status=DQStatus.PASS) for cid in DQSnapshot.REQUIRED_CHECKS]
        checks[0] = DQCheck(check_id=checks[0].check_id, name=checks[0].name, status=DQStatus.FAIL)
        snap = DQSnapshot(symbol="BTCUSDT", checks=checks)
        assert not snap.all_required_pass()

    def test_warmup_without_complete_fails(self):
        checks = [DQCheck(check_id=cid, name=cid, status=DQStatus.PASS) for cid in DQSnapshot.REQUIRED_CHECKS]
        snap = DQSnapshot(symbol="BTCUSDT", checks=checks, warmup_complete=False)
        assert snap.all_required_pass()
        assert not snap.has_warmup_data()


# ============================================================================
# BD-CV13: PITUniverseSnapshot
# ============================================================================


class TestPITUniverse:
    def test_executable_symbols(self):
        entries = [
            UniverseEntry(symbol="BTCUSDT", is_executable=True),
            UniverseEntry(symbol="ETHUSDT", is_executable=True),
            UniverseEntry(symbol="XRPUSDT", is_executable=False, exclude_reason="low_liquidity"),
        ]
        snap = PITUniverseSnapshot(universe_id="u1", entries=entries)
        assert snap.executable_symbols() == ["BTCUSDT", "ETHUSDT"]
        blocked = snap.blocked_symbols()
        assert "XRPUSDT" in blocked
        assert blocked["XRPUSDT"] == "low_liquidity"

    def test_unknown_critical(self):
        # M02-F03: UNKNOWN = None（缺失证据）；零值是合法事实不得误报。
        entries = [
            UniverseEntry(symbol="BTCUSDT", funding_rate=None, open_interest=None),
        ]
        snap = PITUniverseSnapshot(universe_id="u1", entries=entries)
        issues = snap.any_unknown_critical()
        assert "BTCUSDT:funding_rate" in issues
        assert "BTCUSDT:open_interest" in issues

    def test_zero_values_are_not_unknown(self):
        # 零费率/零 OI 是合法市场事实，不得被当作 UNKNOWN 阻断。
        entries = [
            UniverseEntry(symbol="BTCUSDT", funding_rate=0.0, open_interest=0.0, capacity_score=0.5),
        ]
        snap = PITUniverseSnapshot(universe_id="u1", entries=entries)
        assert snap.any_unknown_critical() == []


# ============================================================================
# BD-CV21: CostBreakdown + CapacityModel
# ============================================================================


class TestCostAndCapacity:
    def test_unverified_cost_not_verifiable(self):
        cb = CostBreakdown(symbol="BTCUSDT", commission_bps=2.0, is_verified=False)
        assert not cb.is_verifiable()

    def test_verified_cost_is_verifiable(self):
        cb = CostBreakdown(symbol="BTCUSDT", commission_bps=2.0, is_verified=True, total_bps=5.0)
        assert cb.is_verifiable()

    def test_capacity_not_constant(self):
        m1 = CapacityModel(symbol="BTCUSDT", max_position_notional=100000.0, is_stale=False)
        m2 = CapacityModel(symbol="BTCUSDT", max_position_notional=200000.0, is_stale=False)
        assert not m1.is_constant(m2)

    def test_capacity_constant(self):
        m1 = CapacityModel(symbol="BTCUSDT", max_position_notional=100000.0)
        m2 = CapacityModel(symbol="BTCUSDT", max_position_notional=100000.001)
        assert m1.is_constant(m2)


# ============================================================================
# BD-CV24: KernelParityResult
# ============================================================================


class TestKernelParity:
    def test_parity_matched(self):
        kp = KernelParityResult(paper_hash="abc", replay_hash="abc", backtest_hash="abc", is_consistent=True)
        assert kp.has_parity()

    def test_parity_mismatched(self):
        kp = KernelParityResult(paper_hash="abc", replay_hash="def", backtest_hash="abc", is_consistent=False)
        assert not kp.has_parity()

    def test_empty_hashes_no_parity(self):
        kp = KernelParityResult()
        assert not kp.has_parity()


# ============================================================================
# BD-CV20: StatisticalValidationResult
# ============================================================================


class TestStatisticalValidation:
    def test_pbo_different_combos_different_hash(self):
        r1 = StatisticalValidationResult(test_type=StatisticalTest.PBO, n_combos=100, deflated_sharpe=0.5)
        r2 = StatisticalValidationResult(test_type=StatisticalTest.PBO, n_combos=200, deflated_sharpe=0.5)
        assert r1.compute_hash() != r2.compute_hash()

    def test_holm_different_pvalue(self):
        r1 = StatisticalValidationResult(test_type=StatisticalTest.HOLM, p_value=0.01)
        r2 = StatisticalValidationResult(test_type=StatisticalTest.HOLM, p_value=0.05)
        assert r1.compute_hash() != r2.compute_hash()

    def test_all_test_types(self):
        for test_type in StatisticalTest:
            r = StatisticalValidationResult(test_type=test_type)
            assert r.compute_hash()


# ============================================================================
# BD-CV21/45: ExecutionCostSnapshot
# ============================================================================


class TestExecutionCostSnapshot:
    def test_cannot_learn_without_fill(self):
        snap = ExecutionCostSnapshot(symbol="BTCUSDT", has_fill=False, has_fee=True)
        assert not snap.can_learn()

    def test_cannot_learn_without_fee(self):
        snap = ExecutionCostSnapshot(symbol="BTCUSDT", has_fill=True, has_fee=False)
        assert not snap.can_learn()

    def test_can_learn(self):
        snap = ExecutionCostSnapshot(symbol="BTCUSDT", has_fill=True, has_fee=True)
        assert snap.can_learn()


# ============================================================================
# BD-CV55: StagedCertification
# ============================================================================


class TestStagedCertification:
    def test_empty_chain_complete(self):
        sc = StagedCertification()
        assert sc.is_chain_complete()

    def test_no_g8_not_verified(self):
        sc = StagedCertification()
        assert not sc.production_parity_verified()

    def test_g8_candidate_only(self):
        g8 = CertificationGate(gate_level=GateLevel.G8, certified=True)
        sc = StagedCertification(stages={"G8": g8})
        assert sc.production_parity_verified()


# ============================================================================
# BD-CV54: FaultInjectionResult
# ============================================================================


class TestFaultInjection:
    def test_all_scenarios_covered(self):
        for scenario in FaultScenario:
            result = FaultInjectionResult(scenario=scenario, passed=True)
            assert result.is_machine_decidable()

    def test_risk_increase_fails(self):
        result = FaultInjectionResult(
            scenario=FaultScenario.DUAL_INSTANCE,
            risk_increase_detected=True,
            passed=False,
        )
        assert not result.is_machine_decidable()

    def test_duplicate_orders_detected(self):
        result = FaultInjectionResult(
            scenario=FaultScenario.TIMEOUT,
            duplicate_orders_detected=1,
            passed=False,
        )
        assert result.duplicate_orders_detected == 1


def test_rule_snapshot_hash_stable_across_refresh_times() -> None:
    """规则内容不变时,hash 不得随刷新时刻翻转 —— 周期刷新(observed_at
    变化)曾让执行器把每个品种的首个订单判 VENUE_RULE_SNAPSHOT_CHANGED,
    且旧实现不回写 hash → 品种被永久拒绝(实测 187 连拒)。"""
    from datetime import datetime, timedelta

    from beidou_exchange.core.rule_snapshot import InstrumentRuleSnapshot

    base = {
        "symbol": "BTCUSDT",
        "tick_size": "0.1",
        "step_size": "0.001",
        "min_qty": "0.001",
        "min_notional": "20.0",
        "price_precision": 1,
        "qty_precision": 3,
        "contract_size": 0.0,
        "position_mode": "ONEWAY",
        "rule_version": 3,
        "source": "exchange_info",
    }
    t1 = datetime.now(timezone.utc).isoformat()
    t2 = (datetime.now(timezone.utc) + timedelta(minutes=25)).isoformat()
    s1 = InstrumentRuleSnapshot(observed_at=t1, **base)
    s2 = InstrumentRuleSnapshot(observed_at=t2, **base)

    assert s1.compute_hash() == s2.compute_hash()


def test_rule_snapshot_hash_changes_when_rules_change() -> None:
    from beidou_exchange.core.rule_snapshot import InstrumentRuleSnapshot

    s1 = InstrumentRuleSnapshot(
        symbol="BTCUSDT",
        tick_size="0.1",
        step_size="0.001",
        min_qty="0.001",
        min_notional="20.0",
        price_precision=1,
        qty_precision=3,
        observed_at=datetime.now(timezone.utc).isoformat(),
    )
    s2 = InstrumentRuleSnapshot(
        symbol="BTCUSDT",
        tick_size="0.1",
        step_size="0.001",
        min_qty="0.001",
        min_notional="20.0",
        price_precision=1,
        qty_precision=4,  # 步长精度变化 = 真实规则变化
        observed_at=datetime.now(timezone.utc).isoformat(),
    )
    assert s1.compute_hash() != s2.compute_hash()
