#!/usr/bin/env python3
"""Independent, local-only recomputation of the frozen Alpha baseline.

This verifier intentionally does not import or read the primary baseline runner,
its tests, or its report.  It reads only the frozen parquet/manifest artifacts and
calls the published ``apps.alpha_app.OfflineAlphaApp`` interface.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.alpha_app import BoundLocalData, OfflineAlphaApp  # noqa: E402
from beidou_shared.contracts.experiment import DatasetRef  # noqa: E402
from beidou_shared.types import InstrumentId, SchemaVersion, VenueId  # noqa: E402

RUN_ID = "2026-08-31-alpha-return-optimization"
DATASET_ID = "alpha-return-2026-08-01_2026-08-31-v1"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
EXPECTED_COLUMNS = ("open_time", "open", "high", "low", "close", "volume", "is_closed")
INTERVAL_MS = 3_600_000
MIN_CLOSE_HISTORY = 51
TRANSACTION_COST_BPS = 6.0
ADVERSE_FUNDING_BPS_PER_HOUR = 0.125
ANNUALIZATION_HOURS = 24 * 365

DEFAULT_DATASET_ROOT = REPO_ROOT / "artifacts" / "datasets" / DATASET_ID
DEFAULT_OUTPUT = REPO_ROOT / "artifacts" / "analysis" / DATASET_ID / "independent-recompute.json"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git_value(*args: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed local git executable and internal constant arguments
        ["/usr/bin/git", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _utc_iso(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric, not bool")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _validate_and_load_symbol(
    dataset_root: Path,
    dataset_index: dict[str, Any],
    symbol: str,
) -> tuple[list[dict[str, object]], dict[str, Any], dict[str, Any]]:
    entry = dataset_index["datasets"][symbol]
    manifest_path = dataset_root / "frozen" / symbol / "1h.manifest.json"
    parquet_path = dataset_root / "frozen" / symbol / "1h.parquet"
    manifest = _read_json(manifest_path)

    manifest_file_sha256 = _sha256_file(manifest_path)
    parquet_file_sha256 = _sha256_file(parquet_path)
    if manifest_file_sha256 != entry["manifest_sha256"]:
        raise ValueError(f"{symbol}: manifest file SHA-256 does not match dataset-index.json")
    if parquet_file_sha256 != entry["parquet_sha256"]:
        raise ValueError(f"{symbol}: parquet file SHA-256 does not match dataset-index.json")

    table = pq.read_table(parquet_path)
    if tuple(table.column_names) != EXPECTED_COLUMNS:
        raise ValueError(f"{symbol}: unexpected parquet columns {table.column_names!r}")
    rows = table.to_pylist()
    expected_rows = int(dataset_index["expected_rows_per_symbol"])
    if len(rows) != expected_rows or len(rows) != int(entry["rows"]) or len(rows) != int(manifest["rows"]):
        raise ValueError(f"{symbol}: row count mismatch")

    expected_open_times = [
        int(dataset_index["datasets"][symbol]["first_open_time"]) + i * INTERVAL_MS
        for i in range(expected_rows)
    ]
    actual_open_times = [int(row["open_time"]) for row in rows]
    if actual_open_times != expected_open_times:
        raise ValueError(f"{symbol}: bars are not a complete ordered 1h grid")
    if actual_open_times[0] != int(manifest["first_open_time"]) or actual_open_times[-1] != int(
        manifest["last_open_time"]
    ):
        raise ValueError(f"{symbol}: manifest time bounds do not match parquet")

    for index, row in enumerate(rows):
        if row["is_closed"] is not True:
            raise ValueError(f"{symbol}[{index}]: unclosed bar")
        open_price = _finite_number(row["open"], f"{symbol}[{index}].open")
        high = _finite_number(row["high"], f"{symbol}[{index}].high")
        low = _finite_number(row["low"], f"{symbol}[{index}].low")
        close = _finite_number(row["close"], f"{symbol}[{index}].close")
        volume = _finite_number(row["volume"], f"{symbol}[{index}].volume")
        if min(open_price, high, low, close) <= 0.0 or volume < 0.0:
            raise ValueError(f"{symbol}[{index}]: non-positive price or negative volume")
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            raise ValueError(f"{symbol}[{index}]: illegal OHLC relationship")

    if manifest["symbol"] != symbol or manifest["interval"] != "1h" or manifest["schema_version"] != "2.0":
        raise ValueError(f"{symbol}: manifest identity/schema mismatch")
    provenance = manifest["provenance"]
    if (
        provenance["venue"] != "BINANCE_USDM"
        or provenance["environment"] != "PUBLIC_READ_ONLY"
        or provenance["intended_use"] != "ECONOMIC_RESEARCH"
        or provenance["source_class"] != "EXCHANGE_PUBLIC_API"
    ):
        raise ValueError(f"{symbol}: manifest provenance is outside the admitted research boundary")
    if manifest["cross_source_validation"]["status"] != "PASS" or entry["assessment"]["status"] != "PASS":
        raise ValueError(f"{symbol}: frozen-data assessment is not PASS")

    evidence = {
        "rows": len(rows),
        "first_open_time": actual_open_times[0],
        "last_open_time": actual_open_times[-1],
        "manifest_schema_version": manifest["schema_version"],
        "manifest_content_sha256": manifest["content_sha256"],
        "manifest_file_sha256": manifest_file_sha256,
        "parquet_file_sha256": parquet_file_sha256,
        "manifest_status": entry["assessment"]["status"],
        "all_bars_closed": True,
        "complete_ordered_hourly_grid": True,
        "schema_columns": list(EXPECTED_COLUMNS),
        "provenance": {
            "venue": provenance["venue"],
            "environment": provenance["environment"],
            "intended_use": provenance["intended_use"],
            "source_class": provenance["source_class"],
            "endpoint": provenance["endpoint"],
            "reconciliation_source_class": manifest["cross_source_validation"]["reference_source_class"],
            "reconciliation_endpoint": manifest["cross_source_validation"]["reference_endpoint"],
        },
    }
    return rows, manifest, evidence


def _compound(returns: list[float]) -> float:
    equity = 1.0
    for value in returns:
        equity *= 1.0 + value
    return equity - 1.0


def _annualized_sharpe(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    stdev = statistics.stdev(returns)
    if stdev == 0.0:
        return None
    return statistics.mean(returns) / stdev * math.sqrt(ANNUALIZATION_HOURS)


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def _series_metrics(returns: list[float]) -> dict[str, float | int | None]:
    return {
        "bars": len(returns),
        "arithmetic_sum": sum(returns),
        "cumulative_compound_return": _compound(returns),
        "mean_hourly_return": statistics.mean(returns),
        "sample_stdev_hourly": statistics.stdev(returns) if len(returns) >= 2 else None,
        "annualized_sharpe_sqrt_8760": _annualized_sharpe(returns),
        "max_drawdown": _max_drawdown(returns),
        "positive_bar_count": sum(value > 0.0 for value in returns),
        "negative_bar_count": sum(value < 0.0 for value in returns),
        "zero_bar_count": sum(value == 0.0 for value in returns),
    }


def _weight_statistics(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "minimum": min(values),
        "maximum": max(values),
        "mean": statistics.mean(values),
        "mean_absolute": statistics.mean(abs(value) for value in values),
        "positive_count": sum(value > 0.0 for value in values),
        "negative_count": sum(value < 0.0 for value in values),
        "zero_count": sum(value == 0.0 for value in values),
    }


def recompute(dataset_root: Path) -> dict[str, Any]:
    dataset_index_path = dataset_root / "dataset-index.json"
    dataset_index = _read_json(dataset_index_path)
    if tuple(dataset_index["symbols"]) != SYMBOLS:
        raise ValueError(f"unexpected symbol order/universe: {dataset_index['symbols']!r}")
    if (
        dataset_index["status"] != "PASS"
        or dataset_index["interval"] != "1h"
        or dataset_index["venue"] != "BINANCE_USDM"
        or int(dataset_index["expected_rows_per_symbol"]) != 720
    ):
        raise ValueError("dataset-index.json is outside the frozen baseline contract")

    rows_by_symbol: dict[str, list[dict[str, object]]] = {}
    manifests: dict[str, dict[str, Any]] = {}
    dataset_evidence: dict[str, dict[str, Any]] = {}
    for symbol in SYMBOLS:
        rows, manifest, evidence = _validate_and_load_symbol(dataset_root, dataset_index, symbol)
        rows_by_symbol[symbol] = rows
        manifests[symbol] = manifest
        dataset_evidence[symbol] = evidence

    reference_times = [int(row["open_time"]) for row in rows_by_symbol[SYMBOLS[0]]]
    if any([int(row["open_time"]) for row in rows_by_symbol[symbol]] != reference_times for symbol in SYMBOLS[1:]):
        raise ValueError("symbol time grids do not align exactly")

    app = OfflineAlphaApp()
    dataset_refs = {
        symbol: DatasetRef(
            dataset_id=f"{DATASET_ID}:{symbol}:1h",
            version=SchemaVersion(str(manifests[symbol]["schema_version"])),
            content_hash=str(manifests[symbol]["content_sha256"]),
        )
        for symbol in SYMBOLS
    }

    previous_weights = [0.0] * len(SYMBOLS)
    gross_returns: list[float] = []
    net_returns: list[float] = []
    benchmark_returns: list[float] = []
    turnover_values: list[float] = []
    transaction_costs: list[float] = []
    funding_costs: list[float] = []
    gross_exposures: list[float] = []
    net_exposures: list[float] = []
    targets_by_symbol: dict[str, list[float]] = {symbol: [] for symbol in SYMBOLS}
    asset_returns_by_symbol: dict[str, list[float]] = {symbol: [] for symbol in SYMBOLS}
    continuous_mark_gross_returns: list[float] = []
    continuous_mark_net_returns: list[float] = []
    continuous_mark_benchmark_returns: list[float] = []
    close_to_next_open_gaps_by_symbol: dict[str, list[float]] = {symbol: [] for symbol in SYMBOLS}
    records: list[dict[str, Any]] = []

    same_bar_execution_count = 0
    lookahead_violation_count = 0
    target_timestamp_mismatch_count = 0
    input_row_count_mismatch_count = 0

    # signal_index is the closed bar t.  execution_index is the next bar t+1.
    for signal_index in range(MIN_CLOSE_HISTORY - 1, len(reference_times) - 1):
        execution_index = signal_index + 1
        signal_open_ms = reference_times[signal_index]
        signal_closed_at_ms = signal_open_ms + INTERVAL_MS
        execution_open_ms = reference_times[execution_index]
        observed_at = datetime.fromtimestamp(signal_closed_at_ms / 1000.0, tz=timezone.utc)

        target_values: list[float] = []
        realized_returns: list[float] = []
        continuous_mark_returns: list[float] = []
        close_to_next_open_gaps: list[float | None] = []
        for symbol in SYMBOLS:
            rows = rows_by_symbol[symbol]
            closes = tuple(float(row["close"]) for row in rows[: signal_index + 1])
            result = app.evaluate(
                BoundLocalData(
                    dataset=dataset_refs[symbol],
                    instrument_id=InstrumentId(symbol),
                    venue_id=VenueId("BINANCE_USDM"),
                    closes=closes,
                    observed_at=observed_at,
                )
            )
            target = float(result.target.target_weight)
            if not math.isfinite(target) or not -1.0 <= target <= 1.0:
                raise ValueError(f"{symbol}[{signal_index}]: target is outside [-1, 1]")
            if result.row_count != signal_index + 1:
                input_row_count_mismatch_count += 1
            if result.target.timestamp.astimezone(timezone.utc) != observed_at:
                target_timestamp_mismatch_count += 1
            target_values.append(target)
            targets_by_symbol[symbol].append(target)

            execution_bar = rows[execution_index]
            realized_return = float(execution_bar["close"]) / float(execution_bar["open"]) - 1.0
            realized_returns.append(realized_return)
            asset_returns_by_symbol[symbol].append(realized_return)
            if execution_index + 1 < len(rows):
                next_open = float(rows[execution_index + 1]["open"])
                execution_close = float(execution_bar["close"])
                gap_return = next_open / execution_close - 1.0
                continuous_return = next_open / float(execution_bar["open"]) - 1.0
                close_to_next_open_gaps_by_symbol[symbol].append(gap_return)
                close_to_next_open_gaps.append(gap_return)
            else:
                # The frozen range has no subsequent open for its final bar, so
                # the sensitivity mark uses the observed final close.
                continuous_return = realized_return
                close_to_next_open_gaps.append(None)
            continuous_mark_returns.append(continuous_return)

        # Each independently generated instrument target receives one quarter of
        # portfolio capital.  This keeps total possible gross exposure <= 1.
        weights = [target / len(SYMBOLS) for target in target_values]
        portfolio_gross = sum(weight * value for weight, value in zip(weights, realized_returns, strict=True))
        benchmark_return = sum(realized_returns) / len(realized_returns)
        # These are risky-asset weights inside four independent capital sleeves,
        # not a fully invested vector whose elements sum to one.  Traded notional
        # is therefore sum(abs(delta weight)); applying another 0.5 would omit
        # the corresponding sleeve cash/margin leg and undercharge by half.
        turnover = sum(abs(weight - previous) for weight, previous in zip(weights, previous_weights, strict=True))
        transaction_cost = turnover * TRANSACTION_COST_BPS / 10_000.0
        gross_exposure = sum(abs(weight) for weight in weights)
        net_exposure = sum(weights)
        funding_cost = gross_exposure * ADVERSE_FUNDING_BPS_PER_HOUR / 10_000.0
        portfolio_net = portfolio_gross - transaction_cost - funding_cost
        continuous_mark_gross = sum(
            weight * value for weight, value in zip(weights, continuous_mark_returns, strict=True)
        )
        continuous_mark_benchmark = sum(continuous_mark_returns) / len(continuous_mark_returns)
        continuous_mark_net = continuous_mark_gross - transaction_cost - funding_cost

        same_bar_execution_count += int(execution_open_ms == signal_open_ms)
        lookahead_violation_count += int(
            signal_closed_at_ms > execution_open_ms
            or signal_open_ms >= execution_open_ms
            or max(reference_times[: signal_index + 1]) >= execution_open_ms
        )

        record = {
            "signal_index": signal_index,
            "signal_bar_open": _utc_iso(signal_open_ms),
            "signal_observed_at_closed_boundary": _utc_iso(signal_closed_at_ms),
            "execution_bar_index": execution_index,
            "execution_bar_open": _utc_iso(execution_open_ms),
            "target_weights": dict(zip(SYMBOLS, target_values, strict=True)),
            "capital_weights": dict(zip(SYMBOLS, weights, strict=True)),
            "next_bar_open_to_close_returns": dict(zip(SYMBOLS, realized_returns, strict=True)),
            "close_to_following_open_gap_returns": dict(zip(SYMBOLS, close_to_next_open_gaps, strict=True)),
            "portfolio_gross_return": portfolio_gross,
            "equal_weight_long_benchmark_return": benchmark_return,
            "turnover": turnover,
            "transaction_cost": transaction_cost,
            "gross_exposure": gross_exposure,
            "net_exposure": net_exposure,
            "adverse_funding_cost": funding_cost,
            "portfolio_net_return": portfolio_net,
        }
        records.append(record)
        gross_returns.append(portfolio_gross)
        net_returns.append(portfolio_net)
        benchmark_returns.append(benchmark_return)
        continuous_mark_gross_returns.append(continuous_mark_gross)
        continuous_mark_net_returns.append(continuous_mark_net)
        continuous_mark_benchmark_returns.append(continuous_mark_benchmark)
        turnover_values.append(turnover)
        transaction_costs.append(transaction_cost)
        funding_costs.append(funding_cost)
        gross_exposures.append(gross_exposure)
        net_exposures.append(net_exposure)
        previous_weights = weights

    if not records:
        raise ValueError("no evaluable signal/return pairs")

    per_symbol_calculation: dict[str, dict[str, Any]] = {}
    for symbol in SYMBOLS:
        positions = targets_by_symbol[symbol]
        asset_returns = asset_returns_by_symbol[symbol]
        symbol_gross = [position * value for position, value in zip(positions, asset_returns, strict=True)]
        symbol_turnover: list[float] = []
        symbol_transaction_costs: list[float] = []
        symbol_funding_costs: list[float] = []
        symbol_net: list[float] = []
        previous = 0.0
        for position, gross_return in zip(positions, symbol_gross, strict=True):
            turnover = abs(position - previous)
            transaction_cost = turnover * TRANSACTION_COST_BPS / 10_000.0
            funding_cost = abs(position) * ADVERSE_FUNDING_BPS_PER_HOUR / 10_000.0
            symbol_turnover.append(turnover)
            symbol_transaction_costs.append(transaction_cost)
            symbol_funding_costs.append(funding_cost)
            symbol_net.append(gross_return - transaction_cost - funding_cost)
            previous = position
        per_symbol_calculation[symbol] = {
            "gross": _series_metrics(symbol_gross),
            "net_after_transaction_and_funding": _series_metrics(symbol_net),
            "benchmark_gross": _series_metrics(asset_returns),
            "turnover_total": sum(symbol_turnover),
            "transaction_cost_arithmetic_sum": sum(symbol_transaction_costs),
            "adverse_funding_cost_arithmetic_sum": sum(symbol_funding_costs),
            "average_absolute_position": statistics.mean(abs(value) for value in positions),
            "target_weight_statistics": _weight_statistics(positions),
        }

    terminal_liquidation_turnover = gross_exposures[-1]
    terminal_liquidation_cost = terminal_liquidation_turnover * TRANSACTION_COST_BPS / 10_000.0
    current_net_compound = _compound(net_returns)
    forced_liquidation_net_compound = (1.0 + current_net_compound) * (1.0 - terminal_liquidation_cost) - 1.0

    audit_sample_indices = sorted({0, len(records) // 2, len(records) - 1})
    calculation = {
        "bar_count": len(records),
        "first_signal_bar_open": records[0]["signal_bar_open"],
        "first_signal_observed_at": records[0]["signal_observed_at_closed_boundary"],
        "first_execution_bar_open": records[0]["execution_bar_open"],
        "last_signal_bar_open": records[-1]["signal_bar_open"],
        "last_signal_observed_at": records[-1]["signal_observed_at_closed_boundary"],
        "last_execution_bar_open": records[-1]["execution_bar_open"],
        "portfolio_gross": _series_metrics(gross_returns),
        "portfolio_net_after_transaction_and_funding": _series_metrics(net_returns),
        "equal_weight_long_benchmark_gross": _series_metrics(benchmark_returns),
        "active_net_minus_benchmark_compound_return": _compound(net_returns) - _compound(benchmark_returns),
        "turnover": {
            "total": sum(turnover_values),
            "mean_per_bar": statistics.mean(turnover_values),
            "maximum_per_bar": max(turnover_values),
            "initial_entry": turnover_values[0],
            "terminal_liquidation_included": False,
        },
        "costs": {
            "transaction_cost_bps_per_unit_turnover": TRANSACTION_COST_BPS,
            "transaction_cost_arithmetic_sum": sum(transaction_costs),
            "adverse_funding_bps_per_hour_per_unit_absolute_exposure": ADVERSE_FUNDING_BPS_PER_HOUR,
            "adverse_funding_cost_arithmetic_sum": sum(funding_costs),
            "combined_cost_arithmetic_sum": sum(transaction_costs) + sum(funding_costs),
            "gross_minus_net_compound_return": _compound(gross_returns) - _compound(net_returns),
        },
        "exposure": {
            "mean_gross": statistics.mean(gross_exposures),
            "maximum_gross": max(gross_exposures),
            "mean_net": statistics.mean(net_exposures),
            "minimum_net": min(net_exposures),
            "maximum_net": max(net_exposures),
        },
        "target_weight_statistics": {
            symbol: _weight_statistics(targets_by_symbol[symbol]) for symbol in SYMBOLS
        },
        "per_symbol": per_symbol_calculation,
        "timing_audit": {
            "signal_uses_closed_bar_t_only": True,
            "execution_uses_next_bar_open": True,
            "realized_return_uses_execution_bar_open_to_close": True,
            "same_bar_execution_count": same_bar_execution_count,
            "lookahead_violation_count": lookahead_violation_count,
            "target_timestamp_mismatch_count": target_timestamp_mismatch_count,
            "input_row_count_mismatch_count": input_row_count_mismatch_count,
            "closed_signal_to_execution_boundary_equal_count": sum(
                record["signal_observed_at_closed_boundary"] == record["execution_bar_open"] for record in records
            ),
        },
        "finite_window_terminal_liquidation": {
            "included_in_primary_result": False,
            "last_gross_exposure": gross_exposures[-1],
            "hypothetical_liquidation_turnover": terminal_liquidation_turnover,
            "hypothetical_liquidation_cost": terminal_liquidation_cost,
            "primary_net_compound_return": current_net_compound,
            "net_compound_return_with_terminal_liquidation": forced_liquidation_net_compound,
            "compound_return_delta": forced_liquidation_net_compound - current_net_compound,
        },
        "close_to_next_open_gap_sensitivity": {
            "primary_mark_excludes_gap": True,
            "strategy_and_benchmark_treatment_is_symmetric": True,
            "gap_pairs_per_symbol": len(close_to_next_open_gaps_by_symbol[SYMBOLS[0]]),
            "final_bar_uses_observed_close_because_following_open_is_outside_frozen_range": True,
            "per_symbol": {
                symbol: {
                    "nonzero_gap_count": sum(value != 0.0 for value in close_to_next_open_gaps_by_symbol[symbol]),
                    "maximum_absolute_gap_bps": max(
                        abs(value) for value in close_to_next_open_gaps_by_symbol[symbol]
                    )
                    * 10_000.0,
                    "arithmetic_gap_sum": sum(close_to_next_open_gaps_by_symbol[symbol]),
                }
                for symbol in SYMBOLS
            },
            "continuous_mark_strategy_gross": _series_metrics(continuous_mark_gross_returns),
            "continuous_mark_strategy_net": _series_metrics(continuous_mark_net_returns),
            "continuous_mark_equal_weight_benchmark": _series_metrics(continuous_mark_benchmark_returns),
            "strategy_gross_compound_delta_vs_primary": _compound(continuous_mark_gross_returns)
            - _compound(gross_returns),
            "strategy_net_compound_delta_vs_primary": _compound(continuous_mark_net_returns) - _compound(net_returns),
            "benchmark_compound_delta_vs_primary": _compound(continuous_mark_benchmark_returns)
            - _compound(benchmark_returns),
        },
        "signal_records_sha256": _canonical_sha256(records),
        "audit_samples": [records[index] for index in audit_sample_indices],
    }

    independent_payload = {
        "dataset": dataset_evidence,
        "methodology": {
            "signal": "OfflineAlphaApp on closes through closed bar t, minimum 51 closes",
            "execution": "target generated at bar t close boundary and executed at bar t+1 open",
            "realized_return": "bar t+1 close / bar t+1 open - 1",
            "portfolio_capital_weight": "OfflineAlphaApp target_weight / 4 for each of four symbols",
            "benchmark": "gross equal-weight long average of the same four t+1 open-to-close returns",
            "turnover": (
                "sum(abs(current risky-asset weight - previous risky-asset weight)); actual traded notional "
                "across four independent capital sleeves"
            ),
            "transaction_cost": "turnover * 6 bps; initial entry included; terminal liquidation excluded",
            "funding": "sum(abs(capital weights)) * 0.125 bps every evaluated hour, always adverse",
            "compounding": "product(1 + hourly return) - 1",
        },
        "calculation": calculation,
    }

    return {
        "schema_version": "1.0",
        "run_id": RUN_ID,
        "dataset_id": DATASET_ID,
        "calculation_status": (
            "PASS"
            if same_bar_execution_count == 0
            and lookahead_violation_count == 0
            and target_timestamp_mismatch_count == 0
            and input_row_count_mismatch_count == 0
            else "FAIL"
        ),
        "status": (
            "PASS_WITH_CONDITIONS"
            if same_bar_execution_count == 0
            and lookahead_violation_count == 0
            and target_timestamp_mismatch_count == 0
            and input_row_count_mismatch_count == 0
            else "FAIL"
        ),
        "decision": "DIAGNOSTIC_ONLY_DO_NOT_PROMOTE",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "verifier": {
            "implementation": "run_independent_frozen_alpha_recompute.py",
            "implementation_sha256": _sha256_file(Path(__file__).resolve()),
            "independence_boundary": (
                "implemented and first executed without reading baseline-report.json, "
                "run_frozen_alpha_baseline.py, or test_frozen_alpha_baseline.py"
            ),
            "input_scope": "local frozen parquet, schema-v2 manifests, dataset-index, OfflineAlphaApp public interface",
            "network_access": "NOT_USED",
            "account_or_order_interfaces": "NOT_USED",
            "testnet_or_mainnet_writes": "NOT_USED",
        },
        "environment": {
            "repo_root": str(REPO_ROOT),
            "branch": _git_value("branch", "--show-current"),
            "head": _git_value("rev-parse", "HEAD"),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "pyarrow": pyarrow.__version__,
            "dataset_index_path": str(dataset_index_path),
            "dataset_index_sha256": _sha256_file(dataset_index_path),
        },
        "limitations": [
            (
                "Both frozen primary and reconciliation evidence are Binance-origin data; this is cross-endpoint, "
                "not cross-venue independence."
            ),
            "Funding is a conservative fixed adverse assumption, not historical symbol-specific funding observations.",
            "The benchmark is a gross reference and is not charged transaction costs or funding.",
            "The one-month frozen interval is not evidence of out-of-sample profitability or production readiness.",
            (
                "This verifier assesses arithmetic and timing only; it does not authorize Paper, Testnet, Mainnet, "
                "deployment, or capital use."
            ),
        ],
        "independent_payload_sha256": _canonical_sha256(independent_payload),
        **independent_payload,
    }


def _comparison(actual: int | float, expected: int | float, *, tolerance: float = 1e-12) -> dict[str, Any]:
    delta = float(actual) - float(expected)
    return {
        "independent": actual,
        "primary_baseline": expected,
        "delta": delta,
        "absolute_tolerance": tolerance,
        "match": abs(delta) <= tolerance,
    }


def _reconcile_with_primary(
    report: dict[str, Any],
    baseline_path: Path,
    *,
    first_report: dict[str, Any] | None,
    first_report_sha256: str | None,
    preserved_first_persisted: dict[str, Any] | None = None,
) -> dict[str, Any]:
    baseline = _read_json(baseline_path)
    calculation = report["calculation"]
    primary_portfolio = baseline["portfolio"]
    primary_timing = baseline["timing_audit"]
    comparisons = {
        "bar_count": _comparison(calculation["bar_count"], primary_portfolio["bars"], tolerance=0.0),
        "portfolio_gross_compound_return": _comparison(
            calculation["portfolio_gross"]["cumulative_compound_return"], primary_portfolio["gross_return"]
        ),
        "portfolio_net_compound_return": _comparison(
            calculation["portfolio_net_after_transaction_and_funding"]["cumulative_compound_return"],
            primary_portfolio["net_return"],
        ),
        "portfolio_net_annualized_hourly_sharpe": _comparison(
            calculation["portfolio_net_after_transaction_and_funding"]["annualized_sharpe_sqrt_8760"],
            primary_portfolio["annualized_hourly_sharpe"],
        ),
        "portfolio_net_max_drawdown": _comparison(
            calculation["portfolio_net_after_transaction_and_funding"]["max_drawdown"],
            primary_portfolio["max_drawdown"],
        ),
        "portfolio_average_absolute_position": _comparison(
            calculation["exposure"]["mean_gross"], primary_portfolio["average_absolute_position"]
        ),
        "portfolio_turnover_units": _comparison(
            calculation["turnover"]["total"], primary_portfolio["turnover_units"]
        ),
        "benchmark_gross_compound_return": _comparison(
            calculation["equal_weight_long_benchmark_gross"]["cumulative_compound_return"],
            primary_portfolio["benchmark"]["gross_return"],
        ),
        "same_bar_execution_count": _comparison(
            calculation["timing_audit"]["same_bar_execution_count"],
            primary_timing["same_bar_executions"],
            tolerance=0.0,
        ),
        "lookahead_violation_count": _comparison(
            calculation["timing_audit"]["lookahead_violation_count"],
            primary_timing["lookahead_violations"],
            tolerance=0.0,
        ),
    }
    per_symbol_comparisons: dict[str, Any] = {}
    for symbol in SYMBOLS:
        independent_symbol = calculation["per_symbol"][symbol]
        primary_symbol = baseline["per_symbol"][symbol]
        primary_metrics = primary_symbol["metrics"]
        per_symbol_comparisons[symbol] = {
            "bar_count": _comparison(
                independent_symbol["net_after_transaction_and_funding"]["bars"],
                primary_metrics["bars"],
                tolerance=0.0,
            ),
            "gross_compound_return": _comparison(
                independent_symbol["gross"]["cumulative_compound_return"], primary_metrics["gross_return"]
            ),
            "net_compound_return": _comparison(
                independent_symbol["net_after_transaction_and_funding"]["cumulative_compound_return"],
                primary_metrics["net_return"],
            ),
            "net_annualized_hourly_sharpe": _comparison(
                independent_symbol["net_after_transaction_and_funding"]["annualized_sharpe_sqrt_8760"],
                primary_metrics["annualized_hourly_sharpe"],
            ),
            "net_max_drawdown": _comparison(
                independent_symbol["net_after_transaction_and_funding"]["max_drawdown"],
                primary_metrics["max_drawdown"],
            ),
            "average_absolute_position": _comparison(
                independent_symbol["average_absolute_position"], primary_metrics["average_absolute_position"]
            ),
            "turnover_units": _comparison(
                independent_symbol["turnover_total"], primary_metrics["turnover_units"]
            ),
        }

    comparison_values = list(comparisons.values()) + [
        value for symbol_values in per_symbol_comparisons.values() for value in symbol_values.values()
    ]
    all_metrics_match = all(bool(value["match"]) for value in comparison_values)

    first_persisted: dict[str, Any] | None = preserved_first_persisted
    if first_persisted is None and first_report is not None and first_report_sha256 is not None:
        first_calculation = first_report["calculation"]
        first_persisted = {
            "output_sha256": first_report_sha256,
            "generated_at": first_report.get("generated_at"),
            "independent_payload_sha256": first_report.get("independent_payload_sha256"),
            "turnover_method": first_report["methodology"]["turnover"],
            "portfolio_gross_compound_return": first_calculation["portfolio_gross"][
                "cumulative_compound_return"
            ],
            "portfolio_net_compound_return": first_calculation["portfolio_net_after_transaction_and_funding"][
                "cumulative_compound_return"
            ],
            "turnover_units": first_calculation["turnover"]["total"],
            "difference_against_primary": {
                "portfolio_gross_compound_return": _comparison(
                    first_calculation["portfolio_gross"]["cumulative_compound_return"],
                    primary_portfolio["gross_return"],
                ),
                "portfolio_net_compound_return": _comparison(
                    first_calculation["portfolio_net_after_transaction_and_funding"]["cumulative_compound_return"],
                    primary_portfolio["net_return"],
                ),
                "turnover_units": _comparison(
                    first_calculation["turnover"]["total"], primary_portfolio["turnover_units"]
                ),
            },
        }

    return {
        "performed_after_first_independent_result_was_persisted": first_persisted is not None,
        "primary_baseline_path": str(baseline_path),
        "primary_baseline_sha256": _sha256_file(baseline_path),
        "primary_status": baseline.get("status"),
        "primary_decision": baseline.get("decision"),
        "primary_economic_truth_overall": baseline.get("economic_truth", {}).get("overall"),
        "primary_oos_status": baseline.get("oos", {}).get("status"),
        "all_recomputed_metrics_match_within_tolerance": all_metrics_match,
        "comparisons": comparisons,
        "per_symbol_comparisons": per_symbol_comparisons,
        "first_persisted_result": first_persisted,
        "differences_and_resolution": [
            {
                "difference": (
                    "The first independent pass matched gross return and benchmark exactly but reported half the "
                    "primary turnover and therefore a higher net return."
                ),
                "cause": (
                    "It applied 0.5 * L1 to risky assets only. Those weights are four independent partially "
                    "invested sleeves, not a fully invested weight vector; omitting each sleeve's cash/margin leg "
                    "halved actual traded notional."
                ),
                "resolution": (
                    "The independent implementation now charges sum(abs(delta portfolio risky weight)), equivalent "
                    "to the average absolute target change across the four sleeves. All reported primary metrics "
                    "then match within 1e-12."
                ),
                "product_or_primary_baseline_modified": False,
            },
            {
                "difference": "No terminal forced-liquidation cost is included in either primary result.",
                "cause": (
                    "The diagnostic marks the finite sample through the final observed close without imposing "
                    "liquidation."
                ),
                "resolution": (
                    "Retained for direct comparability and quantified separately as a sensitivity, not silently added."
                ),
                "product_or_primary_baseline_modified": False,
            },
            {
                "difference": "Hourly close-to-following-open gaps are excluded from the primary marked return.",
                "cause": "Both strategy and benchmark use the identical execution-bar open-to-close return substrate.",
                "resolution": (
                    "Confirmed symmetric treatment and added a continuous open-to-following-open sensitivity, "
                    "using the final observed close where no following open exists."
                ),
                "product_or_primary_baseline_modified": False,
            },
        ],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--baseline-report",
        type=Path,
        help="Optional post-first-run primary report used only for reconciliation, never for the recomputation.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    dataset_root = args.dataset_root.resolve()
    output = args.output.resolve()
    first_report = _read_json(output) if args.baseline_report is not None and output.exists() else None
    first_report_sha256 = _sha256_file(output) if first_report is not None else None
    preserved_first_persisted = (
        first_report.get("reconciliation", {}).get("first_persisted_result")
        if first_report is not None
        else None
    )
    forbidden_outputs = {
        (REPO_ROOT / "artifacts" / "analysis" / DATASET_ID / "baseline-report.json").resolve(),
        (REPO_ROOT / "docs" / "optimization" / "runs" / RUN_ID / "tools" / "run_frozen_alpha_baseline.py").resolve(),
    }
    if output in forbidden_outputs:
        raise ValueError("refusing to overwrite a primary baseline artifact")
    report = recompute(dataset_root)
    if args.baseline_report is not None:
        baseline_path = args.baseline_report.resolve()
        report["reconciliation"] = _reconcile_with_primary(
            report,
            baseline_path,
            first_report=first_report,
            first_report_sha256=first_report_sha256,
            preserved_first_persisted=preserved_first_persisted,
        )
        if not report["reconciliation"]["all_recomputed_metrics_match_within_tolerance"]:
            report["status"] = "FAIL"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(  # noqa: T201 - concise CLI evidence summary
        json.dumps(
            {
                "status": report["status"],
                "output": str(output),
                "output_sha256": _sha256_file(output),
                "independent_payload_sha256": report["independent_payload_sha256"],
                "bar_count": report["calculation"]["bar_count"],
                "portfolio_gross_cumulative": report["calculation"]["portfolio_gross"][
                    "cumulative_compound_return"
                ],
                "portfolio_net_cumulative": report["calculation"]["portfolio_net_after_transaction_and_funding"][
                    "cumulative_compound_return"
                ],
                "benchmark_gross_cumulative": report["calculation"]["equal_weight_long_benchmark_gross"][
                    "cumulative_compound_return"
                ],
                "total_turnover": report["calculation"]["turnover"]["total"],
                "transaction_cost_sum": report["calculation"]["costs"]["transaction_cost_arithmetic_sum"],
                "funding_cost_sum": report["calculation"]["costs"]["adverse_funding_cost_arithmetic_sum"],
                "same_bar_execution_count": report["calculation"]["timing_audit"]["same_bar_execution_count"],
                "lookahead_violation_count": report["calculation"]["timing_audit"]["lookahead_violation_count"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] in {"PASS", "PASS_WITH_CONDITIONS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
