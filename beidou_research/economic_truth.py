"""E0–E6 Economic Truth gates (V4.0 验收矩阵 §C).

These gates evaluate *economic* claims about factors/strategies.  They are
deliberately independent from Testnet execution readiness: a Testnet order
proves the execution chain, not profitability (PKG-09-M03).

Every gate is fail-closed: evidence that is missing or malformed yields
``NOT_EVALUATED``, never ``PASS``, and no synthetic promotion is allowed
(INV-09).  Later gates require every earlier gate to PASS — a factor cannot
claim after-cost positivity (E4) while its OOS robustness (E3) is unproven.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, cast

_HEX64 = re.compile(r"[0-9a-f]{64}")
_ECONOMIC_MARKET_DATA_SOURCES = frozenset({"OFFICIAL_PUBLIC_ARCHIVE", "EXCHANGE_PUBLIC_API"})


class TruthGate(str, Enum):
    """E0–E6 economic truth gates."""

    E0 = "DATA_TRUSTED"
    E1 = "FACTOR_SANITY"
    E2 = "RESEARCH_SIGNIFICANT"
    E3 = "OOS_ROBUST"
    E4 = "AFTER_COST_POSITIVE"
    E5 = "PAPER_CONFIRMED"
    E6 = "PORTFOLIO_INCREMENTAL"


GATE_ORDER: tuple[TruthGate, ...] = (
    TruthGate.E0,
    TruthGate.E1,
    TruthGate.E2,
    TruthGate.E3,
    TruthGate.E4,
    TruthGate.E5,
    TruthGate.E6,
)


class GateStatus(str, Enum):
    PASS = "PASS"  # noqa: S105  # nosec B105 - gate status value, not a credential
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


@dataclass(frozen=True, slots=True)
class GateResult:
    gate: TruthGate
    status: GateStatus
    reasons: tuple[str, ...] = ()
    evidence_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate.value,
            "status": self.status.value,
            "reasons": list(self.reasons),
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True, slots=True)
class EconomicTruthAssessment:
    """Aggregated E0–E6 assessment with prerequisite enforcement."""

    results: tuple[GateResult, ...]

    @property
    def overall(self) -> GateStatus:
        if not self.results:
            return GateStatus.NOT_EVALUATED
        if any(result.status is GateStatus.FAIL for result in self.results):
            return GateStatus.FAIL
        if self.results[-1].gate is not TruthGate.E6 or self.results[-1].status is not GateStatus.PASS:
            return GateStatus.NOT_EVALUATED
        return GateStatus.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall.value,
            "gates": [result.to_dict() for result in self.results],
        }


def _gate_evidence_hash(evidence: Any) -> str:
    try:
        payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    except (TypeError, ValueError):
        payload = repr(evidence)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_fields(section: Mapping[str, Any] | None, fields: tuple[str, ...]) -> list[str]:
    if not isinstance(section, Mapping):
        return list(fields)
    return [field for field in fields if field not in section]


def _bool_flag(section: Mapping[str, Any], name: str, *, default: bool | None = None) -> bool | None:
    value = section.get(name, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    return None  # malformed evidence is never trusted


def _finite_number(section: Mapping[str, Any], name: str) -> float | None:
    value = section.get(name)
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _evaluate_e0(evidence: Mapping[str, Any] | None) -> GateResult:
    section = evidence.get("data") if isinstance(evidence, Mapping) else None
    required = (
        "pit_manifest_hash",
        "closed_bar_manifest_hash",
        "lineage_hash",
        "lookahead_audit",
        "market_data_assessment",
    )
    missing = _require_fields(section, required)
    if missing:
        return GateResult(
            TruthGate.E0,
            GateStatus.NOT_EVALUATED,
            tuple(f"MISSING_EVIDENCE:{field}" for field in missing),
            _gate_evidence_hash(section),
        )
    section = cast(Mapping[str, Any], section)  # missing-evidence branch returned above
    market_data = section["market_data_assessment"]
    if not isinstance(market_data, Mapping):
        return GateResult(
            TruthGate.E0,
            GateStatus.FAIL,
            ("MARKET_DATA_ASSESSMENT_MALFORMED",),
            _gate_evidence_hash(section),
        )
    market_data_reasons = market_data.get("reasons")
    market_data_trusted = (
        market_data.get("status") == "PASS"
        and _HEX64.fullmatch(str(market_data.get("manifest_hash", ""))) is not None
        and market_data.get("schema_version") == "2.0"
        and market_data.get("source_class") in _ECONOMIC_MARKET_DATA_SOURCES
        and market_data.get("environment") == "PUBLIC_READ_ONLY"
        and market_data.get("intended_use") == "ECONOMIC_RESEARCH"
        and isinstance(market_data_reasons, (list, tuple))
        and len(market_data_reasons) == 0
    )
    if not market_data_trusted:
        return GateResult(TruthGate.E0, GateStatus.FAIL, ("MARKET_DATA_NOT_TRUSTED",), _gate_evidence_hash(section))
    audit = section["lookahead_audit"]
    if not isinstance(audit, Mapping):
        return GateResult(TruthGate.E0, GateStatus.FAIL, ("MALFORMED_LOOKAHEAD_AUDIT",), _gate_evidence_hash(section))
    violations = audit.get("violations", 1)
    try:
        violation_count = int(violations)
    except (TypeError, ValueError):
        violation_count = 1
    if violation_count > 0 or audit.get("passed") is not True:
        return GateResult(
            TruthGate.E0,
            GateStatus.FAIL,
            (f"LOOKAHEAD_VIOLATIONS:{violation_count}",),
            _gate_evidence_hash(section),
        )
    return GateResult(TruthGate.E0, GateStatus.PASS, (), _gate_evidence_hash(section))


def _evaluate_e1(evidence: Mapping[str, Any] | None) -> GateResult:
    section = evidence.get("factor_sanity") if isinstance(evidence, Mapping) else None
    required = ("non_constant", "finite", "direction_plausible", "missing_handling", "sensitivity_passed")
    missing = _require_fields(section, required)
    if missing:
        return GateResult(
            TruthGate.E1,
            GateStatus.NOT_EVALUATED,
            tuple(f"MISSING_EVIDENCE:{field}" for field in missing),
            _gate_evidence_hash(section),
        )
    section = cast(Mapping[str, Any], section)  # missing-evidence branch returned above
    failures: list[str] = []
    for flag in ("non_constant", "finite", "direction_plausible", "sensitivity_passed"):
        if _bool_flag(section, flag) is not True:
            failures.append(f"FLAG_FALSE:{flag}")
    if not isinstance(section["missing_handling"], str) or not section["missing_handling"].strip():
        failures.append("MISSING_HANDLING_UNDECLARED")
    if failures:
        return GateResult(TruthGate.E1, GateStatus.FAIL, tuple(failures), _gate_evidence_hash(section))
    return GateResult(TruthGate.E1, GateStatus.PASS, (), _gate_evidence_hash(section))


def _evaluate_e2(evidence: Mapping[str, Any] | None) -> GateResult:
    section = evidence.get("significance") if isinstance(evidence, Mapping) else None
    required = ("multiple_testing_corrected", "adjusted_p_value", "alpha", "p_hacking_controls")
    missing = _require_fields(section, required)
    if missing:
        return GateResult(
            TruthGate.E2,
            GateStatus.NOT_EVALUATED,
            tuple(f"MISSING_EVIDENCE:{field}" for field in missing),
            _gate_evidence_hash(section),
        )
    section = cast(Mapping[str, Any], section)  # missing-evidence branch returned above
    failures: list[str] = []
    if _bool_flag(section, "multiple_testing_corrected") is not True:
        failures.append("MULTIPLE_TESTING_NOT_CORRECTED")
    controls = section["p_hacking_controls"]
    if not isinstance(controls, (list, tuple)) or not controls:
        failures.append("NO_P_HACKING_CONTROLS")
    adjusted_p = _finite_number(section, "adjusted_p_value")
    alpha = _finite_number(section, "alpha")
    if adjusted_p is None or alpha is None or alpha <= 0:
        failures.append("INVALID_P_VALUE_OR_ALPHA")
    elif adjusted_p > alpha:
        failures.append(f"NOT_SIGNIFICANT:p={adjusted_p} alpha={alpha}")
    if failures:
        return GateResult(TruthGate.E2, GateStatus.FAIL, tuple(failures), _gate_evidence_hash(section))
    return GateResult(TruthGate.E2, GateStatus.PASS, (), _gate_evidence_hash(section))


def _evaluate_e3(evidence: Mapping[str, Any] | None) -> GateResult:
    section = evidence.get("oos") if isinstance(evidence, Mapping) else None
    required = ("purged_wfo", "cpcv", "embargo", "regime_perturbation", "parameter_perturbation")
    missing = _require_fields(section, required)
    if missing:
        return GateResult(
            TruthGate.E3,
            GateStatus.NOT_EVALUATED,
            tuple(f"MISSING_EVIDENCE:{field}" for field in missing),
            _gate_evidence_hash(section),
        )
    section = cast(Mapping[str, Any], section)  # missing-evidence branch returned above
    failures = [f"FLAG_FALSE:{flag}" for flag in required if _bool_flag(section, flag) is not True]
    if failures:
        return GateResult(TruthGate.E3, GateStatus.FAIL, tuple(failures), _gate_evidence_hash(section))
    return GateResult(TruthGate.E3, GateStatus.PASS, (), _gate_evidence_hash(section))


def _evaluate_e4(evidence: Mapping[str, Any] | None) -> GateResult:
    section = evidence.get("after_cost") if isinstance(evidence, Mapping) else None
    required = (
        "costs_subtracted",
        "expected_return_after_cost",
        "fee_bps",
        "funding_bps",
        "spread_bps",
        "slippage_bps",
    )
    missing = _require_fields(section, required)
    if missing:
        return GateResult(
            TruthGate.E4,
            GateStatus.NOT_EVALUATED,
            tuple(f"MISSING_EVIDENCE:{field}" for field in missing),
            _gate_evidence_hash(section),
        )
    section = cast(Mapping[str, Any], section)  # missing-evidence branch returned above
    failures: list[str] = []
    if _bool_flag(section, "costs_subtracted") is not True:
        failures.append("COSTS_NOT_SUBTRACTED")
    expectancy = _finite_number(section, "expected_return_after_cost")
    if expectancy is None:
        failures.append("INVALID_EXPECTED_RETURN")
    elif expectancy <= 0:
        failures.append(f"NOT_POSITIVE:expectancy={expectancy}")
    for cost_field in ("fee_bps", "funding_bps", "spread_bps", "slippage_bps"):
        if _finite_number(section, cost_field) is None:
            failures.append(f"INVALID_COST:{cost_field}")
    if failures:
        return GateResult(TruthGate.E4, GateStatus.FAIL, tuple(failures), _gate_evidence_hash(section))
    return GateResult(TruthGate.E4, GateStatus.PASS, (), _gate_evidence_hash(section))


def _evaluate_e5(evidence: Mapping[str, Any] | None) -> GateResult:
    section = evidence.get("paper") if isinstance(evidence, Mapping) else None
    required = ("behavior_match_ratio", "synthetic_promotion")
    missing = _require_fields(section, required)
    if missing:
        return GateResult(
            TruthGate.E5,
            GateStatus.NOT_EVALUATED,
            tuple(f"MISSING_EVIDENCE:{field}" for field in missing),
            _gate_evidence_hash(section),
        )
    section = cast(Mapping[str, Any], section)  # missing-evidence branch returned above
    failures: list[str] = []
    if _bool_flag(section, "synthetic_promotion") is True:
        failures.append("SYNTHETIC_PROMOTION")
    ratio = _finite_number(section, "behavior_match_ratio")
    if ratio is None or not 0.0 <= ratio <= 1.0:
        failures.append("INVALID_BEHAVIOR_MATCH_RATIO")
    elif ratio < 0.9:
        failures.append(f"BEHAVIOR_MISMATCH:ratio={ratio}")
    if failures:
        return GateResult(TruthGate.E5, GateStatus.FAIL, tuple(failures), _gate_evidence_hash(section))
    return GateResult(TruthGate.E5, GateStatus.PASS, (), _gate_evidence_hash(section))


def _evaluate_e6(evidence: Mapping[str, Any] | None) -> GateResult:
    section = evidence.get("portfolio") if isinstance(evidence, Mapping) else None
    required = ("incremental_risk_adjusted_return",)
    missing = _require_fields(section, required)
    if missing:
        return GateResult(
            TruthGate.E6,
            GateStatus.NOT_EVALUATED,
            tuple(f"MISSING_EVIDENCE:{field}" for field in missing),
            _gate_evidence_hash(section),
        )
    section = cast(Mapping[str, Any], section)  # missing-evidence branch returned above
    incremental = _finite_number(section, "incremental_risk_adjusted_return")
    if incremental is None:
        return GateResult(TruthGate.E6, GateStatus.FAIL, ("INVALID_INCREMENTAL_RETURN",), _gate_evidence_hash(section))
    if incremental <= 0:
        return GateResult(
            TruthGate.E6,
            GateStatus.FAIL,
            (f"NOT_POSITIVE:incremental={incremental}",),
            _gate_evidence_hash(section),
        )
    return GateResult(TruthGate.E6, GateStatus.PASS, (), _gate_evidence_hash(section))


def _evaluate_single_gate(gate: TruthGate, evidence: Mapping[str, Any]) -> GateResult:
    if gate is TruthGate.E0:
        return _evaluate_e0(evidence)
    if gate is TruthGate.E1:
        return _evaluate_e1(evidence)
    if gate is TruthGate.E2:
        return _evaluate_e2(evidence)
    if gate is TruthGate.E3:
        return _evaluate_e3(evidence)
    if gate is TruthGate.E4:
        return _evaluate_e4(evidence)
    if gate is TruthGate.E5:
        return _evaluate_e5(evidence)
    if gate is TruthGate.E6:
        return _evaluate_e6(evidence)
    return GateResult(gate, GateStatus.NOT_EVALUATED, ("UNKNOWN_GATE",))


def assess_economic_truth(evidence: Mapping[str, Any] | None = None) -> EconomicTruthAssessment:
    """Evaluate E0–E6 in order with prerequisite enforcement.

    A gate is only evaluated when every earlier gate PASSes.  Otherwise it is
    ``NOT_EVALUATED`` with an explicit prerequisite reason, so an E4 claim can
    never ride on an unproven E3.
    """

    evidence = evidence or {}
    results: list[GateResult] = []
    for gate in GATE_ORDER:
        if results and results[-1].status is not GateStatus.PASS:
            blocker = results[-1]
            results.append(
                GateResult(
                    gate,
                    GateStatus.NOT_EVALUATED,
                    (f"PREREQUISITE_{blocker.gate.value}_{blocker.status.value}",),
                    blocker.evidence_hash,
                )
            )
            continue
        results.append(_evaluate_single_gate(gate, evidence))
    return EconomicTruthAssessment(tuple(results))


__all__ = [
    "GATE_ORDER",
    "EconomicTruthAssessment",
    "GateResult",
    "GateStatus",
    "TruthGate",
    "assess_economic_truth",
]
