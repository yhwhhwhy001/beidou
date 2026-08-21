"""Additive PnL attribution contracts for Alpha V3.

The record deliberately keeps an unexplained residual.  Missing evidence is
represented as ``None``/``NOT_VERIFIABLE`` rather than silently converted to a
zero contribution.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from beidou_shared.types import CorrelationId, SchemaVersion

from .engine import EvidenceTier, ReportReference

_CONTRIBUTION_FIELDS = (
    "alpha_selection_contribution",
    "timing_contribution",
    "no_action_drag",
    "veto_drag",
    "degrade_drag",
    "cash_drag",
    "gross_constraint_drag",
    "beta_constraint_drag",
    "turnover_drag",
    "liquidity_drag",
    "fee_drag",
    "slippage_drag",
    "funding_drag",
    "protection_contribution",
)

_EXPOSURE_FIELDS = (
    "gross_exposure",
    "net_exposure",
    "portfolio_beta",
    "target_beta",
    "target_gross",
    "target_net",
    "target_volatility",
)


def _finite_or_none(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    """Stable lineage from a V2 decision probe to a future PnL record."""

    decision_id: str
    timestamp: datetime
    market_data_hash: str
    state_hash: str
    proposal_hash: str
    context_hash: str = ""
    benchmark_snapshot_hash: str | None = None
    alpha_forecast_hash: str | None = None
    ensemble_forecast_hash: str | None = None
    exposure_target_hash: str | None = None
    portfolio_target_hash: str | None = None
    schema_version: str | None = None
    model_version: str | None = None
    policy_version: str | None = None
    attribution_record_id: str | None = None
    correlation_id: CorrelationId | None = None
    status: EvidenceTier = EvidenceTier.NOT_VERIFIABLE

    @classmethod
    def from_probe(cls, probe: dict[str, Any]) -> DecisionTrace:
        """Create a trace from read-only probe evidence without inventing PnL."""
        features = probe.get("features", {})
        state = probe.get("state", {})
        canonical = json.dumps(
            {
                "symbol": str(probe.get("symbol", "")),
                "features": features,
                "state": state,
                "graph_hash": str(probe.get("graph_hash", "")),
                "proposal_hash": str(probe.get("proposal_hash", "")),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        decision_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        timestamp = probe.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        if not isinstance(timestamp, datetime):
            timestamp = datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        explicit_state_hash = ""
        if isinstance(state, dict):
            explicit_state_hash = _optional_string(state.get("state_hash")) or ""
        explicit_market_data_hash = _optional_string(probe.get("market_data_hash"))
        return cls(
            decision_id=decision_id,
            timestamp=timestamp,
            market_data_hash=explicit_market_data_hash
            or hashlib.sha256(
                json.dumps(features, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
            ).hexdigest(),
            state_hash=explicit_state_hash
            or hashlib.sha256(
                json.dumps(state, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
            ).hexdigest(),
            proposal_hash=str(probe.get("proposal_hash", "")),
            context_hash=str(probe.get("context_hash", "")),
            benchmark_snapshot_hash=_optional_string(probe.get("benchmark_snapshot_hash")),
            alpha_forecast_hash=_optional_string(probe.get("alpha_forecast_hash")),
            ensemble_forecast_hash=_optional_string(probe.get("ensemble_forecast_hash")),
            exposure_target_hash=_optional_string(probe.get("exposure_target_hash")),
            portfolio_target_hash=_optional_string(probe.get("portfolio_target_hash")),
            schema_version=_optional_string(probe.get("schema_version")),
            model_version=_optional_string(probe.get("model_version")),
            policy_version=_optional_string(probe.get("policy_version")),
            correlation_id=probe.get("correlation_id"),
        )

    def link_attribution(self, record: AttributionRecord) -> DecisionTrace:
        """Link an independently finalized attribution record to this trace."""
        return replace(
            self,
            attribution_record_id=record.decision_id,
            correlation_id=record.correlation_id or self.correlation_id,
            status=record.evidence_tier,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "timestamp": self.timestamp.isoformat(),
            "market_data_hash": self.market_data_hash,
            "state_hash": self.state_hash,
            "proposal_hash": self.proposal_hash,
            "context_hash": self.context_hash,
            "benchmark_snapshot_hash": self.benchmark_snapshot_hash,
            "alpha_forecast_hash": self.alpha_forecast_hash,
            "ensemble_forecast_hash": self.ensemble_forecast_hash,
            "exposure_target_hash": self.exposure_target_hash,
            "portfolio_target_hash": self.portfolio_target_hash,
            "schema_version": self.schema_version,
            "model_version": self.model_version,
            "policy_version": self.policy_version,
            "attribution_record_id": self.attribution_record_id,
            "correlation_id": str(self.correlation_id) if self.correlation_id else None,
            "status": self.status.value,
        }


def _optional_string(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


@dataclass(frozen=True, slots=True)
class AttributionRecord:
    """One decision-cycle attribution snapshot.

    ``realized_beta_contribution`` is preferred for the balance equation; the
    expected value is retained so forecast-vs-realized drift remains visible.
    """

    decision_id: str
    benchmark_id: str = ""
    benchmark_return: float | None = None
    portfolio_return: float | None = None
    expected_beta_contribution: float | None = None
    realized_beta_contribution: float | None = None
    gross_exposure: float | None = None
    net_exposure: float | None = None
    portfolio_beta: float | None = None
    target_beta: float | None = None
    target_gross: float | None = None
    target_net: float | None = None
    target_volatility: float | None = None
    alpha_contributions: tuple[tuple[str, float], ...] = ()
    alpha_selection_contribution: float | None = None
    timing_contribution: float | None = None
    no_action_drag: float | None = None
    veto_drag: float | None = None
    degrade_drag: float | None = None
    cash_drag: float | None = None
    gross_constraint_drag: float | None = None
    beta_constraint_drag: float | None = None
    turnover_drag: float | None = None
    liquidity_drag: float | None = None
    fee_drag: float | None = None
    slippage_drag: float | None = None
    funding_drag: float | None = None
    protection_contribution: float | None = None
    unexplained_residual: float | None = None
    benchmark_snapshot_hash: str = ""
    alpha_forecast_hash: str = ""
    ensemble_forecast_hash: str = ""
    exposure_target_hash: str = ""
    portfolio_target_hash: str = ""
    source_hash: str = ""
    policy_version: str = ""
    schema_version: SchemaVersion = field(default_factory=lambda: SchemaVersion("3.0.0"))
    correlation_id: CorrelationId | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    evidence_tier: EvidenceTier = EvidenceTier.NOT_VERIFIABLE
    evidence_references: tuple[ReportReference, ...] = ()

    def __post_init__(self) -> None:
        if not self.decision_id.strip():
            raise ValueError("decision_id must not be empty")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        numeric_fields = (
            "benchmark_return",
            "portfolio_return",
            "expected_beta_contribution",
            "realized_beta_contribution",
            *_EXPOSURE_FIELDS,
            *_CONTRIBUTION_FIELDS,
            "unexplained_residual",
        )
        for name in numeric_fields:
            object.__setattr__(self, name, _finite_or_none(getattr(self, name), name))
        if self.gross_exposure is not None and self.gross_exposure < 0:
            raise ValueError("gross_exposure must not be negative")
        if self.target_gross is not None and self.target_gross < 0:
            raise ValueError("target_gross must not be negative")
        if self.target_volatility is not None and self.target_volatility < 0:
            raise ValueError("target_volatility must not be negative")
        object.__setattr__(self, "evidence_references", tuple(self.evidence_references))
        alpha_values = (
            self.alpha_contributions.items() if isinstance(self.alpha_contributions, dict) else self.alpha_contributions
        )
        normalized_alpha = tuple(sorted((str(alpha_id), float(value)) for alpha_id, value in alpha_values))
        if any(not alpha_id.strip() or not math.isfinite(value) for alpha_id, value in normalized_alpha):
            raise ValueError("alpha_contributions must contain finite named values")
        object.__setattr__(self, "alpha_contributions", normalized_alpha)

    @property
    def is_complete(self) -> bool:
        beta_present = self.realized_beta_contribution is not None or self.expected_beta_contribution is not None
        core_present = self.benchmark_return is not None and self.portfolio_return is not None
        exposures_present = all(getattr(self, name) is not None for name in _EXPOSURE_FIELDS)
        contributions_present = all(getattr(self, name) is not None for name in _CONTRIBUTION_FIELDS)
        lineage_present = bool(self.source_hash and self.policy_version and self.correlation_id)
        return beta_present and core_present and exposures_present and contributions_present and lineage_present

    @property
    def is_full_attribution_complete(self) -> bool:
        """A5 completeness additionally requires per-alpha and hash lineage."""
        return (
            self.is_complete
            and bool(self.alpha_contributions)
            and all(
                bool(value.strip())
                for value in (
                    self.benchmark_snapshot_hash,
                    self.alpha_forecast_hash,
                    self.ensemble_forecast_hash,
                    self.exposure_target_hash,
                    self.portfolio_target_hash,
                )
            )
        )

    def _known_contributions(self) -> list[float]:
        beta = self.realized_beta_contribution
        if beta is None:
            beta = self.expected_beta_contribution
        values = [beta] if beta is not None else []
        values.extend(value for name in _CONTRIBUTION_FIELDS if (value := getattr(self, name)) is not None)
        return values

    def calculate_residual(self) -> float | None:
        if self.portfolio_return is None:
            return None
        contributions = self._known_contributions()
        if not contributions:
            return None
        return self.portfolio_return - sum(contributions)

    def finalize(self, *, residual_tolerance: float, require_full: bool = False) -> AttributionRecord:
        """Compute the residual and assign evidence tier using explicit policy."""
        tolerance = float(residual_tolerance)
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("residual_tolerance must be finite and non-negative")
        residual = self.calculate_residual()
        complete = self.is_full_attribution_complete if require_full else self.is_complete
        if residual is None:
            tier = EvidenceTier.NOT_VERIFIABLE
        elif complete and abs(residual) <= tolerance:
            tier = EvidenceTier.VERIFIED
        elif complete:
            tier = EvidenceTier.NOT_VERIFIABLE
        else:
            tier = EvidenceTier.NOT_VERIFIABLE
        return replace(self, unexplained_residual=residual, evidence_tier=tier)

    def field_values(self) -> dict[str, Any]:
        """Return stable report keys without serializing evidence as a fake zero."""
        names = (
            "decision_id",
            "benchmark_id",
            "benchmark_return",
            "portfolio_return",
            "expected_beta_contribution",
            "realized_beta_contribution",
            *_EXPOSURE_FIELDS,
            "alpha_contributions",
            *_CONTRIBUTION_FIELDS,
            "unexplained_residual",
            "benchmark_snapshot_hash",
            "alpha_forecast_hash",
            "ensemble_forecast_hash",
            "exposure_target_hash",
            "portfolio_target_hash",
            "source_hash",
            "policy_version",
            "schema_version",
            "correlation_id",
            "timestamp",
        )
        return {name: getattr(self, name) for name in names}
