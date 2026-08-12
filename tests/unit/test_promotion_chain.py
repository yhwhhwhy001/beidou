"""Task 9: 逐级证据链 promotion_chain 生成（replay 缺失即不产出）。

验证 build_promotion_chain 输出 8 级 IDEA→ACTIVE 链：
- 状态序与 FACTOR_LIFECYCLE_TRANSITIONS 一致
- 每级 evidence_ids 覆盖 PROMOTION_EVIDENCE_REQUIREMENTS 的 required_evidence
- 绑定字段非空
- replay 缺失时 fail-closed 返回 None
"""

from beidou_research.backtest.replay import PaperReplayResult
from beidou_research.factors.factor import (
    FACTOR_LIFECYCLE_TRANSITIONS,
    PROMOTION_EVIDENCE_REQUIREMENTS,
    FactorLifecycle,
)
from beidou_research.mining.evidence import EvidenceBundle
from beidou_research.mining.runner import build_promotion_chain


def _bundle() -> EvidenceBundle:
    return EvidenceBundle(
        bundle_id="b-1",
        candidate_id="c-1",
        factor_id="tmpl_test_v1",
        factor_version="2.0.0",
        candidate_hash="a" * 16,
        factor_code_hash="",
        factor_expression_hash="e" * 16,
        dataset_manifest_hash="d" * 64,
        feature_manifest_hash="f" * 64,
        label_spec_hash="l" * 16,
        cost_model_version="bf06-v1",
        policy_version="2.0.0",
        random_seed=42,
        gate_decision="PASS",
    )


def _replay() -> PaperReplayResult:
    return PaperReplayResult(
        paper_sharpe=0.5,
        paper_drawdown_pct=-5.0,
        signal_consistency=0.6,
        challenger_icir=0.25,
        window_bars=600,
        n_trades=10,
    )


def test_chain_has_8_transitions_in_correct_order() -> None:
    chain = build_promotion_chain(
        _bundle(),
        ic=0.05,
        icir=0.4,
        sample_count=600,
        replay=_replay(),
        git_commit="abc123",
        expression_string="close",
        role="entry",
    )
    assert chain is not None and len(chain) == 8
    states = [c["from"] for c in chain] + [chain[-1]["to"]]
    assert states == [
        "IDEA",
        "GENERATED",
        "SANITY_PASSED",
        "RESEARCH_VALIDATED",
        "OOS_VERIFIED",
        "COST_CAPACITY_VERIFIED",
        "PAPER_TRADING",
        "CHALLENGER",
        "ACTIVE",
    ]
    for step in chain:
        assert step["to"] in FACTOR_LIFECYCLE_TRANSITIONS[step["from"]]


def test_chain_evidence_ids_cover_requirements() -> None:
    chain = build_promotion_chain(
        _bundle(),
        ic=0.05,
        icir=0.4,
        sample_count=600,
        replay=_replay(),
        git_commit="abc123",
        expression_string="close",
        role="entry",
    )
    assert chain is not None

    for step in chain:
        target = FactorLifecycle(step["to"])
        required = PROMOTION_EVIDENCE_REQUIREMENTS[target]["required_evidence"]
        assert set(required).issubset(set(step["evidence_ids"])), (step["to"], required, step["evidence_ids"])


def test_chain_bindings_nonempty() -> None:
    chain = build_promotion_chain(
        _bundle(),
        ic=0.05,
        icir=0.4,
        sample_count=600,
        replay=_replay(),
        git_commit="abc123",
        expression_string="close",
        role="entry",
    )
    assert chain is not None
    for step in chain:
        assert step["commit"].strip() and step["dataset_hash"].strip()
        assert step["policy_version"].strip() and step["falsifier"].strip()


def test_chain_none_without_replay() -> None:
    assert (
        build_promotion_chain(
            _bundle(),
            ic=0.05,
            icir=0.4,
            sample_count=600,
            replay=None,
            git_commit="abc123",
            expression_string="close",
            role="entry",
        )
        is None
    )
