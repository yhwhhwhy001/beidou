"""Shared deterministic helpers for Alpha V3 forecast producers.

The helpers in this module are deliberately stdlib-only.  They normalize
point-in-time inputs and cost evidence without making research dependencies a
runtime requirement.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast


def finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def clip(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def resolve_timestamp(context: Mapping[str, Any], features: Mapping[str, Any]) -> datetime:
    parsed = parse_timestamp(context.get("timestamp", features.get("timestamp")))
    return parsed if parsed is not None else datetime(1970, 1, 1, tzinfo=UTC)


def stable_hash(value: object) -> str:
    def safe(item: object) -> object:
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, Mapping):
            return {str(key): safe(item[key]) for key in sorted(item, key=lambda key: str(key))}
        if isinstance(item, (list, tuple)):
            return [safe(element) for element in item]
        if isinstance(item, set | frozenset):
            return [safe(element) for element in sorted(item, key=str)]
        if isinstance(item, float) and not math.isfinite(item):
            return f"NON_FINITE:{item!r}"
        return item

    payload = json.dumps(safe(value), sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def context_features(context: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = context.get("features", context)
    return raw if isinstance(raw, Mapping) else {}


def resolve_costs(
    context: Mapping[str, Any], features: Mapping[str, Any]
) -> tuple[float | None, float | None, float | None, str]:
    raw = context.get("costs", features.get("costs"))
    if not isinstance(raw, Mapping):
        raw = context
    fee = finite(raw.get("expected_fee_bps"))
    slippage = finite(raw.get("expected_slippage_bps"))
    funding = finite(raw.get("expected_funding_bps"))
    source_hash = str(raw.get("source_hash", "")).strip()
    if fee is None or slippage is None or funding is None or fee < 0 or slippage < 0 or not source_hash:
        return None, None, None, ""
    return fee, slippage, funding, source_hash


def horizon_values(features: Mapping[str, Any], names: tuple[str, ...]) -> tuple[dict[int, float], bool]:
    for name in names:
        raw = features.get(name)
        if raw is None:
            continue
        if not isinstance(raw, Mapping) or not raw:
            return {}, True
        values: dict[int, float] = {}
        for horizon, value in raw.items():
            try:
                parsed_horizon = int(horizon)
            except (TypeError, ValueError, OverflowError):
                return {}, True
            parsed_value = finite(value)
            if parsed_horizon <= 0 or parsed_value is None:
                return {}, True
            values[parsed_horizon] = parsed_value
        return values, False
    return {}, False


def quality_factor(features: Mapping[str, Any]) -> float:
    quality = str(features.get("data_quality", features.get("quality", ""))).upper()
    return {"PASS": 1.0, "GOOD": 1.0, "RELIABLE": 1.0, "CONDITIONAL": 0.7, "DEGRADED": 0.4}.get(quality, 0.0)  # nosec B105 - quality score labels


def point_in_time_universe(context: Mapping[str, Any], features: Mapping[str, Any]) -> bool:
    decision_time = resolve_timestamp(context, features)
    snapshot = parse_timestamp(features.get("universe_snapshot_timestamp", features.get("universe_as_of")))
    source_hash = str(features.get("universe_source_hash", "")).strip()
    return bool(source_hash and snapshot is not None and snapshot <= decision_time)


def feature_payload(features: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: features[key] for key in sorted(allowed & set(features))}


def side_for_score(score: float, threshold: float) -> str:
    if score >= threshold:
        return "BUY"
    if score <= -threshold:
        return "SELL"
    return "NO_ACTION"
