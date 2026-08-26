"""Independent offline Candidate-versus-Champion decision oracle."""

from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from beidou_research.mining.selection.marginal_contribution import recompute_marginal_contribution

from .contracts import (
    APPROVED_POLICY_SHA256,
    COST_COMPONENTS,
    PortfolioDecision,
    PortfolioNotVerifiable,
    PortfolioOwnerPolicy,
    canonical_digest,
    validate_decision_evidence,
)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_std = _std(left)
    right_std = _std(right)
    if left_std == 0.0 or right_std == 0.0:
        return 0.0
    left_mean, right_mean = _mean(left), _mean(right)
    covariance = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True)) / (len(left) - 1)
    return covariance / (left_std * right_std)


def _max_drawdown_loss(returns: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return -worst


def _expected_shortfall_loss(returns: Sequence[float], confidence: float) -> float:
    count = max(1, math.ceil((1.0 - confidence) * len(returns)))
    return -_mean(sorted(returns)[:count])


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(probability * (len(ordered) - 1))))
    return ordered[index]


def _bootstrap(
    *,
    champion: Sequence[float],
    combined: Sequence[float],
    delta_gross: Sequence[float],
    incremental_cost: Sequence[float],
    replicates: int,
    seed: int,
    stress_multipliers: Sequence[float],
) -> dict[str, Any]:
    n = len(champion)
    block_length = max(1, math.ceil(math.sqrt(n)))
    rng = random.Random(seed)  # noqa: S311 - deterministic statistical resampling
    metrics: dict[str, list[float]] = {
        "delta_R_mean": [],
        "incremental_annualized_sharpe": [],
        "delta_MDD": [],
        "delta_ES_95": [],
        "delta_ES_99": [],
    }
    stress_means = {str(multiplier): [] for multiplier in stress_multipliers}
    for _ in range(replicates):
        indices: list[int] = []
        while len(indices) < n:
            start = rng.randrange(n - block_length + 1)
            indices.extend(range(start, start + block_length))
        indices = indices[:n]
        champion_sample = [champion[index] for index in indices]
        combined_sample = [combined[index] for index in indices]
        delta_sample = [combined_sample[index] - champion_sample[index] for index in range(n)]
        mean_delta = _mean(delta_sample)
        std_delta = _std(delta_sample)
        metrics["delta_R_mean"].append(mean_delta)
        metrics["incremental_annualized_sharpe"].append(
            mean_delta / std_delta * math.sqrt(8760.0) if std_delta > 0.0 else 0.0
        )
        metrics["delta_MDD"].append(_max_drawdown_loss(combined_sample) - _max_drawdown_loss(champion_sample))
        metrics["delta_ES_95"].append(
            _expected_shortfall_loss(combined_sample, 0.95) - _expected_shortfall_loss(champion_sample, 0.95)
        )
        metrics["delta_ES_99"].append(
            _expected_shortfall_loss(combined_sample, 0.99) - _expected_shortfall_loss(champion_sample, 0.99)
        )
        for multiplier in stress_multipliers:
            stress_means[str(multiplier)].append(
                _mean([delta_gross[index] - multiplier * incremental_cost[index] for index in indices])
            )
    intervals = {
        name: {"lower_95": _percentile(values, 0.025), "upper_95": _percentile(values, 0.975)}
        for name, values in metrics.items()
    }
    return {
        "method": "moving-block-bootstrap",
        "replicates": replicates,
        "block_length": block_length,
        "seed": seed,
        "intervals": intervals,
        "stress_delta_mean_lower_95": {
            multiplier: _percentile(values, 0.025) for multiplier, values in stress_means.items()
        },
    }


def _decision_identity(source: Mapping[str, Any], policy: PortfolioOwnerPolicy, reason: str) -> PortfolioDecision:
    candidate = source.get("candidate_identity", {}) if isinstance(source, Mapping) else {}
    champion = source.get("champion_identity", {}) if isinstance(source, Mapping) else {}
    candidate_id = str(candidate.get("candidate_id", "")) if isinstance(candidate, Mapping) else ""
    champion_id = str(champion.get("champion_id", "")) if isinstance(champion, Mapping) else ""
    candidate_digest = str(candidate.get("identity_digest", "")) if isinstance(candidate, Mapping) else ""
    champion_digest = str(champion.get("identity_digest", "")) if isinstance(champion, Mapping) else ""
    claimed_input = str(source.get("input_bundle_digest", "")) if isinstance(source, Mapping) else ""
    timestamp = str(source.get("decision_timestamp_utc", "")) if isinstance(source, Mapping) else ""
    portfolio_id = str(source.get("portfolio_id", "")) if isinstance(source, Mapping) else ""
    supersedes = source.get("supersedes_decision_id") if isinstance(source, Mapping) else None
    identity_scope = {
        "candidate_identity_digest": candidate_digest,
        "champion_identity_digest": champion_digest,
        "policy_digest": policy.digest,
        "input_bundle_digest": claimed_input,
        "decision_timestamp_utc": timestamp,
    }
    return PortfolioDecision(
        status="NOT_VERIFIABLE",
        disposition="NOT_VERIFIABLE",
        reasons=(reason,),
        hard_gates={},
        recomputed={},
        decision_id=canonical_digest(identity_scope),
        portfolio_id=portfolio_id,
        candidate_id=candidate_id,
        champion_id=champion_id,
        candidate_identity_digest=candidate_digest,
        champion_identity_digest=champion_digest,
        policy_digest=policy.digest,
        input_bundle_digest=claimed_input,
        decision_timestamp_utc=timestamp,
        supersedes_decision_id=supersedes if isinstance(supersedes, str) else None,
    )


def _portfolio_return(bar: Mapping[str, Any], weight_field: str) -> float:
    return sum(float(weight) * float(bar["asset_returns"][asset]) for asset, weight in bar[weight_field].items())


def _cost_decimal(bar: Mapping[str, Any], field: str, order_field: str) -> float:
    """Convert bps on traded notional into a portfolio-NAV return."""
    cost_bps = sum(float(bar[field][component]) for component in COST_COMPONENTS)
    return cost_bps / 10_000.0 * float(bar[order_field]) / float(bar["portfolio_nav_quote"])


def _constraint_gate(evidence: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    config = evidence["constraints"]
    violations: list[dict[str, Any]] = []
    maxima = {
        "gross_exposure": 0.0,
        "abs_net_exposure": 0.0,
        "leverage": 0.0,
        "asset_weight": 0.0,
        "universe_exposure": 0.0,
        "turnover": 0.0,
        "participation_rate": 0.0,
        "venue_exposure": 0.0,
    }
    for index, bar in enumerate(evidence["bars"]):
        for portfolio_name, weights_field, cash_field, turnover_field in (
            ("champion", "champion_weights", "champion_cash_buffer", "champion_turnover_pct"),
            ("combined", "combined_weights", "combined_cash_buffer", "combined_turnover_pct"),
        ):
            weights = bar[weights_field]
            gross = sum(abs(float(weight)) for weight in weights.values())
            net = abs(sum(float(weight) for weight in weights.values()))
            leverage = gross
            max_weight = max(abs(float(weight)) for weight in weights.values())
            universe_exposures: dict[str, float] = {}
            venue_exposures: dict[str, float] = {}
            for asset, weight in weights.items():
                universe = config["asset_universes"].get(asset)
                venue = config["asset_venues"].get(asset)
                if universe is None or venue is None or venue not in config["venue_limits"]:
                    violations.append({"bar": index, "portfolio": portfolio_name, "constraint": "asset_mapping"})
                    continue
                universe_exposures[universe] = universe_exposures.get(universe, 0.0) + abs(float(weight))
                venue_exposures[venue] = venue_exposures.get(venue, 0.0) + abs(float(weight))
            max_universe = max(universe_exposures.values(), default=0.0)
            max_venue = max(venue_exposures.values(), default=0.0)
            turnover = float(bar[turnover_field])
            participation = float(bar["incremental_order_notional_quote"]) / float(bar["market_volume_quote"])
            maxima["gross_exposure"] = max(maxima["gross_exposure"], gross)
            maxima["abs_net_exposure"] = max(maxima["abs_net_exposure"], net)
            maxima["leverage"] = max(maxima["leverage"], leverage)
            maxima["asset_weight"] = max(maxima["asset_weight"], max_weight)
            maxima["universe_exposure"] = max(maxima["universe_exposure"], max_universe)
            maxima["turnover"] = max(maxima["turnover"], turnover)
            maxima["participation_rate"] = max(maxima["participation_rate"], participation)
            maxima["venue_exposure"] = max(maxima["venue_exposure"], max_venue)
            checks = {
                "gross_exposure": gross <= float(config["max_gross_exposure"]),
                "abs_net_exposure": net <= float(config["max_abs_net_exposure"]),
                "leverage": leverage <= float(config["max_leverage"]),
                "asset_weight": max_weight <= float(config["max_asset_weight"]),
                "universe_exposure": max_universe <= float(config["max_universe_exposure"]),
                "turnover": turnover <= float(config["max_turnover_pct_per_bar"]),
                "participation_rate": participation <= float(config["max_participation_rate"]),
                "cash_buffer": float(bar[cash_field]) >= float(config["min_cash_buffer"]),
                "venue_limits": all(
                    exposure <= float(config["venue_limits"][venue]) for venue, exposure in venue_exposures.items()
                ),
            }
            violations.extend(
                {"bar": index, "portfolio": portfolio_name, "constraint": name}
                for name, passed in checks.items()
                if not passed
            )
    return not violations, {"maxima": maxima, "violations": violations}


def _diversification(
    evidence: Mapping[str, Any], champion_net: Sequence[float], combined_net: Sequence[float]
) -> dict[str, float]:
    asset_ids = sorted(evidence["bars"][0]["asset_returns"])
    asset_volatility = {
        asset: _std([float(bar["asset_returns"][asset]) for bar in evidence["bars"]]) for asset in asset_ids
    }
    champion_weights = evidence["bars"][0]["champion_weights"]
    combined_weights = evidence["bars"][0]["combined_weights"]
    champion_vol = _std(champion_net)
    combined_vol = _std(combined_net)
    champion_numerator = sum(abs(float(weight)) * asset_volatility[asset] for asset, weight in champion_weights.items())
    combined_numerator = sum(abs(float(weight)) * asset_volatility[asset] for asset, weight in combined_weights.items())
    return {
        "champion_variance": champion_vol**2,
        "combined_variance": combined_vol**2,
        "champion_ratio": champion_numerator / champion_vol if champion_vol > 0.0 else 0.0,
        "combined_ratio": combined_numerator / combined_vol if combined_vol > 0.0 else 0.0,
    }


def decide_candidate_vs_champion(policy: PortfolioOwnerPolicy, source: Mapping[str, Any]) -> PortfolioDecision:
    """Recompute every T08 hard gate from one immutable offline input bundle."""

    try:
        evidence = validate_decision_evidence(policy, source)
    except PortfolioNotVerifiable as exc:
        return _decision_identity(source, policy, str(exc))

    candidate = evidence["candidate_identity"]
    champion = evidence["champion_identity"]
    candidate_id = candidate["candidate_id"]
    bars = evidence["bars"]
    champion_gross = [_portfolio_return(bar, "champion_weights") for bar in bars]
    combined_gross = [_portfolio_return(bar, "combined_weights") for bar in bars]
    champion_cost = [
        _cost_decimal(bar, "champion_cost_components_bps", "champion_order_notional_quote") for bar in bars
    ]
    incremental_cost = [
        _cost_decimal(bar, "incremental_cost_components_bps", "incremental_order_notional_quote") for bar in bars
    ]
    champion_net = [gross - cost for gross, cost in zip(champion_gross, champion_cost, strict=True)]
    combined_net = [
        gross - base_cost - delta_cost
        for gross, base_cost, delta_cost in zip(combined_gross, champion_cost, incremental_cost, strict=True)
    ]
    delta_gross = [combined - champion for champion, combined in zip(champion_gross, combined_gross, strict=True)]
    marginal = recompute_marginal_contribution(champion_net, combined_net)
    candidate_returns = [
        float(bar["asset_returns"][candidate_id])
        - incremental_cost[index] / max(abs(float(bar["combined_weights"][candidate_id])), 1e-12)
        for index, bar in enumerate(bars)
    ]
    candidate_std = _std(candidate_returns)
    candidate_sharpe = _mean(candidate_returns) / candidate_std * math.sqrt(8760.0) if candidate_std > 0.0 else 0.0

    policy_stresses = [float(value) for value in policy.document["turnover_and_incremental_cost"]["stress_multipliers"]]
    uncertainty = _bootstrap(
        champion=champion_net,
        combined=combined_net,
        delta_gross=delta_gross,
        incremental_cost=incremental_cost,
        replicates=int(evidence["uncertainty_spec"]["replicates"]),
        seed=int(evidence["uncertainty_spec"]["seed"]),
        stress_multipliers=policy_stresses,
    )
    intervals = uncertainty["intervals"]
    correlation = _correlation(candidate_returns, champion_net)
    diversification = _diversification(evidence, champion_net, combined_net)
    max_turnover = max(float(bar["combined_turnover_pct"]) for bar in bars)
    max_delta_turnover = max(float(bar["combined_turnover_pct"]) - float(bar["champion_turnover_pct"]) for bar in bars)
    participation = [float(bar["incremental_order_notional_quote"]) / float(bar["market_volume_quote"]) for bar in bars]
    capacity_notional = min(
        float(bar["market_volume_quote"]) * float(policy.document["capacity_and_impact"]["max_participation_rate"])
        for bar in bars
    )
    max_order = max(float(bar["incremental_order_notional_quote"]) for bar in bars)
    stress_means = {
        str(multiplier): _mean(
            [gross - multiplier * cost for gross, cost in zip(delta_gross, incremental_cost, strict=True)]
        )
        for multiplier in policy_stresses
    }
    delta_mdd = _max_drawdown_loss(combined_net) - _max_drawdown_loss(champion_net)
    delta_es95 = _expected_shortfall_loss(combined_net, 0.95) - _expected_shortfall_loss(champion_net, 0.95)
    delta_es99 = _expected_shortfall_loss(combined_net, 0.99) - _expected_shortfall_loss(champion_net, 0.99)
    regime_means: dict[str, float] = {}
    for regime in policy.document["regime_stability"]["required_regimes"]:
        multiplier = 2.0 if regime == "cost_shock" else 1.0
        values = [
            delta_gross[index] - multiplier * incremental_cost[index]
            for index, bar in enumerate(bars)
            if bar["regime"] == regime
        ]
        regime_means[regime] = _mean(values)
    positive_regime_fraction = sum(value > 0.0 for value in regime_means.values()) / len(regime_means)
    constraints_ok, constraint_metrics = _constraint_gate(evidence)

    thresholds = policy.document
    hard_gates = {
        "identity": True,
        "marginal": marginal.mean_delta_return > 0.0
        and marginal.marginal_annualized_sharpe
        >= float(thresholds["marginal_contribution"]["required_threshold"].split(">=")[-1].split(",")[0].strip()),
        "uncertainty": intervals["delta_R_mean"]["lower_95"] > 0.0
        and intervals["incremental_annualized_sharpe"]["lower_95"] >= 0.05
        and intervals["delta_MDD"]["upper_95"] <= 0.0
        and intervals["delta_ES_95"]["upper_95"] <= 0.0
        and intervals["delta_ES_99"]["upper_95"] <= 0.0,
        "correlation": correlation <= float(thresholds["correlation_and_diversification"]["max_pairwise_correlation"]),
        "diversification": diversification["combined_ratio"]
        >= float(thresholds["correlation_and_diversification"]["minimum_diversification_ratio"])
        and diversification["combined_variance"] <= diversification["champion_variance"],
        "turnover_cost": max_turnover <= float(thresholds["turnover_and_incremental_cost"]["max_turnover_pct_per_bar"])
        and max_delta_turnover <= float(thresholds["turnover_and_incremental_cost"]["max_turnover_pct_per_bar"])
        and all(value > 0.0 for value in stress_means.values()),
        "capacity": max(participation) <= float(thresholds["capacity_and_impact"]["max_participation_rate"])
        and max_order <= capacity_notional
        and all(value > 0.0 for value in uncertainty["stress_delta_mean_lower_95"].values()),
        "tail": delta_mdd <= 0.0
        and delta_es95 <= 0.0
        and delta_es99 <= 0.0
        and intervals["delta_MDD"]["upper_95"] <= 0.0
        and intervals["delta_ES_95"]["upper_95"] <= 0.0
        and intervals["delta_ES_99"]["upper_95"] <= 0.0,
        "regime": positive_regime_fraction >= float(thresholds["regime_stability"]["minimum_positive_regime_fraction"]),
        "constraints": constraints_ok,
    }
    recomputed = {
        "sample_count": len(bars),
        "mean_delta_return": marginal.mean_delta_return,
        "marginal_volatility": marginal.marginal_volatility,
        "marginal_annualized_sharpe": marginal.marginal_annualized_sharpe,
        "standalone_candidate_annualized_sharpe": candidate_sharpe,
        "candidate_champion_correlation": correlation,
        "diversification": diversification,
        "max_turnover_pct_per_bar": max_turnover,
        "max_delta_turnover_pct_per_bar": max_delta_turnover,
        "stress_delta_means": stress_means,
        "maximum_participation_rate": max(participation),
        "capacity_notional_quote": capacity_notional,
        "maximum_order_notional_quote": max_order,
        "tail": {"delta_MDD": delta_mdd, "delta_ES_95": delta_es95, "delta_ES_99": delta_es99},
        "regime_delta_means": regime_means,
        "positive_regime_fraction": positive_regime_fraction,
        "constraints": constraint_metrics,
        "uncertainty": uncertainty,
    }
    failed = [gate for gate, passed in hard_gates.items() if not passed]
    status = "PASS" if not failed else "FAIL"
    disposition = "SHADOW_ELIGIBLE" if not failed else "REJECT"
    decision_scope = {
        "candidate_identity_digest": candidate["identity_digest"],
        "champion_identity_digest": champion["identity_digest"],
        "policy_digest": policy.digest,
        "input_bundle_digest": evidence["input_bundle_digest"],
        "decision_timestamp_utc": evidence["decision_timestamp_utc"],
    }
    return PortfolioDecision(
        status=status,
        disposition=disposition,
        reasons=tuple(f"GATE_FAILED:{gate}" for gate in failed),
        hard_gates=hard_gates,
        recomputed=recomputed,
        decision_id=canonical_digest(decision_scope),
        portfolio_id=evidence["portfolio_id"],
        candidate_id=candidate_id,
        champion_id=champion["champion_id"],
        candidate_identity_digest=candidate["identity_digest"],
        champion_identity_digest=champion["identity_digest"],
        policy_digest=policy.digest,
        input_bundle_digest=evidence["input_bundle_digest"],
        decision_timestamp_utc=evidence["decision_timestamp_utc"],
        supersedes_decision_id=evidence["supersedes_decision_id"],
    )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )


def write_decision_artifacts(
    evidence_dir: str | Path,
    *,
    policy: PortfolioOwnerPolicy,
    decision: PortfolioDecision,
    counterexamples: Iterable[Mapping[str, Any]],
) -> None:
    """Write deterministic offline T08 evidence without runtime promotion hooks."""

    if (
        policy.source_sha256 != APPROVED_POLICY_SHA256
        or hashlib.sha256(policy.source_bytes).hexdigest() != APPROVED_POLICY_SHA256
    ):
        raise PortfolioNotVerifiable("POLICY_SOURCE_CUSTODY_MISMATCH")
    destination = Path(evidence_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "portfolio-owner-policy.json").write_bytes(policy.source_bytes)
    _write_json(destination / "candidate-champion-decision.json", decision.as_dict())
    _write_json(
        destination / "independent-marginal-recomputation.json",
        {"hard_gates": decision.hard_gates, "recomputed": decision.recomputed},
    )
    _write_json(destination / "portfolio-counterexamples.json", list(counterexamples))
    _write_json(
        destination / "side-effect-inventory.json",
        {
            "account_queries": 0,
            "capital_allocations": 0,
            "network_calls": 0,
            "order_actions": 0,
            "paper_promotions": 0,
            "testnet_promotions": 0,
        },
    )


__all__ = ["decide_candidate_vs_champion", "write_decision_artifacts"]
