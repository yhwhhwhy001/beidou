from __future__ import annotations

from beidou_strategy.alpha.forecast import CalibrationArtifact
from beidou_strategy.alpha.model_registry import CalibrationRegistry
from beidou_strategy.alpha.pipeline import AlphaV3ShadowEngine
from beidou_strategy.alpha.residual_momentum import ResidualMomentumArtifact


def _published_registry() -> CalibrationRegistry:
    registry = CalibrationRegistry()
    for alpha_id in (
        "trend_v3",
        "breakout-v3",
        "relative-strength-v3",
        "residual-momentum-v3",
        "mean_reversion_v3",
    ):
        artifact = CalibrationArtifact.create(
            artifact_id=f"cal-{alpha_id}",
            model_version=f"cal-model-{alpha_id}",
            feature_schema_version="alpha-features-v3",
            coefficients={"raw_score": 0.02},
            intercept=0.005,
            training_window="2025-01-01/2026-01-01",
            oos_window="2026-01-01/2026-06-30",
            regime_scope="ALL",
            metrics={"mae": 0.001, "hit_rate": 0.55},
        )
        assert registry.publish(
            artifact,
            evidence_ids=[f"oos-{alpha_id}"],
            published_by="test-reviewer",
            alpha_id=alpha_id,
        )
    return registry


def _residual_artifact() -> ResidualMomentumArtifact:
    return ResidualMomentumArtifact.create(
        artifact_id="residual-v3",
        model_version="residual-model-v3",
        feature_schema_version="alpha-features-v3",
        beta=0.8,
        residual_scale=0.05,
        training_window="2025-01-01/2026-01-01",
        oos_window="2026-01-01/2026-06-30",
        regime_scope="ALL",
    )


def _context(*, with_rules: bool = True) -> dict[str, object]:
    features: dict[str, object] = {
        "benchmark_returns": {5: 0.03, 20: 0.10, 50: 0.25},
        "trend_slopes": {5: 0.003, 20: 0.01, 50: 0.025},
        "breadth": 0.85,
        "breakout_breadth": 0.80,
        "volume_expansion": 1.3,
        "realized_volatility": 0.2,
        "dispersion": 0.1,
        "cross_sectional_correlation": 0.3,
        "liquidity_score": 0.9,
        "market_beta": 1.0,
        "data_quality": "PASS",
        "close": 120.0,
        "high_history": [100.0, 102.0, 105.0, 108.0, 110.0],
        "low_history": [95.0, 96.0, 98.0, 99.0, 100.0],
        "atr": 5.0,
        "volume_ratio": 2.0,
        "asset_returns": {5: 0.10, 20: 0.20},
        "prices": [100.0 + index for index in range(30)],
        "calibrated_expected_return": 0.01,
        "market_regime": "RANGING",
        "residual_artifact": _residual_artifact(),
        "universe_returns": {
            "ETHUSDT": {5: 0.10, 20: 0.20},
            "BTCUSDT": {5: 0.02, 20: 0.05},
        },
        "universe_snapshot_timestamp": "2026-08-21T11:59:00+00:00",
        "universe_source_hash": "universe-v1",
    }
    if with_rules:
        features["exchange_rules"] = {"min_notional": 10.0, "step_size": 0.001, "tick_size": 0.01}
    return {
        "timestamp": "2026-08-21T12:00:00+00:00",
        "instrument_id": "ETHUSDT",
        "venue_id": "BINANCE",
        "features": features,
        "costs": {
            "expected_fee_bps": 2.0,
            "expected_slippage_bps": 1.0,
            "expected_funding_bps": 0.5,
            "source_hash": "cost-v1",
        },
        "account_facts": {"equity": 100000.0},
    }


def test_v3_shadow_pipeline_has_complete_hash_chain_without_writes() -> None:
    first = AlphaV3ShadowEngine(_published_registry()).evaluate(_context())
    second = AlphaV3ShadowEngine(_published_registry()).evaluate(_context())

    assert first.trace_hash == second.trace_hash
    assert first.benchmark_snapshot_hash
    assert first.alpha_forecast_hash
    assert first.ensemble_forecast_hash
    assert first.exposure_target_hash
    assert first.portfolio_target_hash
    assert first.exposure_target.target_gross > 0.0


def test_v3_shadow_pipeline_without_published_calibration_is_not_tradable() -> None:
    result = AlphaV3ShadowEngine().evaluate(_context())

    assert result.status == "NOT_VERIFIABLE"
    assert result.exposure_target.target_gross == 0.0
    assert result.portfolio_result.weights == {}
    assert any(reason.startswith("CALIBRATION_ARTIFACT_UNKNOWN:") for reason in result.failure_reasons)


def test_v3_shadow_pipeline_keeps_missing_exchange_rules_not_verifiable() -> None:
    result = AlphaV3ShadowEngine(_published_registry()).evaluate(_context(with_rules=False))

    assert result.portfolio_result.weights == {}
    assert result.status == "NOT_VERIFIABLE"
    assert "EXCHANGE_RULES_UNKNOWN" in result.failure_reasons
