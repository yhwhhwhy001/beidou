"""Adversarial contracts for R0-R10 evaluation and registry completeness."""

from __future__ import annotations

from typing import ClassVar

import pytest

from beidou_safety.risk.rules import (
    RiskRule,
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


@pytest.mark.parametrize(
    ("rule", "context"),
    [
        (_r2_drawdown, {"drawdown_pct": -1, "max_drawdown_pct": 10}),
        (_r2_drawdown, {"drawdown_pct": "bad", "max_drawdown_pct": 10}),
        (_r2_drawdown, {"drawdown_pct": float("nan"), "max_drawdown_pct": 10}),
        (_r3_daily_loss, {"daily_loss_pct": -1, "max_daily_loss_pct": 10}),
        (_r3_daily_loss, {"daily_loss_pct": "bad", "max_daily_loss_pct": 10}),
        (_r4_consecutive_losses, {"consecutive_losses": -1, "max_consecutive_losses": 5}),
        (_r4_consecutive_losses, {"consecutive_losses": 1.5, "max_consecutive_losses": 5}),
        (_r4_consecutive_losses, {"consecutive_losses": True, "max_consecutive_losses": 5}),
        (_r5_sharpe, {"rolling_sharpe": "bad", "min_sharpe_rolling": 0}),
        (_r5_sharpe, {"rolling_sharpe": float("inf"), "min_sharpe_rolling": 0}),
        (_r10_duplicate_order, {"duplicate_orders_24h": -1}),
        (_r10_duplicate_order, {"duplicate_orders_24h": 1.5}),
        (_r10_duplicate_order, {"duplicate_orders_24h": True}),
    ],
)
def test_untrusted_numeric_facts_remain_unknown(rule, context) -> None:
    assert rule(context) is RuleDecision.UNKNOWN


@pytest.mark.parametrize(
    ("rule", "context"),
    [
        (_r0_max_leverage, {"max_leverage": 2}),
        (_r0_max_leverage, {"leverage": "bad", "max_leverage": 2}),
        (_r0_max_leverage, {"leverage": 0, "max_leverage": 2}),
        (_r1_concentration, {"max_concentration_pct": 20}),
        (_r1_concentration, {"concentration_pct": "bad", "max_concentration_pct": 20}),
        (_r1_concentration, {"concentration_pct": 0, "max_concentration_pct": 20}),
        (_r2_drawdown, {"drawdown_pct": 1}),
        (_r3_daily_loss, {"daily_loss_pct": 1}),
        (_r4_consecutive_losses, {"consecutive_losses": 1}),
        (_r4_consecutive_losses, {"consecutive_losses": "bad", "max_consecutive_losses": 2}),
        (_r5_sharpe, {"rolling_sharpe": 1}),
        (_r6_margin, {"margin_ratio": 0.1}),
        (_r6_margin, {"margin_ratio": "bad", "max_margin_ratio": 0.5}),
        (_r6_margin, {"margin_ratio": 0, "max_margin_ratio": 0.5}),
        (_r7_liquidation_distance, {"position_qty": "bad"}),
        (_r7_liquidation_distance, {"position_qty": float("inf")}),
        (_r7_liquidation_distance, {"position_qty": 0, "liquidation_price": 1}),
        (_r7_liquidation_distance, {"position_qty": 1, "liquidation_price": "bad", "current_price": 100}),
        (_r7_liquidation_distance, {"position_qty": 1, "liquidation_price": 90, "current_price": float("inf")}),
        (_r7_liquidation_distance, {"position_qty": 1, "liquidation_price": 0, "current_price": 100}),
        (_r7_liquidation_distance, {"position_qty": 1, "liquidation_price": 90, "current_price": 100}),
        (
            _r7_liquidation_distance,
            {"position_qty": 1, "liquidation_price": 90, "current_price": 100, "min_liquidation_distance_pct": "bad"},
        ),
        (
            _r7_liquidation_distance,
            {"position_qty": 1, "liquidation_price": 90, "current_price": 100, "min_liquidation_distance_pct": -1},
        ),
        (_r8_protection_coverage, {"protected_positions": "bad", "total_positions": 1}),
        (_r8_protection_coverage, {"protected_positions": float("inf"), "total_positions": 1}),
        (_r8_protection_coverage, {"protected_positions": -1, "total_positions": 1}),
        (_r8_protection_coverage, {"protected_positions": 2, "total_positions": 1}),
        (_r9_account_capability, {"can_trade": 1, "can_withdraw": False}),
        (_r9_account_capability, {"can_trade": False, "can_withdraw": False}),
        (_r10_duplicate_order, {}),
        (_r10_duplicate_order, {"duplicate_orders_24h": "bad"}),
    ],
)
def test_missing_malformed_or_inconsistent_rule_facts_are_unknown(rule, context) -> None:
    assert rule(context) is RuleDecision.UNKNOWN


def test_zero_open_positions_need_zero_protected_positions() -> None:
    assert _r8_protection_coverage({"protected_positions": 0, "total_positions": 0}) is RuleDecision.PASS


def test_numeric_strings_are_normalized_before_decision() -> None:
    assert _r2_drawdown({"drawdown_pct": "2", "max_drawdown_pct": "5"}) is RuleDecision.PASS
    assert _r3_daily_loss({"daily_loss_pct": "7", "max_daily_loss_pct": "5"}) is RuleDecision.REJECT
    assert _r4_consecutive_losses({"consecutive_losses": "2", "max_consecutive_losses": "5"}) is RuleDecision.PASS
    assert _r5_sharpe({"rolling_sharpe": "1.5", "min_sharpe_rolling": "1"}) is RuleDecision.PASS
    assert _r10_duplicate_order({"duplicate_orders_24h": "0"}) is RuleDecision.PASS


def test_liquidation_distance_uses_signed_position_direction() -> None:
    base = {"current_price": 100, "min_liquidation_distance_pct": 5}
    assert _r7_liquidation_distance({**base, "position_qty": 1, "liquidation_price": 90}) is RuleDecision.PASS
    assert _r7_liquidation_distance({**base, "position_qty": -1, "liquidation_price": 110}) is RuleDecision.PASS
    assert _r7_liquidation_distance({**base, "position_qty": 1, "liquidation_price": 110}) is RuleDecision.UNKNOWN
    assert _r7_liquidation_distance({**base, "position_qty": -1, "liquidation_price": 90}) is RuleDecision.UNKNOWN


def test_registry_approval_requires_the_complete_exact_rule_set() -> None:
    all_pass = dict.fromkeys(RiskRuleRegistry.RULES, RuleDecision.PASS)
    assert RiskRuleRegistry.is_approved(all_pass)
    assert not RiskRuleRegistry.is_approved({})
    assert not RiskRuleRegistry.is_approved({"R0": RuleDecision.PASS})
    assert not RiskRuleRegistry.is_approved({**all_pass, "R11": RuleDecision.PASS})
    assert not RiskRuleRegistry.is_approved({**all_pass, "R0": "PASS"})  # type: ignore[dict-item]


def test_registry_rejects_invalid_or_conflicting_definitions() -> None:
    class Registry(RiskRuleRegistry):
        RULES: ClassVar[dict[str, RiskRule]] = {}

    rule = RiskRule("R0", "name", "description", 0, lambda _context: RuleDecision.PASS)
    Registry.register(rule)
    with pytest.raises(ValueError, match="already registered"):
        Registry.register(rule)
    with pytest.raises(ValueError, match="rule_id"):
        Registry.register(RiskRule("", "name", "description", 0, lambda _context: RuleDecision.PASS))
    with pytest.raises(ValueError, match="callable"):
        Registry.register(RiskRule("R1", "name", "description", 1, None))  # type: ignore[arg-type]


def test_registry_converts_invalid_callback_output_and_exception_to_unknown() -> None:
    class Registry(RiskRuleRegistry):
        RULES: ClassVar[dict[str, RiskRule]] = {
            "R0": RiskRule("R0", "bad", "bad", 0, lambda _context: "PASS"),  # type: ignore[arg-type]
            "R1": RiskRule("R1", "boom", "boom", 1, lambda _context: 1 / 0),
        }

    assert Registry.evaluate_all({}) == {"R0": RuleDecision.UNKNOWN, "R1": RuleDecision.UNKNOWN}
