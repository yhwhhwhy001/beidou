from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from beidou_strategy.portfolio.exposure_governor import ExposureTarget


@given(
    gross=st.floats(min_value=0.0, max_value=3.0, allow_nan=False, allow_infinity=False),
    net_fraction=st.floats(min_value=-1.0, max_value=1.0, allow_nan=False, allow_infinity=False),
)
def test_exposure_target_gross_net_invariant(gross: float, net_fraction: float) -> None:
    target = ExposureTarget.from_policy_values(
        target_beta=0.0,
        beta_min=-1.0,
        beta_max=1.0,
        target_gross=gross,
        gross_min=0.0,
        gross_max=3.0,
        target_net=gross * net_fraction,
        net_min=-3.0,
        net_max=3.0,
        target_volatility=0.2,
        confidence=0.5,
        reason_codes=("PROPERTY",),
        policy_version="property-v1",
    )

    assert target.target_gross >= abs(target.target_net)
