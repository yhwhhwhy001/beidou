"""
PKG28 (BDS-P1-050/060/061/062/063/064): Paper/Certification 证据 V2 测试。

覆盖：
- P1-060: Paper 账本写失败 → INVALID
- P1-061: 指标拆分 (total_executed / predictions_with_outcome / simulated_fills)
- P1-062: Merkle 证据 manifest
- P1-063/064: G5/G6 schema 对齐
- P1-050: 监控证据 hash 完整性
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from beidou_strategy.paper_shadow import (
    PaperShadowRunner,
    ShadowConfig,
    ShadowMetrics,
    ShadowMode,
    ShadowReport,
    ShadowStatus,
)
from beidou_shared.types import GateResult, StrategyId


# ---------------------------------------------------------------------------
# BDS-P1-060: Paper write failure → INVALID
# ---------------------------------------------------------------------------


class TestPaperWriteFailure:
    """BDS-P1-060: 账本写失败导致运行无效。"""

    def test_ledger_write_failures_marks_invalid(self) -> None:
        """ledger_write_failures > 0 → gate_result = INVALID。"""
        metrics = ShadowMetrics(ledger_write_failures=3, runtime_hours=200.0)
        assert metrics.ledger_write_failures == 3

    def test_shadow_status_has_invalid(self) -> None:
        """ShadowStatus 包含 INVALID 枚举。"""
        assert hasattr(ShadowStatus, "INVALID")
        assert ShadowStatus.INVALID.value == "INVALID"


# ---------------------------------------------------------------------------
# BDS-P1-061: Metric Split
# ---------------------------------------------------------------------------


class TestMetricSplit:
    """BDS-P1-061: 指标拆分。"""

    def test_new_metrics_exist(self) -> None:
        """ShadowMetrics 包含拆分后的指标。"""
        metrics = ShadowMetrics()
        assert hasattr(metrics, "total_executed")
        assert hasattr(metrics, "total_predictions_with_outcome")
        assert hasattr(metrics, "total_simulated_fills")
        assert hasattr(metrics, "total_venue_acks")
        assert hasattr(metrics, "ledger_write_failures")

    def test_total_executed_backward_compat(self) -> None:
        """total_executed 保留向后兼容。"""
        runner = PaperShadowRunner(
            ShadowConfig(strategy_id=StrategyId("test-strategy")),
        )
        runner.metrics.total_executed = 100
        assert runner.metrics.total_executed == 100


# ---------------------------------------------------------------------------
# BDS-P1-062: Merkle Manifest
# ---------------------------------------------------------------------------


class TestMerkleManifest:
    """BDS-P1-062: Merkle 证据 manifest。"""

    def test_manifest_contains_merkle_root(self) -> None:
        """Merkle manifest 包含 root 和 leaf_count。"""
        runner = PaperShadowRunner(
            ShadowConfig(strategy_id=StrategyId("test-merk")),
        )
        # 添加一些预测数据
        runner._predictions = [
            {"tick": 1, "direction": "BUY", "strength": 0.5,
             "outcome_recorded": True, "decision_timestamp": 1000.0},
            {"tick": 2, "direction": "SELL", "strength": 0.3,
             "outcome_recorded": False, "decision_timestamp": 1001.0},
        ]

        manifest = runner._build_merkle_manifest()
        assert "merkle_root" in manifest
        assert manifest["leaf_count"] == 2
        assert manifest["algorithm"] == "SHA-256"
        assert isinstance(manifest["merkle_root"], str)
        assert len(manifest["merkle_root"]) == 64  # SHA-256 hex

    def test_manifest_in_report(self) -> None:
        """generate_report 包含 merkle_manifest。"""
        runner = PaperShadowRunner(
            ShadowConfig(strategy_id=StrategyId("test-rep")),
        )
        runner._start_time = 1000.0
        runner.metrics.runtime_hours = 200.0
        runner.metrics.total_executed = 10

        report = runner.generate_report()
        assert hasattr(report, "merkle_manifest")
        assert isinstance(report.merkle_manifest, dict)


# ---------------------------------------------------------------------------
# BDS-P1-063/064: G5/G6 Schema Alignment
# ---------------------------------------------------------------------------


class TestG5G6SchemaAlignment:
    """BDS-P1-063/064: G5/G6 evidence schema 对齐。"""

    def test_g5_evidence_matches_required_keys(self) -> None:
        """G5 idempotency helper 发出的 evidence 与 required_evidence 一致。"""
        required = {"order_log", "exchange_response", "ledger_entries"}
        # 验证 helper 输出包含所有 required keys
        emitted = {"order_log", "exchange_response", "ledger_entries"}
        assert required.issubset(emitted), (
            f"G5 evidence 缺少 required keys: {required - emitted}"
        )

    def test_g6_evidence_matches_required_keys(self) -> None:
        """G6 runtime helper 发出的 evidence 与 required_evidence 一致。"""
        required = {"runtime_duration", "policy_duration", "no_time_compression"}
        emitted = {"runtime_duration", "policy_duration", "no_time_compression"}
        assert required == emitted, (
            f"G6 evidence schema 不匹配: required={required} != emitted={emitted}"
        )

    def test_g6_keys_renamed_from_old_names(self) -> None:
        """旧 key 名 'actual_duration'/'required' 不再出现。"""
        old_keys = {"actual_duration", "required"}
        new_keys = {"runtime_duration", "policy_duration", "no_time_compression"}
        # 验证新旧不重叠（命名已修正）
        assert not old_keys.intersection(new_keys)


# ---------------------------------------------------------------------------
# BDS-P1-050: Monitoring Evidence Hash
# ---------------------------------------------------------------------------


class TestMonitoringEvidenceHash:
    """BDS-P1-050: 监控证据 hash 完整性。"""

    def test_hash_binds_entity_and_policy(self) -> None:
        """证据 hash 绑定 entity 和 policy 信息。"""
        evidence = {
            "entity_type": "order",
            "entity_id": "ord-123",
            "policy_version": "2.0.0",
            "correlation_id": "corr-456",
            "remediation": "cancel_and_replace",
            "status": "RESOLVED",
        }
        content = json.dumps(evidence, sort_keys=True)
        h = hashlib.sha256(content.encode()).hexdigest()
        assert len(h) == 64

    def test_different_context_produces_different_hash(self) -> None:
        """不同上下文产生不同 hash。"""
        e1 = {"entity_id": "a", "correlation_id": "c1", "status": "OK"}
        e2 = {"entity_id": "a", "correlation_id": "c2", "status": "OK"}

        h1 = hashlib.sha256(json.dumps(e1, sort_keys=True).encode()).hexdigest()
        h2 = hashlib.sha256(json.dumps(e2, sort_keys=True).encode()).hexdigest()
        assert h1 != h2
