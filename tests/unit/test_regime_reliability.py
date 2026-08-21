from __future__ import annotations

import pytest

from beidou_research.factors import RegimeConditionalForecastEvidence, build_regime_reliability


def test_regime_reliability_is_cost_and_correlation_aware() -> None:
    weights = build_regime_reliability(
        [
            RegimeConditionalForecastEvidence(
                "trend_v3", "TRENDING_UP", icir=0.8, sample_count=100, capacity_score=0.9
            ),
            RegimeConditionalForecastEvidence(
                "breakout_v3",
                "TRENDING_UP",
                icir=0.8,
                sample_count=100,
                correlation_penalty=0.5,
                cost_adjusted_ic=-0.01,
            ),
        ],
        regime="TRENDING_UP",
    )

    assert weights["trend_v3"] == pytest.approx(0.72)
    assert weights["breakout_v3"] == 0.0


def test_insufficient_regime_evidence_is_zero_not_a_default_reliability() -> None:
    weights = build_regime_reliability(
        [RegimeConditionalForecastEvidence("rs_v3", "RANGING", icir=0.9, sample_count=2)], regime="RANGING"
    )

    assert weights == {"rs_v3": 0.0}
