"""Beta-neutralized residual momentum with published runtime artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from beidou_shared.types import InstrumentId, SchemaVersion, StrategyId, VenueId

from ._forecast_utils import (
    clip,
    context_features,
    feature_payload,
    finite,
    horizon_values,
    quality_factor,
    resolve_costs,
    resolve_timestamp,
    side_for_score,
    stable_hash,
)
from .contracts import AlphaForecast


@dataclass(frozen=True, slots=True)
class ResidualMomentumArtifact:
    artifact_id: str
    model_version: str
    feature_schema_version: str
    beta: float
    residual_scale: float
    training_window: str
    oos_window: str
    regime_scope: str
    checksum: str = ""

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.artifact_id,
                self.model_version,
                self.feature_schema_version,
                self.training_window,
                self.oos_window,
                self.regime_scope,
            )
        ):
            raise ValueError("residual artifact identity and windows are required")
        if not math.isfinite(self.beta) or not math.isfinite(self.residual_scale) or self.residual_scale <= 0:
            raise ValueError("residual artifact parameters must be finite")

    @classmethod
    def create(cls, **kwargs: Any) -> ResidualMomentumArtifact:
        candidate = cls(**kwargs)
        return replace(candidate, checksum=candidate.expected_checksum())

    def payload(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "model_version": self.model_version,
            "feature_schema_version": self.feature_schema_version,
            "beta": self.beta,
            "residual_scale": self.residual_scale,
            "training_window": self.training_window,
            "oos_window": self.oos_window,
            "regime_scope": self.regime_scope,
        }

    def expected_checksum(self) -> str:
        return hashlib.sha256(
            json.dumps(self.payload(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    def verify_checksum(self) -> bool:
        return bool(self.checksum and self.checksum == self.expected_checksum())

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ResidualMomentumArtifact | None:
        try:
            artifact = cls(
                artifact_id=str(raw["artifact_id"]),
                model_version=str(raw["model_version"]),
                feature_schema_version=str(raw["feature_schema_version"]),
                beta=float(raw["beta"]),
                residual_scale=float(raw["residual_scale"]),
                training_window=str(raw["training_window"]),
                oos_window=str(raw["oos_window"]),
                regime_scope=str(raw["regime_scope"]),
                checksum=str(raw["checksum"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return artifact if artifact.verify_checksum() else None


class ResidualMomentumAlpha:
    def __init__(
        self,
        *,
        alpha_id: str = "residual-momentum-v3",
        strategy_id: StrategyId | None = None,
        policy_version: str = "residual-momentum-policy-v3-v1",
        entry_threshold: float = 0.2,
        expected_return_scale: float = 0.02,
        horizon_seconds: int = 3600,
    ) -> None:
        if (
            not policy_version.strip()
            or not 0 < entry_threshold <= 1
            or expected_return_scale < 0
            or horizon_seconds <= 0
        ):
            raise ValueError("invalid residual momentum policy")
        self.alpha_id = alpha_id
        self.strategy_id = strategy_id or StrategyId("residual-momentum-v3")
        self.policy_version = policy_version
        self.entry_threshold = entry_threshold
        self.expected_return_scale = expected_return_scale
        self.horizon_seconds = horizon_seconds

    def _feature_hash(self, features: Mapping[str, Any], artifact: ResidualMomentumArtifact | None) -> str:
        supplied = str(features.get("feature_hash", "")).strip()
        allowed = {
            "asset_returns",
            "benchmark_returns",
            "residual_returns",
            "realized_volatility",
            "data_quality",
            "liquidity_score",
            "liquidity",
        }
        payload = feature_payload(features, allowed)
        payload["artifact_checksum"] = artifact.checksum if artifact else ""
        return supplied or stable_hash(payload)

    def _not_verifiable(
        self, context: Mapping[str, Any], features: Mapping[str, Any], artifact: ResidualMomentumArtifact | None
    ) -> AlphaForecast:
        fee, slippage, funding, source_hash = resolve_costs(context, features)
        volatility = finite(features.get("realized_volatility", features.get("ann_volatility")))
        return AlphaForecast(
            alpha_id=self.alpha_id,
            strategy_id=context.get("strategy_id", self.strategy_id),
            instrument_id=context.get("instrument_id", InstrumentId("UNKNOWN")),
            venue_id=context.get("venue_id", VenueId("UNKNOWN")),
            side="NO_ACTION",
            raw_score=0.0,
            expected_return=None,
            expected_return_after_cost=None,
            expected_volatility=volatility if volatility is not None and volatility >= 0 else None,
            horizon_seconds=self.horizon_seconds,
            probability_positive=0.5,
            confidence=0.0,
            uncertainty=1.0,
            expected_fee_bps=fee,
            expected_slippage_bps=slippage,
            expected_funding_bps=funding,
            market_beta=0.0,
            regime_fit=0.0,
            capacity_score=None,
            model_version=artifact.model_version if artifact else SchemaVersion("3.0.0"),
            policy_version=self.policy_version,
            feature_hash=self._feature_hash(features, artifact),
            timestamp=resolve_timestamp(context, features),
            cost_source_hash=source_hash,
        )

    def generate_forecast(self, context: Mapping[str, Any]) -> AlphaForecast:
        features = context_features(context)
        raw_artifact = context.get("residual_artifact", features.get("residual_artifact"))
        artifact = (
            raw_artifact
            if isinstance(raw_artifact, ResidualMomentumArtifact)
            else ResidualMomentumArtifact.from_mapping(raw_artifact)
            if isinstance(raw_artifact, Mapping)
            else None
        )
        if artifact is None or not artifact.verify_checksum():
            return self._not_verifiable(context, features, None)
        asset, asset_invalid = horizon_values(features, ("asset_returns", "returns_by_horizon"))
        benchmark, benchmark_invalid = horizon_values(features, ("benchmark_returns",))
        if asset_invalid or benchmark_invalid or not asset or not benchmark:
            return self._not_verifiable(context, features, artifact)
        residual_values, residual_invalid = horizon_values(features, ("residual_returns",))
        if residual_invalid:
            return self._not_verifiable(context, features, artifact)
        if not residual_values:
            residual_values = {
                horizon: asset[horizon] - artifact.beta * benchmark[horizon]
                for horizon in asset
                if horizon in benchmark
            }
        if not residual_values:
            return self._not_verifiable(context, features, artifact)
        score = sum(residual_values.values()) / len(residual_values)
        raw_score = clip(math.tanh(score / artifact.residual_scale), -1.0, 1.0)
        fee, slippage, funding, source_hash = resolve_costs(context, features)
        expected_return = raw_score * self.expected_return_scale
        total_cost = None if fee is None or slippage is None or funding is None else fee + slippage + funding
        after_cost = expected_return - total_cost / 10000.0 if total_cost is not None and source_hash else None
        volatility = finite(features.get("realized_volatility", features.get("ann_volatility")))
        liquidity = finite(features.get("liquidity_score", features.get("liquidity")))
        confidence = clip(abs(raw_score) * quality_factor(features), 0.0, 1.0)
        return AlphaForecast(
            alpha_id=self.alpha_id,
            strategy_id=context.get("strategy_id", self.strategy_id),
            instrument_id=context.get("instrument_id", InstrumentId("UNKNOWN")),
            venue_id=context.get("venue_id", VenueId("UNKNOWN")),
            side=side_for_score(raw_score, self.entry_threshold),
            raw_score=raw_score,
            expected_return=expected_return,
            expected_return_after_cost=after_cost,
            expected_volatility=volatility if volatility is not None and volatility >= 0 else None,
            horizon_seconds=self.horizon_seconds,
            probability_positive=1.0 / (1.0 + math.exp(-raw_score / 0.25)),
            confidence=confidence,
            uncertainty=1.0 - confidence,
            expected_fee_bps=fee,
            expected_slippage_bps=slippage,
            expected_funding_bps=funding,
            market_beta=0.0,
            regime_fit=clip(abs(raw_score), 0.0, 1.0),
            capacity_score=clip(liquidity, 0.0, 1.0) if liquidity is not None else None,
            model_version=artifact.model_version,
            policy_version=self.policy_version,
            feature_hash=self._feature_hash(features, artifact),
            timestamp=resolve_timestamp(context, features),
            cost_source_hash=source_hash,
        )

    def generate(self, context: Mapping[str, Any]) -> AlphaForecast:
        return self.generate_forecast(context)

    def validate(self) -> bool:
        return bool(self.alpha_id and self.policy_version)
