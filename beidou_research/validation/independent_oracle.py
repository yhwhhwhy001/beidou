"""Independent T07 scientific-validation reference oracle.

This module deliberately reimplements the frozen Metric Owner formulas.  It
does not call or import decision functions from ``beidou_research.mining``.
Stored PASS/FAIL fields are compared only after independent recomputation.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    ContractNotVerifiable,
    MetricOwnerPolicy,
    ScientificValidationResult,
    validate_raw_evidence,
)

_COST_COMPONENTS = (
    "maker_fee",
    "taker_fee",
    "half_spread",
    "slippage",
    "funding",
    "market_impact",
)
_SAMPLE_FIELDS = {
    "key",
    "venue",
    "symbol",
    "interval",
    "observation_time",
    "label_end_time",
    "available_as_of",
    "is_closed",
    "revision",
    "label_return",
    "candidate_predictions",
    "cost_components_bps",
    "cost_source_digests",
    "market_volume_base",
    "price_quote",
    "order_notional_quote",
    "regime",
}


def _unique(reasons: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reasons))


def _looks_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _safe_result(
    *,
    status: str,
    reasons: Iterable[str],
    policy: MetricOwnerPolicy,
    evidence: Mapping[str, Any] | None,
    hard_gates: dict[str, bool] | None = None,
    recomputed: dict[str, Any] | None = None,
    agreement: str = "NOT_COMPARABLE",
) -> ScientificValidationResult:
    family = evidence.get("family", {}) if isinstance(evidence, Mapping) else {}
    raw_digest = evidence.get("raw_evidence_digest", "") if isinstance(evidence, Mapping) else ""
    family_digest = family.get("family_registry_digest", "") if isinstance(family, Mapping) else ""
    return ScientificValidationResult(
        status=status,
        reasons=_unique(reasons),
        hard_gates=hard_gates or {},
        recomputed=recomputed or {},
        policy_digest=policy.digest,
        family_digest=family_digest if _looks_hex64(family_digest) else "",
        raw_evidence_digest=raw_digest if _looks_hex64(raw_digest) else "",
        implementation_agreement=agreement,
        promotable=False,
    )


def _utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ContractNotVerifiable("CLOCK_FIELD_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractNotVerifiable("CLOCK_FIELD_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ContractNotVerifiable("CLOCK_NOT_UTC")
    return parsed.astimezone(timezone.utc)


def _validate_samples(policy: MetricOwnerPolicy, evidence: Mapping[str, Any]) -> None:
    family = evidence["family"]
    candidate_ids = [candidate["candidate_id"] for candidate in family["candidates"]]
    expected_candidates = set(candidate_ids)
    expected_interval = policy.document["sampling_clock"]["primary_interval"]
    horizon = timedelta(hours=int(policy.document["sampling_clock"]["horizon_bars"]))
    configured_costs = policy.document["cost_capacity"]["configured_values"]
    prior_order: tuple[str, str, str, datetime, int, int] | None = None
    keys: set[str] = set()
    for sample in evidence["samples"]:
        if not isinstance(sample, dict):
            raise ContractNotVerifiable("SAMPLE_FIELDS_INVALID")
        if {"market_volume_base", "price_quote", "order_notional_quote"} - set(sample):
            raise ContractNotVerifiable("CAPACITY_INPUT_MISSING")
        if set(sample) != _SAMPLE_FIELDS:
            raise ContractNotVerifiable("SAMPLE_FIELDS_INVALID")
        key = sample["key"]
        if not isinstance(key, str) or not key or key in keys:
            raise ContractNotVerifiable("SAMPLE_KEY_DUPLICATE")
        keys.add(key)
        observation = _utc(sample["observation_time"])
        label_end = _utc(sample["label_end_time"])
        available = _utc(sample["available_as_of"])
        if label_end != observation + horizon:
            raise ContractNotVerifiable("LABEL_WINDOW_MISMATCH")
        if sample["is_closed"] is not True or observation > available:
            raise ContractNotVerifiable("SAMPLE_NOT_ELIGIBLE")
        if sample["interval"] != expected_interval:
            raise ContractNotVerifiable("SAMPLE_INTERVAL_MISMATCH")
        order = (
            str(sample["venue"]),
            str(sample["symbol"]),
            str(sample["interval"]),
            observation,
            int(policy.document["sampling_clock"]["horizon_bars"]),
            int(sample["revision"]),
        )
        if prior_order is not None and order <= prior_order:
            raise ContractNotVerifiable("SAMPLE_CLOCK_NOT_STRICTLY_INCREASING")
        prior_order = order
        predictions = sample["candidate_predictions"]
        if not isinstance(predictions, dict) or set(predictions) != expected_candidates:
            raise ContractNotVerifiable("FAMILY_PREDICTIONS_INCOMPLETE")
        components = sample["cost_components_bps"]
        sources = sample["cost_source_digests"]
        if not isinstance(components, dict) or set(components) != set(_COST_COMPONENTS):
            raise ContractNotVerifiable("COST_COMPONENTS_INCOMPLETE")
        if not isinstance(sources, dict) or set(sources) != set(_COST_COMPONENTS):
            raise ContractNotVerifiable("COST_SOURCE_BINDINGS_INCOMPLETE")
        if any(float(components[name]) <= 0.0 for name in _COST_COMPONENTS):
            raise ContractNotVerifiable("COST_COMPONENT_UNKNOWN")
        if not all(_looks_hex64(sources[name]) for name in _COST_COMPONENTS):
            raise ContractNotVerifiable("COST_SOURCE_BINDING_INVALID")
        try:
            volume = float(sample["market_volume_base"])
            price = float(sample["price_quote"])
            notional = float(sample["order_notional_quote"])
        except (TypeError, ValueError) as exc:
            raise ContractNotVerifiable("CAPACITY_INPUT_MISSING") from exc
        if volume <= 0.0 or price <= 0.0 or notional <= 0.0:
            raise ContractNotVerifiable("CAPACITY_INPUT_MISSING")
        expected_costs = {
            "maker_fee": float(configured_costs["maker_fee_bps"]),
            "taker_fee": float(configured_costs["taker_fee_bps"]),
            "half_spread": float(configured_costs["avg_spread_bps"]) / 2.0,
            "slippage": float(configured_costs["slippage_bps"]),
            "funding": float(configured_costs["funding_rate_8h_pct"]) * 100.0 * horizon.total_seconds() / (8 * 3600),
            "market_impact": float(configured_costs["impact_bps_per_10k"]) * notional / 10_000.0,
        }
        if any(
            not math.isclose(float(components[name]), expected_costs[name], rel_tol=0.0, abs_tol=1e-12)
            for name in _COST_COMPONENTS
        ):
            raise ContractNotVerifiable("COST_POLICY_BINDING_MISMATCH")


def _split_indices(n: int, groups: int) -> list[range]:
    base, remainder = divmod(n, groups)
    result: list[range] = []
    start = 0
    for group in range(groups):
        size = base + (1 if group < remainder else 0)
        result.append(range(start, start + size))
        start += size
    return result


def _cost_bps(sample: Mapping[str, Any], multiplier: float) -> float:
    return multiplier * sum(float(sample["cost_components_bps"][component]) for component in _COST_COMPONENTS)


def _strategy_return(sample: Mapping[str, Any], candidate_id: str, multiplier: float = 1.0) -> float:
    prediction = float(sample["candidate_predictions"][candidate_id])
    direction = 1.0 if prediction > 0.0 else -1.0 if prediction < 0.0 else 0.0
    return direction * float(sample["label_return"]) - _cost_bps(sample, multiplier) / 10_000.0


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sample_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_mean = _mean(left)
    right_mean = _mean(right)
    left_std = _sample_std(left)
    right_std = _sample_std(right)
    if left_std == 0.0 or right_std == 0.0:
        return 0.0
    covariance = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True)) / (len(left) - 1)
    return covariance / (left_std * right_std)


def _wfo_recomputation(
    policy: MetricOwnerPolicy,
    evidence: Mapping[str, Any],
    candidate_id: str,
) -> tuple[dict[str, Any], list[str], bool]:
    config = policy.document["wfo"]
    samples = evidence["samples"]
    n = len(samples)
    n_folds = int(config["n_folds"])
    fold_span = n // n_folds
    test_span = math.floor(fold_span * (1.0 - float(config["train_fraction"])))
    purge_span = max(math.ceil(float(config["purge_fraction"]) * fold_span), 1)
    embargo_span = math.ceil(float(config["embargo_fraction"]) * fold_span)
    expected: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    reasons: list[str] = []
    prior_test_ranges: list[range] = []
    for fold_id in range(n_folds):
        test_start = math.floor(fold_span * fold_id + fold_span * float(config["train_fraction"]))
        test_end = min(test_start + test_span, n)
        test_indices = list(range(test_start, test_end))
        embargoed = {
            index
            for previous in prior_test_ranges
            for index in range(previous.stop, min(previous.stop + embargo_span, n))
        }
        test_clock = _utc(samples[test_start]["observation_time"]) if test_start < n else None
        train_indices = [
            index
            for index in range(max(0, test_start - purge_span))
            if index not in embargoed
            and test_clock is not None
            and _utc(samples[index]["label_end_time"]) <= test_clock
        ]
        expected.append(
            {
                "fold_id": fold_id,
                "train_keys": [samples[index]["key"] for index in train_indices],
                "test_keys": [samples[index]["key"] for index in test_indices],
            }
        )
        if len(train_indices) < int(config["min_train_samples"]) or len(test_indices) < int(config["min_test_samples"]):
            reasons.append(f"WFO_FOLD_SAMPLES_INSUFFICIENT:{fold_id}")
        predictions = [float(samples[index]["candidate_predictions"][candidate_id]) for index in test_indices]
        labels = [float(samples[index]["label_return"]) for index in test_indices]
        net_returns = [_strategy_return(samples[index], candidate_id) for index in test_indices]
        ic = _pearson(predictions, labels)
        mean_net = _mean(net_returns)
        std_net = _sample_std(net_returns)
        metrics.append(
            {
                "fold_id": fold_id,
                "train_samples": len(train_indices),
                "test_samples": len(test_indices),
                "ic_mean": ic,
                "mean_net_return": mean_net,
                "annualized_sharpe": (mean_net / std_net * math.sqrt(8760.0)) if std_net > 0 else 0.0,
            }
        )
        prior_test_ranges.append(range(test_start, test_end))
    if evidence["recorded_wfo_folds"] != expected:
        reasons.append("WFO_FOLD_MEMBERSHIP_MISMATCH")
    if len(expected) < int(config["min_folds_for_verdict"]):
        reasons.append("WFO_FOLDS_INSUFFICIENT")
    positive = sum(metric["mean_net_return"] > 0.0 and metric["ic_mean"] > 0.0 for metric in metrics)
    consistency = positive / len(metrics) if metrics else 0.0
    fold_ics = [metric["ic_mean"] for metric in metrics]
    ic_std = _sample_std(fold_ics)
    result = {
        "fold_span": fold_span,
        "test_span": test_span,
        "purge_span": purge_span,
        "embargo_span": embargo_span,
        "fold_consistency": consistency,
        "icir": (_mean(fold_ics) / ic_std) if ic_std > 0.0 else 0.0,
        "folds": metrics,
    }
    gate = not reasons and consistency >= float(config["fold_consistency_threshold"])
    return result, reasons, gate


def _cpcv_recomputation(
    policy: MetricOwnerPolicy,
    evidence: Mapping[str, Any],
    selected_candidate_id: str,
) -> tuple[dict[str, Any], list[str], bool, bool]:
    config = policy.document["cpcv"]
    samples = evidence["samples"]
    candidates = [candidate["candidate_id"] for candidate in evidence["family"]["candidates"]]
    groups = _split_indices(len(samples), int(config["n_groups"]))
    expected: list[dict[str, Any]] = []
    pbo_overfit = 0
    consistent_paths = 0
    path_metrics: list[dict[str, Any]] = []
    for path_id, test_groups in enumerate(
        itertools.combinations(range(int(config["n_groups"])), int(config["n_test_groups"]))
    ):
        test_indices = sorted(index for group in test_groups for index in groups[group])
        excluded = set(test_indices)
        purge_bars = int(config["purge_bars"])
        for index in test_indices:
            excluded.update(range(max(0, index - purge_bars), min(len(samples), index + purge_bars + 1)))
        for group in test_groups:
            stop = groups[group].stop
            excluded.update(range(stop, min(len(samples), stop + int(config["embargo_bars"]))))
        train_indices = [index for index in range(len(samples)) if index not in excluded]
        expected.append(
            {
                "path_id": path_id,
                "test_groups": list(test_groups),
                "train_keys": [samples[index]["key"] for index in train_indices],
                "test_keys": [samples[index]["key"] for index in test_indices],
            }
        )
        train_scores = {
            candidate: _mean([_strategy_return(samples[index], candidate) for index in train_indices])
            for candidate in candidates
        }
        test_scores = {
            candidate: _mean([_strategy_return(samples[index], candidate) for index in test_indices])
            for candidate in candidates
        }
        selected_is = max(candidates, key=lambda candidate: (train_scores[candidate], candidate))
        ordered_oos = sorted(candidates, key=lambda candidate: (test_scores[candidate], candidate))
        selected_oos_rank = ordered_oos.index(selected_is) + 1
        if selected_oos_rank <= math.floor(len(candidates) / 2):
            pbo_overfit += 1
        if test_scores[selected_candidate_id] > 0.0:
            consistent_paths += 1
        path_metrics.append(
            {
                "path_id": path_id,
                "train_samples": len(train_indices),
                "test_samples": len(test_indices),
                "selected_is_candidate": selected_is,
                "selected_oos_rank": selected_oos_rank,
                "selected_oos_net_return": test_scores[selected_candidate_id],
            }
        )
    reasons: list[str] = []
    expected_count = math.comb(int(config["n_groups"]), int(config["n_test_groups"]))
    if len(evidence["recorded_cpcv_paths"]) != expected_count:
        reasons.append("CPCV_PATH_COUNT_MISMATCH")
    elif evidence["recorded_cpcv_paths"] != expected:
        reasons.append("CPCV_PATH_MEMBERSHIP_MISMATCH")
    if any(
        metric["train_samples"] < int(config["min_train_samples"])
        or metric["test_samples"] < int(config["min_test_samples"])
        for metric in path_metrics
    ):
        reasons.append("CPCV_PATH_SAMPLES_INSUFFICIENT")
    if len(path_metrics) < int(config["min_paths_for_verdict"]):
        reasons.append("CPCV_PATHS_INSUFFICIENT")
    pbo = pbo_overfit / len(path_metrics) if path_metrics else 1.0
    consistency = consistent_paths / len(path_metrics) if path_metrics else 0.0
    result = {"path_count": len(path_metrics), "path_consistency": consistency, "pbo": pbo, "paths": path_metrics}
    cpcv_gate = not reasons and consistency >= float(config["min_path_consistency"])
    pbo_gate = not reasons and pbo <= float(policy.document["multiple_testing"]["pbo"]["threshold"])
    return result, reasons, cpcv_gate, pbo_gate


def _bh_adjusted(pvalues: Sequence[float]) -> list[float]:
    n = len(pvalues)
    indexed = sorted(enumerate(pvalues), key=lambda item: (item[1], item[0]))
    adjusted = [1.0] * n
    running = 1.0
    for reverse_index in range(n - 1, -1, -1):
        original_index, pvalue = indexed[reverse_index]
        rank = reverse_index + 1
        running = min(running, pvalue * n / rank, 1.0)
        adjusted[original_index] = running
    return adjusted


def _holm_adjusted(pvalues: Sequence[float]) -> list[float]:
    n = len(pvalues)
    indexed = sorted(enumerate(pvalues), key=lambda item: (item[1], item[0]))
    adjusted = [1.0] * n
    running = 0.0
    for rank, (original_index, pvalue) in enumerate(indexed, start=1):
        running = max(running, (n - rank + 1) * pvalue)
        adjusted[original_index] = min(1.0, running)
    return adjusted


def _moments(values: Sequence[float]) -> tuple[float, float, float, float]:
    mean = _mean(values)
    std = _sample_std(values)
    if std == 0.0:
        return mean, std, 0.0, 3.0
    n = len(values)
    skewness = sum(((value - mean) / std) ** 3 for value in values) / n
    kurtosis = sum(((value - mean) / std) ** 4 for value in values) / n
    return mean, std, skewness, kurtosis


def _multiple_testing(
    policy: MetricOwnerPolicy,
    evidence: Mapping[str, Any],
    net_returns: Sequence[float],
) -> tuple[dict[str, Any], dict[str, bool], list[str]]:
    family = evidence["family"]
    candidates = family["candidates"]
    selected = family["selected_candidate_id"]
    selected_index = next(index for index, candidate in enumerate(candidates) if candidate["candidate_id"] == selected)
    pvalues = [float(candidate["p_value"]) for candidate in candidates]
    bh = _bh_adjusted(pvalues)
    holm = _holm_adjusted(pvalues)
    mean, std, skewness, kurtosis = _moments(net_returns)
    annualized_sharpe = mean / std * math.sqrt(8760.0) if std > 0.0 else 0.0
    n = len(net_returns)
    m_family = len(candidates)
    reasons: list[str] = []
    if m_family <= 1 or n < 2:
        reasons.append("DSR_INPUTS_INSUFFICIENT")
        probability = 0.0
        one_sided = 1.0
        se_sr = 0.0
        expected_max = 0.0
    else:
        variance_term = 1.0 - skewness * annualized_sharpe + ((kurtosis - 1.0) / 4.0) * annualized_sharpe**2
        if variance_term <= 0.0:
            reasons.append("DSR_STANDARD_ERROR_INVALID")
            probability = 0.0
            one_sided = 1.0
            se_sr = 0.0
            expected_max = 0.0
        else:
            se_sr = math.sqrt(variance_term / n)
            quantile = NormalDist().inv_cdf((m_family - 0.326) / (m_family + 0.348))
            expected_max = se_sr * quantile
            z_score = (annualized_sharpe - expected_max) / se_sr if se_sr > 0.0 else float("-inf")
            probability = NormalDist().cdf(z_score)
            one_sided = 1.0 - probability
    alpha = float(policy.document["multiple_testing"]["fdr"]["alpha"])
    gates = {
        "fdr": bh[selected_index] <= alpha,
        "holm": holm[selected_index] <= float(policy.document["multiple_testing"]["holm"]["alpha"]),
        "dsr": probability >= 0.95 and one_sided <= 0.05,
    }
    return (
        {
            "m_family": m_family,
            "selected_index": selected_index,
            "selected_p_value": pvalues[selected_index],
            "selected_q_value": bh[selected_index],
            "selected_holm_adjusted_p": holm[selected_index],
            "annualized_net_sharpe": annualized_sharpe,
            "skewness": skewness,
            "kurtosis": kurtosis,
            "sharpe_standard_error": se_sr,
            "expected_max_sharpe": expected_max,
            "dsr_probability": probability,
            "one_sided_p_value": one_sided,
        },
        gates,
        reasons,
    )


def _bootstrap_lower_bound(values: Sequence[float], *, replicates: int, seed: int) -> tuple[float, float]:
    n = len(values)
    block_length = max(1, math.ceil(math.sqrt(n)))
    n_blocks = math.ceil(n / block_length)
    prefix = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
    rng = random.Random(seed)  # noqa: S311  # nosec B311 - deterministic statistical resampling
    means: list[float] = []
    for _ in range(replicates):
        remaining = n
        total = 0.0
        for _ in range(n_blocks):
            take = min(block_length, remaining)
            start = rng.randrange(n - block_length + 1)
            total += prefix[start + take] - prefix[start]
            remaining -= take
            if remaining <= 0:
                break
        means.append(total / n)
    means.sort()
    lower_index = max(0, math.floor(0.025 * (replicates - 1)))
    upper_index = min(replicates - 1, math.ceil(0.975 * (replicates - 1)))
    return means[lower_index], means[upper_index]


def _cost_capacity_uncertainty(
    policy: MetricOwnerPolicy,
    evidence: Mapping[str, Any],
    candidate_id: str,
) -> tuple[dict[str, Any], dict[str, bool]]:
    samples = evidence["samples"]
    config = policy.document["cost_capacity"]
    multipliers = [float(value) for value in config["configured_values"]["stress_multipliers"]]
    seed_material = f"{policy.digest}{evidence['family']['family_id']}{candidate_id}"
    seed = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
    stress: list[dict[str, Any]] = []
    all_positive = True
    for multiplier in multipliers:
        returns = [_strategy_return(sample, candidate_id, multiplier) for sample in samples]
        lower, upper = _bootstrap_lower_bound(
            returns,
            replicates=int(policy.document["uncertainty"]["replicates"]),
            seed=seed,
        )
        all_positive = all_positive and lower > 0.0
        stress.append(
            {
                "multiplier": multiplier,
                "mean_net_return": _mean(returns),
                "ci_lower_95": lower,
                "ci_upper_95": upper,
            }
        )
    participation_rates = [
        float(sample["order_notional_quote"]) / (float(sample["market_volume_base"]) * float(sample["price_quote"]))
        for sample in samples
    ]
    maximum_participation = max(participation_rates)
    capacity_notional = min(
        float(sample["market_volume_base"]) * float(sample["price_quote"]) * float(config["participation_rate_max"])
        for sample in samples
    )
    capacity_ok = (
        maximum_participation <= float(config["participation_rate_max"])
        and all(float(sample["order_notional_quote"]) <= capacity_notional for sample in samples)
        and all_positive
    )
    return (
        {
            "bootstrap_seed": seed,
            "block_length": max(1, math.ceil(math.sqrt(len(samples)))),
            "replicates": int(policy.document["uncertainty"]["replicates"]),
            "stress": stress,
            "maximum_participation": maximum_participation,
            "capacity_notional": capacity_notional,
        },
        {"cost_capacity": capacity_ok, "uncertainty": all_positive},
    )


def _net_return_from_stability_sample(sample: Mapping[str, Any], multiplier: float = 1.0) -> float:
    required = {"key", "prediction", "label_return", "cost_components_bps", "cost_source_digests"}
    if set(sample) != required:
        raise ContractNotVerifiable("STABILITY_SAMPLE_FIELDS_INVALID")
    components = sample["cost_components_bps"]
    sources = sample["cost_source_digests"]
    if not isinstance(components, dict) or set(components) != set(_COST_COMPONENTS):
        raise ContractNotVerifiable("COST_COMPONENTS_INCOMPLETE")
    if not isinstance(sources, dict) or set(sources) != set(_COST_COMPONENTS):
        raise ContractNotVerifiable("COST_SOURCE_BINDINGS_INCOMPLETE")
    direction = 1.0 if float(sample["prediction"]) > 0 else -1.0 if float(sample["prediction"]) < 0 else 0.0
    return (
        direction * float(sample["label_return"])
        - multiplier * sum(float(components[key]) for key in _COST_COMPONENTS) / 10_000.0
    )


def _stability_recomputation(
    policy: MetricOwnerPolicy,
    evidence: Mapping[str, Any],
    candidate_id: str,
) -> tuple[dict[str, Any], list[str], bool]:
    config = policy.document["stability"]
    required_dimensions = set(config["dimensions"])
    supplied = evidence["stability"]
    if set(supplied) != {"timeframe_1h_vs_1d", "parameter_perturbation"}:
        return {}, ["STABILITY_DIMENSION_MISSING"], False
    samples = evidence["samples"]
    baseline_returns = [_strategy_return(sample, candidate_id) for sample in samples]
    baseline = _mean(baseline_returns)
    epsilon = 1e-12
    threshold = float(config["degradation_threshold"])
    min_regime = int(config["min_samples_per_regime"])
    reasons: list[str] = []
    dimensions: dict[str, Any] = {}

    regimes: dict[str, list[float]] = {}
    for sample, net_return in zip(samples, baseline_returns, strict=True):
        regimes.setdefault(str(sample["regime"]), []).append(net_return)
    if len(regimes) < 2 or any(len(values) < min_regime for values in regimes.values()):
        reasons.append("STABILITY_REGIME_SAMPLES_INSUFFICIENT")
    regime_degradations = {
        regime: (baseline - _mean(values)) / max(abs(baseline), epsilon) for regime, values in regimes.items()
    }
    dimensions["regime"] = regime_degradations

    daily = supplied["timeframe_1h_vs_1d"]
    if not isinstance(daily, list) or len(daily) < min_regime:
        reasons.append("STABILITY_TIMEFRAME_SAMPLES_INSUFFICIENT")
        daily_metric = 0.0
    else:
        daily_metric = _mean([_net_return_from_stability_sample(sample) for sample in daily])
    timeframe_degradation = (baseline - daily_metric) / max(abs(baseline), epsilon)
    dimensions["timeframe_1h_vs_1d"] = timeframe_degradation

    perturbations = supplied["parameter_perturbation"]
    if not isinstance(perturbations, list) or len(perturbations) != int(config["n_perturbations"]):
        reasons.append("STABILITY_PERTURBATIONS_INSUFFICIENT")
        perturbation_degradations: list[float] = []
    else:
        perturbation_degradations = []
        for perturbation in perturbations:
            if not isinstance(perturbation, dict) or set(perturbation) != {
                "perturbation_id",
                "parameter_delta_pct",
                "predictions",
            }:
                raise ContractNotVerifiable("STABILITY_PERTURBATION_FIELDS_INVALID")
            if not math.isclose(
                abs(float(perturbation["parameter_delta_pct"])),
                float(config["parameter_perturbation_pct"]),
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ContractNotVerifiable("STABILITY_PERTURBATION_POLICY_MISMATCH")
            predictions = perturbation["predictions"]
            if not isinstance(predictions, list) or len(predictions) != len(samples):
                raise ContractNotVerifiable("STABILITY_PERTURBATION_LENGTH_MISMATCH")
            returns = []
            for prediction, sample in zip(predictions, samples, strict=True):
                direction = 1.0 if float(prediction) > 0 else -1.0 if float(prediction) < 0 else 0.0
                returns.append(direction * float(sample["label_return"]) - _cost_bps(sample, 1.0) / 10_000.0)
            metric = _mean(returns)
            perturbation_degradations.append((baseline - metric) / max(abs(baseline), epsilon))
    dimensions["parameter_perturbation"] = perturbation_degradations
    if required_dimensions != {"regime", "timeframe_1h_vs_1d", "parameter_perturbation"}:
        reasons.append("STABILITY_POLICY_DIMENSIONS_UNKNOWN")
    all_degradations = [*regime_degradations.values(), timeframe_degradation, *perturbation_degradations]
    gate = not reasons and bool(all_degradations) and all(value <= threshold for value in all_degradations)
    return {"baseline_metric": baseline, "dimensions": dimensions}, reasons, gate


def validate_scientific_evidence(
    policy: MetricOwnerPolicy,
    source: Mapping[str, Any],
) -> ScientificValidationResult:
    """Recompute every mandatory T07 gate from raw immutable evidence."""

    try:
        evidence = validate_raw_evidence(policy, source)
        _validate_samples(policy, evidence)
    except ContractNotVerifiable as exc:
        return _safe_result(status="NOT_VERIFIABLE", reasons=[str(exc)], policy=policy, evidence=source)

    family = evidence["family"]
    candidate_id = family["selected_candidate_id"]
    reasons: list[str] = []
    hard_gates: dict[str, bool] = {}
    recomputed: dict[str, Any] = {}

    wfo, wfo_reasons, wfo_gate = _wfo_recomputation(policy, evidence, candidate_id)
    recomputed["wfo"] = wfo
    reasons.extend(wfo_reasons)
    hard_gates["wfo"] = wfo_gate

    cpcv, cpcv_reasons, cpcv_gate, pbo_gate = _cpcv_recomputation(policy, evidence, candidate_id)
    recomputed["cpcv"] = cpcv
    reasons.extend(cpcv_reasons)
    hard_gates["cpcv"] = cpcv_gate
    hard_gates["pbo"] = pbo_gate

    net_returns = [_strategy_return(sample, candidate_id) for sample in evidence["samples"]]
    multiple_testing, testing_gates, testing_reasons = _multiple_testing(policy, evidence, net_returns)
    recomputed["multiple_testing"] = multiple_testing
    hard_gates.update(testing_gates)
    reasons.extend(testing_reasons)

    cost_uncertainty, cost_gates = _cost_capacity_uncertainty(policy, evidence, candidate_id)
    recomputed["cost_capacity_uncertainty"] = cost_uncertainty
    hard_gates.update(cost_gates)

    try:
        stability, stability_reasons, stability_gate = _stability_recomputation(policy, evidence, candidate_id)
    except ContractNotVerifiable as exc:
        stability, stability_reasons, stability_gate = {}, [str(exc)], False
    recomputed["stability"] = stability
    reasons.extend(stability_reasons)
    hard_gates["stability"] = stability_gate

    sample_gate = (
        len(evidence["samples"]) >= int(policy.document["minimum_samples"]["fast_screen"]["min_sample_count"])
        and len(wfo["folds"]) >= int(policy.document["minimum_samples"]["wfo"]["min_folds"])
        and cpcv["path_count"] >= int(policy.document["minimum_samples"]["cpcv"]["min_paths"])
    )
    hard_gates["sample_sufficiency"] = sample_gate
    if not sample_gate:
        reasons.append("SAMPLES_OR_FOLDS_INSUFFICIENT")

    evidence_gap = bool(reasons)
    recomputed_status = "NOT_VERIFIABLE" if evidence_gap else "PASS" if all(hard_gates.values()) else "FAIL"
    claim = evidence["implementation_claim"]
    claim_bound = claim["family_denominator"] == len(family["candidates"]) and claim["policy_digest"] == policy.digest
    agreement = "AGREE" if claim_bound and claim["status"] == recomputed_status else "DISAGREE"
    if agreement == "DISAGREE":
        reasons.append("IMPLEMENTATION_RECOMPUTATION_DISAGREEMENT")
        status = "NOT_VERIFIABLE"
    else:
        status = recomputed_status
    return _safe_result(
        status=status,
        reasons=reasons,
        policy=policy,
        evidence=evidence,
        hard_gates=hard_gates,
        recomputed=recomputed,
        agreement=agreement,
    )


def rollback_scientific_validation(result: ScientificValidationResult) -> dict[str, Any]:
    """Freeze promotion while preserving immutable historical bindings."""

    return {
        "status": "NOT_VERIFIABLE",
        "promotion_enabled": False,
        "historical_status": result.status,
        "preserved_policy_digest": result.policy_digest,
        "preserved_family_digest": result.family_digest,
        "preserved_raw_evidence_digest": result.raw_evidence_digest,
        "diagnostic_fallback_only": True,
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )


def write_acceptance_artifacts(
    evidence_dir: str | Path,
    *,
    policy: MetricOwnerPolicy,
    recomputation: ScientificValidationResult,
    negative_results: Sequence[ScientificValidationResult],
    mutation_outcomes: Mapping[str, str],
) -> None:
    """Write the four deterministic artifacts required by T07 acceptance."""

    destination = Path(evidence_dir)
    destination.mkdir(parents=True, exist_ok=True)
    killed = sum(status != "PASS" for status in mutation_outcomes.values())
    total = len(mutation_outcomes)
    score = killed / total if total else 0.0
    _write_json(destination / "metric-owner-policy.json", policy.document)
    _write_json(destination / "independent-recomputation.json", recomputation.as_dict())
    _write_json(
        destination / "semantic-negative-fixtures.json",
        {
            "status": "PASS" if all(result.status != "PASS" for result in negative_results) else "FAIL",
            "results": [result.as_dict() for result in negative_results],
        },
    )
    _write_json(
        destination / "gate-mutation-score.json",
        {
            "status": "PASS" if total > 0 and killed == total else "FAIL",
            "killed": killed,
            "total": total,
            "score": score,
            "outcomes": dict(mutation_outcomes),
        },
    )


__all__ = [
    "rollback_scientific_validation",
    "validate_scientific_evidence",
    "write_acceptance_artifacts",
]
