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


# --- M09（仓位/杠杆参数治理） ---


def test_adaptive_leverage_defaults_match_legacy_behavior() -> None:
    """M09-F01: 默认档位与旧硬编码行为一致（行为兼容）。"""
    from beidou_core.engine import adaptive_leverage

    assert adaptive_leverage(0.1) == 3.0
    assert adaptive_leverage(0.3) == 2.0
    assert adaptive_leverage(0.5) == 1.0
    assert adaptive_leverage(0.9) == 0.5
    assert adaptive_leverage(0.0) == 0.0  # UNKNOWN → no new risk
    assert adaptive_leverage(float("nan")) == 0.0


def test_adaptive_leverage_policy_override() -> None:
    """M09-F01: 档位/阈值经参数覆盖。"""
    from beidou_core.engine import adaptive_leverage

    custom = adaptive_leverage(
        0.24,
        levels=(4.0, 2.5, 1.5, 0.75),
        thresholds=(0.15, 0.25, 0.5),
    )
    assert custom == 2.5  # 0.24 落在 tier2(<0.25)


def test_adaptive_position_pct_params() -> None:
    """M09-F02: 基数与惩罚参数化,默认行为不变。"""
    from beidou_core.engine import adaptive_position_pct

    base = adaptive_position_pct(0.5, 0.3, 10.0)
    custom = adaptive_position_pct(0.5, 0.3, 10.0, base_pct=0.04)
    assert custom == base * 2  # 基数翻倍,惩罚相同
    assert adaptive_position_pct(0.0, 0.3, 10.0) == 0.0  # 零强度
    assert adaptive_position_pct(0.5, float("inf"), 10.0) == 0.0  # 非有限


def test_position_cap_ratio_policy_key_validated() -> None:
    """M09-F03: position_cap_ratio 越界值被策略校验拒绝。"""
    engine = _bare_engine(policy={"position_cap_ratio": 1.5})
    engine._validate_audited_policy_params()
    assert engine._policy_error is not None
    assert "position_cap_ratio" in engine._policy_error


# --- M09-R2（对抗审查：排序约束/组合约束） ---


def test_vol_tiers_must_be_strictly_increasing() -> None:
    """反转的波动阈值(0.6/0.4/0.2)必须被策略校验拒绝。"""
    engine = _bare_engine(policy={"vol_tier_1": 0.6, "vol_tier_2": 0.4, "vol_tier_3": 0.2})
    engine._validate_audited_policy_params()
    assert engine._policy_error is not None
    assert "vol_tiers" in engine._policy_error


def test_leverage_levels_must_be_non_increasing() -> None:
    """反转的杠杆档位(低波动 0.5x/极端波动 3x)必须被拒绝。"""
    engine = _bare_engine(
        policy={
            "leverage_low_vol": 0.5,
            "leverage_mid_vol": 1.0,
            "leverage_high_vol": 2.0,
            "leverage_extreme_vol": 3.0,
        }
    )
    engine._validate_audited_policy_params()
    assert engine._policy_error is not None
    assert "leverage_levels" in engine._policy_error


def test_combined_exposure_cross_constraint() -> None:
    """base×cap×max_lev 组合约束(>1.5 拒绝)。"""
    engine = _bare_engine(
        policy={
            "position_pct_base": 0.5,
            "position_cap_ratio": 1.0,
            "leverage_low_vol": 20.0,
            "leverage_mid_vol": 10.0,
            "leverage_high_vol": 5.0,
            "leverage_extreme_vol": 2.0,
        }
    )
    engine._validate_audited_policy_params()
    assert engine._policy_error is not None
    assert "combined_exposure" in engine._policy_error


def test_ordered_tiers_and_levels_pass_validation() -> None:
    """合法排序的档位配置必须通过校验。"""
    engine = _bare_engine(
        policy={
            "vol_tier_1": 0.2,
            "vol_tier_2": 0.4,
            "vol_tier_3": 0.6,
            "leverage_low_vol": 3.0,
            "leverage_mid_vol": 2.0,
            "leverage_high_vol": 1.0,
            "leverage_extreme_vol": 0.5,
        }
    )
    engine._validate_audited_policy_params()
    assert engine._policy_error is None
