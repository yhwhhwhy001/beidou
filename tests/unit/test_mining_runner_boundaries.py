"""Research-runner boundary coverage for fail-closed provenance and statistics."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from beidou_research.backtest.replay import PaperReplayResult
from beidou_research.mining.contracts import (
    LabelQuality,
    LabelRecord,
    LabelSpec,
    PredictionKey,
    PriceType,
    ReturnType,
)
from beidou_research.mining.evidence import EvidenceBundle
from beidou_research.mining.label_builder import PricePoint
from beidou_research.mining.runner import (
    MiningRunner,
    PipelineConfig,
    _bars_per_year_for_timeframe,
    _candidate_full_sample_p_value,
    _compute_ic,
    _compute_ic_np,
    _compute_sharpe,
    _compute_sharpe_np,
    _compute_volatility_from_returns,
    _correlation_p_value,
    _estimate_adv_from_price_data,
    _is_finite,
    _max_turnover_for_timeframe,
    _normal_cdf,
    _timeframe_to_hours,
    build_promotion_chain,
)
from beidou_shared.types import FactorId, InstrumentId, SchemaVersion, VenueId


def _policy_payload() -> dict:
    return {
        "policy_version": "test-policy-1",
        "generation": {
            "random_seed": 7,
            "template_grid": {
                "primitives": ["close"],
                "windows": [5],
                "transforms": ["identity"],
                "normalizations": ["none"],
            },
        },
        "fast_screen": {
            "min_sample_count": 20,
            "max_missing_rate": 0.1,
            "min_variance": 1e-12,
            "max_extreme_ratio": 0.05,
            "extreme_sigma": 5.0,
            "max_complexity_score": 20.0,
            "min_effective_samples": 10,
            "max_turnover_pct": 50.0,
        },
        "walk_forward": {
            "n_folds": 3,
            "train_fraction": 0.6,
            "purge_fraction": 0.05,
            "embargo_fraction": 0.02,
            "min_train_samples": 20,
            "min_test_samples": 10,
            "min_folds_for_verdict": 2,
        },
        "cost": {
            "taker_fee_bps": 4.0,
            "maker_fee_bps": 2.0,
            "avg_spread_bps": 1.0,
            "slippage_bps": 1.0,
            "funding_rate_8h_pct": 0.01,
            "impact_bps_per_10k": 0.1,
        },
        "resources": {"time_budget_minutes": 3},
    }


def _valid_label(index: int, value: float, *, quality: LabelQuality = LabelQuality.VALID) -> LabelRecord:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index)
    key = PredictionKey(
        venue=VenueId("BINANCE"),
        symbol=InstrumentId("BTCUSDT"),
        timeframe="1h",
        prediction_time=start,
        data_available_time=start,
        horizon=1,
        horizon_unit="bar",
        factor_id=FactorId("factor"),
        factor_version=SchemaVersion("1.0.0"),
    )
    return LabelRecord(
        label_id=f"label-{index}",
        prediction_key=key,
        label_start_time=start,
        label_end_time=start + timedelta(hours=1),
        label_available_time=start + timedelta(hours=1),
        entry_price_type=PriceType.CLOSE,
        exit_price_type=PriceType.CLOSE,
        cost_model_version="cost-v1",
        label_value=value,
        gross_return=value + 0.001,
        expected_cost_bps=1.0,
        quality_status=quality,
    )


def test_pipeline_config_rejects_incomplete_policy_sections(tmp_path) -> None:
    for payload, expected in (
        ([], "MINING_POLICY_MUST_BE_MAPPING"),
        ({"policy_version": ""}, "MINING_POLICY_VERSION_REQUIRED"),
        ({"policy_version": "1", "generation": []}, "MINING_POLICY_REQUIRED_SECTIONS_MISSING"),
        (
            {
                "policy_version": "1",
                "generation": {},
                "fast_screen": {},
                "walk_forward": {},
                "cost": {},
            },
            "MINING_POLICY_TEMPLATE_GRID_MISSING",
        ),
    ):
        path = tmp_path / "invalid.yaml"
        path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        with pytest.raises(ValueError, match=expected):
            PipelineConfig.from_yaml(str(path))

    payload = _policy_payload()
    del payload["generation"]["template_grid"]["transforms"]
    path = tmp_path / "template-missing.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="MINING_POLICY_TEMPLATE_GRID_INCOMPLETE"):
        PipelineConfig.from_yaml(str(path))

    payload = _policy_payload()
    del payload["cost"]["slippage_bps"]
    path = tmp_path / "cost-missing.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="MINING_POLICY_COST_MODEL_INCOMPLETE"):
        PipelineConfig.from_yaml(str(path))


def test_pipeline_config_parses_all_bound_policy_values(tmp_path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(_policy_payload()), encoding="utf-8")
    config = PipelineConfig.from_yaml(str(path))

    assert config.strict_policy is True
    assert config.policy_version == "test-policy-1"
    assert config.random_seed == 7
    assert config.fast_screen is not None
    assert config.fast_screen.min_sample_count == 20
    assert config.fold_config is not None
    assert config.fold_config.n_folds == 3
    assert config.cost_model is not None
    assert config.cost_model.model_version == "test-policy-1:cost"
    assert config.time_budget_minutes == 3.0


def test_provenance_and_budget_boundaries(tmp_path) -> None:
    runner = MiningRunner(PipelineConfig(evidence_dir=str(tmp_path)))
    assert runner._compute_dataset_hash([]) == "UNKNOWN"
    assert runner._compute_dataset_hash([{"close": 1}] * 99) == "UNKNOWN"
    assert runner._compute_dataset_hash([{"close": 1}] * 100) == "UNKNOWN"
    assert runner._validated_manifest_hash("a" * 64) == "a" * 64
    assert runner._validated_manifest_hash("not-a-manifest") == "UNKNOWN"
    assert runner._validated_manifest_hash("f" * 63 + "g") == "UNKNOWN"

    runner._budget_deadline = None
    assert runner._within_time_budget() is True
    runner._budget_clock = lambda: 10.0
    runner._budget_deadline = 10.0
    assert runner._within_time_budget() is True
    runner._budget_deadline = 9.0
    assert runner._within_time_budget() is False

    assert runner._compute_residual_values([1.0, 2.0, 3.0], {"control": [1.0, 1.5, 2.5]})


def test_statistical_helpers_cover_short_constant_and_finite_paths() -> None:
    rows = [
        {"symbol": "BTCUSDT", "close": 100.0, "volume": 2.0},
        {"symbol": "BTCUSDT", "close": 110.0, "volume": 3.0},
        {"symbol": "ETHUSDT", "close": 200.0, "volume": 4.0},
        {"symbol": "XRPUSDT", "close": 0.0, "volume": 4.0},
    ]
    assert _estimate_adv_from_price_data([], "BTCUSDT") == 0.0
    assert _estimate_adv_from_price_data(rows, "BTCUSDT") == pytest.approx(265.0)
    assert _estimate_adv_from_price_data([{"symbol": "ETHUSDT", "close": 2.0, "volume": 5.0}], "BTCUSDT") == 10.0
    assert _estimate_adv_from_price_data([{"close": 0.0, "volume": 1}], "BTCUSDT") == 0.0

    assert _compute_volatility_from_returns([]) == 0.0
    assert _compute_volatility_from_returns([math.nan]) == 0.0
    assert _compute_volatility_from_returns([0.01, math.nan, -0.01]) > 0.0
    assert _is_finite(1.0) is True
    assert _is_finite(None) is False
    assert _is_finite(True) is False
    assert _is_finite(float("nan")) is False
    assert _is_finite("bad") is False

    assert _compute_ic([], []) == 0.0
    assert _compute_ic([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) == 0.0
    assert _compute_ic([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)
    assert _compute_sharpe([]) == 0.0
    assert _compute_sharpe([1.0]) == 0.0
    assert _compute_sharpe([1.0, 1.0]) == 0.0
    assert _compute_sharpe([0.01, -0.01]) == 0.0

    p = np.array([1.0, 2.0, 3.0])
    r = np.array([3.0, 2.0, 1.0])
    assert _compute_ic_np(np.array([]), np.array([])) == 0.0
    assert _compute_ic_np(p, r) == pytest.approx(-1.0)
    assert _compute_sharpe_np(np.array([1.0])) == 0.0
    assert _compute_sharpe_np(np.array([1.0, 1.0])) == 0.0
    assert _compute_sharpe_np(np.array([1.0, 2.0])) > 0.0


def test_timeframe_and_p_value_helpers_are_conservative() -> None:
    assert _max_turnover_for_timeframe("1m") == 180_000.0
    assert _max_turnover_for_timeframe(" unknown ") == 3_000.0
    assert _bars_per_year_for_timeframe("1d") == 365
    assert _bars_per_year_for_timeframe("unknown") == 8_760
    assert _timeframe_to_hours("5m") == pytest.approx(5 / 60)
    assert _timeframe_to_hours("2w") == pytest.approx(336.0)
    assert _timeframe_to_hours("") == 0.0
    assert _timeframe_to_hours("garbage") == 0.0
    assert _normal_cdf(0.0) == pytest.approx(0.5)
    assert _correlation_p_value(0.5, 3) == 1.0
    assert _correlation_p_value(float("nan"), 20) == 1.0
    assert _correlation_p_value(1.0, 20) == 0.0
    assert 0.0 <= _correlation_p_value(0.25, 50) <= 1.0


def test_candidate_full_sample_p_value_aligns_and_rejects_invalid_labels() -> None:
    labels = [_valid_label(i, float(i + 1)) for i in range(6)]
    labels[1] = _valid_label(1, 2.0, quality=LabelQuality.STALE)
    assert _candidate_full_sample_p_value([1.0, 2.0], labels) == 1.0
    p_value = _candidate_full_sample_p_value([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], labels)
    assert 0.0 <= p_value <= 1.0


def test_build_promotion_chain_is_fail_closed_without_replay_and_checks_replay_metrics(monkeypatch) -> None:
    bundle = EvidenceBundle(
        bundle_id="bundle",
        candidate_id="candidate",
        factor_id="factor",
        factor_version="2.0.0",
        candidate_hash="c" * 16,
        factor_expression_hash="e" * 16,
        dataset_manifest_hash="d" * 64,
        feature_manifest_hash="f" * 64,
        label_spec_hash="l" * 64,
        cost_model_version="cost-v1",
        policy_version="policy-v1",
        gate_decision="PASS",
    )
    assert (
        build_promotion_chain(
            bundle,
            ic=0.2,
            icir=0.4,
            sample_count=100,
            replay=None,
            git_commit="a" * 40,
            expression_string="close",
            role="entry",
        )
        is None
    )

    class _ApproveAllGates:
        def validate_evidence(self, **_kwargs):
            return SimpleNamespace(approved=True, reason="approved-for-boundary-test")

    monkeypatch.setattr("beidou_research.factors.factor.FactorPromotionGate", _ApproveAllGates)
    replay = PaperReplayResult(paper_sharpe=-1.0, paper_drawdown_pct=-60.0, paper_ir=0.0)
    chain = build_promotion_chain(
        bundle,
        ic=0.2,
        icir=0.4,
        sample_count=100,
        replay=replay,
        git_commit="a" * 40,
        expression_string="close",
        role="entry",
    )
    assert chain
    paper_step = next(step for step in chain if step["to"] == "PAPER_TRADING")
    assert paper_step["approved"] is False
    assert "paper_sharpe" in paper_step["reason"]

    good_sharpe = PaperReplayResult(paper_sharpe=1.0, paper_drawdown_pct=-5.0, paper_ir=0.0)
    chain = build_promotion_chain(
        bundle,
        ic=0.2,
        icir=0.4,
        sample_count=100,
        replay=good_sharpe,
        git_commit="a" * 40,
        expression_string="close",
        role="entry",
    )
    challenger_step = next(step for step in chain if step["to"] == "CHALLENGER")
    assert challenger_step["approved"] is False
    assert "paper_ir" in challenger_step["reason"]


def test_label_spec_round_trip_is_available_for_runner_contract() -> None:
    spec = LabelSpec(
        label_id="test",
        horizon_bars=4,
        price_type=PriceType.CLOSE,
        return_type=ReturnType.LOG,
        cost_adjusted=True,
    )
    assert len(spec.to_hash()) == 16


def test_runner_feature_expression_policy_and_callback_boundaries(tmp_path) -> None:
    runner = MiningRunner(PipelineConfig(evidence_dir=str(tmp_path)))
    points = [
        PricePoint(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index),
            close=100.0 + (1.0 if index % 2 else -0.5) * index,
            open=100.0,
            high=101.0,
            low=99.0,
            volume=10.0,
            is_closed=True,
        )
        for index in range(20)
    ]
    features = runner._build_feature_dict(points)
    assert set(features) == {"close", "open", "high", "low", "volume", "log_return", "spread", "rsi"}
    assert math.isnan(features["log_return"][0])
    assert all(math.isfinite(value) for value in features["rsi"][14:])
    invalid_point = PricePoint(
        venue=VenueId("BINANCE"),
        symbol=InstrumentId("BTCUSDT"),
        timeframe="1h",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        close="bad",  # type: ignore[arg-type]
        high=90.0,
        low=100.0,
        is_closed=True,
    )
    invalid_features = runner._build_feature_dict([invalid_point])
    assert math.isnan(invalid_features["close"][0])
    assert math.isnan(invalid_features["spread"][0])

    assert runner._spec_to_expression("close", 5, "identity", "none") == "close"
    assert runner._spec_to_expression("close", 5, "pct_change", "zscore") == "zscore(pct_change(close, 5), 5)"
    assert runner._spec_to_expression("close", 5, "zscore", "robust_zscore") == "robust_zscore(zscore(close, 5), 5)"
    assert runner._spec_to_expression("close", 5, "diff", "rank") == "ts_rank(diff(close, 5), 5)"
    assert runner._spec_to_expression("close", 5, "unknown", "unknown") == "close"

    assert runner._evaluate_candidate({}, points) == []
    assert runner._evaluate_candidate({"expression_string": "not_a_real_expression()"}, points) == []
    runner._feature_dict = {"close": [1.0, 2.0, 3.0]}
    assert runner._evaluate_candidate({"expression_string": "close"}, points) == [1.0, 2.0, 3.0]
    runner._notify(None, "stage", 1, 2)
    runner._notify(lambda *_args: (_ for _ in ()).throw(RuntimeError("callback")), "stage", 1, 2)

    role_policy = tmp_path / "role.yaml"
    role_policy.write_text(yaml.safe_dump({"generation": {"role": "filter"}}), encoding="utf-8")
    runner.config.policy_path = str(role_policy)
    assert runner._pipeline_role() == "filter"
    role_policy.write_text(yaml.safe_dump({"generation": {"role": "unsupported"}}), encoding="utf-8")
    assert runner._pipeline_role() == "entry"
    runner.config.policy_path = str(tmp_path / "missing.yaml")
    assert runner._pipeline_role() == "entry"


def test_runner_candidate_generation_is_strict_about_policy(tmp_path) -> None:
    points: list[PricePoint] = []
    venue = VenueId("BINANCE")
    symbol = InstrumentId("BTCUSDT")
    missing = MiningRunner(PipelineConfig(policy_path=str(tmp_path / "missing.yaml"), strict_policy=True))
    assert missing._generate_candidates(points, venue, symbol, "1h") == []
    assert missing._generation_policy_error.startswith("GENERATION_POLICY_UNAVAILABLE")

    incomplete_policy = tmp_path / "incomplete.yaml"
    incomplete_policy.write_text(
        yaml.safe_dump({"generation": {"max_candidates": 1, "template_grid": {"primitives": ["close"]}}}),
        encoding="utf-8",
    )
    incomplete = MiningRunner(PipelineConfig(policy_path=str(incomplete_policy), strict_policy=True))
    assert incomplete._generate_candidates(points, venue, symbol, "1h") == []
    assert incomplete._generation_policy_error == "GENERATION_TEMPLATE_POLICY_INCOMPLETE"

    valid_policy = tmp_path / "valid-generation.yaml"
    valid_policy.write_text(
        yaml.safe_dump(
            {
                "generation": {
                    "max_candidates": 4,
                    "template_grid": {
                        "primitives": ["close"],
                        "windows": [5],
                        "transforms": ["pct_change"],
                        "normalizations": ["none"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    valid = MiningRunner(PipelineConfig(policy_path=str(valid_policy), strict_policy=False))
    candidates = valid._generate_candidates(points, venue, symbol, "1h")
    assert candidates and candidates[0]["expression_string"] == "pct_change(close, 5)"
