from __future__ import annotations

from beidou_strategy.portfolio.exposure_governor import ExposureGovernor
from tests.integration.test_alpha_to_portfolio_v3 import _ensemble, _state


def test_strong_bull_has_a_core_participation_target_without_leverage_inflation() -> None:
    target = ExposureGovernor().compute(_state(), _ensemble(0.01), account_facts={"equity": 100000.0})

    assert target.target_beta > 0.0
    assert target.target_gross <= target.gross_max
    assert target.target_gross < 1.0
