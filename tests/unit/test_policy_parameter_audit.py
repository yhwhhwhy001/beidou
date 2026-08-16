"""签名策略参数治理测试（M00-F08）。

覆盖: _policy_float_audited（策略提供→生效;缺失→保守默认+WARN 不静默）、
ATR 止损钳制（默认与策略覆盖）、费率档与资本预算的策略覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beidou_core.engine import AutonomousEngine
from beidou_shared.types import MonetaryValue


def _bare_engine(*, policy: dict | None = None, can_write: bool = False, **attrs: object) -> AutonomousEngine:
    engine = object.__new__(AutonomousEngine)
    engine._policy_params = dict(policy or {})
    engine._can_write = can_write
    engine._policy_error = None
    for key, value in attrs.items():
        setattr(engine, key, value)
    return engine


class _FakeCostModel:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_fee_tier(self, venue_id, tier, maker_bps, taker_bps) -> None:
        self.calls.append((str(venue_id), tier, maker_bps, taker_bps))


def test_policy_float_audited_uses_policy_value() -> None:
    engine = _bare_engine(policy={"maker_fee_bps": 1.0})
    assert engine._policy_float_audited("maker_fee_bps", 2.0) == 1.0
    assert not getattr(engine, "_policy_fallback_warned_maker_fee_bps", False)


def test_policy_float_audited_falls_back_with_single_warn(capsys: pytest.CaptureFixture[str]) -> None:
    engine = _bare_engine()
    assert engine._policy_float_audited("maker_fee_bps", 2.0) == 2.0
    assert engine._policy_float_audited("maker_fee_bps", 2.0) == 2.0
    assert getattr(engine, "_policy_fallback_warned_maker_fee_bps", False)
    output = capsys.readouterr().out
    assert output.count("PARAMETER_MISSING_FALLBACK:maker_fee_bps=2.0") == 1  # 首次 WARN,不重复


def test_stop_loss_pct_defaults_clamp() -> None:
    engine = _bare_engine()
    assert engine._compute_stop_loss_pct(atr_pct=10.0) == 5.0  # 1.5×10 超上限 → 5%
    assert engine._compute_stop_loss_pct(atr_pct=0.5) == 1.0  # 0.75 低于下限 → 1%
    assert engine._compute_stop_loss_pct(atr_pct=2.0) == 3.0  # 区间内


def test_stop_loss_pct_policy_override() -> None:
    engine = _bare_engine(
        policy={"stop_loss_min_pct": 2.0, "stop_loss_max_pct": 10.0, "stop_loss_atr_multiplier": 3.0}
    )
    assert engine._compute_stop_loss_pct(atr_pct=2.0) == 6.0
    assert engine._compute_stop_loss_pct(atr_pct=0.5) == 2.0  # 下限策略覆盖
    assert engine._compute_stop_loss_pct(atr_pct=10.0) == 10.0  # 上限策略覆盖


def test_fee_tier_defaults_and_policy_override(capsys: pytest.CaptureFixture[str]) -> None:
    cost_model = _FakeCostModel()
    engine = _bare_engine(_cost_model=cost_model)
    engine._apply_fee_tier(SimpleNamespace())
    assert cost_model.calls[-1][1:] == ("vip1", 2.0, 4.0)
    assert "PARAMETER_MISSING_FALLBACK:maker_fee_bps" in capsys.readouterr().out

    engine2 = _bare_engine(policy={"maker_fee_bps": 0.5, "taker_fee_bps": 1.5}, _cost_model=cost_model)
    engine2._apply_fee_tier(SimpleNamespace())
    assert cost_model.calls[-1][1:] == ("vip1", 0.5, 1.5)


def test_capital_budget_defaults_and_policy_override(capsys: pytest.CaptureFixture[str]) -> None:
    engine = _bare_engine()
    budget = engine._capital_budget_amount(1000.0)
    assert isinstance(budget, MonetaryValue)
    assert float(budget.amount) == pytest.approx(100.0)
    assert "PARAMETER_MISSING_FALLBACK:capital_budget_ratio=0.1" in capsys.readouterr().out

    engine2 = _bare_engine(policy={"capital_budget_ratio": 0.2})
    assert float(engine2._capital_budget_amount(1000.0).amount) == pytest.approx(200.0)


def test_max_margin_ratio_reads_policy() -> None:
    engine = _bare_engine()
    assert engine._policy_float_audited("max_margin_ratio", 0.95) == 0.95
    engine2 = _bare_engine(policy={"max_margin_ratio": 0.8})
    assert engine2._policy_float_audited("max_margin_ratio", 0.95) == 0.8


# --- M00-F08-R2: 签名值域校验（对抗审查反例 J/K） ---


def test_out_of_range_signed_param_sets_policy_error() -> None:
    """越界签名值（margin 120%）必须置策略错误（write 阻断），不得静默放大风险。"""
    engine = _bare_engine(policy={"max_margin_ratio": 1.2})
    engine._validate_audited_policy_params()
    assert engine._policy_error is not None
    assert "max_margin_ratio" in engine._policy_error


def test_non_numeric_signed_param_sets_policy_error() -> None:
    engine = _bare_engine(policy={"maker_fee_bps": "free"})
    engine._validate_audited_policy_params()
    assert engine._policy_error is not None
    assert "maker_fee_bps" in engine._policy_error


def test_non_finite_signed_param_sets_policy_error() -> None:
    engine = _bare_engine(policy={"stop_loss_max_pct": float("inf")})
    engine._validate_audited_policy_params()
    assert engine._policy_error is not None


def test_stop_loss_cross_constraint_invalid() -> None:
    engine = _bare_engine(policy={"stop_loss_min_pct": 5.0, "stop_loss_max_pct": 2.0})
    engine._validate_audited_policy_params()
    assert engine._policy_error is not None


def test_valid_signed_params_do_not_set_policy_error() -> None:
    engine = _bare_engine(
        policy={
            "max_margin_ratio": 0.8,
            "maker_fee_bps": 1.0,
            "taker_fee_bps": 2.0,
            "stop_loss_min_pct": 1.0,
            "stop_loss_max_pct": 5.0,
            "stop_loss_atr_multiplier": 1.5,
            "capital_budget_ratio": 0.1,
        }
    )
    engine._validate_audited_policy_params()
    assert engine._policy_error is None
