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
        paper_ir=0.25,
        window_bars=600,
        n_trades=10,
    )


def test_chain_has_8_transitions_in_correct_order() -> None:
    # M05-R2: runner 侧不传 evidence_bundle → ACTIVE 步恒拒 —— 链为
    # 7 步通过 + 1 步拒绝(ACTIVE),最终 approved=False(桥侧带 bundle
    # 复验可反向批准,runner 链的 approved 不是最终判定)。
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
    assert chain[-1]["to"] == "ACTIVE"
    assert chain[-1]["approved"] is False
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


def test_chain_is_honest_for_below_threshold_icir() -> None:
    """M05-F04: 低于 SANITY_PASSED 阈值的 icir 不得产生全链自证 approved=True。"""
    chain = build_promotion_chain(
        _bundle(),
        ic=0.02,
        icir=0.05,  # < 0.3 阈值
        sample_count=600,
        replay=_replay(),
        git_commit="abc123",
        expression_string="close",
        role="entry",
    )
    assert chain is not None
    assert len(chain) == 2  # IDEA→GENERATED→SANITY_PASSED(拒绝) 后终止
    assert chain[0]["approved"] is True
    assert chain[1]["approved"] is False
    assert "ICIR" in chain[1]["reason"]


def test_chain_rejects_bad_replay_values() -> None:
    """M05-R2: replay 真实值校验 —— paper_sharpe<=0 不得自证到 PAPER_TRADING。"""
    bad_replay = PaperReplayResult(
        paper_sharpe=-0.2,
        paper_drawdown_pct=-5.0,
        signal_consistency=0.6,
        paper_ir=0.25,
        window_bars=600,
        n_trades=10,
    )
    chain = build_promotion_chain(
        _bundle(),
        ic=0.05,
        icir=0.4,
        sample_count=600,
        replay=bad_replay,
        git_commit="abc123",
        expression_string="close",
        role="entry",
    )
    assert chain is not None
    paper_step = next(step for step in chain if step["to"] == "PAPER_TRADING")
    assert paper_step["approved"] is False
    assert "paper_sharpe" in paper_step["reason"]
