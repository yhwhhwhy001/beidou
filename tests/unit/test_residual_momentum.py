from __future__ import annotations

from beidou_strategy.alpha.residual_momentum import (
    ResidualMomentumAlpha,
    ResidualMomentumArtifact,
)


def _artifact() -> ResidualMomentumArtifact:
    return ResidualMomentumArtifact.create(
        artifact_id="residual-v1",
        model_version="residual-model-v1",
        feature_schema_version="features-v3",
        beta=1.2,
        residual_scale=0.02,
        training_window="2025-01-01/2026-01-01",
        oos_window="2026-01-01/2026-06-30",
        regime_scope="ALL",
    )


def _context() -> dict[str, object]:
    return {
        "timestamp": "2026-08-21T12:00:00+00:00",
        "instrument_id": "ETHUSDT",
        "venue_id": "BINANCE",
        "features": {
            "asset_returns": {5: 0.10, 20: 0.18},
            "benchmark_returns": {5: 0.08, 20: 0.10},
            "realized_volatility": 0.03,
            "data_quality": "PASS",
            "liquidity_score": 0.9,
        },
        "residual_artifact": _artifact(),
        "costs": {
            "expected_fee_bps": 2.0,
            "expected_slippage_bps": 1.0,
            "expected_funding_bps": 0.5,
            "source_hash": "cost-v1",
        },
    }


def test_residual_momentum_is_beta_adjusted_and_artifact_bound() -> None:
    forecast = ResidualMomentumAlpha().generate_forecast(_context())

    assert forecast.side == "BUY"
    assert forecast.market_beta == 0.0
    assert forecast.expected_return is not None and forecast.expected_return > 0
    assert forecast.model_version == "residual-model-v1"
    assert forecast.feature_hash


def test_missing_or_tampered_artifact_is_not_tradable() -> None:
    context = _context()
    context.pop("residual_artifact")
    missing = ResidualMomentumAlpha().generate_forecast(context)
    assert missing.side == "NO_ACTION"
    assert missing.expected_return is None

    tampered = _context()
    artifact = _artifact()
    tampered["residual_artifact"] = {
        "artifact_id": artifact.artifact_id,
        "model_version": artifact.model_version,
        "feature_schema_version": artifact.feature_schema_version,
        "beta": 9.0,
        "residual_scale": artifact.residual_scale,
        "training_window": artifact.training_window,
        "oos_window": artifact.oos_window,
        "regime_scope": artifact.regime_scope,
        "checksum": artifact.checksum,
    }
    rejected = ResidualMomentumAlpha().generate_forecast(tampered)
    assert rejected.side == "NO_ACTION"
    assert rejected.expected_return is None


def test_residual_momentum_ignores_future_labels() -> None:
    first = ResidualMomentumAlpha().generate_forecast(_context())
    context = _context()
    features = dict(context["features"])
    features["future_return"] = 100.0
    features["label"] = "SHORT"
    context["features"] = features
    second = ResidualMomentumAlpha().generate_forecast(context)

    assert first.forecast_hash == second.forecast_hash
