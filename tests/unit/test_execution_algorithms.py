"""PKG-23 自适应执行算法测试。TWAP/POV/Adaptive Slice/IOC/PostOnly/Passive/Emergency。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from beidou_safety.execution.algorithms import (
    AdaptiveSliceAlgorithm,
    BaseExecutionAlgorithm,
    EmergencyReduceOnlyAlgorithm,
    ExecutionAlgorithmSelector,
    ExecutionAlgorithmType,
    ExecutionContext,
    ExecutionPlan,
    IOCAlgorithm,
    MarketableLimitAlgorithm,
    OrderSlice,
    PassiveAlgorithm,
    PostOnlyAlgorithm,
    POVAlgorithm,
    SliceInvariantChecker,
    TWAPAlgorithm,
)
from beidou_shared.types import (
    InstrumentId,
    OrderId,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    TimeInForce,
    VenueId,
    VenueInstrument,
)


def _make_ctx(
    side: OrderSide = OrderSide.BUY,
    urgency: float = 0.5,
    spread_bps: float = 5.0,
    bid_depth: float = 50000.0,
    ask_depth: float = 50000.0,
    limit_price: float | None = 50000.0,
    net_alpha_bps: float = 10.0,
    predicted_cost_bps: float = 2.0,
    **kwargs,
) -> ExecutionContext:
    return ExecutionContext(
        venue_instrument=VenueInstrument(
            venue_id=VenueId("BINANCE"),
            instrument_id=InstrumentId("BTCUSDT"),
        ),
        side=side,
        total_quantity=Quantity(amount="1.0"),
        limit_price=Price(amount=str(limit_price)) if limit_price else None,
        urgency=urgency,
        best_bid=Price(amount="50000.0"),
        best_ask=Price(amount="50005.0"),
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        spread_bps=spread_bps,
        net_alpha_bps=net_alpha_bps,
        predicted_cost_bps=predicted_cost_bps,
        hard_slippage_limit_bps=kwargs.get("hard_slippage_limit_bps", 50.0),
        alpha_decay_seconds=kwargs.get("alpha_decay_seconds", 60.0),
        min_quantity=kwargs.get("min_quantity", 0.001),  # PKG13: venue rules default
        reduce_only=kwargs.get("reduce_only", False),
    )


FIXED_ORDER_ID = OrderId("order-001")


class TestPostOnlyAlgorithm:
    def test_can_handle_low_urgency_with_limit(self):
        ctx = _make_ctx(urgency=0.2, spread_bps=3.0, limit_price=49900.0)
        algo = PostOnlyAlgorithm()
        assert algo.can_handle(ctx)

    def test_cannot_handle_high_urgency(self):
        ctx = _make_ctx(urgency=0.8, spread_bps=3.0)
        algo = PostOnlyAlgorithm()
        assert not algo.can_handle(ctx)

    def test_plan_creates_single_slice(self):
        ctx = _make_ctx(urgency=0.2, spread_bps=3.0, limit_price=49900.0)
        algo = PostOnlyAlgorithm()
        plan = algo.plan(ctx, FIXED_ORDER_ID)
        assert plan.algorithm == ExecutionAlgorithmType.POST_ONLY
        assert len(plan.slices) == 1
        assert plan.slices[0].time_in_force == TimeInForce.GTX  # P1-020: 真正 maker-only


class TestPassiveAlgorithm:
    def test_can_handle_low_spread_low_urgency(self):
        ctx = _make_ctx(urgency=0.1, spread_bps=5.0)
        algo = PassiveAlgorithm()
        assert algo.can_handle(ctx)

    def test_cannot_handle_high_spread(self):
        ctx = _make_ctx(urgency=0.1, spread_bps=15.0)
        algo = PassiveAlgorithm()
        assert not algo.can_handle(ctx)

    def test_plan_uses_side_specific_best_price(self):
        buy_plan = PassiveAlgorithm().plan(_make_ctx(side=OrderSide.BUY), FIXED_ORDER_ID)
        sell_plan = PassiveAlgorithm().plan(_make_ctx(side=OrderSide.SELL), FIXED_ORDER_ID)
        assert buy_plan.slices[0].price == Price(amount="50000.0")
        assert sell_plan.slices[0].price == Price(amount="50005.0")


class TestMarketableLimitAlgorithm:
    def test_can_handle_with_limit(self):
        ctx = _make_ctx(urgency=0.5, limit_price=50100.0)
        algo = MarketableLimitAlgorithm()
        assert algo.can_handle(ctx)

    def test_plan_uses_limit_price(self):
        ctx = _make_ctx(urgency=0.5, limit_price=50100.0)
        algo = MarketableLimitAlgorithm()
        plan = algo.plan(ctx, FIXED_ORDER_ID)
        assert plan.algorithm == ExecutionAlgorithmType.MARKETABLE_LIMIT
        assert plan.slices[0].time_in_force == TimeInForce.IOC
        assert isinstance(plan.slices[0].price, Price)
        assert plan.slices[0].price.amount == "50005.0"

    def test_plan_cancels_without_a_limit_price(self):
        plan = MarketableLimitAlgorithm().plan(_make_ctx(limit_price=None), FIXED_ORDER_ID)
        assert plan.is_canceled
        assert plan.cancel_reason == "MARKETABLE_LIMIT_MISSING_LIMIT_PRICE"

    def test_sell_plan_never_crosses_below_limit(self):
        ctx = _make_ctx(side=OrderSide.SELL, limit_price=49900.0)
        plan = MarketableLimitAlgorithm().plan(ctx, FIXED_ORDER_ID)
        assert plan.slices[0].price == Price(amount="50000.0")


class TestIOCAlgorithm:
    def test_can_handle_high_urgency(self):
        ctx = _make_ctx(urgency=0.6)
        algo = IOCAlgorithm()
        assert algo.can_handle(ctx)

    def test_plan_ioc_single(self):
        ctx = _make_ctx(urgency=0.7)
        algo = IOCAlgorithm()
        plan = algo.plan(ctx, FIXED_ORDER_ID)
        assert plan.algorithm == ExecutionAlgorithmType.IOC
        assert plan.slices[0].time_in_force == TimeInForce.IOC


class TestTWAPAlgorithm:
    def test_can_handle_moderate_urgency(self):
        ctx = _make_ctx(urgency=0.4, alpha_decay_seconds=3600.0)
        algo = TWAPAlgorithm(slice_count=10, interval_seconds=60.0)
        assert algo.can_handle(ctx)

    def test_plan_generates_correct_slices(self):
        ctx = _make_ctx(urgency=0.3, alpha_decay_seconds=3600.0, net_alpha_bps=10.0, predicted_cost_bps=3.0)
        algo = TWAPAlgorithm(slice_count=5, interval_seconds=60.0)
        plan = algo.plan(ctx, FIXED_ORDER_ID)
        assert not plan.is_canceled
        assert len(plan.slices) == 5
        assert plan.algorithm == ExecutionAlgorithmType.TWAP

    def test_cancels_when_cost_exceeds_alpha(self):
        ctx = _make_ctx(urgency=0.3, net_alpha_bps=2.0, predicted_cost_bps=5.0)
        algo = TWAPAlgorithm(slice_count=5, interval_seconds=60.0)
        plan = algo.plan(ctx, FIXED_ORDER_ID)
        assert plan.is_canceled

    def test_slices_have_sequence_numbers(self):
        ctx = _make_ctx(urgency=0.3, alpha_decay_seconds=3600.0, net_alpha_bps=10.0, predicted_cost_bps=3.0)
        algo = TWAPAlgorithm(slice_count=3, interval_seconds=60.0)
        plan = algo.plan(ctx, FIXED_ORDER_ID)
        assert plan.slices[0].sequence_number == 0
        assert plan.slices[1].sequence_number == 1
        assert plan.slices[2].sequence_number == 2

    def test_total_quantity_equals_original(self):
        ctx = _make_ctx(urgency=0.3, alpha_decay_seconds=3600.0, net_alpha_bps=10.0, predicted_cost_bps=3.0)
        algo = TWAPAlgorithm(slice_count=4, interval_seconds=60.0)
        plan = algo.plan(ctx, FIXED_ORDER_ID)
        assert abs(plan.total_quantity() - 1.0) < 0.01

    def test_rolling_replan_covers_completion_cost_and_success_paths(self):
        algo = TWAPAlgorithm(slice_count=4, interval_seconds=30.0)
        ctx = _make_ctx()
        assert algo.rolling_replan(ctx, FIXED_ORDER_ID, remaining_qty=1.0, slices_completed=4).is_canceled
        assert algo.rolling_replan(ctx, FIXED_ORDER_ID, remaining_qty=0.0, slices_completed=1).is_canceled

        expensive = replace(ctx, net_alpha_bps=1.0, predicted_cost_bps=2.0)
        assert algo.rolling_replan(expensive, FIXED_ORDER_ID, remaining_qty=1.0, slices_completed=1).is_canceled

        replanned = algo.rolling_replan(ctx, FIXED_ORDER_ID, remaining_qty=0.75, slices_completed=1)
        assert not replanned.is_canceled
        assert len(replanned.slices) == 3
        assert abs(replanned.total_quantity() - 0.75) < 1e-12


class TestPOVAlgorithm:
    def test_can_handle_with_depth(self):
        ctx = _make_ctx(urgency=0.3, bid_depth=100000.0, ask_depth=100000.0, net_alpha_bps=10.0, predicted_cost_bps=3.0)
        algo = POVAlgorithm(participation_rate=0.1)
        assert algo.can_handle(ctx)

    def test_plan_respects_participation_rate(self):
        ctx = _make_ctx(
            urgency=0.3,
            bid_depth=100000.0,
            ask_depth=100000.0,
            total_quantity=0.5,
            net_alpha_bps=10.0,
            predicted_cost_bps=3.0,
        )
        algo = POVAlgorithm(participation_rate=0.1)
        plan = algo.plan(ctx, FIXED_ORDER_ID)
        assert not plan.is_canceled
        for s in plan.slices:
            assert float(s.quantity.amount) <= 100000.0 * 0.1  # <= max participation

    def test_cancels_when_cost_high(self):
        ctx = _make_ctx(urgency=0.3, bid_depth=100000.0, net_alpha_bps=3.0, predicted_cost_bps=8.0)
        algo = POVAlgorithm(participation_rate=0.1)
        plan = algo.plan(ctx, FIXED_ORDER_ID)
        assert plan.is_canceled

    def test_plan_has_bounded_remainder_fallback(self):
        ctx = _make_ctx(
            bid_depth=1.0,
            ask_depth=1.0,
            net_alpha_bps=10.0,
            predicted_cost_bps=1.0,
        )
        plan = POVAlgorithm(participation_rate=0.01).plan(ctx, FIXED_ORDER_ID)
        assert len(plan.slices) == 52
        assert abs(plan.total_quantity() - 1.0) < 1e-12

    def test_plan_rejects_non_positive_approved_quantity(self):
        ctx = replace(_make_ctx(), total_quantity=Quantity(amount="-1.0"))
        with pytest.raises(RuntimeError, match="quantity conservation"):
            POVAlgorithm().plan(ctx, FIXED_ORDER_ID)


class TestAdaptiveSliceAlgorithm:
    def test_can_handle_normal_market(self):
        ctx = _make_ctx(urgency=0.5)
        algo = AdaptiveSliceAlgorithm()
        assert algo.can_handle(ctx)

    def test_plan_adapts_to_market(self):
        ctx = _make_ctx(
            urgency=0.4,
            alpha_decay_seconds=3600.0,
            net_alpha_bps=15.0,
            predicted_cost_bps=3.0,
            spread_bps=3.0,
            min_quantity=0.001,
        )
        algo = AdaptiveSliceAlgorithm(min_slice_pct=0.05, max_slice_pct=0.25)
        plan = algo.plan(ctx, FIXED_ORDER_ID)
        assert not plan.is_canceled
        assert len(plan.slices) > 0

    def test_plan_uses_marketable_crossing_price(self) -> None:
        """BUY 穿透 best_ask、SELL 穿透 best_bid（marketable limit）。

        同侧挂价（BUY@bid / SELL@ask）+ IOC 组合在流动性不足时必然
        EXPIRED —— 2026-08-14 15:03 实测 5 单 3 秒全过期、零成交。
        穿透价保留限价保护的同时保证撮合。
        """
        buy_ctx = _make_ctx(side=OrderSide.BUY, urgency=0.4, min_quantity=0.001)
        buy_plan = AdaptiveSliceAlgorithm(min_slice_pct=0.05, max_slice_pct=0.25).plan(buy_ctx, FIXED_ORDER_ID)
        for s in buy_plan.slices:
            assert s.order_type is OrderType.LIMIT
            assert s.time_in_force is TimeInForce.IOC
            assert float(s.price.amount) == float(buy_ctx.best_ask.amount)  # 穿透卖一

        sell_ctx = _make_ctx(side=OrderSide.SELL, urgency=0.4, min_quantity=0.001)
        sell_plan = AdaptiveSliceAlgorithm(min_slice_pct=0.05, max_slice_pct=0.25).plan(sell_ctx, FIXED_ORDER_ID)
        for s in sell_plan.slices:
            assert float(s.price.amount) == float(sell_ctx.best_bid.amount)  # 穿透买一

    def test_cancels_high_cost(self):
        ctx = _make_ctx(urgency=0.4, net_alpha_bps=3.0, predicted_cost_bps=10.0)
        algo = AdaptiveSliceAlgorithm()
        plan = algo.plan(ctx, FIXED_ORDER_ID)
        assert plan.is_canceled

    def test_market_condition_slice_percentages(self):
        algo = AdaptiveSliceAlgorithm(min_slice_pct=0.05, max_slice_pct=0.25)
        liquid = _make_ctx(spread_bps=3.0, bid_depth=100001.0)
        wide = _make_ctx(spread_bps=25.0, bid_depth=1.0)
        normal = _make_ctx(spread_bps=10.0, bid_depth=1.0)
        assert algo._determine_slice_pct(liquid) == 0.25
        assert algo._determine_slice_pct(wide) == 0.05
        assert algo._determine_slice_pct(normal) == 0.05

    def test_plan_requires_venue_quantity_rules(self):
        ctx = _make_ctx(min_quantity=0.0)
        with pytest.raises(ValueError, match="VENUE_RULES_UNKNOWN"):
            AdaptiveSliceAlgorithm().plan(ctx, FIXED_ORDER_ID)

    def test_plan_uses_side_price_fallbacks_and_minimums(self):
        algo = AdaptiveSliceAlgorithm(min_slice_pct=0.05, max_slice_pct=0.25)
        buy_without_limit = replace(_make_ctx(limit_price=None), best_ask=Price(amount="50005.0"))
        sell_without_limit = replace(_make_ctx(side=OrderSide.SELL, limit_price=None), best_bid=Price(amount="50000.0"))
        assert algo.plan(buy_without_limit, FIXED_ORDER_ID).slices
        assert algo.plan(sell_without_limit, FIXED_ORDER_ID).slices

        no_market_price = replace(buy_without_limit, best_bid=None, best_ask=None)
        assert algo.plan(no_market_price, FIXED_ORDER_ID).slices

        merged = algo.plan(_make_ctx(min_quantity=2.0), FIXED_ORDER_ID)
        assert len(merged.slices) == 1
        assert abs(merged.total_quantity() - 1.0) < 1e-12

        decaying = _make_ctx(alpha_decay_seconds=0.01, net_alpha_bps=10.0, min_quantity=0.05)
        decaying_plan = algo.plan(decaying, FIXED_ORDER_ID)
        assert decaying_plan.slices
        assert decaying_plan.total_quantity() <= 1.0 + 1e-12


class TestEmergencyReduceOnlyAlgorithm:
    def test_can_handle_high_urgency_sell(self):
        ctx = _make_ctx(side=OrderSide.SELL, urgency=0.9, reduce_only=True)
        algo = EmergencyReduceOnlyAlgorithm()
        assert algo.can_handle(ctx)

    def test_cannot_handle_low_urgency(self):
        ctx = _make_ctx(side=OrderSide.SELL, urgency=0.5)
        algo = EmergencyReduceOnlyAlgorithm()
        assert not algo.can_handle(ctx)

    def test_can_handle_buy_for_short_positions(self):
        """PKG13: Emergency 现在也处理 BUY（SHORT 仓位需要 BUY reduce-only）。"""
        ctx = _make_ctx(side=OrderSide.BUY, urgency=0.9, reduce_only=True)
        algo = EmergencyReduceOnlyAlgorithm()
        assert algo.can_handle(ctx)

    def test_cannot_handle_risk_increasing_order_even_when_urgent(self):
        ctx = _make_ctx(side=OrderSide.BUY, urgency=0.99, reduce_only=False)
        algo = EmergencyReduceOnlyAlgorithm()
        assert not algo.can_handle(ctx)

    def test_plan_market_order(self):
        ctx = _make_ctx(side=OrderSide.SELL, urgency=0.95, predicted_cost_bps=50.0, reduce_only=True)
        algo = EmergencyReduceOnlyAlgorithm()
        plan = algo.plan(ctx, FIXED_ORDER_ID)
        assert plan.algorithm == ExecutionAlgorithmType.EMERGENCY_REDUCE_ONLY
        assert plan.slices[0].order_type == OrderType.MARKET
        assert not plan.is_canceled


class TestExecutionAlgorithmSelector:
    def test_selects_post_only_for_low_urgency(self):
        sel = ExecutionAlgorithmSelector()
        ctx = _make_ctx(urgency=0.1, spread_bps=3.0, limit_price=49900.0)
        algo = sel.select(ctx)
        assert algo is not None
        assert algo.algorithm_type in sel._approved

    def test_selects_ioc_for_high_urgency(self):
        sel = ExecutionAlgorithmSelector()
        ctx = _make_ctx(urgency=0.7)
        algo = sel.select(ctx)
        assert algo is not None

    def test_selects_emergency_for_critical(self):
        sel = ExecutionAlgorithmSelector()
        ctx = _make_ctx(side=OrderSide.SELL, urgency=0.95, reduce_only=True)
        algo = sel.select(ctx)
        assert algo is not None
        assert algo.algorithm_type == ExecutionAlgorithmType.EMERGENCY_REDUCE_ONLY

    def test_never_falls_back_to_emergency_for_risk_increase(self):
        sel = ExecutionAlgorithmSelector(approved_algorithm_types={ExecutionAlgorithmType.EMERGENCY_REDUCE_ONLY})
        ctx = _make_ctx(side=OrderSide.BUY, urgency=0.99, reduce_only=False)
        assert sel.select(ctx) is None

    def test_restricted_to_approved_set(self):
        sel = ExecutionAlgorithmSelector(approved_algorithm_types={ExecutionAlgorithmType.TWAP})
        ctx = _make_ctx(urgency=0.3, alpha_decay_seconds=3600.0, net_alpha_bps=10.0, predicted_cost_bps=3.0)
        algo = sel.select(ctx)
        assert algo is not None
        assert algo.algorithm_type == ExecutionAlgorithmType.TWAP

    def test_only_approved_algorithms_available(self):
        sel = ExecutionAlgorithmSelector(approved_algorithm_types={ExecutionAlgorithmType.TWAP})
        approved = sel.approved_algorithms
        assert len(approved) == 1
        assert approved[0].algorithm_type == ExecutionAlgorithmType.TWAP

    def test_update_quality_improves_score(self):
        sel = ExecutionAlgorithmSelector()
        old_score = sel._quality_scores[ExecutionAlgorithmType.TWAP]
        sel.update_quality(ExecutionAlgorithmType.TWAP, realized_cost_bps=1.0, slippage_bps=0.5)
        new_score = sel._quality_scores[ExecutionAlgorithmType.TWAP]
        assert new_score > old_score

    def test_update_quality_reduces_score(self):
        sel = ExecutionAlgorithmSelector()
        old_score = sel._quality_scores[ExecutionAlgorithmType.TWAP]
        sel.update_quality(ExecutionAlgorithmType.TWAP, realized_cost_bps=80.0, slippage_bps=50.0)
        new_score = sel._quality_scores[ExecutionAlgorithmType.TWAP]
        assert new_score < old_score

    def test_fallback_when_none_applicable(self):
        sel = ExecutionAlgorithmSelector(approved_algorithm_types={ExecutionAlgorithmType.POST_ONLY})
        # Post-only 在高紧迫性下不可用 → 无可选算法即不交易(Fail-Closed)
        ctx = _make_ctx(urgency=0.9, spread_bps=5.0)
        algo = sel.select(ctx)
        # Fail-Closed: 无法选择算法时返回 None，调用者应拒绝交易
        assert algo is None or algo.algorithm_type == ExecutionAlgorithmType.POST_ONLY

    def test_set_approved_replaces_the_authorized_set(self):
        sel = ExecutionAlgorithmSelector()
        sel.set_approved({ExecutionAlgorithmType.IOC})
        assert [algo.algorithm_type for algo in sel.approved_algorithms] == [ExecutionAlgorithmType.IOC]


class TestSliceInvariantChecker:
    def test_valid_slice_passes(self):
        ctx = _make_ctx(spread_bps=5.0, hard_slippage_limit_bps=50.0)
        slice_ = OrderSlice(
            slice_id="slice-1",
            parent_order_id=FIXED_ORDER_ID,
            quantity=Quantity(amount="0.5"),
            price=Price(amount="50000"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            algorithm=ExecutionAlgorithmType.TWAP,
            sequence_number=0,
            remaining_alpha_bps=5.0,
        )
        ok, msg = SliceInvariantChecker.check_slice(slice_, ctx)
        assert ok, msg

    def test_zero_quantity_fails(self):
        ctx = _make_ctx()
        slice_ = OrderSlice(
            slice_id="slice-2",
            parent_order_id=FIXED_ORDER_ID,
            quantity=Quantity(amount="0"),
            price=Price(amount="50000"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            algorithm=ExecutionAlgorithmType.TWAP,
            sequence_number=0,
        )
        ok, _msg = SliceInvariantChecker.check_slice(slice_, ctx)
        assert not ok

    def test_exceeds_approved_quantity_fails(self):
        ctx = _make_ctx()
        slice_ = OrderSlice(
            slice_id="slice-3",
            parent_order_id=FIXED_ORDER_ID,
            quantity=Quantity(amount="5.0"),  # > approved 1.0
            price=Price(amount="50000"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            algorithm=ExecutionAlgorithmType.TWAP,
            sequence_number=0,
        )
        ok, _msg = SliceInvariantChecker.check_slice(slice_, ctx)
        assert not ok

    def test_exceeds_slippage_limit_fails(self):
        ctx = _make_ctx(spread_bps=80.0, hard_slippage_limit_bps=50.0)
        slice_ = OrderSlice(
            slice_id="slice-4",
            parent_order_id=FIXED_ORDER_ID,
            quantity=Quantity(amount="0.5"),
            price=Price(amount="50000"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            algorithm=ExecutionAlgorithmType.TWAP,
            sequence_number=0,
        )
        ok, _msg = SliceInvariantChecker.check_slice(slice_, ctx)
        assert not ok

    def test_alpha_depleted_fails(self):
        ctx = _make_ctx()
        slice_ = OrderSlice(
            slice_id="slice-5",
            parent_order_id=FIXED_ORDER_ID,
            quantity=Quantity(amount="0.5"),
            price=Price(amount="50000"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            algorithm=ExecutionAlgorithmType.TWAP,
            sequence_number=0,
            remaining_alpha_bps=-0.1,
        )
        ok, _msg = SliceInvariantChecker.check_slice(slice_, ctx)
        assert not ok

    def test_validate_plan_safety(self):
        ctx = _make_ctx(spread_bps=5.0, hard_slippage_limit_bps=50.0)
        algo = TWAPAlgorithm(slice_count=5, interval_seconds=60.0)
        ctx_valid = _make_ctx(
            urgency=0.3,
            alpha_decay_seconds=3600.0,
            net_alpha_bps=10.0,
            predicted_cost_bps=3.0,
            spread_bps=5.0,
            hard_slippage_limit_bps=50.0,
        )
        plan = algo.plan(ctx_valid, FIXED_ORDER_ID)
        ok, msg = SliceInvariantChecker.validate_plan(plan, ctx)
        assert ok, msg

    def test_canceled_plan_is_always_valid(self):
        ctx = _make_ctx()
        plan = ExecutionPlan(algorithm=ExecutionAlgorithmType.TWAP, is_canceled=True, cancel_reason="test")
        ok, _msg = SliceInvariantChecker.validate_plan(plan, ctx)
        assert ok

    def test_validate_plan_reports_invalid_slice_and_total(self):
        ctx = _make_ctx()
        invalid_slice = OrderSlice(
            slice_id="invalid",
            parent_order_id=FIXED_ORDER_ID,
            quantity=Quantity(amount="5.0"),
            price=Price(amount="50000"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            algorithm=ExecutionAlgorithmType.TWAP,
            sequence_number=0,
        )
        invalid_plan = ExecutionPlan(algorithm=ExecutionAlgorithmType.TWAP, slices=[invalid_slice])
        ok, message = SliceInvariantChecker.validate_plan(invalid_plan, ctx)
        assert not ok and "invalid" in message

        first = replace(invalid_slice, slice_id="first", quantity=Quantity(amount="0.6"))
        second = replace(invalid_slice, slice_id="second", quantity=Quantity(amount="0.6"))
        total_plan = ExecutionPlan(algorithm=ExecutionAlgorithmType.TWAP, slices=[first, second])
        ok, message = SliceInvariantChecker.validate_plan(total_plan, ctx)
        assert not ok and "approved quantity" in message

    def test_execution_plan_invariant_summary(self):
        valid = ExecutionPlan(
            algorithm=ExecutionAlgorithmType.TWAP,
            slices=[
                OrderSlice(
                    slice_id="valid",
                    parent_order_id=FIXED_ORDER_ID,
                    quantity=Quantity(amount="0.1"),
                    price=Price(amount="50000"),
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                    algorithm=ExecutionAlgorithmType.TWAP,
                    sequence_number=0,
                )
            ],
        )
        invalid = replace(valid, slices=[replace(valid.slices[0], invariants_check_passed=False)])
        assert valid.all_invariants_pass()
        assert not invalid.all_invariants_pass()


def test_base_algorithm_abstract_contracts_raise() -> None:
    ctx = _make_ctx()
    with pytest.raises(NotImplementedError):
        BaseExecutionAlgorithm.can_handle(PostOnlyAlgorithm(), ctx)
    with pytest.raises(NotImplementedError):
        BaseExecutionAlgorithm.plan(PostOnlyAlgorithm(), ctx, FIXED_ORDER_ID)


def test_base_invariant_checks_cover_market_data_and_spread_failures() -> None:
    algo = PostOnlyAlgorithm()
    spread_bad = replace(_make_ctx(), spread_bps=60.0, hard_slippage_limit_bps=50.0)
    assert algo.check_invariants(spread_bad) == (False, "Spread 60.0bps exceeds hard slippage limit 50.0bps")
    assert algo.check_invariants(replace(_make_ctx(), best_ask=None))[0] is False
    assert algo.check_invariants(replace(_make_ctx(side=OrderSide.SELL), best_bid=None))[0] is False


class TestExecutionContext:
    def test_context_creation(self):
        ctx = _make_ctx()
        assert ctx.side == OrderSide.BUY
        assert float(ctx.total_quantity.amount) == 1.0
        assert ctx.hard_slippage_limit_bps == 50.0
        assert ctx.spread_bps == 5.0

    def test_context_urgency_range(self):
        ctx = _make_ctx(urgency=1.0)
        assert 0 <= ctx.urgency <= 1

    def test_context_high_urgency_emergency(self):
        ctx = _make_ctx(side=OrderSide.SELL, urgency=0.99, predicted_cost_bps=100.0)
        assert ctx.urgency >= 0.8
