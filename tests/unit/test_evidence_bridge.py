"""Task 11: EvidenceBridge 启动扫描桥接单元测试。"""

import hashlib
import json
from pathlib import Path

from beidou_core.evidence_bridge import BridgeReport, EvidenceBridge
from beidou_research.backtest.replay import PaperReplayResult
from beidou_research.factors.factor import FactorLifecycle, FactorPromotionGate, FactorRegistry
from beidou_research.mining.evidence import EvidenceBundle
from beidou_research.mining.runner import build_promotion_chain


def _valid_bundle() -> EvidenceBundle:
    b = EvidenceBundle(
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
    b.seal()
    return b


def _chain(b: EvidenceBundle) -> list[dict]:
    replay = PaperReplayResult(
        paper_sharpe=0.5, paper_drawdown_pct=-3.0, signal_consistency=0.6, paper_ir=0.25, window_bars=600, n_trades=5
    )
    chain = build_promotion_chain(
        b,
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
    """与 runner 写入端 / bridge 校验端完全一致的确定性链哈希。"""
    return hashlib.sha256(json.dumps(chain, sort_keys=True, default=str).encode()).hexdigest()


def _write_evidence(tmp_path: Path, bundle: EvidenceBundle, chain: list[dict], expression: str = "close") -> Path:
    d = tmp_path / "evidence" / "factors"
    d.mkdir(parents=True)
    path = d / "bundle.json"
    # GAP-10: 与 JSONFileFactorStore.save_factor_version 透传格式一致 —
    # 扩展键（promotion_chain/hash/source/expression/role）全部在 data 内层
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
                    "expression_string": expression,
                    "role": "entry",
                },
            }
        )
    )
    return d


def _registry() -> FactorRegistry:
    reg = FactorRegistry()
    from beidou_research.factors.factor import FactorDefinition
    from beidou_shared.types import SchemaVersion, VenueId

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


def test_valid_bundle_promotes_to_active(tmp_path: Path) -> None:
    bundle = _valid_bundle()
    evidence_dir = _write_evidence(tmp_path, bundle, _chain(bundle))
    registry = _registry()
    gate = FactorPromotionGate(strict=True)
    comp_reg: dict = {"meanrev_entry_v1": (object, ())}
    report = EvidenceBridge.load_and_apply(
        registry=registry,
        gate=gate,
        env_mode="testnet",
        component_registry=comp_reg,
        entry_ids={"meanrev_entry_v1"},
        filter_ids=set(),
        exit_ids=set(),
        evidence_dir=str(evidence_dir),
    )
    assert report.applied == ["tmpl_x_v1"]
    record = registry.get("tmpl_x_v1")
    assert record is not None and record.lifecycle == FactorLifecycle.ACTIVE
    assert record.has_authorized_active_evidence() is True
    assert "tmpl_x_v1" in comp_reg  # 表达式组件已注册


def test_tampered_bundle_rejected(tmp_path: Path) -> None:
    bundle = _valid_bundle()
    evidence_dir = _write_evidence(tmp_path, bundle, _chain(bundle))
    # 篡改 raw_metrics 后不重算 artifact_hash → 对拍失败
    victim = evidence_dir / "bundle.json"
    data = json.loads(victim.read_text())
    data["data"]["raw_metrics"]["ic_mean"] = 0.99
    victim.write_text(json.dumps(data))
    registry = _registry()
    gate = FactorPromotionGate(strict=True)
    report = EvidenceBridge.load_and_apply(
        registry=registry,
        gate=gate,
        env_mode="testnet",
        component_registry={"meanrev_entry_v1": (object, ())},
        entry_ids={"meanrev_entry_v1"},
        filter_ids=set(),
        exit_ids=set(),
        evidence_dir=str(evidence_dir),
    )
    assert report.applied == [] and report.rejected
    assert registry.get("tmpl_x_v1") is None


def test_replay_evidence_rejected_in_canary(tmp_path: Path) -> None:
    bundle = _valid_bundle()
    evidence_dir = _write_evidence(tmp_path, bundle, _chain(bundle))
    registry = _registry()
    gate = FactorPromotionGate(strict=True)
    report = EvidenceBridge.load_and_apply(
        registry=registry,
        gate=gate,
        env_mode="canary",
        component_registry={"meanrev_entry_v1": (object, ())},
        entry_ids={"meanrev_entry_v1"},
        filter_ids=set(),
        exit_ids=set(),
        evidence_dir=str(evidence_dir),
    )
    assert report.applied == [] and any("replay" in reason for _, reason in report.rejected)


def test_old_format_without_chain_is_ignored(tmp_path: Path) -> None:
    bundle = _valid_bundle()
    evidence_dir = _write_evidence(tmp_path, bundle, _chain(bundle))
    victim = evidence_dir / "bundle.json"
    data = json.loads(victim.read_text())
    del data["data"]["promotion_chain"]
    victim.write_text(json.dumps(data))
    registry = _registry()
    gate = FactorPromotionGate(strict=True)
    report = EvidenceBridge.load_and_apply(
        registry=registry,
        gate=gate,
        env_mode="testnet",
        component_registry={"meanrev_entry_v1": (object, ())},
        entry_ids={"meanrev_entry_v1"},
        filter_ids=set(),
        exit_ids=set(),
        evidence_dir=str(evidence_dir),
    )
    assert report.applied == [] and registry.get("tmpl_x_v1") is None


def test_evidence_bridge_report_shape() -> None:
    report = BridgeReport()
    assert report.applied == [] and report.rejected == []


def test_non_dict_payload_is_isolated_not_fatal(tmp_path: Path) -> None:
    # F1: 合法 JSON 但非 dict（list）→ 单文件隔离，不终止扫描，其他文件仍晋级
    bundle = _valid_bundle()
    evidence_dir = _write_evidence(tmp_path, bundle, _chain(bundle))
    bad = evidence_dir / "malformed.json"
    bad.write_text("[]")
    registry = _registry()
    gate = FactorPromotionGate(strict=True)
    report = EvidenceBridge.load_and_apply(
        registry=registry,
        gate=gate,
        env_mode="testnet",
        component_registry={"meanrev_entry_v1": (object, ())},
        entry_ids={"meanrev_entry_v1"},
        filter_ids=set(),
        exit_ids=set(),
        evidence_dir=str(evidence_dir),
    )
    assert report.applied == ["tmpl_x_v1"]  # 排序后 bundle.json 先于 malformed.json
    assert any(str(bad) in f and "unhandled" in reason for f, reason in report.rejected)


def test_tampered_chain_rejected(tmp_path: Path) -> None:
    # F2: 链篡改（icir 0.4 → 0.99）不重算 promotion_chain_hash → 对拍失败不晋级
    bundle = _valid_bundle()
    evidence_dir = _write_evidence(tmp_path, bundle, _chain(bundle))
    victim = evidence_dir / "bundle.json"
    data = json.loads(victim.read_text())
    data["data"]["promotion_chain"][0]["icir"] = 0.99
    victim.write_text(json.dumps(data))
    registry = _registry()
    gate = FactorPromotionGate(strict=True)
    report = EvidenceBridge.load_and_apply(
        registry=registry,
        gate=gate,
        env_mode="testnet",
        component_registry={"meanrev_entry_v1": (object, ())},
        entry_ids={"meanrev_entry_v1"},
        filter_ids=set(),
        exit_ids=set(),
        evidence_dir=str(evidence_dir),
    )
    assert report.applied == []
    assert any("promotion_chain_hash_mismatch" in reason for _, reason in report.rejected)
    assert registry.get("tmpl_x_v1") is None


def test_nan_chain_step_fail_closed(tmp_path: Path) -> None:
    # NaN fail-closed 回归：非有限 icir（JSON 字符串 "NaN"）与 null ic 均归一 0.0
    # → 门禁在 ACTIVE 级拒绝，因子不达 ACTIVE。同步重算哈希使 NaN 走门禁复验路径。
    bundle = _valid_bundle()
    chain = _chain(bundle)
    chain[-1]["icir"] = "NaN"
    chain[-1]["ic"] = None
    evidence_dir = _write_evidence(tmp_path, bundle, chain)
    victim = evidence_dir / "bundle.json"
    data = json.loads(victim.read_text())
    data["data"]["promotion_chain_hash"] = _chain_hash(data["data"]["promotion_chain"])
    victim.write_text(json.dumps(data))
    registry = _registry()
    gate = FactorPromotionGate(strict=True)
    report = EvidenceBridge.load_and_apply(
        registry=registry,
        gate=gate,
        env_mode="testnet",
        component_registry={"meanrev_entry_v1": (object, ())},
        entry_ids={"meanrev_entry_v1"},
        filter_ids=set(),
        exit_ids=set(),
        evidence_dir=str(evidence_dir),
    )
    assert report.applied == []
    assert any("gate_rejected@ACTIVE" in reason for _, reason in report.rejected)
    record = registry.get("tmpl_x_v1")
    assert record is not None and record.lifecycle != FactorLifecycle.ACTIVE


def test_top_level_chain_layout_rejected(tmp_path: Path) -> None:
    # GAP-10 回归钉：扩展键放文件顶层（旧错误布局，非 store 透传格式）
    # → data 内层无链，fail-closed 拒绝，不晋级。
    bundle = _valid_bundle()
    evidence_dir = _write_evidence(tmp_path, bundle, _chain(bundle))
    victim = evidence_dir / "bundle.json"
    data = json.loads(victim.read_text())
    data["promotion_chain"] = data["data"]["promotion_chain"]
    data["promotion_chain_hash"] = data["data"]["promotion_chain_hash"]
    del data["data"]["promotion_chain"]
    del data["data"]["promotion_chain_hash"]
    victim.write_text(json.dumps(data))
    registry = _registry()
    gate = FactorPromotionGate(strict=True)
    report = EvidenceBridge.load_and_apply(
        registry=registry,
        gate=gate,
        env_mode="testnet",
        component_registry={"meanrev_entry_v1": (object, ())},
        entry_ids={"meanrev_entry_v1"},
        filter_ids=set(),
        exit_ids=set(),
        evidence_dir=str(evidence_dir),
    )
    assert report.applied == []
    assert any("missing_promotion_chain" in reason for _, reason in report.rejected)
    assert registry.get("tmpl_x_v1") is None
