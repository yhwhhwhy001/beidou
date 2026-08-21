from __future__ import annotations

import pytest

from beidou_strategy.alpha.forecast import (
    CalibrationArtifact,
    DeterministicForecastCalibrator,
)
from beidou_strategy.alpha.model_registry import CalibrationRegistry


def test_calibration_artifact_is_versioned_hashed_and_deterministic() -> None:
    artifact = CalibrationArtifact.create(
        artifact_id="cal-v1",
        model_version="cal-model-v1",
        feature_schema_version="alpha-features-v3",
        coefficients={"raw_score": 0.02, "confidence": 0.001},
        intercept=0.001,
        training_window="2025-01-01/2026-01-01",
        oos_window="2026-01-01/2026-06-30",
        regime_scope="ALL",
        metrics={"mae": 0.001, "hit_rate": 0.55},
    )
    calibrator = DeterministicForecastCalibrator(artifact)

    first = calibrator.predict({"raw_score": 0.5, "confidence": 0.8})
    second = calibrator.predict({"confidence": 0.8, "raw_score": 0.5})

    assert artifact.checksum
    assert first == second
    assert calibrator.verify()


def test_tampered_calibration_artifact_is_rejected() -> None:
    artifact = CalibrationArtifact.create(
        artifact_id="cal-v1",
        model_version="cal-model-v1",
        feature_schema_version="alpha-features-v3",
        coefficients={"raw_score": 0.02},
        intercept=0.0,
        training_window="train",
        oos_window="oos",
        regime_scope="ALL",
        metrics={"mae": 0.001},
    )
    with pytest.raises(ValueError, match="checksum"):
        DeterministicForecastCalibrator(
            CalibrationArtifact(
                artifact_id=artifact.artifact_id,
                model_version=artifact.model_version,
                feature_schema_version=artifact.feature_schema_version,
                coefficients=artifact.coefficients,
                intercept=artifact.intercept + 0.1,
                training_window=artifact.training_window,
                oos_window=artifact.oos_window,
                regime_scope=artifact.regime_scope,
                metrics=artifact.metrics,
                checksum=artifact.checksum,
            )
        )


def test_non_finite_calibration_input_fails_closed() -> None:
    artifact = CalibrationArtifact.create(
        artifact_id="cal-v1",
        model_version="cal-model-v1",
        feature_schema_version="alpha-features-v3",
        coefficients={"raw_score": 0.02},
        intercept=0.0,
        training_window="train",
        oos_window="oos",
        regime_scope="ALL",
        metrics={"mae": 0.001},
    )
    calibrator = DeterministicForecastCalibrator(artifact)

    assert calibrator.predict({"raw_score": float("nan")}) is None


def test_runtime_calibration_requires_published_evidence() -> None:
    artifact = CalibrationArtifact.create(
        artifact_id="cal-v1",
        model_version="cal-model-v1",
        feature_schema_version="alpha-features-v3",
        coefficients={"raw_score": 0.02},
        intercept=0.0,
        training_window="train",
        oos_window="oos",
        regime_scope="ALL",
        metrics={"mae": 0.001},
    )
    registry = CalibrationRegistry()

    assert not registry.publish(artifact, evidence_ids=[], published_by="")
    assert registry.publish(artifact, evidence_ids=["oos-001"], published_by="reviewer")
    assert registry.is_published(artifact)
    assert registry.get("cal-v1") == artifact
