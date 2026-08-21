from __future__ import annotations

from datetime import UTC, datetime

from beidou_shared.types import InstrumentId, VenueId
from beidou_strategy.alpha.contracts import EnsembleComponent, EnsembleForecast
from beidou_strategy.portfolio.exposure_governor import ExposureTarget
from beidou_strategy.portfolio.optimizer import ActivePortfolioOptimizer, PortfolioOptimizationInput

TIMESTAMP = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _forecast(symbol: str, value: float) -> EnsembleForecast:
    return EnsembleForecast(
        instrument_id=InstrumentId(symbol),
        venue_id=VenueId("BINANCE"),
        expected_return=value,
        expected_return_after_cost=value,
        expected_volatility=0.2,
        uncertainty=0.1,
        conflict_score=0.0,
        components=(
            EnsembleComponent(
                alpha_id=f"alpha-{symbol}",
                weight=1.0,
                expected_return=value,
                contribution=value,
                regime_fit=0.9,
                reliability=0.9,
            ),
        ),
        model_version="ensemble-v3",
        timestamp=TIMESTAMP,
    )


def _target() -> ExposureTarget:
    return ExposureTarget(
        target_beta=0.20,
        beta_min=-0.50,
        beta_max=0.80,
        target_gross=0.60,
        gross_min=0.0,
        gross_max=0.80,
        target_net=0.20,
        net_min=-0.60,
        net_max=0.60,
        target_volatility=0.20,
        confidence=0.9,
        reason_codes=("TEST_POLICY",),
        policy_version="exposure-policy-v3-v1",
        timestamp=TIMESTAMP,
    )


def _inputs(*, missing_rules: bool = False) -> PortfolioOptimizationInput:
    rules = {} if missing_rules else {"BTCUSDT": 10.0, "ETHUSDT": 10.0}
    steps = {} if missing_rules else {"BTCUSDT": 0.001, "ETHUSDT": 0.001}
    ticks = {} if missing_rules else {"BTCUSDT": 0.01, "ETHUSDT": 0.01}
    return PortfolioOptimizationInput(
        forecasts={"BTCUSDT": _forecast("BTCUSDT", 0.02), "ETHUSDT": _forecast("ETHUSDT", 0.01)},
        current_weights={"BTCUSDT": 0.0, "ETHUSDT": 0.0},
        covariance={"BTCUSDT": {"BTCUSDT": 0.04, "ETHUSDT": 0.01}, "ETHUSDT": {"BTCUSDT": 0.01, "ETHUSDT": 0.04}},
        asset_betas={"BTCUSDT": 1.0, "ETHUSDT": 1.1},
        exposure_target=_target(),
        account_equity=100000.0,
        fee_costs={"BTCUSDT": 2.0, "ETHUSDT": 2.0},
        slippage_costs={"BTCUSDT": 1.0, "ETHUSDT": 1.0},
        funding_costs={"BTCUSDT": 0.5, "ETHUSDT": 0.5},
        liquidity_limits={"BTCUSDT": 0.80, "ETHUSDT": 0.80},
        capacity_limits={"BTCUSDT": 0.80, "ETHUSDT": 0.80},
        min_notional=rules,
        step_sizes=steps,
        tick_sizes=ticks,
        policy_version="portfolio-v3-policy-v1",
    )


def test_active_optimizer_uses_expected_return_and_respects_exposure_math() -> None:
    result = ActivePortfolioOptimizer().optimize(_inputs())

    assert result.rejected == {}
    assert result.gross_exposure >= abs(result.net_exposure)
    assert result.gross_exposure <= _target().gross_max + 1e-12
    assert _target().net_min <= result.net_exposure <= _target().net_max
    assert _target().beta_min <= result.portfolio_beta <= _target().beta_max
    assert result.weights


def test_missing_exchange_rules_fail_closed_without_magic_values() -> None:
    result = ActivePortfolioOptimizer().optimize(_inputs(missing_rules=True))

    assert result.weights == {}
    assert result.rejected
    assert all("EXCHANGE_RULES_UNKNOWN" in reason for reason in result.rejected.values())
