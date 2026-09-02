"""Versioned K-line dataset manifests and economic-research admission.

A content hash proves which bytes were used; it does not prove where those
bytes came from or whether they represent an economic market. This module
therefore binds content identity to endpoint provenance and keeps Demo/Testnet
data explicitly execution-only.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import pandas as pd

DATASET_MANIFEST_SCHEMA_VERSION = "2.0"
CONTENT_HASH_ALGORITHM = "SHA256_OHLCV_FLOATHEX_V1"
ECONOMIC_RESEARCH = "ECONOMIC_RESEARCH"
EXECUTION_ONLY = "EXECUTION_ONLY"
PUBLIC_READ_ONLY = "PUBLIC_READ_ONLY"

_PROVENANCE_FIELDS = frozenset({"venue", "endpoint", "source_class", "retrieved_at", "environment", "intended_use"})
_CROSS_SOURCE_FIELDS = frozenset(
    {
        "status",
        "reference_endpoint",
        "reference_source_class",
        "checked_at",
        "sample_scope",
        "sample_count",
        "max_close_deviation_bps",
        "evidence_sha256",
    }
)
_CONTENT_METADATA_FIELDS = (
    "symbol",
    "interval",
    "rows",
    "first_open_time",
    "last_open_time",
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        *_CONTENT_METADATA_FIELDS,
        "content_hash_algorithm",
        "content_sha256",
        "provenance",
        "cross_source_validation",
    }
)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_OFFICIAL_ARCHIVE_KLINE_PATH = re.compile(
    r"/data/futures/um/(?:daily|monthly)/klines/[A-Z0-9_]+/[1-9][0-9]*[mhdwM]/[^/]+\.zip"
)
_INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
_EXTREME_RETURN_LIMIT = {
    "1m": 0.15,
    "5m": 0.20,
    "15m": 0.25,
    "1h": 0.35,
    "4h": 0.60,
    "1d": 1.50,
}
_RESEARCH_SOURCE_CLASSES = frozenset({"OFFICIAL_PUBLIC_ARCHIVE", "EXCHANGE_PUBLIC_API"})


def _canonical_utc(value: str) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    # Appending an explicit +00:00 makes every successfully parsed value
    # timezone-aware UTC; malformed or offset-bearing inputs fail above.
    return parsed


def _endpoint_classification(endpoint: str) -> tuple[str, str, str]:
    """Return source class, environment, and permitted use for an exact host."""

    try:
        parsed = urlsplit(str(endpoint))
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return ("UNKNOWN", "UNKNOWN", EXECUTION_ONLY)
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return ("UNKNOWN", "UNKNOWN", EXECUTION_ONLY)
    if host == "demo-fapi.binance.com" and parsed.path == "/fapi/v1/klines":
        return ("EXCHANGE_DEMO_API", "DEMO", EXECUTION_ONLY)
    if host == "testnet.binancefuture.com" and parsed.path == "/fapi/v1/klines":
        return ("EXCHANGE_TESTNET_API", "TESTNET", EXECUTION_ONLY)
    if host == "data.binance.vision" and _OFFICIAL_ARCHIVE_KLINE_PATH.fullmatch(parsed.path) is not None:
        return ("OFFICIAL_PUBLIC_ARCHIVE", PUBLIC_READ_ONLY, ECONOMIC_RESEARCH)
    if host == "fapi.binance.com" and parsed.path == "/fapi/v1/klines":
        return ("EXCHANGE_PUBLIC_API", PUBLIC_READ_ONLY, ECONOMIC_RESEARCH)
    return ("UNKNOWN", "UNKNOWN", EXECUTION_ONLY)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"DUPLICATE_JSON_KEY:{key}")
        value[key] = item
    return value


@dataclass(frozen=True, slots=True)
class MarketDataProvenance:
    venue: str
    endpoint: str
    source_class: str
    retrieved_at: str
    environment: str
    intended_use: str

    @classmethod
    def from_endpoint(cls, endpoint: str, *, retrieved_at: str, venue: str = "BINANCE_USDM") -> MarketDataProvenance:
        source_class, environment, intended_use = _endpoint_classification(endpoint)
        return cls(
            venue=venue,
            endpoint=str(endpoint),
            source_class=source_class,
            retrieved_at=retrieved_at,
            environment=environment,
            intended_use=intended_use,
        )

    @classmethod
    def unknown(cls) -> MarketDataProvenance:
        return cls.from_endpoint("", retrieved_at="")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MarketDataProvenance:
        if set(value) != _PROVENANCE_FIELDS:
            raise ValueError("INVALID_PROVENANCE_FIELDS")
        if any(not isinstance(value[field], str) for field in _PROVENANCE_FIELDS):
            raise TypeError("INVALID_PROVENANCE_FIELD_TYPE")
        return cls(**{field: str(value[field]) for field in _PROVENANCE_FIELDS})

    def to_dict(self) -> dict[str, str]:
        return {
            "venue": self.venue,
            "endpoint": self.endpoint,
            "source_class": self.source_class,
            "retrieved_at": self.retrieved_at,
            "environment": self.environment,
            "intended_use": self.intended_use,
        }

    def source_identity(self) -> tuple[str, str, str, str, str]:
        """Identity fields that must remain stable across a resumed backfill."""

        return (self.venue, self.endpoint, self.source_class, self.environment, self.intended_use)


@dataclass(frozen=True, slots=True)
class CrossSourceValidation:
    status: str
    reference_endpoint: str
    reference_source_class: str
    checked_at: str
    sample_scope: str
    sample_count: int
    max_close_deviation_bps: float
    evidence_sha256: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CrossSourceValidation:
        if set(value) != _CROSS_SOURCE_FIELDS:
            raise ValueError("INVALID_CROSS_SOURCE_FIELDS")
        string_fields = _CROSS_SOURCE_FIELDS - {"sample_count", "max_close_deviation_bps"}
        if any(not isinstance(value[field], str) for field in string_fields):
            raise TypeError("INVALID_CROSS_SOURCE_FIELD_TYPE")
        sample_count = value["sample_count"]
        max_deviation = value["max_close_deviation_bps"]
        if isinstance(sample_count, bool) or not isinstance(sample_count, int):
            raise TypeError("INVALID_CROSS_SOURCE_SAMPLE_COUNT")
        if isinstance(max_deviation, bool) or not isinstance(max_deviation, (int, float)):
            raise TypeError("INVALID_CROSS_SOURCE_DEVIATION")
        return cls(
            status=str(value["status"]),
            reference_endpoint=str(value["reference_endpoint"]),
            reference_source_class=str(value["reference_source_class"]),
            checked_at=str(value["checked_at"]),
            sample_scope=str(value["sample_scope"]),
            sample_count=sample_count,
            max_close_deviation_bps=float(max_deviation),
            evidence_sha256=str(value["evidence_sha256"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reference_endpoint": self.reference_endpoint,
            "reference_source_class": self.reference_source_class,
            "checked_at": self.checked_at,
            "sample_scope": self.sample_scope,
            "sample_count": self.sample_count,
            "max_close_deviation_bps": self.max_close_deviation_bps,
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class DatasetResearchAssessment:
    eligible: bool
    reasons: tuple[str, ...]
    manifest_hash: str
    schema_version: str
    source_class: str
    environment: str
    intended_use: str

    def to_economic_truth_evidence(self) -> dict[str, Any]:
        return {
            "status": "PASS" if self.eligible else "FAIL",
            "manifest_hash": self.manifest_hash,
            "schema_version": self.schema_version,
            "source_class": self.source_class,
            "environment": self.environment,
            "intended_use": self.intended_use,
            "reasons": list(self.reasons),
        }


class DatasetManifest:
    @staticmethod
    def _canonical_content_sha256(ordered: pd.DataFrame) -> str:
        """Hash normalized scalar values, independent of parquet/pandas metadata."""

        digest = hashlib.sha256()
        digest.update(b"BEIDOU_OHLCV_FLOATHEX_V1\n")
        columns = ("open_time", "open", "high", "low", "close", "volume", "is_closed")
        for record in ordered.loc[:, columns].itertuples(index=False, name=None):
            encoded = (
                str(int(record[0])),
                float(record[1]).hex(),
                float(record[2]).hex(),
                float(record[3]).hex(),
                float(record[4]).hex(),
                float(record[5]).hex(),
                "1" if bool(record[6]) else "0",
            )
            digest.update("\x1f".join(encoded).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    @staticmethod
    def _content_fields(df: pd.DataFrame, symbol: str, interval: str) -> dict[str, Any]:
        required_columns = {"open_time", "open", "high", "low", "close", "volume", "is_closed"}
        missing = required_columns - set(df.columns)
        if missing:
            raise ValueError(f"MISSING_DATASET_COLUMNS:{','.join(sorted(missing))}")
        if df.empty:
            raise ValueError("EMPTY_DATASET")
        ordered = df.sort_values("open_time").reset_index(drop=True)
        return {
            "symbol": symbol,
            "interval": interval,
            "rows": len(ordered),
            "first_open_time": int(ordered["open_time"].min()),
            "last_open_time": int(ordered["open_time"].max()),
            "content_hash_algorithm": CONTENT_HASH_ALGORITHM,
            "content_sha256": DatasetManifest._canonical_content_sha256(ordered),
        }

    @staticmethod
    def content_verification_reasons(
        df: pd.DataFrame,
        manifest: Mapping[str, Any],
        symbol: str,
        interval: str,
    ) -> tuple[str, ...]:
        """Recompute immutable content identity without judging permitted use."""

        reasons: list[str] = []
        try:
            expected_content = DatasetManifest._content_fields(df, symbol, interval)
        except (TypeError, ValueError, OverflowError):
            return ("DATASET_CONTENT_UNVERIFIABLE",)
        for field in _CONTENT_METADATA_FIELDS:
            if field not in manifest:
                reasons.append(f"MISSING_MANIFEST_FIELD:{field}")
                continue
            if manifest[field] != expected_content[field]:
                reasons.append(f"MANIFEST_METADATA_MISMATCH:{field}")
        algorithm = manifest.get("content_hash_algorithm")
        if algorithm is None:
            reasons.append("MISSING_MANIFEST_FIELD:content_hash_algorithm")
        elif algorithm != CONTENT_HASH_ALGORITHM:
            reasons.append("CONTENT_HASH_ALGORITHM_UNSUPPORTED")
        if "content_sha256" not in manifest:
            reasons.append("MISSING_MANIFEST_FIELD:content_sha256")
        elif algorithm == CONTENT_HASH_ALGORITHM and manifest["content_sha256"] != expected_content["content_sha256"]:
            reasons.append("MANIFEST_CONTENT_MISMATCH")
        return tuple(reasons)

    @staticmethod
    def compute(
        df: pd.DataFrame,
        symbol: str,
        interval: str,
        *,
        provenance: MarketDataProvenance | None = None,
        cross_source_validation: CrossSourceValidation | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": DATASET_MANIFEST_SCHEMA_VERSION,
            **DatasetManifest._content_fields(df, symbol, interval),
            "provenance": (provenance or MarketDataProvenance.unknown()).to_dict(),
            "cross_source_validation": (
                cross_source_validation.to_dict() if cross_source_validation is not None else None
            ),
        }

    @staticmethod
    def hash_of(manifest: Mapping[str, Any]) -> str:
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def write(path: Path, manifest: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    @staticmethod
    def read(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
        except (OSError, UnicodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _assess_cross_source(
        value: Any,
        provenance: MarketDataProvenance,
        row_count: int,
        reasons: list[str],
    ) -> bool:
        if not isinstance(value, Mapping):
            reasons.append("CROSS_SOURCE_VALIDATION_MISSING")
            return False
        try:
            check = CrossSourceValidation.from_dict(value)
        except (TypeError, ValueError):
            reasons.append("CROSS_SOURCE_VALIDATION_INVALID")
            return False

        if check.status != "PASS":
            reasons.append("CROSS_SOURCE_STATUS_NOT_PASS")
        reference_class, reference_environment, reference_use = _endpoint_classification(check.reference_endpoint)
        if (
            check.reference_source_class != reference_class
            or reference_class not in _RESEARCH_SOURCE_CLASSES
            or reference_environment != PUBLIC_READ_ONLY
            or reference_use != ECONOMIC_RESEARCH
        ):
            reasons.append("CROSS_SOURCE_REFERENCE_NOT_TRUSTED")
        if check.reference_endpoint == provenance.endpoint or reference_class == provenance.source_class:
            reasons.append("CROSS_SOURCE_REFERENCE_NOT_DISTINCT")
        checked_at = _canonical_utc(check.checked_at)
        retrieved_at = _canonical_utc(provenance.retrieved_at)
        if checked_at is None or (retrieved_at is not None and checked_at < retrieved_at):
            reasons.append("CROSS_SOURCE_CHECK_TIME_INVALID")
        elif checked_at > datetime.now(timezone.utc):
            reasons.append("CROSS_SOURCE_CHECK_TIME_IN_FUTURE")
        if check.sample_scope != "EXTREMA_AND_RANDOM":
            reasons.append("CROSS_SOURCE_SAMPLE_SCOPE_INSUFFICIENT")
        if check.sample_count < 10:
            reasons.append("CROSS_SOURCE_SAMPLE_COUNT_INSUFFICIENT")
        if check.sample_count > row_count:
            reasons.append("CROSS_SOURCE_SAMPLE_COUNT_EXCEEDS_ROWS")
        if (
            not math.isfinite(check.max_close_deviation_bps)
            or check.max_close_deviation_bps < 0
            or check.max_close_deviation_bps > 5.0
        ):
            reasons.append("CROSS_SOURCE_DEVIATION_EXCEEDED")
        if _HEX64.fullmatch(check.evidence_sha256) is None:
            reasons.append("CROSS_SOURCE_EVIDENCE_DIGEST_INVALID")
        return not any(reason.startswith("CROSS_SOURCE_") for reason in reasons)

    @staticmethod
    def _assess_frame_quality(df: pd.DataFrame, interval: str, extrema_covered: bool, reasons: list[str]) -> None:
        required_columns = ("open_time", "open", "high", "low", "close", "volume", "is_closed")
        if any(column not in df.columns for column in required_columns):
            reasons.append("DATASET_SCHEMA_INVALID")
            return
        if df.empty:
            reasons.append("DATASET_EMPTY")
            return

        open_times = pd.to_numeric(df["open_time"], errors="coerce")
        timestamps_valid = all(math.isfinite(float(value)) and float(value).is_integer() for value in open_times)
        if not timestamps_valid:
            reasons.append("BAR_TIMESTAMP_INVALID")
        if open_times.isna().any() or open_times.duplicated().any() or not open_times.is_monotonic_increasing:
            reasons.append("BAR_TIME_ORDER_INVALID")
        interval_ms = _INTERVAL_MS.get(interval)
        if interval_ms is None:
            reasons.append("INTERVAL_UNSUPPORTED")
        elif len(open_times) > 1 and not open_times.diff().iloc[1:].eq(interval_ms).all():
            reasons.append("BAR_INTERVAL_DISCONTINUITY")
        if not df["is_closed"].map(lambda value: value is True).all():
            reasons.append("UNCLOSED_BARS")

        numeric = df[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
        finite = numeric.notna().all().all() and all(
            math.isfinite(float(value)) for row in numeric.itertuples(index=False, name=None) for value in row
        )
        if not finite:
            reasons.append("NON_FINITE_OHLCV")
            return
        if (
            (numeric[["open", "high", "low", "close"]] <= 0).any().any()
            or (numeric["volume"] < 0).any()
            or (numeric["high"] < numeric[["open", "close", "low"]].max(axis=1)).any()
            or (numeric["low"] > numeric[["open", "close", "high"]].min(axis=1)).any()
        ):
            reasons.append("OHLCV_INVARIANT_VIOLATION")

        returns = numeric["close"].pct_change(fill_method=None)
        limit = _EXTREME_RETURN_LIMIT.get(interval, 0.35)
        extreme = returns.abs().gt(limit)
        opposite = returns.mul(returns.shift(1)).lt(0) & returns.abs().ge(0.20) & returns.shift(1).abs().ge(0.20)
        if extreme.any() and not extrema_covered:
            reasons.append("EXTREME_RETURN_UNRECONCILED")
        if opposite.any() and not extrema_covered:
            reasons.append("OPPOSITE_REVERSAL_UNRECONCILED")

    @staticmethod
    def assess_economic_research(
        df: pd.DataFrame,
        manifest: Mapping[str, Any],
        symbol: str,
        interval: str,
    ) -> DatasetResearchAssessment:
        reasons: list[str] = []
        unknown_fields = set(manifest) - _MANIFEST_FIELDS
        if unknown_fields:
            reasons.append(f"UNKNOWN_MANIFEST_FIELDS:{','.join(sorted(unknown_fields))}")
        schema_version = str(manifest.get("schema_version", ""))
        if schema_version != DATASET_MANIFEST_SCHEMA_VERSION:
            reasons.append("UNKNOWN_MANIFEST_SCHEMA")
        reasons.extend(DatasetManifest.content_verification_reasons(df, manifest, symbol, interval))

        provenance_value = manifest.get("provenance")
        provenance = MarketDataProvenance.unknown()
        if not isinstance(provenance_value, Mapping):
            reasons.append("MISSING_PROVENANCE")
        else:
            try:
                provenance = MarketDataProvenance.from_dict(provenance_value)
            except (TypeError, ValueError):
                reasons.append("INVALID_PROVENANCE")
            else:
                expected_class, expected_environment, expected_use = _endpoint_classification(provenance.endpoint)
                if (
                    provenance.source_class != expected_class
                    or provenance.environment != expected_environment
                    or provenance.intended_use != expected_use
                ):
                    reasons.append("PROVENANCE_CLASSIFICATION_MISMATCH")
                if provenance.venue != "BINANCE_USDM":
                    reasons.append("VENUE_NOT_SUPPORTED")
                if provenance.source_class not in _RESEARCH_SOURCE_CLASSES:
                    reasons.append(f"SOURCE_NOT_RESEARCH_ELIGIBLE:{provenance.source_class}")
                if provenance.environment != PUBLIC_READ_ONLY:
                    reasons.append(f"ENVIRONMENT_NOT_PUBLIC_READ_ONLY:{provenance.environment}")
                if provenance.intended_use != ECONOMIC_RESEARCH:
                    reasons.append(f"INTENDED_USE_NOT_ECONOMIC_RESEARCH:{provenance.intended_use}")
                retrieved_at = _canonical_utc(provenance.retrieved_at)
                if retrieved_at is None:
                    reasons.append("RETRIEVED_AT_INVALID")
                else:
                    if retrieved_at > datetime.now(timezone.utc):
                        reasons.append("RETRIEVED_AT_IN_FUTURE")
                    interval_ms = _INTERVAL_MS.get(interval, 0)
                    last_open_time = manifest.get("last_open_time")
                    data_end = None
                    if isinstance(last_open_time, int) and not isinstance(last_open_time, bool):
                        try:
                            data_end = datetime.fromtimestamp(
                                (last_open_time + interval_ms) / 1000,
                                tz=timezone.utc,
                            )
                        except (OSError, OverflowError, ValueError):
                            reasons.append("DATA_END_TIME_INVALID")
                    if data_end is not None and retrieved_at < data_end:
                        reasons.append("RETRIEVED_AT_BEFORE_DATA_END")

        extrema_covered = DatasetManifest._assess_cross_source(
            manifest.get("cross_source_validation"), provenance, len(df), reasons
        )
        DatasetManifest._assess_frame_quality(df, interval, extrema_covered, reasons)

        deduplicated_reasons = tuple(dict.fromkeys(reasons))
        try:
            manifest_hash = DatasetManifest.hash_of(manifest)
        except (TypeError, ValueError):
            manifest_hash = ""
            deduplicated_reasons = (*deduplicated_reasons, "MANIFEST_HASH_UNVERIFIABLE")
        return DatasetResearchAssessment(
            eligible=not deduplicated_reasons,
            reasons=deduplicated_reasons,
            manifest_hash=manifest_hash,
            schema_version=schema_version,
            source_class=provenance.source_class,
            environment=provenance.environment,
            intended_use=provenance.intended_use,
        )


__all__ = [
    "CONTENT_HASH_ALGORITHM",
    "DATASET_MANIFEST_SCHEMA_VERSION",
    "ECONOMIC_RESEARCH",
    "EXECUTION_ONLY",
    "PUBLIC_READ_ONLY",
    "CrossSourceValidation",
    "DatasetManifest",
    "DatasetResearchAssessment",
    "MarketDataProvenance",
]
