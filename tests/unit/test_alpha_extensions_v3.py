from __future__ import annotations

from datetime import UTC, datetime

import pytest

from beidou_shared.types import InstrumentId, VenueId
from beidou_strategy.alpha.extensions import (
    DerivativeRiskContext,
    FundingRateSignal,
    LiquidationCascadeRisk,
)


def test_funding_adjustment_is_signed_by_position_and_liquidation_is_monotone() -> None:
    venue = VenueId("BINANCE")
    instrument = InstrumentId("BTCUSDT")
    funding = FundingRateSignal(
        venue,
        instrument,
        predicted_rate=0.001,
        annualized_rate_pct=10.0,
        confidence=0.9,
        source_hash="funding-v1",
        timestamp=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )
    liquidation = LiquidationCascadeRisk(
        venue,
        instrument,
        cascade_probability=0.7,
        source_hash="liq-v1",
        timestamp=datetime(2026, 8, 21, 12, 1, tzinfo=UTC),
    )

    context = DerivativeRiskContext.from_signals(funding=funding, liquidation=liquidation, position_sign=1.0)

    assert context.funding_return_adjustment == -0.001
    assert context.liquidation_stress_scalar == pytest.approx(0.3)
    assert context.is_verifiable
    assert funding.net_return_adjustment(-1.0) == 0.001


def test_derivative_risk_without_source_hash_is_not_verifiable() -> None:
    funding = FundingRateSignal(
        VenueId("BINANCE"),
        InstrumentId("BTCUSDT"),
        predicted_rate=0.0,
        annualized_rate_pct=0.0,
    )

    context = DerivativeRiskContext.from_signals(funding=funding, liquidation=None, position_sign=0.0)

    assert not context.is_verifiable
