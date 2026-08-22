"""Behavioral coverage for evidence-to-runtime bridge rejection and replay paths."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from beidou_core.evidence_bridge import BridgeReport, EvidenceBridge
from beidou_research.backtest.replay import PaperReplayResult
from beidou_research.factors.factor import (
    FactorDefinition,
    FactorLifecycle,
    FactorPromotionGate,
    FactorRecord,
    FactorRegistry,
)
from beidou_research.mining.evidence import EvidenceBundle
from beidou_research.mining.runner import build_promotion_chain
from beidou_shared.types import SchemaVersion, VenueId


def _bundle(factor_id: str = "bridge_factor", gate_decision: str = "PASS") -> EvidenceBundle:
    bundle = EvidenceBundle(
        bundle_id=f"bundle-{factor_id}",
        candidate_id=f"candidate-{factor_id}",
        factor_id=factor_id,
        factor_version="2.0.0",
        candidate_hash="a" * 16,
        factor_expression_hash="b" * 16,
        dataset_manifest_hash="c" * 64,
        feature_manifest_hash="d" * 64,
        label_spec_hash="e" * 16,
        cost_model_version="bf06-v1",
        policy_version="2.0.0",
        random_seed=7,
        raw_metrics={"ic_mean": 0.05, "sharpe": 0.4, "sample_count": 600},
        gate_decision=gate_decision,
    )
    bundle.seal()
    return bundle


def _chain(bundle: EvidenceBundle) -> list[dict[str, Any]]:
    chain = build_promotion_chain(
        bundle,
        ic=0.05,
        icir=0.4,
        sample_count=600,
        replay=PaperReplayResult(
            paper_sharpe=0.5,
            paper_drawdown_pct=-3.0,
            signal_consistency=0.6,
            paper_ir=0.25,
            window_bars=600,
            n_trades=5,
        ),
        git_commit="commit-bridge",
        expression_string="close",
        role="entry",
    )
    assert chain is not None
    return chain


def _chain_hash(chain: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(chain, sort_keys=True, default=str).encode()).hexdigest()


def _payload(
    bundle: EvidenceBundle, chain: list[dict[str, Any]], *, expression: str = "close", role: str = "entry"
) -> dict[str, Any]:
    return {
        "factor_id": f"BTCUSDT:{bundle.candidate_id}",
        "version": "2.0.0",
        "data": {
            **bundle.to_dict(),
            "promotion_chain": chain,
            "promotion_chain_hash": _chain_hash(chain),
            "evidence_source": "historical_replay",
            "expression_string": expression,
            "role": role,
        },
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _registry(factor_id: str = "bridge_factor") -> FactorRegistry:
    registry = FactorRegistry()
    registry.register(
        FactorDefinition(
            factor_id=factor_id,
            name=f"name-{factor_id}",
            version=SchemaVersion("2.0.0"),
            description="bridge coverage factor",
            author="tests",
            category="coverage",
            universe=frozenset({VenueId("BINANCE")}),
            instrument_types=frozenset({"perpetual"}),
            economic_rationale="test evidence",
            lookback_period="1h",
            rebalance_interval="1h",
        )
    )
    return registry


def _apply_kwargs(
    registry: FactorRegistry, root: Path, *, component_registry: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "registry": registry,
        "gate": FactorPromotionGate(strict=True),
        "env_mode": "testnet",
        "component_registry": component_registry if component_registry is not None else {},
        "entry_ids": set(),
        "filter_ids": set(),
        "exit_ids": set(),
        "evidence_dir": str(root),
    }


def test_bridge_isolates_missing_root_files_and_unpromotable_bundles(tmp_path: Path) -> None:
    missing = EvidenceBridge.load_and_apply(
        registry=object(),
        gate=object(),
        env_mode="testnet",
        component_registry={},
        entry_ids=set(),
        filter_ids=set(),
        exit_ids=set(),
        evidence_dir=str(tmp_path / "missing"),
    )
    assert missing == BridgeReport()

    root = tmp_path / "evidence"
    root.mkdir()
    (root / "00-invalid.json").write_text("{bad", encoding="utf-8")
    _write(root / "01-no-data.json", {})
    _write(root / "02-no-chain.json", {"data": {"promotion_chain": []}})
    chain = [{"from": "IDEA", "to": "GENERATED"}]
    _write(
        root / "03-bad-bundle.json",
        {"data": {"promotion_chain": chain, "promotion_chain_hash": _chain_hash(chain)}},
    )
    failed = _bundle(gate_decision="FAIL")
    _write(root / "04-gate-failed.json", _payload(failed, _chain(failed)))

    report = EvidenceBridge.load_and_apply(**_apply_kwargs(FactorRegistry(), root))
    reasons = [reason for _, reason in report.rejected]
    assert any(reason.startswith("unreadable:JSONDecodeError") for reason in reasons)
    assert "missing_bundle_data" in reasons
    assert "missing_promotion_chain" in reasons
    assert any(reason.startswith("bundle_construct:") for reason in reasons)
    assert any(reason.startswith("bundle:gate_not_passed") for reason in reasons)


def test_bridge_registers_roles_and_replays_active_factor_idempotently(tmp_path: Path) -> None:
    bundle = _bundle("active_factor")
    chain = _chain(bundle)
    root = tmp_path / "evidence"
    root.mkdir()
    _write(root / "a.json", _payload(bundle, chain, role="entry"))
    _write(root / "b.json", _payload(bundle, chain, role="entry"))
    registry = _registry("active_factor")
    components: dict[str, Any] = {}
    first = EvidenceBridge.load_and_apply(**_apply_kwargs(registry, root, component_registry=components))
    assert first.applied == ["active_factor"]
    assert registry.get("active_factor").has_authorized_active_evidence() is True
    second = EvidenceBridge.load_and_apply(**_apply_kwargs(registry, root, component_registry=components))
    assert second.applied == ["active_factor"]
    assert "active_factor" in components

    entry_ids: set[str] = set()
    filter_ids: set[str] = set()
    exit_ids: set[str] = set()
    for role, expected in (("filter", filter_ids), ("exit", exit_ids), ("other", entry_ids)):
        EvidenceBridge._register_expression_component(
            f"{role}-factor", "close", role, {}, entry_ids, filter_ids, exit_ids
        )
        assert f"{role}-factor" in expected


def test_bridge_chain_defensive_paths_and_expression_requirement(tmp_path: Path) -> None:
    bundle = _bundle("chain_factor")
    root = tmp_path / "evidence"
    root.mkdir()
    _write(root / "missing-expression.json", _payload(bundle, _chain(bundle), expression=""))
    report = EvidenceBridge.load_and_apply(**_apply_kwargs(_registry("chain_factor"), root))
    assert any(reason == "missing_expression_string" for _, reason in report.rejected)

    registry = _registry("chain_factor")
    record = registry.get("chain_factor")
    assert record is not None
    report = BridgeReport()
    fake_decision = SimpleNamespace(
        decision_id="decision-1",
        factor_id="chain_factor",
        approved=True,
        reason="approved",
        factor_version="2.0.0",
        commit="commit",
        dataset_hash="dataset",
        evidence_ids=[],
        policy_version="policy",
        falsifier="tests",
    )

    class Gate:
        def validate_evidence(self, **_kwargs: Any) -> Any:
            return fake_decision

    bundle_path = root / "chain.json"
    applied = EvidenceBridge._apply_chain(
        record,
        Gate(),
        [{"from": "IDEA", "to": "GENERATED", "sample_count": "bad", "evidence_ids": "bad"}],
        bundle,
        bundle_path,
        report,
    )
    assert applied is True
    assert record.lifecycle is FactorLifecycle.GENERATED

    same_target = FactorRecord(definition=record.definition, lifecycle=FactorLifecycle.GENERATED)
    skip_report = BridgeReport()
    assert (
        EvidenceBridge._apply_chain(
            same_target,
            Gate(),
            [{"from": "IDEA", "to": "GENERATED"}],
            bundle,
            bundle_path,
            skip_report,
        )
        is True
    )

    mismatch = FactorRecord(definition=record.definition)
    mismatch_report = BridgeReport()
    assert (
        EvidenceBridge._apply_chain(
            mismatch,
            Gate(),
            [{"from": "GENERATED", "to": "SANITY_PASSED"}],
            bundle,
            bundle_path,
            mismatch_report,
        )
        is False
    )
    assert any("chain_order_mismatch" in reason for _, reason in mismatch_report.rejected)

    invalid_report = BridgeReport()
    assert (
        EvidenceBridge._apply_chain(
            FactorRecord(definition=record.definition),
            Gate(),
            [{"from": "NOT_A_STATE", "to": "GENERATED"}],
            bundle,
            bundle_path,
            invalid_report,
        )
        is False
    )
    assert any(reason.startswith("chain_state:") for _, reason in invalid_report.rejected)
