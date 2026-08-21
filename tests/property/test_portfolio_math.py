from __future__ import annotations

from beidou_strategy.portfolio.optimizer import signed_exposure_math


def test_signed_portfolio_math_keeps_gross_and_net_distinct() -> None:
    gross, net = signed_exposure_math({"LONG": 100.0, "SHORT": -80.0})

    assert gross == 180.0
    assert net == 20.0
    assert gross >= abs(net)
