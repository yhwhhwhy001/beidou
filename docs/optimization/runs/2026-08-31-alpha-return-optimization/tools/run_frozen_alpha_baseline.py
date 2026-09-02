"""Causal, fixed-policy diagnostic baseline for the frozen August dataset.

This tool intentionally evaluates only the already-shipped ``OfflineAlphaApp``
and does not generate candidates or tune parameters.  The authorized dataset
was inspected before an OOS seal existed, so every result is explicitly
research-diagnostic and cannot be used for promotion or a profitability claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from apps.alpha_app import BoundLocalData, OfflineAlphaApp  # noqa: E402
from beidou_research.data.dataset_manifest import DatasetManifest  # noqa: E402
from beidou_research.economic_truth import TruthGate, assess_economic_truth  # noqa: E402
from beidou_shared.contracts.experiment import DatasetRef  # noqa: E402
from beidou_shared.types import InstrumentId, SchemaVersion, VenueId  # noqa: E402
from beidou_strategy.alpha.trend import DEFAULT_TREND_ALPHA_POLICY  # noqa: E402

DATASET = REPOSITORY_ROOT / "artifacts/datasets/alpha-return-2026-08-01_2026-08-31-v1"
COST_POLICY = REPOSITORY_ROOT / "config/factor_mining_policy.yaml"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
INTERVAL = "1h"
HOUR_MS = 3_600_000
WARMUP_BARS = max(DEFAULT_TREND_ALPHA_POLICY.horizons) + 1


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_signal_timing(source_open_ms: int, interval_ms: int, execution_open_ms: int) -> int:
    """Return source availability time or reject same-bar/lookahead execution."""

    available_at = int(source_open_ms) + int(interval_ms)
    if int(execution_open_ms) < available_at:
        raise ValueError("LOOKAHEAD_OR_SAME_BAR_EXECUTION")
    return available_at


def apply_explicit_costs(
    gross_returns: Sequence[float],
    positions: Sequence[float],
    *,
    turnover_cost_bps: float,
    adverse_hourly_funding_bps: float,
) -> tuple[float, ...]:
    """Apply one-way turnover cost plus adverse hourly funding carry."""

    if len(gross_returns) != len(positions):
        raise ValueError("GROSS_RETURN_POSITION_LENGTH_MISMATCH")
    if not math.isfinite(turnover_cost_bps) or turnover_cost_bps < 0:
        raise ValueError("TURNOVER_COST_INVALID")
    if not math.isfinite(adverse_hourly_funding_bps) or adverse_hourly_funding_bps < 0:
        raise ValueError("FUNDING_COST_INVALID")
    previous = 0.0
    result: list[float] = []
    for gross_return, position in zip(gross_returns, positions, strict=True):
        gross = float(gross_return)
        target = float(position)
        if not math.isfinite(gross) or not math.isfinite(target):
            raise ValueError("NON_FINITE_RETURN_OR_POSITION")
        turnover = abs(target - previous)
        transaction_cost = turnover * turnover_cost_bps / 10_000.0
        funding_cost = abs(target) * adverse_hourly_funding_bps / 10_000.0
        result.append(gross - transaction_cost - funding_cost)
        previous = target
    return tuple(result)


def _compound(values: Sequence[float]) -> float:
    equity = 1.0
    for value in values:
        if value <= -1.0:
            return -1.0
        equity *= 1.0 + value
    return equity - 1.0


def _max_drawdown(values: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)
    return drawdown


def _sample_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _metrics(
    gross_returns: Sequence[float],
    net_returns: Sequence[float],
    positions: Sequence[float],
    *,
    turnover_units: float | None = None,
) -> dict[str, float | int | None]:
    if not gross_returns or not (len(gross_returns) == len(net_returns) == len(positions)):
        raise ValueError("METRIC_SERIES_INVALID")
    net_mean = sum(net_returns) / len(net_returns)
    net_std = _sample_std(net_returns)
    previous = 0.0
    calculated_turnover = 0.0
    for position in positions:
        calculated_turnover += abs(float(position) - previous)
        previous = float(position)
    return {
        "bars": len(net_returns),
        "gross_return": _compound(gross_returns),
        "net_return": _compound(net_returns),
        "annualized_hourly_sharpe": net_mean / net_std * math.sqrt(24 * 365) if net_std > 0 else None,
        "max_drawdown": _max_drawdown(net_returns),
        "average_absolute_position": sum(abs(float(value)) for value in positions) / len(positions),
        "turnover_units": calculated_turnover if turnover_units is None else turnover_units,
        "nonzero_target_bars": sum(abs(float(value)) > 1e-12 for value in positions),
    }


def _load_cost_policy(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cost"), dict):
        raise ValueError("COST_POLICY_REQUIRED")
    cost = payload["cost"]
    required = (
        "taker_fee_bps",
        "avg_spread_bps",
        "slippage_bps",
        "funding_rate_8h_pct",
        "impact_bps_per_10k",
    )
    if any(name not in cost for name in required):
        raise ValueError("COST_POLICY_INCOMPLETE")
    values = {name: float(cost[name]) for name in required}
    if any(not math.isfinite(value) or value < 0 for value in values.values()):
        raise ValueError("COST_POLICY_VALUE_INVALID")
    turnover = values["taker_fee_bps"] + values["avg_spread_bps"] + values["slippage_bps"]
    funding_hourly = values["funding_rate_8h_pct"] * 100.0 / 8.0
    return {
        "policy_version": str(payload.get("policy_version", "")),
        "policy_sha256": _sha256(path),
        "policy_path": str(path.relative_to(REPOSITORY_ROOT)),
        "turnover_cost_bps": turnover,
        "adverse_hourly_funding_bps": funding_hourly,
        "components": values,
        "impact_application": "NOT_EVALUATED_NO_NOTIONAL_OR_PARTICIPATION_FACTS",
        "interpretation": "diagnostic adverse carry; not a calibrated execution-cost model",
    }


def _evaluate_symbol(
    *,
    dataset_root: Path,
    symbol: str,
    cost_model: dict[str, Any],
) -> tuple[
    dict[str, Any],
    tuple[float, ...],
    tuple[float, ...],
    tuple[float, ...],
    tuple[float, ...],
    tuple[int, ...],
]:
    parquet_path = dataset_root / "frozen" / symbol / f"{INTERVAL}.parquet"
    manifest_path = dataset_root / "frozen" / symbol / f"{INTERVAL}.manifest.json"
    frame = pd.read_parquet(parquet_path).sort_values("open_time").reset_index(drop=True)
    manifest = DatasetManifest.read(manifest_path)
    if manifest is None:
        raise ValueError(f"MANIFEST_UNREADABLE:{symbol}")
    assessment = DatasetManifest.assess_economic_research(frame, manifest, symbol, INTERVAL)
    if not assessment.eligible:
        raise ValueError(f"MARKET_DATA_NOT_ELIGIBLE:{symbol}:{','.join(assessment.reasons)}")
    if len(frame) <= WARMUP_BARS:
        raise ValueError(f"BASELINE_SAMPLE_TOO_SHORT:{symbol}")

    times = tuple(int(value) for value in frame["open_time"].tolist())
    if any(right - left != HOUR_MS for left, right in pairwise(times)):
        raise ValueError(f"BAR_CADENCE_MISMATCH:{symbol}")
    if not frame["is_closed"].map(bool).all():
        raise ValueError(f"UNCLOSED_BAR:{symbol}")

    closes = tuple(float(value) for value in frame["close"].tolist())
    opens = tuple(float(value) for value in frame["open"].tolist())
    app = OfflineAlphaApp()
    dataset_ref = DatasetRef(
        dataset_id=f"alpha-return-2026-08:{symbol}:{INTERVAL}",
        version=SchemaVersion("2.0"),
        content_hash=str(manifest["content_sha256"]),
    )
    positions: list[float] = []
    gross_returns: list[float] = []
    benchmark_returns: list[float] = []
    execution_times: list[int] = []
    signal_records: list[dict[str, object]] = []
    lookahead_violations = 0
    same_bar_executions = 0
    threshold = float(DEFAULT_TREND_ALPHA_POLICY.entry_threshold)
    subthreshold_nonzero = 0

    for source_index in range(WARMUP_BARS - 1, len(frame) - 1):
        execution_index = source_index + 1
        source_open = times[source_index]
        execution_open = times[execution_index]
        try:
            available_at = validate_signal_timing(source_open, HOUR_MS, execution_open)
        except ValueError:
            lookahead_violations += 1
            raise
        if execution_open == source_open:
            same_bar_executions += 1
        observed_at = datetime.fromtimestamp(available_at / 1000.0, tz=timezone.utc)
        result = app.evaluate(
            BoundLocalData(
                dataset=dataset_ref,
                instrument_id=InstrumentId(symbol),
                venue_id=VenueId("BINANCE_USDM"),
                closes=closes[: source_index + 1],
                observed_at=observed_at,
            )
        )
        position = float(result.target.target_weight)
        if 1e-12 < abs(position) < threshold:
            subthreshold_nonzero += 1
        gross_return = position * (closes[execution_index] / opens[execution_index] - 1.0)
        positions.append(position)
        gross_returns.append(gross_return)
        benchmark_returns.append(closes[execution_index] / opens[execution_index] - 1.0)
        execution_times.append(execution_open)
        signal_records.append(
            {
                "source_open_time_ms": source_open,
                "source_available_at_ms": available_at,
                "execution_open_time_ms": execution_open,
                "target_weight": position,
                "forecast_hash": result.target.forecast_hash,
            }
        )

    net_returns = apply_explicit_costs(
        gross_returns,
        positions,
        turnover_cost_bps=float(cost_model["turnover_cost_bps"]),
        adverse_hourly_funding_bps=float(cost_model["adverse_hourly_funding_bps"]),
    )
    manifest_hash = DatasetManifest.hash_of(manifest)
    market_data_evidence = assessment.to_economic_truth_evidence()
    economic = assess_economic_truth(
        {
            "data": {
                "closed_bar_manifest_hash": manifest_hash,
                "lookahead_audit": {"passed": lookahead_violations == 0, "violations": lookahead_violations},
                "market_data_assessment": market_data_evidence,
            }
        }
    )
    e0 = next(result for result in economic.results if result.gate is TruthGate.E0)
    report = {
        "rows": len(frame),
        "bars": len(gross_returns),
        "first_open_time_ms": times[0],
        "last_open_time_ms": times[-1],
        "manifest_hash": manifest_hash,
        "market_data_status": market_data_evidence["status"],
        "signal_manifest_hash": _canonical_hash(signal_records),
        "lookahead_violations": lookahead_violations,
        "same_bar_executions": same_bar_executions,
        "subthreshold_nonzero_targets": subthreshold_nonzero,
        "entry_threshold": threshold,
        "metrics": _metrics(gross_returns, net_returns, positions),
        "benchmark_metrics": _metrics(benchmark_returns, benchmark_returns, [1.0] * len(benchmark_returns)),
        "e0_status": e0.status.value,
        "e0_reasons": list(e0.reasons),
    }
    return (
        report,
        tuple(gross_returns),
        tuple(net_returns),
        tuple(positions),
        tuple(benchmark_returns),
        tuple(execution_times),
    )


def run_baseline(
    *,
    dataset_root: Path = DATASET,
    cost_policy_path: Path = COST_POLICY,
) -> dict[str, Any]:
    """Run the fixed current Alpha baseline and return a canonical report."""

    dataset_root = dataset_root.resolve()
    cost_policy_path = cost_policy_path.resolve()
    index_path = dataset_root / "dataset-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("status") != "PASS" or tuple(index.get("symbols", ())) != SYMBOLS:
        raise ValueError("DATASET_INDEX_SCOPE_MISMATCH")
    cost_model = _load_cost_policy(cost_policy_path)

    per_symbol: dict[str, Any] = {}
    gross_by_symbol: dict[str, tuple[float, ...]] = {}
    net_by_symbol: dict[str, tuple[float, ...]] = {}
    positions_by_symbol: dict[str, tuple[float, ...]] = {}
    benchmark_by_symbol: dict[str, tuple[float, ...]] = {}
    execution_times: tuple[int, ...] | None = None
    for symbol in SYMBOLS:
        symbol_report, gross, net, positions, benchmark, times = _evaluate_symbol(
            dataset_root=dataset_root,
            symbol=symbol,
            cost_model=cost_model,
        )
        if execution_times is not None and times != execution_times:
            raise ValueError("PORTFOLIO_TIME_ALIGNMENT_MISMATCH")
        execution_times = times
        per_symbol[symbol] = symbol_report
        gross_by_symbol[symbol] = gross
        net_by_symbol[symbol] = net
        positions_by_symbol[symbol] = positions
        benchmark_by_symbol[symbol] = benchmark

    assert execution_times is not None
    portfolio_gross = tuple(
        sum(gross_by_symbol[symbol][index] for symbol in SYMBOLS) / len(SYMBOLS)
        for index in range(len(execution_times))
    )
    portfolio_net = tuple(
        sum(net_by_symbol[symbol][index] for symbol in SYMBOLS) / len(SYMBOLS)
        for index in range(len(execution_times))
    )
    portfolio_gross_exposure = tuple(
        sum(abs(positions_by_symbol[symbol][index]) for symbol in SYMBOLS) / len(SYMBOLS)
        for index in range(len(execution_times))
    )
    portfolio_benchmark = tuple(
        sum(benchmark_by_symbol[symbol][index] for symbol in SYMBOLS) / len(SYMBOLS)
        for index in range(len(execution_times))
    )
    portfolio_turnover = sum(float(per_symbol[symbol]["metrics"]["turnover_units"]) for symbol in SYMBOLS) / len(
        SYMBOLS
    )

    timing_violations = sum(int(per_symbol[symbol]["lookahead_violations"]) for symbol in SYMBOLS)
    same_bar = sum(int(per_symbol[symbol]["same_bar_executions"]) for symbol in SYMBOLS)
    e0_by_symbol = {symbol: str(per_symbol[symbol]["e0_status"]) for symbol in SYMBOLS}
    return {
        "schema_version": "1.0",
        "status": "DIAGNOSTIC_ONLY",
        "decision": "DO_NOT_PROMOTE",
        "dataset": {
            "status": "PASS",
            "path": str(dataset_root.relative_to(REPOSITORY_ROOT)),
            "index_sha256": _sha256(index_path),
            "symbols": list(SYMBOLS),
            "interval": INTERVAL,
            "start_inclusive": index["start_inclusive"],
            "end_exclusive": index["end_exclusive"],
            "rows_per_symbol": index["expected_rows_per_symbol"],
        },
        "alpha": {
            "entrypoint": "apps.alpha_app.OfflineAlphaApp",
            "policy_version": DEFAULT_TREND_ALPHA_POLICY.policy_version,
            "policy_hash": _canonical_hash(asdict(DEFAULT_TREND_ALPHA_POLICY)),
            "parameter_search": False,
            "candidate_selection": False,
            "alpha_source_sha256": _sha256(REPOSITORY_ROOT / "apps/alpha_app/composition.py"),
            "trend_source_sha256": _sha256(REPOSITORY_ROOT / "beidou_strategy/alpha/trend.py"),
        },
        "cost_model": cost_model,
        "timing_audit": {
            "signal_basis": "closed bar t using history through t only",
            "execution_assumption": "next bar open, PnL marked next open-to-close",
            "lookahead_violations": timing_violations,
            "same_bar_executions": same_bar,
            "signal_rows": len(execution_times) * len(SYMBOLS),
        },
        "per_symbol": per_symbol,
        "portfolio": {
            **_metrics(
                portfolio_gross,
                portfolio_net,
                portfolio_gross_exposure,
                turnover_units=portfolio_turnover,
            ),
            "construction": "equal-capital four-sleeve average; max theoretical gross exposure 1.0",
            "benchmark": _metrics(portfolio_benchmark, portfolio_benchmark, [1.0] * len(portfolio_benchmark)),
        },
        "economic_truth": {
            "overall": "NOT_EVALUATED",
            "e0_by_symbol": e0_by_symbol,
            "reasons": [
                "PIT_LINEAGE_MANIFEST_ABSENT",
                "FEATURE_LINEAGE_MANIFEST_ABSENT",
                "OOS_SEAL_ABSENT_AND_PRESEAL_RESULTS_ALREADY_INSPECTED",
                "METRIC_OWNER_POLICY_NOT_PROVIDED",
                "IMPACT_AND_CAPACITY_NOT_EVALUATED",
            ],
        },
        "oos": {
            "status": "INVALIDATED_FOR_PROMOTION",
            "reasons": [
                "PRESEAL_RESULT_INSPECTION",
                "NO_UNSEEN_HOLDOUT_REMAINS_IN_AUTHORIZED_RANGE",
                "ONLY_135_EVALUABLE_BARS_POSTDATE_OFFLINE_ALPHA_APP_COMMIT",
            ],
        },
        "limitations": [
            "The 720-hour range is a single short market episode and cannot establish multi-regime robustness.",
            "The authorized range was inspected before an immutable OOS seal, so all metrics are descriptive only.",
            "The cost policy is explicit but not calibrated to fills; impact and capacity are not evaluated.",
            "Annualized hourly Sharpe is reported for arithmetic reproducibility and is unstable on this short sample.",
            "No Paper, Testnet, Mainnet, account, order, deployment, restart, commit, or push action occurred.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--cost-policy", type=Path, default=COST_POLICY)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_baseline(dataset_root=args.dataset, cost_policy_path=args.cost_policy)
    rendered = json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(f"{args.output.suffix}.tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(args.output)
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
