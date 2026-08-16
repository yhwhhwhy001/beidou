"""M06-F02: alpha 组件 validate() 真实校验测试。

旧实现 8 个组件 validate() 全部 return True 空桩。修复后:
- 引擎型组件(MeanReversion/Momentum)校验内部参数;
- 字面量型组件校验显式化阈值常量;
- 未初始化组件(object.__new__)validate 必须 False。
"""

from __future__ import annotations

from beidou_core.engine import (
    BreakoutEntry,
    MeanReversionEntry,
    MomentumFilter,
    TimeExit,
    TrailingExit,
    TrendFollowingEntry,
    VolatilityFilter,
    VolumeFilter,
)


def test_all_components_validate_true_when_constructed() -> None:
    for component in (
        MeanReversionEntry(),
        MomentumFilter(),
        TrendFollowingEntry(),
        BreakoutEntry(),
        VolatilityFilter(),
        VolumeFilter(),
        TrailingExit(),
        TimeExit(),
    ):
        assert component.validate() is True, f"{type(component).__name__}.validate() must be True"


def test_uninitialized_engine_components_validate_false() -> None:
    """引擎型组件未初始化(无 _engine/_momentum)不得通过 validate。"""
    for cls in (MeanReversionEntry, MomentumFilter):
        bare = object.__new__(cls)
        assert bare.validate() is False, f"{cls.__name__} uninitialized validate must be False"


def test_corrupted_engine_params_validate_false() -> None:
    """引擎参数损坏(非有限/越界)必须被 validate 拒绝。"""
    component = MeanReversionEntry()
    component._engine._z_threshold = float("nan")
    assert component.validate() is False
    component2 = MeanReversionEntry()
    component2._engine._window = -5
    assert component2.validate() is False


def test_threshold_constants_sane() -> None:
    assert TrendFollowingEntry.RSI_CONFIRM_MIN < TrendFollowingEntry.RSI_ENTRY_MAX <= 100.0
    assert BreakoutEntry.VOL_RATIO_MIN >= 1.0
    assert VolatilityFilter.RSI_OVERSOLD < VolatilityFilter.RSI_OVERBOUGHT <= 100.0
    assert VolumeFilter.VOL_RATIO_VERY_LOW <= VolumeFilter.VOL_RATIO_LOW <= VolumeFilter.VOL_RATIO_HIGH
    assert 0.0 <= TrailingExit.RSI_TAKE_PROFIT <= 100.0
    schedule = TimeExit.STRENGTH_SCHEDULE
    assert schedule == tuple(sorted(schedule)) and all(0.0 <= s <= 1.0 for s in schedule)
