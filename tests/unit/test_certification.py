"""PKG-35~40 认证框架测试。G5-G8 Gate 独立发证、场景评估、P0回退。"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from beidou_shared.types import GateResult
from beidou_certification.engine import (
    CertificationGate,
    ScenarioStatus,
    CertificationScenario,
    ScenarioResult,
    GateCertificate,
    CertificationFramework,
    G5TestnetCertification,
    G6ShadowCertification,
    G7LiveCertification,
    G8UnattendedCertification,
    CertificationManager,
    create_l2_canary_certification,
)


class TestCertificationScenario:
    """认证场景定义测试。"""

    def test_scenario_creation(self):
        s = CertificationScenario(
            scenario_id="test-001",
            name="Test Scenario",
            description="A test scenario",
            gate=CertificationGate.G5_TESTNET,
            category="idempotency",
            required_evidence=["log_1", "log_2"],
        )
        assert s.scenario_id == "test-001"
        assert s.gate == CertificationGate.G5_TESTNET
        assert s.is_blocking

    def test_scenario_result_pass(self):
        s = CertificationScenario(
            scenario_id="test-002", name="Pass Test", description="",
            gate=CertificationGate.G5_TESTNET, category="test",
        )
        r = ScenarioResult(
            scenario=s, status=ScenarioStatus.PASS,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        assert r.is_pass()
        assert "PASS" in r.summary()

    def test_scenario_result_fail(self):
        s = CertificationScenario(
            scenario_id="test-003", name="Fail Test", description="",
            gate=CertificationGate.G5_TESTNET, category="test",
        )
        r = ScenarioResult(
            scenario=s, status=ScenarioStatus.FAIL,
            error_detail="Something broke",
        )
        assert not r.is_pass()
        assert "FAIL" in r.summary()

    def test_not_verifiable(self):
        s = CertificationScenario(
            scenario_id="test-004", name="NV Test", description="",
            gate=CertificationGate.G5_TESTNET, category="test",
        )
        r = ScenarioResult(scenario=s, status=ScenarioStatus.NOT_VERIFIABLE)
        assert not r.is_pass()


class TestGateCertificate:
    """Gate 证书测试。"""

    def test_pass_certificate(self):
        cert = GateCertificate(
            certificate_id="cert-test-001",
            gate=CertificationGate.G5_TESTNET,
            result=GateResult.PASS,
        )
        assert cert.is_pass()
        assert cert.blocking_p0_count() == 0

    def test_fail_certificate_with_blocking(self):
        s = CertificationScenario(
            scenario_id="block-1", name="Blocking", description="",
            gate=CertificationGate.G5_TESTNET, category="test", is_blocking=True,
        )
        r = ScenarioResult(scenario=s, status=ScenarioStatus.FAIL)
        cert = GateCertificate(
            certificate_id="cert-fail", gate=CertificationGate.G5_TESTNET,
            result=GateResult.FAIL, scenarios=[r],
        )
        assert not cert.is_pass()
        assert cert.blocking_p0_count() == 1


class TestCertificationFramework:
    """认证框架基类测试。"""

    def test_register_scenario(self):
        fw = CertificationFramework(CertificationGate.G5_TESTNET)
        s = CertificationScenario(
            scenario_id="fw-001", name="FW Test", description="",
            gate=CertificationGate.G5_TESTNET, category="test",
        )
        fw.register_scenario(s)
        assert len(fw.get_scenarios()) == 1

    def test_register_wrong_gate_raises(self):
        fw = CertificationFramework(CertificationGate.G5_TESTNET)
        s = CertificationScenario(
            scenario_id="wrong-gate", name="Wrong", description="",
            gate=CertificationGate.G6_SHADOW, category="test",
        )
        with pytest.raises(ValueError):
            fw.register_scenario(s)

    def test_not_complete_until_all_run(self):
        fw = CertificationFramework(CertificationGate.G5_TESTNET)
        s1 = CertificationScenario(
            scenario_id="s1", name="S1", description="",
            gate=CertificationGate.G5_TESTNET, category="test",
        )
        s2 = CertificationScenario(
            scenario_id="s2", name="S2", description="",
            gate=CertificationGate.G5_TESTNET, category="test",
        )
        fw.register_scenario(s1)
        fw.register_scenario(s2)
        assert not fw.all_scenarios_complete()
        fw.record_result(ScenarioResult(scenario=s1, status=ScenarioStatus.PASS))
        fw.record_result(ScenarioResult(scenario=s2, status=ScenarioStatus.PASS))
        assert fw.all_scenarios_complete()

    def test_evaluate_all_pass(self):
        fw = CertificationFramework(CertificationGate.G5_TESTNET)
        s = CertificationScenario(
            scenario_id="pass-1", name="Pass", description="",
            gate=CertificationGate.G5_TESTNET, category="test",
        )
        fw.register_scenario(s)
        fw.record_result(ScenarioResult(scenario=s, status=ScenarioStatus.PASS))
        cert = fw.evaluate()
        assert cert.is_pass()
        assert cert.blocking_p0_count() == 0

    def test_evaluate_blocking_fail(self):
        fw = CertificationFramework(CertificationGate.G5_TESTNET)
        s = CertificationScenario(
            scenario_id="block-2", name="Block", description="",
            gate=CertificationGate.G5_TESTNET, category="test", is_blocking=True,
        )
        fw.register_scenario(s)
        fw.record_result(ScenarioResult(scenario=s, status=ScenarioStatus.FAIL))
        cert = fw.evaluate()
        assert not cert.is_pass()
        assert cert.blocking_p0_count() == 1
        assert "P0_FAILURE" in cert.degradation_conditions


class TestG5TestnetCertification:
    """G5 Testnet 认证测试。"""

    def test_default_scenarios_registered(self):
        g5 = G5TestnetCertification()
        scenarios = g5.get_scenarios()
        assert len(scenarios) >= 7  # 默认7个场景

    def test_idempotency_pass(self):
        g5 = G5TestnetCertification()
        result = g5.run_idempotency_check("client-order-001", order_count=1, duplicate_orders=0)
        assert result.is_pass()

    def test_idempotency_fail(self):
        g5 = G5TestnetCertification()
        result = g5.run_idempotency_check("client-order-002", order_count=2, duplicate_orders=1)
        assert result.status == ScenarioStatus.FAIL

    def test_idempotency_not_verifiable(self):
        g5 = G5TestnetCertification()
        result = g5.run_idempotency_check("client-order-003", order_count=0, duplicate_orders=0)
        assert result.status == ScenarioStatus.NOT_VERIFIABLE

    def test_full_evaluation(self):
        g5 = G5TestnetCertification()
        for s in g5.get_scenarios():
            g5.record_result(ScenarioResult(scenario=s, status=ScenarioStatus.PASS))
        cert = g5.evaluate()
        assert cert.gate == CertificationGate.G5_TESTNET
        assert cert.is_pass()


class TestG6ShadowCertification:
    """G6 Shadow 认证测试。"""

    def test_default_scenarios(self):
        g6 = G6ShadowCertification()
        scenarios = g6.get_scenarios()
        assert len(scenarios) >= 5

    def test_runtime_check_pass(self):
        g6 = G6ShadowCertification()
        result = g6.record_runtime_check(
            actual_duration_seconds=90000,  # 25 hours
            policy_duration_seconds=86400,  # 24 hours
            time_compressed=False,
        )
        assert result.is_pass()

    def test_runtime_check_time_compressed(self):
        g6 = G6ShadowCertification()
        result = g6.record_runtime_check(
            actual_duration_seconds=86400,
            policy_duration_seconds=86400,
            time_compressed=True,
        )
        assert result.status == ScenarioStatus.FAIL

    def test_runtime_check_insufficient(self):
        g6 = G6ShadowCertification()
        result = g6.record_runtime_check(
            actual_duration_seconds=3600,  # only 1 hour
            policy_duration_seconds=86400,
            time_compressed=False,
        )
        assert result.status == ScenarioStatus.NOT_VERIFIABLE


class TestG7LiveCertification:
    """G7 实盘认证测试。"""

    def test_default_scenarios(self):
        g7 = G7LiveCertification(CertificationGate.G7_L2_CANARY)
        scenarios = g7.get_scenarios()
        assert len(scenarios) >= 6

    def test_l2_canary_has_extra_scenarios(self):
        g7l2 = create_l2_canary_certification()
        scenarios = g7l2.get_scenarios()
        # L2 has default 6 + 3 extra (single instrument, hard stop, any discrepancy)
        assert len(scenarios) >= 9

    def test_evaluate_all_pass(self):
        g7 = G7LiveCertification(CertificationGate.G7_L3_RAMP)
        for s in g7.get_scenarios():
            g7.record_result(ScenarioResult(scenario=s, status=ScenarioStatus.PASS))
        cert = g7.evaluate()
        assert cert.is_pass()


class TestG8UnattendedCertification:
    """G8 30天无人值守认证测试。"""

    def test_default_scenarios(self):
        g8 = G8UnattendedCertification()
        scenarios = g8.get_scenarios()
        assert len(scenarios) >= 7

    def test_elapsed_time_pass(self):
        g8 = G8UnattendedCertification()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=31)
        result = g8.verify_elapsed_time(start, end, min_days=30)
        assert result.is_pass()

    def test_elapsed_time_fail(self):
        g8 = G8UnattendedCertification()
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=10)
        result = g8.verify_elapsed_time(start, end, min_days=30)
        assert result.status == ScenarioStatus.FAIL

    def test_owner_disconnect_safe_lock(self):
        g8 = G8UnattendedCertification()
        result = g8.verify_owner_disconnect(
            last_owner_action=datetime(2026, 1, 1, tzinfo=timezone.utc),
            system_behavior="LOCKED_SAFE",
        )
        assert result.is_pass()

    def test_owner_disconnect_auto_resume_fail(self):
        g8 = G8UnattendedCertification()
        result = g8.verify_owner_disconnect(
            last_owner_action=datetime(2026, 1, 1, tzinfo=timezone.utc),
            system_behavior="AUTO_RESUMED",
        )
        assert result.status == ScenarioStatus.FAIL


class TestCertificationManager:
    """认证管理器测试。"""

    def test_register_frameworks(self):
        mgr = CertificationManager()
        g5 = G5TestnetCertification()
        g6 = G6ShadowCertification()
        mgr.register_framework(g5)
        mgr.register_framework(g6)
        assert mgr.get_framework(CertificationGate.G5_TESTNET) is not None
        assert mgr.get_framework(CertificationGate.G6_SHADOW) is not None
        assert mgr.get_framework(CertificationGate.G8_UNATTENDED) is None

    def test_can_promote_sequential(self):
        mgr = CertificationManager()
        g5 = G5TestnetCertification()
        g6 = G6ShadowCertification()
        mgr.register_framework(g5)
        mgr.register_framework(g6)
        # 全部 PASS
        for s in g5.get_scenarios():
            g5.record_result(ScenarioResult(scenario=s, status=ScenarioStatus.PASS))
        g5.evaluate()
        assert mgr.can_promote(CertificationGate.G5_TESTNET, CertificationGate.G6_SHADOW)

    def test_cannot_promote_without_pass(self):
        mgr = CertificationManager()
        g5 = G5TestnetCertification()
        mgr.register_framework(g5)
        # G5 没运行 → 不能晋升
        assert not mgr.can_promote(CertificationGate.G5_TESTNET, CertificationGate.G6_SHADOW)

    def test_cannot_skip_gates(self):
        mgr = CertificationManager()
        g5 = G5TestnetCertification()
        mgr.register_framework(g5)
        for s in g5.get_scenarios():
            g5.record_result(ScenarioResult(scenario=s, status=ScenarioStatus.PASS))
        g5.evaluate()
        # G5 PASS 但 G6/G7 不存在 → 不能直接跳到 G8
        assert not mgr.can_promote(CertificationGate.G5_TESTNET, CertificationGate.G8_UNATTENDED)

    def test_any_p0_failure(self):
        mgr = CertificationManager()
        g5 = G5TestnetCertification()
        mgr.register_framework(g5)
        # Blocking failure
        blocking_s = CertificationScenario(
            scenario_id="p0-block", name="P0 Block", description="",
            gate=CertificationGate.G5_TESTNET, category="test", is_blocking=True,
        )
        g5.register_scenario(blocking_s)
        g5.record_result(ScenarioResult(scenario=blocking_s, status=ScenarioStatus.FAIL))
        g5.evaluate()
        mgr.evaluate_all()
        assert mgr.any_p0_failure()

    def test_evaluate_all(self):
        mgr = CertificationManager()
        g5 = G5TestnetCertification()
        mgr.register_framework(g5)
        for s in g5.get_scenarios():
            g5.record_result(ScenarioResult(scenario=s, status=ScenarioStatus.PASS))
        certs = mgr.evaluate_all()
        assert len(certs) >= 1
        assert certs[0].gate == CertificationGate.G5_TESTNET
