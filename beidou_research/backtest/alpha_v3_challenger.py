"""V2/V3 challenger validation with explicit OOS, cost and paper gates.

The challenger accepts gross, point-in-time return series and applies one
shared turnover-cost model to both candidates.  It is an offline report
builder only: it never creates an order intent and it never promotes a
strategy by itself.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .replay import PaperReplayResult


def _finite_series(values: Sequence[float], name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if not result:
        raise ValueError(f"{name} must not be empty")
    if any(not math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain only finite values")
    return result


def _compound(values: Sequence[float]) -> float:
    equity = 1.0
    for value in values:
        if value <= -1.0:
            return -1.0
        equity *= 1.0 + value
    return equity - 1.0


def _max_drawdown(values: Sequence[float]) -> float:
    equity = 1.0
    peak = equity
    drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, (equity - peak) / peak)
    return drawdown


def _sample_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(variance, 0.0))


def _ratio(numerator: float, denominator: float) -> float | None:
    if abs(denominator) <= 1e-15:
        return None
    return numerator / denominator


def _series_hash(values: Sequence[float]) -> str:
    payload = json.dumps(tuple(values), separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ChallengerMetrics:
    """Comparable OOS metrics for one candidate."""

    bars: int
    gross_return: float
    net_return: float
    active_return: float
    information_ratio: float | None
    max_drawdown: float
    upside_capture: float | None
    downside_capture: float | None
    realized_beta: float | None
    avg_gross_exposure: float | None
    avg_net_exposure: float | None
    avg_beta_exposure: float | None
    cost_paid: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_id: int
    train_start: int
    train_end: int
    oos_start: int
    oos_end: int
    v2: ChallengerMetrics
    v3: ChallengerMetrics

    def to_dict(self) -> dict[str, object]:
        return {
            "fold_id": self.fold_id,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "oos_start": self.oos_start,
            "oos_end": self.oos_end,
            "v2": self.v2.to_dict(),
            "v3": self.v3.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ChallengerReport:
    """Auditable challenger result and a fail-closed promotion decision."""

    status: str
    decision: str
    data_snapshot_hash_v2: str | None
    data_snapshot_hash_v3: str | None
    cost_model_hash_v2: str | None
    cost_model_hash_v3: str | None
    cost_bps: float
    point_in_time_validated: bool
    v2: ChallengerMetrics
    v3: ChallengerMetrics
    by_regime: dict[str, dict[str, ChallengerMetrics]]
    folds: tuple[WalkForwardFold, ...]
    paper_evidence: dict[str, object] | None
    leverage_only_improvement: bool | None
    checks: dict[str, bool]
    reasons: tuple[str, ...]
    report_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "decision": self.decision,
            "data_snapshot_hash_v2": self.data_snapshot_hash_v2,
            "data_snapshot_hash_v3": self.data_snapshot_hash_v3,
            "cost_model_hash_v2": self.cost_model_hash_v2,
            "cost_model_hash_v3": self.cost_model_hash_v3,
            "cost_bps": self.cost_bps,
            "point_in_time_validated": self.point_in_time_validated,
            "v2": self.v2.to_dict(),
            "v3": self.v3.to_dict(),
            "by_regime": {
                regime: {candidate: metrics.to_dict() for candidate, metrics in metrics_by_candidate.items()}
                for regime, metrics_by_candidate in self.by_regime.items()
            },
            "folds": [fold.to_dict() for fold in self.folds],
            "paper_evidence": self.paper_evidence,
            "leverage_only_improvement": self.leverage_only_improvement,
            "checks": dict(self.checks),
            "reasons": list(self.reasons),
            "report_hash": self.report_hash,
        }


def _apply_costs(
    gross_returns: Sequence[float], positions: Sequence[float], cost_bps: float
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    previous = 0.0
    net_returns: list[float] = []
    costs: list[float] = []
    for gross_return, position in zip(gross_returns, positions, strict=True):
        cost = abs(position - previous) * cost_bps / 10000.0
        net_returns.append(gross_return - cost)
        costs.append(cost)
        previous = position
    return tuple(net_returns), tuple(costs)


def _metrics(
    benchmark_returns: Sequence[float],
    gross_returns: Sequence[float],
    positions: Sequence[float],
    beta_exposure: Sequence[float] | None,
    cost_bps: float,
) -> ChallengerMetrics:
    net_returns, costs = _apply_costs(gross_returns, positions, cost_bps)
    active = tuple(net - benchmark for net, benchmark in zip(net_returns, benchmark_returns, strict=True))
    active_std = _sample_std(active)
    benchmark_mean = sum(benchmark_returns) / len(benchmark_returns)
    net_mean = sum(net_returns) / len(net_returns)
    benchmark_variance = sum((value - benchmark_mean) ** 2 for value in benchmark_returns)
    covariance = sum(
        (net - net_mean) * (benchmark - benchmark_mean)
        for net, benchmark in zip(net_returns, benchmark_returns, strict=True)
    )
    realized_beta = _ratio(covariance, benchmark_variance)
    positive_benchmark = [index for index, value in enumerate(benchmark_returns) if value > 0.0]
    negative_benchmark = [index for index, value in enumerate(benchmark_returns) if value < 0.0]
    upside_capture = _ratio(
        sum(net_returns[index] for index in positive_benchmark),
        sum(benchmark_returns[index] for index in positive_benchmark),
    )
    downside_capture = _ratio(
        sum(net_returns[index] for index in negative_benchmark),
        sum(benchmark_returns[index] for index in negative_benchmark),
    )
    return ChallengerMetrics(
        bars=len(gross_returns),
        gross_return=_compound(gross_returns),
        net_return=_compound(net_returns),
        active_return=_compound(net_returns) - _compound(benchmark_returns),
        information_ratio=(sum(active) / len(active)) / active_std if active_std > 0.0 else None,
        max_drawdown=_max_drawdown(net_returns),
        upside_capture=upside_capture,
        downside_capture=downside_capture,
        realized_beta=realized_beta,
        avg_gross_exposure=sum(abs(position) for position in positions) / len(positions),
        avg_net_exposure=sum(positions) / len(positions),
        avg_beta_exposure=(sum(beta_exposure) / len(beta_exposure) if beta_exposure is not None else None),
        cost_paid=sum(costs),
    )


def _paper_dict(paper_evidence: PaperReplayResult | None) -> dict[str, object] | None:
    if paper_evidence is None:
        return None
    return {
        "paper_sharpe": paper_evidence.paper_sharpe,
        "paper_drawdown_pct": paper_evidence.paper_drawdown_pct,
        "paper_ir": paper_evidence.paper_ir,
        "signal_consistency": paper_evidence.signal_consistency,
        "window_bars": paper_evidence.window_bars,
        "n_trades": paper_evidence.n_trades,
        "evidence_source": paper_evidence.evidence_source,
    }


def run_alpha_v3_challenger(
    benchmark_returns: Sequence[float],
    v2_gross_returns: Sequence[float],
    v3_gross_returns: Sequence[float],
    *,
    v2_positions: Sequence[float],
    v3_positions: Sequence[float],
    cost_bps: float,
    data_snapshot_hash_v2: str | None,
    data_snapshot_hash_v3: str | None,
    cost_model_hash_v2: str | None,
    cost_model_hash_v3: str | None,
    train_bars: int,
    oos_bars: int,
    regimes: Sequence[str] | None = None,
    v2_beta_exposure: Sequence[float] | None = None,
    v3_beta_exposure: Sequence[float] | None = None,
    point_in_time_validated: bool = False,
    paper_evidence: PaperReplayResult | None = None,
) -> ChallengerReport:
    """Run the same-data/same-cost challenger on walk-forward OOS windows."""
    benchmark = _finite_series(benchmark_returns, "benchmark_returns")
    v2_gross = _finite_series(v2_gross_returns, "v2_gross_returns")
    v3_gross = _finite_series(v3_gross_returns, "v3_gross_returns")
    v2_pos = _finite_series(v2_positions, "v2_positions")
    v3_pos = _finite_series(v3_positions, "v3_positions")
    lengths = {len(benchmark), len(v2_gross), len(v3_gross), len(v2_pos), len(v3_pos)}
    if len(lengths) != 1:
        raise ValueError("benchmark, V2, V3 and position series must have identical lengths")
    n = len(benchmark)
    if train_bars <= 0 or oos_bars <= 0 or train_bars >= n:
        raise ValueError("train_bars and oos_bars must leave a non-empty OOS window")
    if not math.isfinite(cost_bps) or cost_bps < 0.0:
        raise ValueError("cost_bps must be finite and non-negative")

    def optional_exposure(values: Sequence[float] | None, name: str) -> tuple[float, ...] | None:
        if values is None:
            return None
        parsed = _finite_series(values, name)
        if len(parsed) != n:
            raise ValueError(f"{name} must have length {n}")
        return parsed

    v2_beta = optional_exposure(v2_beta_exposure, "v2_beta_exposure")
    v3_beta = optional_exposure(v3_beta_exposure, "v3_beta_exposure")
    if regimes is None:
        regime_values = tuple("UNKNOWN" for _ in range(n))
        regimes_supplied = False
    else:
        if len(regimes) != n:
            raise ValueError(f"regimes must have length {n}")
        regime_values = tuple(str(regime).strip() or "UNKNOWN" for regime in regimes)
        regimes_supplied = True

    def make_metrics(start: int, end: int) -> tuple[ChallengerMetrics, ChallengerMetrics]:
        v2_metrics = _metrics(
            benchmark[start:end],
            v2_gross[start:end],
            v2_pos[start:end],
            v2_beta[start:end] if v2_beta is not None else None,
            cost_bps,
        )
        v3_metrics = _metrics(
            benchmark[start:end],
            v3_gross[start:end],
            v3_pos[start:end],
            v3_beta[start:end] if v3_beta is not None else None,
            cost_bps,
        )
        return v2_metrics, v3_metrics

    folds: list[WalkForwardFold] = []
    oos_start = train_bars
    fold_id = 1
    while oos_start < n:
        oos_end = min(oos_start + oos_bars, n)
        v2_metrics, v3_metrics = make_metrics(oos_start, oos_end)
        folds.append(
            WalkForwardFold(
                fold_id=fold_id,
                train_start=max(0, oos_start - train_bars),
                train_end=oos_start,
                oos_start=oos_start,
                oos_end=oos_end,
                v2=v2_metrics,
                v3=v3_metrics,
            )
        )
        fold_id += 1
        oos_start = oos_end

    v2, v3 = make_metrics(train_bars, n)
    by_regime: dict[str, dict[str, ChallengerMetrics]] = {}
    oos_regimes = regime_values[train_bars:]
    for regime in sorted(set(oos_regimes)):
        indices = [index for index in range(train_bars, n) if regime_values[index] == regime]
        by_regime[regime] = {
            "v2": _metrics(
                [benchmark[index] for index in indices],
                [v2_gross[index] for index in indices],
                [v2_pos[index] for index in indices],
                [v2_beta[index] for index in indices] if v2_beta is not None else None,
                cost_bps,
            ),
            "v3": _metrics(
                [benchmark[index] for index in indices],
                [v3_gross[index] for index in indices],
                [v3_pos[index] for index in indices],
                [v3_beta[index] for index in indices] if v3_beta is not None else None,
                cost_bps,
            ),
        }

    same_data = bool(data_snapshot_hash_v2 and data_snapshot_hash_v3) and (
        data_snapshot_hash_v2 == data_snapshot_hash_v3
    )
    same_cost = bool(cost_model_hash_v2 and cost_model_hash_v3) and cost_model_hash_v2 == cost_model_hash_v3
    paper_dict = _paper_dict(paper_evidence)
    paper_ok = (
        paper_evidence is not None and paper_evidence.window_bars > 0 and bool(paper_evidence.evidence_source.strip())
    )
    leverage_only: bool | None
    if v2.avg_gross_exposure is None or v3.avg_gross_exposure is None:
        leverage_only = None
    else:
        beta_scaled = (
            v2.avg_beta_exposure is not None
            and v3.avg_beta_exposure is not None
            and v3.avg_beta_exposure > v2.avg_beta_exposure * 1.05
        )
        drawdown_scaled = abs(v3.max_drawdown) > abs(v2.max_drawdown) * 1.05
        leverage_only = (
            v3.net_return > v2.net_return + 1e-12
            and v3.avg_gross_exposure > v2.avg_gross_exposure * 1.05
            and (beta_scaled or drawdown_scaled)
        )
    checks = {
        "same_data": same_data,
        "same_cost_model": same_cost,
        "point_in_time": point_in_time_validated,
        "walk_forward": len(folds) >= 2,
        "regime_segmented": regimes_supplied and len(set(oos_regimes)) >= 2,
        "paper_shadow": paper_ok,
        "no_leverage_only_improvement": leverage_only is False,
    }
    reasons = tuple(key.upper() for key, passed in checks.items() if not passed)
    not_verifiable_keys = {"same_data", "same_cost_model", "point_in_time", "paper_shadow"}
    has_missing_evidence = any(key in not_verifiable_keys for key in checks if not checks[key])
    status = "PASS" if all(checks.values()) else "NOT_VERIFIABLE" if has_missing_evidence else "FAIL"
    decision = "PROMOTE" if status == "PASS" else "DO_NOT_PROMOTE"
    payload: dict[str, Any] = {
        "status": status,
        "decision": decision,
        "v2": v2.to_dict(),
        "v3": v3.to_dict(),
        "folds": [fold.to_dict() for fold in folds],
        "by_regime": {
            regime: {candidate: metrics.to_dict() for candidate, metrics in candidate_metrics.items()}
            for regime, candidate_metrics in by_regime.items()
        },
        "checks": checks,
        "data_snapshot_hash_v2": data_snapshot_hash_v2,
        "data_snapshot_hash_v3": data_snapshot_hash_v3,
        "cost_model_hash_v2": cost_model_hash_v2,
        "cost_model_hash_v3": cost_model_hash_v3,
        "cost_bps": cost_bps,
        "point_in_time_validated": point_in_time_validated,
        "paper_evidence": paper_dict,
        "leverage_only_improvement": leverage_only,
        "reasons": reasons,
    }
    report_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:16]
    return ChallengerReport(
        status=status,
        decision=decision,
        data_snapshot_hash_v2=data_snapshot_hash_v2,
        data_snapshot_hash_v3=data_snapshot_hash_v3,
        cost_model_hash_v2=cost_model_hash_v2,
        cost_model_hash_v3=cost_model_hash_v3,
        cost_bps=cost_bps,
        point_in_time_validated=point_in_time_validated,
        v2=v2,
        v3=v3,
        by_regime=by_regime,
        folds=tuple(folds),
        paper_evidence=paper_dict,
        leverage_only_improvement=leverage_only,
        checks=checks,
        reasons=reasons,
        report_hash=report_hash,
    )


__all__ = [
    "ChallengerMetrics",
    "ChallengerReport",
    "WalkForwardFold",
    "run_alpha_v3_challenger",
]
