"""Read-only Alpha V3 shadow pipeline from benchmark to portfolio target."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from beidou_shared.types import InstrumentId, VenueId

from ..portfolio.contracts import ExposureTarget, PortfolioOptimizationInput
from ..portfolio.exposure_governor import ExposureGovernor
from ..portfolio.optimizer import ActiveOptimizationResult, ActivePortfolioOptimizer
from ..state.benchmark import BenchmarkSnapshot
from ..state.market_state import MarketStateEstimator, MarketStateVector
from ._forecast_utils import context_features, finite, horizon_values, parse_timestamp, stable_hash
from .breakout import BreakoutAlpha
from .contracts import AlphaForecast, EnsembleForecast
from .extensions import DerivativeRiskContext
from .forecast import CalibrationArtifact, DeterministicForecastCalibrator, EnsembleFuser
from .mean_reversion import MeanReversionAlpha
from .model_registry import CalibrationRegistry
from .relative_strength import RelativeStrengthAlpha
from .residual_momentum import ResidualMomentumAlpha
from .trend import TrendAlpha


@dataclass(frozen=True, slots=True)
class AlphaV3ShadowResult:
    benchmark_snapshot: BenchmarkSnapshot
    market_state: MarketStateVector
    forecasts: tuple[AlphaForecast, ...]
    ensemble_forecast: EnsembleForecast
    exposure_target: ExposureTarget
    portfolio_result: ActiveOptimizationResult
    status: str
    failure_reasons: tuple[str, ...]
    benchmark_snapshot_hash: str
    alpha_forecast_hash: str
    ensemble_forecast_hash: str
    exposure_target_hash: str
    portfolio_target_hash: str
    trace_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "failure_reasons": list(self.failure_reasons),
            "benchmark_snapshot_hash": self.benchmark_snapshot_hash,
            "alpha_forecast_hash": self.alpha_forecast_hash,
            "ensemble_forecast_hash": self.ensemble_forecast_hash,
            "exposure_target_hash": self.exposure_target_hash,
            "portfolio_target_hash": self.portfolio_target_hash,
            "trace_hash": self.trace_hash,
            "forecast_count": len(self.forecasts),
            "portfolio_weights": dict(self.portfolio_result.weights),
            "portfolio_rejected": dict(self.portfolio_result.rejected),
        }


def _hash_payload(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()[
        :16
    ]


class AlphaV3ShadowEngine:
    """Compute V3 artifacts without creating order intent or changing V2."""

    def __init__(self, calibration_registry: CalibrationRegistry | None = None) -> None:
        self._state_estimator = MarketStateEstimator()
        self._fuser = EnsembleFuser()
        self._governor = ExposureGovernor()
        self._optimizer = ActivePortfolioOptimizer()
        self._calibration_registry = calibration_registry

    def evaluate(self, context: dict[str, Any]) -> AlphaV3ShadowResult:
        features = context_features(context)
        instrument = InstrumentId(str(context.get("instrument_id", "UNKNOWN")))
        venue = VenueId(str(context.get("venue_id", "UNKNOWN")))
        timestamp = parse_timestamp(context.get("timestamp")) or datetime(1970, 1, 1, tzinfo=UTC)
        benchmark = self._benchmark_snapshot(context, features, instrument, timestamp)
        state_features = dict(features)
        if "benchmark_returns" not in state_features and benchmark.returns_by_horizon:
            state_features["benchmark_returns"] = dict(benchmark.returns_by_horizon)
        if "realized_volatility" not in state_features and benchmark.realized_vol_by_horizon:
            state_features["realized_volatility"] = max(benchmark.realized_vol_by_horizon.values())
        state = self._state_estimator.estimate(venue, instrument, state_features, timestamp=timestamp)
        alpha_context = {
            **context,
            "features": state_features,
            "state": {"direction": state.direction.regime, "trend_probability": state.direction.trend_probability},
        }
        raw_forecasts = (
            TrendAlpha().generate_forecast(alpha_context),
            BreakoutAlpha().generate_forecast(alpha_context),
            RelativeStrengthAlpha().generate_forecast(alpha_context),
            ResidualMomentumAlpha().generate_forecast(alpha_context),
            MeanReversionAlpha().generate_forecast(alpha_context),
        )
        risk_adjusted_forecasts, derivative_failures = self._apply_derivative_risk(
            raw_forecasts, context, state_features
        )
        forecasts, calibration_failures = self._apply_published_calibration(risk_adjusted_forecasts, state_features)
        ensemble = self._fuser.fuse(forecasts)
        account_facts = context.get("account_facts", {})
        if not isinstance(account_facts, dict):
            account_facts = {}
        exposure = self._governor.compute(state, ensemble, account_facts=account_facts)
        if calibration_failures:
            exposure = replace(
                exposure,
                target_beta=0.0,
                target_gross=0.0,
                target_net=0.0,
                confidence=0.0,
                reason_codes=(*exposure.reason_codes, "CALIBRATION_NOT_VERIFIABLE"),
            )
        try:
            optimizer_input = self._optimizer_input(context, features, ensemble, exposure, instrument)
            portfolio_result = self._optimizer.optimize(optimizer_input)
        except (TypeError, ValueError) as exc:
            portfolio_result = self._blocked_portfolio_result(exposure, f"PORTFOLIO_INPUT_UNKNOWN:{type(exc).__name__}")
        failure_reasons = list(portfolio_result.rejected.values())
        failure_reasons.extend(derivative_failures)
        failure_reasons.extend(calibration_failures)
        if not state.is_tradable():
            failure_reasons.append("MARKET_STATE_NOT_TRADABLE")
        if ensemble.expected_return_after_cost is None:
            failure_reasons.append("ENSEMBLE_AFTER_COST_UNKNOWN")
        status = "PASS" if not failure_reasons and portfolio_result.weights else "NOT_VERIFIABLE"
        benchmark_hash = _hash_payload(
            {
                "benchmark_id": benchmark.benchmark_id,
                "timestamp": benchmark.timestamp.isoformat(),
                "returns": dict(benchmark.returns_by_horizon.items()),
                "source_hash": benchmark.source_hash,
                "policy_version": benchmark.policy_version,
            }
        )
        alpha_hash = _hash_payload([forecast.forecast_hash for forecast in forecasts])
        ensemble_hash = ensemble.forecast_hash
        exposure_hash = _hash_payload(
            {
                "target_beta": exposure.target_beta,
                "target_gross": exposure.target_gross,
                "target_net": exposure.target_net,
                "policy_version": exposure.policy_version,
            }
        )
        portfolio_hash = portfolio_result.output_hash
        trace_hash = _hash_payload(
            {
                "benchmark": benchmark_hash,
                "state": state.state_hash,
                "alpha": alpha_hash,
                "ensemble": ensemble_hash,
                "exposure": exposure_hash,
                "portfolio": portfolio_hash,
            }
        )
        return AlphaV3ShadowResult(
            benchmark_snapshot=benchmark,
            market_state=state,
            forecasts=forecasts,
            ensemble_forecast=ensemble,
            exposure_target=exposure,
            portfolio_result=portfolio_result,
            status=status,
            failure_reasons=tuple(sorted(set(failure_reasons))),
            benchmark_snapshot_hash=benchmark_hash,
            alpha_forecast_hash=alpha_hash,
            ensemble_forecast_hash=ensemble_hash,
            exposure_target_hash=exposure_hash,
            portfolio_target_hash=portfolio_hash,
            trace_hash=trace_hash,
        )

    @staticmethod
    def _apply_derivative_risk(
        forecasts: tuple[AlphaForecast, ...],
        context: dict[str, Any],
        features: Mapping[str, Any],
    ) -> tuple[tuple[AlphaForecast, ...], tuple[str, ...]]:
        raw_context = context.get("derivative_risk_context", features.get("derivative_risk_context"))
        if raw_context is None:
            return forecasts, ()
        if not isinstance(raw_context, DerivativeRiskContext) or not raw_context.is_verifiable:
            return tuple(
                replace(forecast, expected_return=None, expected_return_after_cost=None) for forecast in forecasts
            ), ("DERIVATIVE_RISK_NOT_VERIFIABLE",)
        adjusted: list[AlphaForecast] = []
        for forecast in forecasts:
            confidence = forecast.confidence * raw_context.liquidation_stress_scalar
            funding_bps = forecast.expected_funding_bps
            cost_source_hash = forecast.cost_source_hash
            if raw_context.funding_return_adjustment is not None:
                funding_bps = -raw_context.funding_return_adjustment * 10000.0
                cost_source_hash = stable_hash(
                    {"base": forecast.cost_source_hash, "derivative": raw_context.source_hash}
                )
            after_cost = forecast.expected_return_after_cost
            fee_bps = forecast.expected_fee_bps
            slippage_bps = forecast.expected_slippage_bps
            if (
                forecast.expected_return is not None
                and fee_bps is not None
                and slippage_bps is not None
                and funding_bps is not None
            ):
                after_cost = forecast.expected_return - (fee_bps + slippage_bps + funding_bps) / 10000.0
            adjusted.append(
                replace(
                    forecast,
                    expected_return_after_cost=after_cost,
                    expected_funding_bps=funding_bps,
                    confidence=max(0.0, min(1.0, confidence)),
                    uncertainty=max(0.0, min(1.0, 1.0 - confidence)),
                    capacity_score=(
                        None
                        if forecast.capacity_score is None
                        else max(0.0, min(1.0, forecast.capacity_score * raw_context.liquidation_stress_scalar))
                    ),
                    regime_fit=max(0.0, min(1.0, (forecast.regime_fit or 0.0) * raw_context.liquidation_stress_scalar)),
                    cost_source_hash=cost_source_hash,
                )
            )
        return tuple(adjusted), ()

    def _apply_published_calibration(
        self,
        forecasts: tuple[AlphaForecast, ...],
        features: Mapping[str, Any],
    ) -> tuple[tuple[AlphaForecast, ...], tuple[str, ...]]:
        """Replace raw score returns only with published checksum-valid artifacts."""
        calibrated: list[AlphaForecast] = []
        failures: list[str] = []
        for forecast in forecasts:
            artifact: CalibrationArtifact | None = None
            if self._calibration_registry is not None:
                artifact = self._calibration_registry.get_for_alpha(forecast.alpha_id)
            if artifact is None or not artifact.verify_checksum():
                failures.append(f"CALIBRATION_ARTIFACT_UNKNOWN:{forecast.alpha_id}")
                calibrated.append(replace(forecast, expected_return=None, expected_return_after_cost=None))
                continue
            if forecast.expected_return is None:
                failures.append(f"RAW_FORECAST_UNKNOWN:{forecast.alpha_id}")
                calibrated.append(forecast)
                continue
            try:
                calibrated.append(DeterministicForecastCalibrator(artifact).apply(forecast, features))
            except (TypeError, ValueError):
                failures.append(f"CALIBRATION_ARTIFACT_INVALID:{forecast.alpha_id}")
                calibrated.append(replace(forecast, expected_return=None, expected_return_after_cost=None))
        return tuple(calibrated), tuple(sorted(set(failures)))

    @staticmethod
    def _blocked_portfolio_result(exposure: ExposureTarget, reason: str) -> ActiveOptimizationResult:
        target_hash = stable_hash(
            {
                "policy_version": exposure.policy_version,
                "target_beta": exposure.target_beta,
                "target_gross": exposure.target_gross,
                "target_net": exposure.target_net,
            }
        )
        return ActiveOptimizationResult(
            weights={},
            rejected={"PORTFOLIO": reason},
            gross_exposure=0.0,
            net_exposure=0.0,
            portfolio_beta=0.0,
            objective=None,
            target_hash=target_hash,
            output_hash=stable_hash({"target_hash": target_hash, "reason": reason}),
            constraint_violations=(),
        )

    def _benchmark_snapshot(
        self,
        context: dict[str, Any],
        features: Mapping[str, Any],
        instrument: InstrumentId,
        timestamp: datetime,
    ) -> BenchmarkSnapshot:
        returns, invalid = horizon_values(features, ("benchmark_returns",))
        if invalid:
            returns = {}
        volatility = {}
        realized = features.get("realized_volatility", features.get("ann_volatility"))
        if realized is not None:
            try:
                volatility = {horizon: float(realized) for horizon in returns}
            except (TypeError, ValueError):
                volatility = {}
        quality = str(features.get("data_quality", features.get("quality", "UNKNOWN"))).upper()
        if quality not in {"PASS", "CONDITIONAL", "DEGRADED", "UNKNOWN", "NOT_VERIFIABLE"}:
            quality = "UNKNOWN"
        source_hash = str(features.get("benchmark_source_hash", "")).strip() or stable_hash(
            {"instrument": str(instrument), "returns": returns, "timestamp": timestamp.isoformat()}
        )
        return BenchmarkSnapshot(
            benchmark_id=str(context.get("benchmark_id", f"single:{instrument}")),
            timestamp=timestamp,
            returns_by_horizon=returns,
            realized_vol_by_horizon=volatility,
            breadth=features.get("breadth"),
            breakout_breadth=features.get("breakout_breadth"),
            dispersion=features.get("dispersion"),
            data_quality=quality,
            source_hash=source_hash,
            policy_version="benchmark-policy-v3-v1",
        )

    @staticmethod
    def _optimizer_input(
        context: dict[str, Any],
        features: Mapping[str, Any],
        ensemble: EnsembleForecast,
        exposure: ExposureTarget,
        instrument: InstrumentId,
    ) -> PortfolioOptimizationInput:
        rules = features.get("exchange_rules", {})
        if not isinstance(rules, dict):
            rules = {}
        symbol = str(instrument)
        costs = context.get("costs", {})
        if not isinstance(costs, dict):
            costs = {}
        liquidity = finite(features.get("liquidity_score", features.get("liquidity")))
        beta = finite(features.get("market_beta"))
        account_facts = context.get("account_facts", {})
        account_equity = finite(account_facts.get("equity")) if isinstance(account_facts, dict) else None
        fee_bps = finite(costs.get("expected_fee_bps"))
        slippage_bps = finite(costs.get("expected_slippage_bps"))
        funding_bps = finite(costs.get("expected_funding_bps"))
        return PortfolioOptimizationInput(
            forecasts={symbol: ensemble},
            current_weights={symbol: 0.0},
            covariance={symbol: {symbol: float(ensemble.expected_volatility or exposure.target_volatility) ** 2}},
            asset_betas={symbol: beta} if beta is not None else {},
            exposure_target=exposure,
            account_equity=account_equity or 0.0,
            fee_costs={symbol: fee_bps} if fee_bps is not None else {},
            slippage_costs={symbol: slippage_bps} if slippage_bps is not None else {},
            funding_costs={symbol: funding_bps} if funding_bps is not None else {},
            liquidity_limits={symbol: liquidity} if liquidity is not None else {},
            capacity_limits={symbol: liquidity} if liquidity is not None else {},
            min_notional={symbol: rules["min_notional"]} if "min_notional" in rules else {},
            step_sizes={symbol: rules["step_size"]} if "step_size" in rules else {},
            tick_sizes={symbol: rules["tick_size"]} if "tick_size" in rules else {},
            policy_version="portfolio-v3-policy-v1",
        )
