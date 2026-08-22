"""Remaining behavior coverage for mining feature and candidate boundaries."""

from __future__ import annotations

import math
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

import beidou_research.mining.runner as runner_module
from beidou_research.mining.contracts import HorizonUnit, LabelQuality, LabelRecord, LabelSpec, PredictionKey, PriceType
from beidou_research.mining.evaluation.cost_capacity import CostModel
from beidou_research.mining.evaluation.purged_walk_forward import FoldResult
from beidou_research.mining.label_builder import PricePoint
from beidou_research.mining.runner import MiningRunner, PipelineConfig, _current_git_commit
from beidou_shared.types import FactorId, InstrumentId, SchemaVersion, VenueId


def _points(closes: list[float | str], *, missing_index: int | None = None) -> list[PricePoint]:
    result: list[PricePoint] = []
    for index, close in enumerate(closes):
        if missing_index is not None and index == missing_index:
            close = "bad"
        result.append(
            PricePoint(
                venue=VenueId("BINANCE"),
                symbol=InstrumentId("BTCUSDT"),
                timeframe="1h",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index),
                close=close,  # type: ignore[arg-type]
                open=close if isinstance(close, (int, float)) else None,
                high=float(close) + 1 if isinstance(close, (int, float)) else None,
                low=float(close) - 1 if isinstance(close, (int, float)) else None,
                volume=10.0,
                is_closed=True,
            )
        )
    return result


def _valid_label(index: int, value: float, *, quality: LabelQuality = LabelQuality.VALID) -> LabelRecord:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index)
    key = PredictionKey(
        venue=VenueId("BINANCE"),
        symbol=InstrumentId("BTCUSDT"),
        timeframe="1h",
        prediction_time=start,
        data_available_time=start,
        horizon=1,
        horizon_unit=HorizonUnit.BAR,
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
        gross_return=value,
        expected_cost_bps=1.0,
        quality_status=quality,
    )


def test_feature_builder_handles_invalid_middle_bar_and_zero_loss_rsi() -> None:
    runner = MiningRunner(PipelineConfig())
    invalid = runner._build_feature_dict(_points([100.0 + index for index in range(20)], missing_index=5))
    assert math.isnan(invalid["log_return"][5])
    assert math.isnan(invalid["rsi"][14])

    increasing = runner._build_feature_dict(_points([100.0 + index for index in range(20)]))
    assert increasing["rsi"][14] == 100.0


def test_candidate_generation_rejects_malformed_strict_policy_and_allows_diagnostic_fallback(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text(yaml.safe_dump({"generation": {"max_candidates": 1}}), encoding="utf-8")
    strict = MiningRunner(PipelineConfig(policy_path=str(malformed), strict_policy=True))
    assert strict._generate_candidates([], VenueId("BINANCE"), InstrumentId("BTCUSDT"), "1h") == []
    assert strict._generation_policy_error == "GENERATION_POLICY_INCOMPLETE"

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("[]", encoding="utf-8")
    diagnostic = MiningRunner(PipelineConfig(policy_path=str(invalid), strict_policy=False))
    candidates = diagnostic._generate_candidates([], VenueId("BINANCE"), InstrumentId("BTCUSDT"), "1h")
    assert candidates
    assert diagnostic._generation_policy_error.startswith("GENERATION_POLICY_UNAVAILABLE")


def test_candidate_evaluation_failure_isolated(monkeypatch) -> None:
    runner = MiningRunner(PipelineConfig())
    runner._feature_dict = {"close": [1.0, 2.0, 3.0]}

    class BrokenExpression:
        def evaluate_series(self, _features: object) -> list[float]:
            raise RuntimeError("evaluation failed")

    runner._ensure_registry()
    monkeypatch.setattr(runner._registry, "parse", lambda _expression: BrokenExpression())
    assert runner._evaluate_candidate({"expression_string": "close"}, []) == []


def test_current_git_commit_failures_are_empty_and_success_is_trimmed(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=" abc123 \n", returncode=0),
    )
    assert _current_git_commit() == "abc123"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="", returncode=1),
    )
    assert _current_git_commit() == ""


def _pipeline_price_data(n: int = 120, *, include_closed: bool = True) -> list[dict]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(n):
        row = {
            "timestamp": base + timedelta(hours=index),
            "close": 100.0 + index,
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "volume": 10_000.0,
        }
        if include_closed:
            row["is_closed"] = True
        rows.append(row)
    return rows


def _runner_labels(*, invalid_first: bool = False) -> list:
    labels = [_valid_label(index, float(index + 1)) for index in range(120)]
    if invalid_first:
        labels[0] = _valid_label(0, 1.0, quality=LabelQuality.STALE)
    return labels


def _patch_pipeline_dependencies(monkeypatch, runner: MiningRunner, labels: list, *, capacity_ok: bool) -> None:
    monkeypatch.setattr(runner._label_builder, "build_labels", lambda **_kwargs: labels)
    monkeypatch.setattr(
        runner._fast_screen,
        "screen",
        lambda **_kwargs: SimpleNamespace(passed=True, failure_reasons=[]),
    )

    def evaluate_candidate(candidate, _points, **_kwargs):
        scale = float(candidate.get("scale", 1.0))
        return [scale * float(index + 1) for index in range(120)]

    monkeypatch.setattr(runner, "_evaluate_candidate", evaluate_candidate)

    def fake_wfo_run(_sample_times, _label_end_times, _duration, *, evaluator, **_kwargs):
        evaluator([0], [0])  # exercise the insufficient-fold fail-closed result
        good = evaluator([0, 1, 2], [3, 4, 5])
        if good.failure_reason:
            good = FoldResult(
                fold_id=0,
                train_samples=3,
                test_samples=3,
                ic_mean=0.2,
                icir=0.2,
                sharpe=0.1,
                strategy_sharpe=0.2,
                cost_adjusted_return=0.01,
                metrics={"train_strategy_sharpe": 0.2, "train_sharpe": 0.2},
            )
        return SimpleNamespace(
            fold_results=[good],
            gate_result=runner_module.GateResult.PASS,
            icir_cv=0.2,
            fold_consistency=1.0,
            n_folds_completed=1,
            failure_reasons=[],
        )

    monkeypatch.setattr(runner._wfo, "run", fake_wfo_run)
    monkeypatch.setattr(
        runner._cpcv,
        "evaluate",
        lambda *_args, **_kwargs: SimpleNamespace(
            gate_result=runner_module.GateResult.PASS,
            n_completed=1,
            n_paths=1,
            metric_mean=0.2,
            metric_q05=0.1,
            failure_reasons=[],
        ),
    )
    monkeypatch.setattr(
        runner._stability,
        "evaluate_time_split",
        lambda *_args, **_kwargs: SimpleNamespace(dimension="time_split", degradation_pct=0.1, is_stable=True),
    )
    capacity = SimpleNamespace(
        recommended_max_aum=100_000.0,
        capacity_at_zero_return=100_000.0,
        capacity_at_half_return=50_000.0,
        signal_decay_ratio=0.9,
        avg_turnover=10.0,
        adv_used=1_000_000.0,
        participation_at_capacity=0.01,
        warnings=[],
    )
    monkeypatch.setattr(
        runner._capacity,
        "evaluate_with_gate",
        lambda *_args, **_kwargs: (
            capacity,
            capacity_ok,
            "capacity_low" if not capacity_ok else "capacity_gate_passed",
        ),
    )
    monkeypatch.setattr(
        runner_module,
        "simulate_paper_window",
        lambda *_args, **_kwargs: SimpleNamespace(
            net_return=0.1,
            gross_return=0.2,
            sharpe=1.0,
            max_drawdown=0.01,
            n_trades=10,
        ),
    )
    monkeypatch.setattr(
        runner_module,
        "evaluate_multiple_testing",
        lambda *_args, **_kwargs: SimpleNamespace(
            verdict="PASS",
            n_total_trials=2,
            n_evaluated=2,
            bh_result=None,
            dsr={"dsr": 1.0},
            pbo=None,
            failure_reasons=[],
        ),
    )


def test_runner_evidence_and_derived_factor_paths_are_behavior_backed(monkeypatch, tmp_path: Path) -> None:
    labels = _runner_labels(invalid_first=True)
    failing_config = PipelineConfig(
        run_id="coverage-failure",
        evidence_dir=str(tmp_path / "failure"),
        label_spec=LabelSpec(label_id="coverage", horizon_bars=4),
        cost_model=CostModel(taker_fee_bps=4.0),
        dataset_manifest_hash="d" * 64,
        feature_manifest_hash="f" * 64,
        policy_version="policy-v1",
    )
    failing = MiningRunner(failing_config)
    _patch_pipeline_dependencies(monkeypatch, failing, labels, capacity_ok=False)
    failure_data = _pipeline_price_data(include_closed=False)
    for row in failure_data:
        row["volume"] = 0.0
    failure_result = failing.run(
        failure_data,
        aux_price_data=_pipeline_price_data(),
        progress_callback=lambda *_args: None,
    )
    assert failure_result.evidence_bundles
    assert any("capacity_gate:capacity_low" in reason for reason in failure_result.evidence_bundles[0].failure_reasons)
    assert "closed_bar_metadata_missing" in failure_result.evidence_bundles[0].failure_reasons

    promotion_config = PipelineConfig(
        run_id="coverage-derived",
        evidence_dir=str(tmp_path / "promotion"),
        label_spec=LabelSpec(label_id="coverage", horizon_bars=4),
        cost_model=CostModel(taker_fee_bps=4.0),
        dataset_manifest_hash="d" * 64,
        feature_manifest_hash="f" * 64,
        policy_version="policy-v1",
    )
    promoted = MiningRunner(promotion_config)
    _patch_pipeline_dependencies(monkeypatch, promoted, _runner_labels(), capacity_ok=True)
    monkeypatch.setattr(
        promoted,
        "_generate_candidates",
        lambda *_args: [
            {"expression_string": "close", "expression_hash": "a" * 16, "factor_id": "a", "scale": 1.0},
            {"expression_string": "close", "expression_hash": "b" * 16, "factor_id": "b", "scale": 2.0},
        ],
    )
    monkeypatch.setattr(
        runner_module,
        "build_promotion_chain",
        lambda *_args, **_kwargs: {"status": "PROMOTABLE", "role": "entry"},
    )
    promotion_data = _pipeline_price_data()
    for row in promotion_data:
        row["volume"] = 0.0
    promoted_result = promoted.run(promotion_data, progress_callback=lambda *_args: None)
    assert promoted_result.candidates_passed == 2
    assert any(bundle.factor_id.startswith("interact_") for bundle in promoted_result.evidence_bundles)
    assert any(bundle.factor_id.startswith("residual_") for bundle in promoted_result.evidence_bundles)


def test_runner_budget_abort_and_policy_role_import_failure(monkeypatch, tmp_path: Path) -> None:
    labels = _runner_labels()
    runner = MiningRunner(
        PipelineConfig(
            run_id="coverage-budget",
            evidence_dir=str(tmp_path),
            label_spec=LabelSpec(label_id="coverage", horizon_bars=4),
        )
    )
    monkeypatch.setattr(runner._label_builder, "build_labels", lambda **_kwargs: labels)
    monkeypatch.setattr(runner, "_generate_candidates", lambda *_args: [{"expression_string": "close"}])
    monkeypatch.setattr(runner, "_evaluate_candidate", lambda *_args, **_kwargs: [float(i + 1) for i in range(120)])
    monkeypatch.setattr(
        runner._fast_screen, "screen", lambda **_kwargs: SimpleNamespace(passed=True, failure_reasons=[])
    )
    budget_values = iter([True, False])
    monkeypatch.setattr(runner, "_within_time_budget", lambda: next(budget_values))
    result = runner.run(_pipeline_price_data())
    assert result.stopped_by_time_budget is True

    original_import = __import__

    def fail_yaml_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("yaml unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fail_yaml_import)
    assert runner._pipeline_role() == "entry"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git unavailable")),
    )
    assert _current_git_commit() == ""
