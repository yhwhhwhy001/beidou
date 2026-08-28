"""Coverage gap tests for beidou_core.evidence_bridge.

Targets the failure-isolation branches not reached by the existing bridge
suite: an invalid lifecycle transition inside ``_apply_chain`` (rollback +
``transition_rejected``), an unhandled gate exception that is rolled back and
re-raised, and the per-candidate exception net in ``load_and_apply``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_core.evidence_bridge import BridgeReport, EvidenceBridge
from beidou_research.backtest.replay import PaperReplayResult
from beidou_research.factors.factor import (
    FactorDefinition,
    FactorLifecycle,
    FactorRecord,
    FactorRegistry,
    PromotionDecision,
)
from beidou_research.mining.evidence import EvidenceBundle
from beidou_research.mining.runner import build_promotion_chain
from beidou_shared.types import SchemaVersion, VenueId


def _valid_bundle() -> EvidenceBundle:
    bundle = EvidenceBundle(
        bundle_id="b1",
        candidate_id="c1",
        factor_id="tmpl_x_v1",
        factor_version="2.0.0",
        candidate_hash="a" * 16,
        factor_expression_hash="e" * 16,
        dataset_manifest_hash="d" * 64,
        feature_manifest_hash="f" * 64,
        label_spec_hash="l" * 16,
        cost_model_version="bf06-v1",
        policy_version="2.0.0",
        random_seed=42,
        raw_metrics={"ic_mean": 0.05, "sharpe": 0.4, "sample_count": 600},
        gate_decision="PASS",
    )
    bundle.seal()
    return bundle


def _chain(bundle: EvidenceBundle) -> list[dict]:
    replay = PaperReplayResult(
        paper_sharpe=0.5, paper_drawdown_pct=-3.0, signal_consistency=0.6, paper_ir=0.25, window_bars=600, n_trades=5
    )
    chain = build_promotion_chain(
        bundle,
        ic=0.05,
        icir=0.4,
        sample_count=600,
        replay=replay,
        git_commit="abc123",
        expression_string="close",
        role="entry",
    )
    assert chain is not None
    return chain


def _chain_hash(chain: list[dict]) -> str:
    return hashlib.sha256(json.dumps(chain, sort_keys=True, default=str).encode()).hexdigest()


def _write_evidence(tmp_path: Path, bundle: EvidenceBundle, chain: list[dict]) -> Path:
    d = tmp_path / "evidence" / "factors"
    d.mkdir(parents=True)
    path = d / "bundle.json"
    path.write_text(
        json.dumps(
            {
                "factor_id": f"BTCUSDT:{bundle.candidate_id}",
                "version": "2.0.0",
                "data": {
                    **bundle.to_dict(),
                    "promotion_chain": chain,
                    "promotion_chain_hash": _chain_hash(chain),
                    "evidence_source": "historical_replay",
                    "expression_string": "close",
                    "role": "entry",
                },
            }
        )
    )
    return d


def _definition() -> FactorDefinition:
    return FactorDefinition(
        factor_id="tmpl_x_v1",
        name="mined",
        version=SchemaVersion("2.0.0"),
        description="d",
        author="a",
        category="meanrev",
        universe=frozenset({VenueId("BINANCE")}),
        instrument_types=frozenset({"perpetual"}),
        economic_rationale="r",
        lookback_period="1h",
        rebalance_interval="1h",
    )


def _registry() -> FactorRegistry:
    reg = FactorRegistry()
    reg.register(
        FactorDefinition(
            factor_id="meanrev_entry_v1",
            name="mr",
            version=SchemaVersion("2.0.0"),
            description="d",
            author="a",
            category="meanrev",
            universe=frozenset({VenueId("BINANCE")}),
            instrument_types=frozenset({"perpetual"}),
            economic_rationale="r",
            lookback_period="1h",
            rebalance_interval="1h",
        )
    )
    return reg


class _AlwaysApproveGate:
    def validate_evidence(self, **kwargs) -> PromotionDecision:
        return PromotionDecision(
            decision_id="d1",
            factor_id=kwargs["factor_id"],
            from_state=kwargs["current_state"],
            to_state=kwargs["target_state"],
            approved=True,
            reason="ok",
            factor_version=kwargs.get("factor_version", ""),
            commit=kwargs.get("commit", ""),
            dataset_hash=kwargs.get("dataset_hash", ""),
            evidence_ids=kwargs.get("evidence_ids", []),
            policy_version=kwargs.get("policy_version", ""),
            falsifier=kwargs.get("falsifier", ""),
        )


class _RaisingGate:
    def validate_evidence(self, **kwargs) -> PromotionDecision:
        raise RuntimeError("gate exploded")


def _step(**overrides: object) -> dict:
    step: dict[str, object] = {
        "from": "IDEA",
        "to": "ACTIVE",
        "icir": 0.4,
        "ic": 0.05,
        "sample_count": 600,
        "evidence_ids": ["x"],
        "factor_version": "2.0.0",
        "commit": "abc",
        "dataset_hash": "d",
        "policy_version": "2.0.0",
        "falsifier": "factor-miner",
    }
    step.update(overrides)
    return step


def test_apply_chain_reports_transition_rejected() -> None:
    record = FactorRecord(definition=_definition())
    report = BridgeReport()
    bundle = SimpleNamespace(artifact_hash="a" * 64)
    applied = EvidenceBridge._apply_chain(record, _AlwaysApproveGate(), [_step()], bundle, Path("x.json"), report)
    assert applied is False
    assert any("transition_rejected@ACTIVE" in reason for _, reason in report.rejected)
    assert record.lifecycle is FactorLifecycle.IDEA


def test_apply_chain_rolls_back_and_reraises_on_unhandled_exception() -> None:
    record = FactorRecord(definition=_definition())
    report = BridgeReport()
    bundle = SimpleNamespace(artifact_hash="a" * 64)
    with pytest.raises(RuntimeError, match="gate exploded"):
        EvidenceBridge._apply_chain(record, _RaisingGate(), [_step()], bundle, Path("x.json"), report)
    assert record.lifecycle is FactorLifecycle.IDEA
    assert record.promotion_history == []


def test_load_and_apply_isolates_candidate_exception(tmp_path: Path) -> None:
    bundle = _valid_bundle()
    evidence_dir = _write_evidence(tmp_path, bundle, _chain(bundle))
    registry = _registry()
    report = EvidenceBridge.load_and_apply(
        registry=registry,
        gate=_RaisingGate(),
        env_mode="testnet",
        component_registry={"meanrev_entry_v1": (object, ())},
        entry_ids={"meanrev_entry_v1"},
        filter_ids=set(),
        exit_ids=set(),
        evidence_dir=str(evidence_dir),
    )
    assert report.applied == []
    assert any("unhandled:RuntimeError" in reason for _, reason in report.rejected)
