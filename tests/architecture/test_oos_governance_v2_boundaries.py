"""Static isolation rules for legacy OOS v1 and governed OOS v2."""

from __future__ import annotations

import ast
from pathlib import Path

from beidou_research.experiments import (
    GovernedOOSAccessReceiptV2,
    GovernedOOSSealV2,
    GovernedPromotionDecisionV2,
    OOSAccessReceipt,
    OOSPromotionStatus,
    OOSSeal,
    PromotableExperimentDecision,
)

ROOT = Path(__file__).resolve().parents[2]


def test_v2_types_are_not_v1_subclasses_or_aliases() -> None:
    assert not issubclass(GovernedOOSSealV2, OOSSeal)
    assert not issubclass(GovernedOOSAccessReceiptV2, OOSAccessReceipt)
    assert not issubclass(GovernedPromotionDecisionV2, (OOSPromotionStatus, PromotableExperimentDecision))


def test_only_governed_v2_module_can_construct_true_v2_eligibility() -> None:
    allowed = ROOT / "beidou_research" / "experiments" / "oos_governance_v2.py"
    findings: list[str] = []
    for path in (ROOT / "beidou_research").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "eligible_for_v2_promotion"
                and isinstance(node.value, ast.Constant)
                and node.value.value is True
                and path != allowed
            ):
                findings.append(str(path.relative_to(ROOT)))
    assert findings == []


def test_legacy_types_default_to_research_only_machine_contract() -> None:
    decision = PromotableExperimentDecision(status="PROMOTABLE", reasons=(), promotable=True)
    status = OOSPromotionStatus(status="PROMOTABLE", reasons=())
    for value in (decision, status):
        assert value.governance_version == 1
        assert value.promotion_scope == "LEGACY_RESEARCH_ONLY"
        assert value.eligible_for_v2_promotion is False
