"""Build record-level PIT lineage for the frozen August diagnostic baseline.

This tool reconstructs the already-shipped fixed policy only.  It does not
generate, rank, or select Alpha candidates.  Record timestamps prove causal
feature/label ordering, while the report explicitly preserves the limitation
that the exact source bytes were retrieved after the historical decisions.
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
from typing import Mapping, Sequence

import pandas as pd

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from apps.alpha_app import BoundLocalData, OfflineAlphaApp  # noqa: E402
from beidou_research.data.dataset_manifest import DatasetManifest  # noqa: E402
from beidou_research.data.pit_lineage import (  # noqa: E402
    REQUIRED_LINEAGE_ROLES,
    build_lineage_artifact,
    build_pit_manifest,
    inspect_lineage,
)
from beidou_research.experiments.contracts import canonical_json  # noqa: E402
from beidou_shared.contracts.experiment import DatasetRef  # noqa: E402
from beidou_shared.types import InstrumentId, SchemaVersion, VenueId  # noqa: E402
from beidou_strategy.alpha.trend import DEFAULT_TREND_ALPHA_POLICY  # noqa: E402

DATASET_ROOT = REPOSITORY_ROOT / "artifacts/datasets/alpha-return-2026-08-01_2026-08-31-v1"
OUTPUT_ROOT = (
    REPOSITORY_ROOT
    / "artifacts/analysis/alpha-return-2026-08-01_2026-08-31-v1/governance/pit-lineage"
)
COST_REPORT = (
    REPOSITORY_ROOT
    / "artifacts/analysis/alpha-return-2026-08-01_2026-08-31-v1/governance/cost-capacity/calibration-report.json"
)
FACTOR_POLICY = REPOSITORY_ROOT / "config/factor_mining_policy.yaml"
METRIC_POLICY = Path(
    "/Users/maguannan/beidou-authorization/BD-AF-P3-T07/metric-owner-policy.json"
)
BASELINE_REPORT = (
    REPOSITORY_ROOT
    / "artifacts/analysis/alpha-return-2026-08-01_2026-08-31-v1/baseline-report.json"
)
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
INTERVAL = "1h"
HOUR_MS = 3_600_000
WARMUP_BARS = max(DEFAULT_TREND_ALPHA_POLICY.horizons) + 1
LINEAGE_ID = "aro-august-2026-fixed-policy-pit-v1"
SOURCE_VINTAGE_STATUS = "NOT_VERIFIABLE_RETROSPECTIVE_RECONSTRUCTION"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("UTC_TIMESTAMP_REQUIRED")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("UTC_TIMESTAMP_REQUIRED")
    return parsed


def _time_from_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: object) -> str:
    return _sha256_bytes(canonical_json(value).encode("utf-8"))


def _domain_digest(domain: str, value: object) -> str:
    return _sha256_bytes(domain.encode("ascii") + b"\x00" + canonical_json(value).encode("utf-8"))


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _write_once(path: Path, value: object) -> None:
    payload = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise RuntimeError(f"IMMUTABLE_PIT_ARTIFACT_MISMATCH:{path}")
        return
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()


def _source_file(path: Path) -> dict[str, str]:
    resolved = path.resolve()
    try:
        rendered = resolved.relative_to(REPOSITORY_ROOT).as_posix()
        scope = "REPOSITORY"
    except ValueError:
        rendered = str(resolved)
        scope = "EXTERNAL_AUTHORIZATION"
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return {"path": rendered, "path_scope": scope, "sha256": _sha256_file(resolved)}


def _resolve_source_file(value: Mapping[str, object]) -> Path:
    path = Path(str(value.get("path", "")))
    scope = value.get("path_scope")
    if scope == "REPOSITORY" and not path.is_absolute():
        return (REPOSITORY_ROOT / path).resolve()
    if scope == "EXTERNAL_AUTHORIZATION" and path.is_absolute():
        return path.resolve()
    raise ValueError("SOURCE_FILE_SCOPE_INVALID")


def _bar_payload(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "open_time_ms": int(row["open_time"]),
        "open_float_hex": float(row["open"]).hex(),
        "high_float_hex": float(row["high"]).hex(),
        "low_float_hex": float(row["low"]).hex(),
        "close_float_hex": float(row["close"]).hex(),
        "volume_float_hex": float(row["volume"]).hex(),
        "is_closed": bool(row["is_closed"]),
    }


def _volatility(closes: Sequence[float]) -> float:
    returns = [float(closes[index]) / float(closes[index - 1]) - 1.0 for index in range(1, len(closes))]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    return max(math.sqrt(variance), 1e-12)


def _legacy_feature_payload(closes: Sequence[float], *, source_hash: str) -> dict[str, object]:
    returns_by_horizon = {
        horizon: float(closes[-1]) / float(closes[-1 - horizon]) - 1.0
        for horizon in DEFAULT_TREND_ALPHA_POLICY.horizons
    }
    features: dict[str, object] = {
        "benchmark_returns": returns_by_horizon,
        "trend_slopes": {
            horizon: value / horizon for horizon, value in returns_by_horizon.items()
        },
        "realized_volatility": _volatility(closes),
        "data_quality": "PASS",
        "liquidity_score": 1.0,
        "expected_fee_bps": 1.0,
        "expected_slippage_bps": 1.0,
        "expected_funding_bps": 0.0,
        "source_hash": source_hash,
    }
    raw = json.dumps(
        features,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )
    features["feature_hash"] = _sha256_bytes(raw.encode("utf-8"))
    return features


def _load_frame_and_manifest(
    dataset_root: Path, symbol: str
) -> tuple[pd.DataFrame, Mapping[str, object], DatasetRef]:
    frame = (
        pd.read_parquet(dataset_root / "frozen" / symbol / f"{INTERVAL}.parquet")
        .sort_values("open_time")
        .reset_index(drop=True)
    )
    manifest = DatasetManifest.read(dataset_root / "frozen" / symbol / f"{INTERVAL}.manifest.json")
    if manifest is None:
        raise ValueError(f"MANIFEST_UNREADABLE:{symbol}")
    assessment = DatasetManifest.assess_economic_research(frame, manifest, symbol, INTERVAL)
    if not assessment.eligible:
        raise ValueError(f"MARKET_DATA_NOT_ELIGIBLE:{symbol}:{','.join(assessment.reasons)}")
    if len(frame) != 720 or not frame["is_closed"].map(bool).all():
        raise ValueError(f"DATASET_SCOPE_INVALID:{symbol}")
    times = [int(value) for value in frame["open_time"].tolist()]
    if any(right - left != HOUR_MS for left, right in pairwise(times)):
        raise ValueError(f"BAR_CADENCE_INVALID:{symbol}")
    reference = DatasetRef(
        dataset_id=f"alpha-return-2026-08:{symbol}:{INTERVAL}",
        version=SchemaVersion("2.0"),
        content_hash=str(manifest["content_sha256"]),
    )
    return frame, manifest, reference


def _pairwise_records(
    *, dataset_root: Path, policy_digest: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    feature_records: list[dict[str, object]] = []
    label_records: list[dict[str, object]] = []
    app = OfflineAlphaApp()
    for symbol in sorted(SYMBOLS):
        frame, manifest, dataset_ref = _load_frame_and_manifest(dataset_root, symbol)
        rows = frame.to_dict(orient="records")
        closes = tuple(float(value) for value in frame["close"].tolist())
        history_digest = _domain_digest(
            "beidou.pit.history-seed.v1",
            {
                "lineage_id": LINEAGE_ID,
                "symbol": symbol,
                "interval": INTERVAL,
                "dataset_content_sha256": manifest["content_sha256"],
            },
        )
        for source_index, row in enumerate(rows[:-1]):
            history_digest = _domain_digest(
                "beidou.pit.history-step.v1",
                {"previous_history_digest": history_digest, "bar": _bar_payload(row)},
            )
            if source_index < WARMUP_BARS - 1:
                continue
            execution_index = source_index + 1
            source_open_ms = int(row["open_time"])
            decision_ms = source_open_ms + HOUR_MS
            execution_open_ms = int(rows[execution_index]["open_time"])
            if decision_ms != execution_open_ms:
                raise ValueError(f"DECISION_EXECUTION_TIME_MISMATCH:{symbol}")
            decision_time = _time_from_ms(decision_ms)
            label_available = _time_from_ms(execution_open_ms + HOUR_MS)
            observed_at = datetime.fromtimestamp(decision_ms / 1000.0, tz=timezone.utc)
            close_history = closes[: source_index + 1]
            result = app.evaluate(
                BoundLocalData(
                    dataset=dataset_ref,
                    instrument_id=InstrumentId(symbol),
                    venue_id=VenueId("BINANCE_USDM"),
                    closes=close_history,
                    observed_at=observed_at,
                )
            )
            features = _legacy_feature_payload(
                close_history, source_hash=str(manifest["content_sha256"])
            )
            feature_digest = _domain_digest(
                "beidou.pit.feature-record.v1",
                {
                    "symbol": symbol,
                    "decision_time": decision_time,
                    "history_digest": history_digest,
                    "policy_digest": policy_digest,
                    "features": features,
                    "target": {
                        "strategy_id": str(result.target.strategy_id),
                        "instrument_id": str(result.target.instrument_id),
                        "venue_id": str(result.target.venue_id),
                        "target_weight_float_hex": float(result.target.target_weight).hex(),
                        "forecast_hash": result.target.forecast_hash,
                        "model_version": str(result.target.model_version),
                        "timestamp": decision_time,
                    },
                },
            )
            label_bar = _bar_payload(rows[execution_index])
            label_digest = _domain_digest(
                "beidou.pit.label-record.v1",
                {
                    "symbol": symbol,
                    "decision_time": decision_time,
                    "label_end": label_available,
                    "bar": label_bar,
                    "open_to_close_return_float_hex": (
                        float(rows[execution_index]["close"])
                        / float(rows[execution_index]["open"])
                        - 1.0
                    ).hex(),
                },
            )
            record_id = _domain_digest(
                "beidou.pit.signal-record-id.v1",
                {
                    "lineage_id": LINEAGE_ID,
                    "symbol": symbol,
                    "decision_time": decision_time,
                },
            )
            feature_records.append(
                {
                    "record_id": record_id,
                    "symbol": symbol,
                    "decision_time": decision_time,
                    "available_as_of": decision_time,
                    "history_digest": history_digest,
                    "feature_digest": feature_digest,
                    "policy_digest": policy_digest,
                }
            )
            label_records.append(
                {
                    "record_id": record_id,
                    "symbol": symbol,
                    "decision_time": decision_time,
                    "available_as_of": label_available,
                    "label_end": label_available,
                    "label_digest": label_digest,
                }
            )
    return feature_records, label_records


def _pairwise_file(kind: str, records: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "kind": kind,
        "lineage_id": LINEAGE_ID,
        "records": records,
        "records_digest": _digest(records),
    }


def _dataset_source_files(index: Mapping[str, object], dataset_root: Path) -> list[dict[str, str]]:
    files = [_source_file(dataset_root / "dataset-index.json")]
    datasets = index.get("datasets")
    if not isinstance(datasets, Mapping):
        raise ValueError("DATASET_INDEX_DATASETS_INVALID")
    for symbol in sorted(SYMBOLS):
        record = datasets.get(symbol)
        if not isinstance(record, Mapping):
            raise ValueError(f"DATASET_INDEX_SYMBOL_INVALID:{symbol}")
        for field in ("manifest_path", "parquet_path", "reconciliation_path"):
            files.append(_source_file(REPOSITORY_ROOT / str(record[field])))
    return files


def _generic_role_payloads(
    *,
    index: Mapping[str, object],
    dataset_root: Path,
    cost_report_path: Path,
    policy_digest: str,
    as_of: str,
) -> dict[str, dict[str, object]]:
    dataset_sources = _dataset_source_files(index, dataset_root)
    cost_report = json.loads(cost_report_path.read_text(encoding="utf-8"))
    baseline = json.loads(BASELINE_REPORT.read_text(encoding="utf-8"))
    return {
        "dataset": {
            "schema_version": "1.0",
            "kind": "FROZEN_DATASET_LINEAGE",
            "dataset_id": "alpha-return-2026-08-01_2026-08-31-v1",
            "venue": "BINANCE_USDM",
            "symbols": list(SYMBOLS),
            "interval": INTERVAL,
            "start_inclusive": index["start_inclusive"],
            "end_exclusive": index["end_exclusive"],
            "index_status": index["status"],
            "source_files": dataset_sources,
        },
        "universe_membership": {
            "schema_version": "1.0",
            "kind": "FIXED_UNIVERSE_MEMBERSHIP",
            "venue": "BINANCE_USDM",
            "symbols": sorted(SYMBOLS),
            "effective_start": index["start_inclusive"],
            "effective_end_exclusive": index["end_exclusive"],
            "selection": "USER_AUTHORIZED_FIXED_SCOPE_BEFORE_ACQUISITION",
            "survivorship_scope": "ONLY_THE_FOUR_EXPLICITLY_AUTHORIZED_SYMBOLS",
            "source_files": [_source_file(dataset_root / "dataset-index.json")],
        },
        "corporate_actions": {
            "schema_version": "1.0",
            "kind": "INSTRUMENT_ACTION_SCOPE",
            "instrument_class": "BINANCE_USDM_PERPETUAL",
            "equity_corporate_actions": "NOT_APPLICABLE",
            "contract_or_symbol_migrations_observed": [],
            "limitation": "BROADER_SYMBOL_LIFECYCLE_NOT_EVALUATED",
        },
        "revisions": {
            "schema_version": "1.0",
            "kind": "RETROSPECTIVE_REVISION_AND_CODE_BINDING",
            "source_vintage_at_decision": SOURCE_VINTAGE_STATUS,
            "archive_api_raw_field_mismatches": 0,
            "source_files": [
                *_dataset_source_files(index, dataset_root),
                _source_file(REPOSITORY_ROOT / "apps/alpha_app/composition.py"),
                _source_file(REPOSITORY_ROOT / "beidou_strategy/alpha/trend.py"),
            ],
            "limitation": (
                "The exact bytes were acquired after the decision timestamps; archive/API agreement "
                "does not prove an immutable source snapshot existed at every historical decision."
            ),
        },
        "calendar": {
            "schema_version": "1.0",
            "kind": "CONTINUOUS_MARKET_CALENDAR",
            "timezone": "UTC",
            "schedule": "24X7",
            "interval_seconds": 3600,
            "expected_rows_per_symbol": 720,
            "closures_or_exclusions": [],
        },
        "timestamps": {
            "schema_version": "1.0",
            "kind": "SIGNAL_LABEL_TIMESTAMP_SEMANTICS",
            "feature_rule": "closed_bar_t_available_at_t_plus_1h",
            "decision_rule": "decision_at_next_bar_open",
            "label_rule": "next_bar_open_to_close_available_at_bar_end",
            "feature_available_after_decision_allowed": False,
            "label_available_at_or_before_decision_allowed": False,
        },
        "timezone": {
            "schema_version": "1.0",
            "kind": "TIMEZONE_CONTRACT",
            "event_timezone": "UTC",
            "display_timezone": "UTC",
            "daylight_saving_adjustment": "NOT_APPLICABLE",
        },
        "costs": {
            "schema_version": "1.0",
            "kind": "COST_IMPACT_CAPACITY_LINEAGE",
            "cost_report_status": cost_report["status"],
            "promotion_use": cost_report["promotion_use"],
            "fee_classification": cost_report["evidence_classification"]["fees"],
            "fills_and_latency": cost_report["evidence_classification"]["fills_and_latency"],
            "strategy_capacity": cost_report["evidence_classification"]["strategy_capacity"],
            "baseline_cost_policy_digest": baseline["cost_model"]["policy_sha256"],
            "feature_policy_digest": policy_digest,
            "source_files": [
                _source_file(cost_report_path),
                _source_file(FACTOR_POLICY),
                _source_file(METRIC_POLICY),
            ],
            "available_as_of": as_of,
        },
    }


def create(
    *,
    dataset_root: Path = DATASET_ROOT,
    output: Path = OUTPUT_ROOT,
    cost_report_path: Path = COST_REPORT,
) -> dict[str, object]:
    dataset_root = dataset_root.resolve()
    output = output.resolve()
    cost_report_path = cost_report_path.resolve()
    manifest_path = output / "pit-lineage-manifest.json"
    if manifest_path.exists():
        existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        as_of = str(existing_manifest.get("as_of", ""))
        _parse_utc(as_of)
    else:
        as_of = _utc_now()

    index = json.loads((dataset_root / "dataset-index.json").read_text(encoding="utf-8"))
    if (
        index.get("status") != "PASS"
        or tuple(index.get("symbols", ())) != SYMBOLS
        or index.get("interval") != INTERVAL
        or index.get("expected_rows_per_symbol") != 720
    ):
        raise ValueError("DATASET_INDEX_SCOPE_INVALID")
    policy_digest = _digest(asdict(DEFAULT_TREND_ALPHA_POLICY))
    feature_records, label_records = _pairwise_records(
        dataset_root=dataset_root, policy_digest=policy_digest
    )
    if len(feature_records) != 2_676 or len(label_records) != 2_676:
        raise ValueError("PAIRWISE_RECORD_COUNT_INVALID")

    roles_dir = output / "roles"
    feature_path = roles_dir / "features.json"
    label_path = roles_dir / "labels.json"
    _write_once(feature_path, _pairwise_file("BEIDOU_PAIRWISE_FEATURE_LINEAGE", feature_records))
    _write_once(label_path, _pairwise_file("BEIDOU_PAIRWISE_LABEL_LINEAGE", label_records))
    generic = _generic_role_payloads(
        index=index,
        dataset_root=dataset_root,
        cost_report_path=cost_report_path,
        policy_digest=policy_digest,
        as_of=as_of,
    )
    for role, payload in generic.items():
        _write_once(roles_dir / f"{role}.json", payload)

    first_decision = str(feature_records[0]["decision_time"])
    last_decision = max(str(record["decision_time"]) for record in feature_records)
    last_label = max(str(record["label_end"]) for record in label_records)
    dataset_start = str(index["start_inclusive"])
    dataset_end = str(index["end_exclusive"])
    cost_completed = str(
        json.loads(cost_report_path.read_text(encoding="utf-8"))["completed_at"]
    )
    if _parse_utc(cost_completed) > _parse_utc(as_of):
        raise ValueError("COST_REPORT_AFTER_LINEAGE_AS_OF")
    role_windows = {
        "features": (first_decision, last_decision),
        "labels": (first_decision, last_label),
        "costs": (dataset_start, cost_completed),
    }
    source_ids = {
        "dataset": "binance-usdm-august-2026-frozen-v1",
        "universe_membership": "BINANCE_USDM|BNBUSDT,BTCUSDT,ETHUSDT,SOLUSDT",
        "corporate_actions": "binance-usdm-perpetual-action-scope-august-2026-v1",
        "revisions": "binance-usdm-august-2026-revision-audit-v1",
        "calendar": "binance-usdm-24x7-calendar-august-2026-v1",
        "timestamps": "aro-fixed-policy-timestamp-semantics-v1",
        "timezone": "utc-timezone-contract-v1",
        "features": "offline-alpha-v1-august-2026-features-v1",
        "labels": "next-bar-open-close-august-2026-labels-v1",
        "costs": "aro-august-2026-cost-capacity-v1",
    }
    artifacts = {}
    for role in sorted(REQUIRED_LINEAGE_ROLES):
        start, end = role_windows.get(role, (dataset_start, dataset_end))
        role_path = roles_dir / f"{role}.json"
        artifacts[role] = build_lineage_artifact(
            role=role,
            path=role_path,
            root=output,
            source_id=source_ids[role],
            revision_id=f"sha256-{_sha256_file(role_path)}",
            available_as_of=as_of,
            event_time_start=start,
            event_time_end=end,
            timezone="UTC",
            point_in_time=True,
        )
    manifest = build_pit_manifest(as_of=as_of, artifacts=artifacts, root=output)
    _write_once(manifest_path, manifest.as_dict())
    inspection = inspect_lineage(
        manifest.as_dict(), root=output, expected_manifest_digest=manifest.digest
    )
    if inspection.status != "VERIFIABLE":
        raise RuntimeError(f"PIT_LINEAGE_NOT_VERIFIABLE:{','.join(inspection.reasons)}")

    report: dict[str, object] = {
        "schema_version": "1.0",
        "report_id": "ARO-07-AUGUST-PIT-FEATURE-LINEAGE",
        "status": "PASS_WITH_CONDITIONS",
        "contract_verification": inspection.status,
        "coverage_percent": inspection.coverage_percent,
        "covered_roles": list(inspection.covered_roles),
        "manifest_digest": manifest.digest,
        "manifest_sha256": _sha256_file(manifest_path),
        "feature_records": len(feature_records),
        "label_records": len(label_records),
        "feature_available_after_decision": 0,
        "label_available_at_or_before_decision": 0,
        "source_vintage_at_decision": SOURCE_VINTAGE_STATUS,
        "candidate_search": False,
        "candidate_selection": False,
        "promotion_use": "NOT_VERIFIABLE",
        "created_at": as_of,
        "limitations": [
            "Record-level feature and label timestamps are causal and content-bound.",
            "The exact market-data bytes were retrieved after the historical decisions, so this is "
            "a retrospective reconstruction rather than proof of a contemporaneously frozen source vintage.",
            "The universe is the four-symbol user-authorized scope; broader survivorship and symbol lifecycle "
            "were not evaluated.",
            "No candidate search, selection, promotion, account/order access, or trading action occurred.",
        ],
        "artifact_hashes": {
            f"roles/{role}.json": _sha256_file(roles_dir / f"{role}.json")
            for role in sorted(REQUIRED_LINEAGE_ROLES)
        },
    }
    _write_once(output / "lineage-report.json", report)
    return report


def verify(*, output: Path = OUTPUT_ROOT) -> dict[str, object]:
    output = output.resolve()
    manifest_path = output / "pit-lineage-manifest.json"
    report_path = output / "lineage-report.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    inspection = inspect_lineage(
        manifest,
        root=output,
        expected_manifest_digest=str(report.get("manifest_digest", "")),
    )
    reasons = list(inspection.reasons)
    if inspection.status != "VERIFIABLE" or inspection.coverage_percent != 100:
        reasons.append("LINEAGE_CONTRACT_NOT_VERIFIABLE")
    if report.get("status") != "PASS_WITH_CONDITIONS":
        reasons.append("REPORT_STATUS_INVALID")
    if report.get("candidate_search") is not False or report.get("promotion_use") != "NOT_VERIFIABLE":
        reasons.append("AUTHORITY_BOUNDARY_INVALID")
    if report.get("source_vintage_at_decision") != SOURCE_VINTAGE_STATUS:
        reasons.append("SOURCE_VINTAGE_LIMIT_MISSING")
    for name, expected in report.get("artifact_hashes", {}).items():
        path = output / name
        if not path.is_file() or _sha256_file(path) != expected:
            reasons.append(f"ARTIFACT_HASH_MISMATCH:{name}")
    for role in ("dataset", "universe_membership", "revisions", "costs"):
        payload = json.loads((output / "roles" / f"{role}.json").read_text(encoding="utf-8"))
        for source in payload.get("source_files", []):
            try:
                path = _resolve_source_file(source)
            except ValueError:
                reasons.append(f"SOURCE_FILE_SCOPE_INVALID:{role}")
                continue
            if not path.is_file() or _sha256_file(path) != source.get("sha256"):
                reasons.append(f"SOURCE_FILE_HASH_MISMATCH:{role}:{path.name}")
    features = json.loads((output / "roles/features.json").read_text(encoding="utf-8"))
    labels = json.loads((output / "roles/labels.json").read_text(encoding="utf-8"))
    if len(features.get("records", [])) != 2_676 or len(labels.get("records", [])) != 2_676:
        reasons.append("PAIRWISE_RECORD_COUNT_INVALID")
    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "schema_version": "1.0",
        "status": "PASS_WITH_CONDITIONS" if not unique_reasons else "FAIL",
        "reasons": unique_reasons,
        "contract_verification": inspection.status,
        "coverage_percent": inspection.coverage_percent,
        "manifest_digest": manifest.get("manifest_digest"),
        "manifest_sha256": _sha256_file(manifest_path),
        "source_vintage_at_decision": SOURCE_VINTAGE_STATUS,
        "verified_at": _utc_now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    create_parser = subcommands.add_parser("create")
    create_parser.add_argument("--dataset", type=Path, default=DATASET_ROOT)
    create_parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    create_parser.add_argument("--cost-report", type=Path, default=COST_REPORT)
    verify_parser = subcommands.add_parser("verify")
    verify_parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    args = parser.parse_args()
    if args.command == "create":
        result = create(
            dataset_root=args.dataset,
            output=args.output,
            cost_report_path=args.cost_report,
        )
    else:
        result = verify(output=args.output)
    print(json.dumps(result, sort_keys=True))  # noqa: T201 - CLI result contract
    return 0 if result["status"] in {"PASS", "PASS_WITH_CONDITIONS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
