"""Coverage gap tests for beidou_research.experiments.contracts.

Targets the rejection branches not exercised elsewhere: non-finite JSON
rejection in ``canonical_json``, empty identity fields, non-integer seeds, and
missing fields in ``ExperimentRunIdentity.from_dict``.
"""

from __future__ import annotations

import pytest

from beidou_research.experiments.contracts import ExperimentRunIdentity, canonical_json


def _identity(**overrides: object) -> ExperimentRunIdentity:
    values: dict[str, object] = {
        "run_id": "run-1",
        "code_commit": "abc",
        "code_tree_digest": "tree",
        "policy_digest": "policy",
        "dataset_manifest_digest": "dataset",
        "pit_manifest_digest": "pit",
        "universe": "BTCUSDT",
        "timeframe": "1h",
        "feature_digest": "feature",
        "label_digest": "label",
        "cost_model": "model",
        "seed": 42,
    }
    values.update(overrides)
    return ExperimentRunIdentity(**values)


def test_canonical_json_rejects_non_finite_floats() -> None:
    with pytest.raises(ValueError, match="NON_FINITE_JSON_VALUE"):
        canonical_json({"x": float("nan")})
    with pytest.raises(ValueError, match="NON_FINITE_JSON_VALUE"):
        canonical_json([float("inf")])


def test_identity_rejects_empty_fields() -> None:
    with pytest.raises(ValueError, match="EMPTY_IDENTITY_FIELD"):
        _identity(run_id="")
    with pytest.raises(ValueError, match="EMPTY_IDENTITY_FIELD"):
        _identity(timeframe="   ")


def test_identity_rejects_non_integer_seed() -> None:
    with pytest.raises(TypeError, match="seed must be an integer"):
        _identity(seed="42")
    with pytest.raises(TypeError, match="seed must be an integer"):
        _identity(seed=True)


def test_from_dict_rejects_missing_fields() -> None:
    with pytest.raises(ValueError, match="MISSING_IDENTITY_FIELDS"):
        ExperimentRunIdentity.from_dict({"run_id": "run-1"})
