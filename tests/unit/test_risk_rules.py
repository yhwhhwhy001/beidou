"""BD-T07: R0-R10 风险规则测试 — 通过/拒绝/边界全覆盖。"""

import pytest

from beidou_safety.risk.rules import (
    RiskRuleRegistry,
    RuleDecision,
    _r0_max_leverage,
    _r1_concentration,
    _r2_drawdown,
    _r3_daily_loss,
    _r4_consecutive_losses,
    _r5_sharpe,
    _r6_margin,
    _r7_liquidation_distance,
    _r8_protection_coverage,
    _r9_account_capability,
    _r10_duplicate_order,
)


def _ctx(**overrides):
    base = {
        "leverage": 1.0,
        "concentration_pct": 20.0,
        "drawdown_pct": 5.0,
        "daily_loss_pct": 1.0,
        "max_daily_loss_pct": 5.0,
        "consecutive_losses": 1,
        "rolling_sharpe": 0.8,
        "min_sharpe_rolling": 0.0,
        "margin_ratio": 0.25,
        "liquidation_price": 45000.0,
        "current_price": 50000.0,
        "is_long": True,
        "protected_positions": 2,
        "total_positions": 2,
        "can_trade": True,
        "can_deposit": True,
        "can_withdraw": False,
        "duplicate_orders_24h": 0,
    }
    base.update(overrides)
    return base


class TestR0R1:
    def test_r0_pass(self):
        assert _r0_max_leverage(_ctx(leverage=1.0)) == RuleDecision.PASS
    def test_r0_reject(self):
        assert _r0_max_leverage(_ctx(leverage=10.0)) == RuleDecision.REJECT
    def test_r0_boundary(self):
        assert _r0_max_leverage(_ctx(leverage=3.0)) == RuleDecision.PASS
    def test_r1_pass(self):
        assert _r1_concentration(_ctx(concentration_pct=20.0)) == RuleDecision.PASS
    def test_r1_reject(self):
        assert _r1_concentration(_ctx(concentration_pct=60.0)) == RuleDecision.REJECT


class TestR2R3R4:
    def test_r2_pass(self):
        assert _r2_drawdown(_ctx(drawdown_pct=5.0)) == RuleDecision.PASS
    def test_r2_reject(self):
        assert _r2_drawdown(_ctx(drawdown_pct=30.0)) == RuleDecision.REJECT
    def test_r2_unknown(self):
        assert _r2_drawdown(_ctx(drawdown_pct=None)) == RuleDecision.UNKNOWN
    def test_r3_pass(self):
        assert _r3_daily_loss(_ctx(daily_loss_pct=1.0)) == RuleDecision.PASS
    def test_r3_reject(self):
        assert _r3_daily_loss(_ctx(daily_loss_pct=8.0)) == RuleDecision.REJECT
    def test_r3_unknown(self):
        assert _r3_daily_loss(_ctx(daily_loss_pct=None)) == RuleDecision.UNKNOWN
    def test_r4_pass(self):
        assert _r4_consecutive_losses(_ctx(consecutive_losses=1)) == RuleDecision.PASS
    def test_r4_reject(self):
        assert _r4_consecutive_losses(_ctx(consecutive_losses=6)) == RuleDecision.REJECT
    def test_r4_unknown(self):
        assert _r4_consecutive_losses(_ctx(consecutive_losses=None)) == RuleDecision.UNKNOWN


class TestR5R6R7:
    def test_r5_pass(self):
        assert _r5_sharpe(_ctx(rolling_sharpe=0.8)) == RuleDecision.PASS
    def test_r5_reject(self):
        assert _r5_sharpe(_ctx(rolling_sharpe=-0.5)) == RuleDecision.REJECT
    def test_r5_unknown(self):
        assert _r5_sharpe(_ctx(rolling_sharpe=None)) == RuleDecision.UNKNOWN
    def test_r6_pass(self):
        assert _r6_margin(_ctx(margin_ratio=0.25)) == RuleDecision.PASS
    def test_r6_reject(self):
        assert _r6_margin(_ctx(margin_ratio=0.95)) == RuleDecision.REJECT
    def test_r7_pass(self):
        assert _r7_liquidation_distance(_ctx(
            liquidation_price=45000.0, current_price=50000.0, is_long=True
        )) == RuleDecision.PASS
    def test_r7_reject(self):
        assert _r7_liquidation_distance(_ctx(
            liquidation_price=49500.0, current_price=50000.0, is_long=True
        )) == RuleDecision.REJECT
    def test_r7_no_liq_price(self):
        assert _r7_liquidation_distance(_ctx(
            current_price=50000.0, is_long=True
        )) == RuleDecision.PASS


class TestR8R9R10:
    def test_r8_pass(self):
        assert _r8_protection_coverage(_ctx(
            protected_positions=2, total_positions=2
        )) == RuleDecision.PASS
    def test_r8_reject(self):
        assert _r8_protection_coverage(_ctx(
            protected_positions=0, total_positions=2
        )) == RuleDecision.REJECT
    def test_r9_pass(self):
        assert _r9_account_capability(_ctx(
            can_trade=True, can_deposit=True, can_withdraw=False
        )) == RuleDecision.PASS
    def test_r9_reject(self):
        assert _r9_account_capability(_ctx(
            can_trade=False, can_deposit=True, can_withdraw=True
        )) == RuleDecision.REJECT
    def test_r10_pass(self):
        assert _r10_duplicate_order(_ctx(duplicate_orders_24h=0)) == RuleDecision.PASS
    def test_r10_reject(self):
        assert _r10_duplicate_order(_ctx(duplicate_orders_24h=3)) == RuleDecision.REJECT


class TestRiskRuleRegistry:
    def test_all_rules_registered(self):
        rule_ids = set(RiskRuleRegistry.RULES.keys())
        expected = {f"R{i}" for i in range(11)}
        missing = expected - rule_ids
        assert not missing, f"Missing: {missing}"

    def test_evaluate_all_pass(self):
        decisions = RiskRuleRegistry.evaluate_all(_ctx())
        for rid, d in decisions.items():
            assert d in (RuleDecision.PASS, RuleDecision.UNKNOWN), \
                f"Rule {rid} unexpectedly {d}"

    def test_evaluate_all_with_failures(self):
        bad = _ctx(leverage=10.0, concentration_pct=60.0, drawdown_pct=30.0)
        decisions = RiskRuleRegistry.evaluate_all(bad)
        has_reject = any(d == RuleDecision.REJECT for d in decisions.values())
        assert has_reject, "Expected at least one REJECTED"
