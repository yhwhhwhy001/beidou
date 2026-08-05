"""BD-00 单元测试 — EnvironmentGuard 状态表、证书绑定、控制面行为。"""

from __future__ import annotations

import json
import os
import tempfile

from beidou_core.guard import (
    EnvironmentGuard,
    EnvironmentMode,
    StartupGateStatus,
)


class TestEnvironmentModes:
    """EnvironmentGuard 环境模式状态表。"""

    def test_paper_mode_allowed(self):
        """Paper 模式应始终允许启动。"""
        guard = EnvironmentGuard(mode="paper")
        result = guard.run_all_checks()
        assert result.status == StartupGateStatus.PASS
        assert len(result.failures) == 0

    def test_testnet_mode_allowed_without_credentials(self):
        """Testnet 模式在无凭据时仍可启动（但 full mode 需求不满足）。"""
        guard = EnvironmentGuard(mode="testnet")
        result = guard.run_all_checks()
        # Testnet without credentials: mode is valid, but full_mode check fails
        assert result.status == StartupGateStatus.FAIL
        assert "Trading credentials missing" in str(result.failures)

    def test_production_mode_blocked(self):
        """Production 模式应被永久阻断。"""
        guard = EnvironmentGuard(mode="production")
        result = guard.run_all_checks()
        assert result.status == StartupGateStatus.FAIL
        assert any("PRODUCTION" in f for f in result.failures)

    def test_safety_only_uses_paper(self):
        """safety_only 不应发送交易请求。"""
        guard = EnvironmentGuard(mode="paper")
        assert guard._mode == EnvironmentMode.PAPER


class TestMainnetURLDetection:
    """Mainnet URL 检测。"""

    def test_testnet_url_allowed(self):
        """Testnet URL 应被允许。"""
        guard = EnvironmentGuard(
            mode="testnet",
            rest_url="https://testnet.binancefuture.com",
        )
        assert guard.check_mainnet_url()

    def test_mainnet_url_blocked_binance_com(self):
        """binance.com (不含 testnet) 应被阻断。"""
        guard = EnvironmentGuard(
            mode="testnet",
            rest_url="https://fapi.binance.com",
        )
        assert not guard.check_mainnet_url()

    def test_mainnet_url_blocked_api_binance(self):
        """api.binance.com 应被阻断。"""
        guard = EnvironmentGuard(
            mode="testnet",
            rest_url="https://api.binance.com",
        )
        assert not guard.check_mainnet_url()

    def test_localhost_allowed(self):
        """本地 URL 不受阻断。"""
        guard = EnvironmentGuard(
            mode="testnet",
            rest_url="http://localhost:8080",
        )
        assert guard.check_mainnet_url()


class TestCredentialValidation:
    """交易凭据验证。"""

    def test_paper_mode_no_credentials_needed(self):
        """Paper 模式不需要交易凭据。"""
        guard = EnvironmentGuard(mode="paper", api_key="", api_secret="")
        assert guard.check_trading_credentials()

    def test_testnet_mode_requires_credentials(self):
        """Testnet 模式需要有效凭据。"""
        guard = EnvironmentGuard(
            mode="testnet",
            api_key="short",
            api_secret="",
        )
        assert not guard.check_trading_credentials()

    def test_testnet_valid_credentials(self):
        """有效长度的凭据应通过。"""
        guard = EnvironmentGuard(
            mode="testnet",
            api_key="a" * 64,
            api_secret="b" * 64,
        )
        assert guard.check_trading_credentials()


class TestFullModeRequirements:
    """full 模式需求检查。"""

    def test_full_mode_requires_testnet_url(self):
        """full 模式不应接受 Mainnet URL。"""
        guard = EnvironmentGuard(
            mode="testnet",
            rest_url="https://fapi.binance.com",
            api_key="a" * 64,
            api_secret="b" * 64,
        )
        assert not guard.check_full_mode_requirements(cli_mode="full")

    def test_full_mode_requires_g5_certificate(self):
        """full 模式需要 G5 证书文件。"""
        guard = EnvironmentGuard(
            mode="testnet",
            rest_url="https://testnet.binancefuture.com",
            api_key="a" * 64,
            api_secret="b" * 64,
        )
        # G5 cert path doesn't exist yet
        assert not guard.check_full_mode_requirements(cli_mode="full")

    def test_full_mode_with_g5_certificate(self):
        """创建 G5 证书后 full mode 应通过。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cert_dir = os.path.join(tmpdir, "evidence", "certificates")
            os.makedirs(cert_dir, exist_ok=True)
            cert_path = os.path.join(cert_dir, "G5.json")
            with open(cert_path, "w") as f:
                json.dump({"commit": "test_commit", "status": "PASS"}, f)

            guard = EnvironmentGuard(
                mode="testnet",
                rest_url="https://testnet.binancefuture.com",
                api_key="a" * 64,
                api_secret="b" * 64,
                commit="test_commit",
                g5_cert_path=cert_path,
            )
            assert guard.check_full_mode_requirements(cli_mode="full")


class TestControlPlaneNoAutoResume:
    """控制面不自动 RESUME。"""

    def test_control_plane_defaults_to_no_new_risk(self):
        """控制面默认状态应为 NO_NEW_RISK。"""
        from beidou_control.plane import ControlAction, ControlPlane

        cp = ControlPlane()
        assert cp.get_status() == ControlAction.NO_NEW_RISK

    def test_resume_is_explicit_only(self):
        """RESUME 必须显式调用。"""
        from beidou_control.plane import ControlAction, ControlPlane

        cp = ControlPlane()
        # 初始状态 NO_NEW_RISK
        assert cp.get_status() == ControlAction.NO_NEW_RISK
        # 不应该自动变为 RESUME
        assert cp.get_status() != ControlAction.RESUME


class TestAuditEvents:
    """安全启动审计事件。"""

    def test_audit_event_generation(self):
        """审计事件应包含关键字段。"""
        event = EnvironmentGuard.generate_startup_audit_event(
            environment="testnet",
            commit="abc123",
            config_hash="def456",
        )
        data = event.to_dict()
        assert data["event_type"] == "STARTUP"
        assert data["environment"] == "testnet"
        assert data["commit"] == "abc123"
        assert data["config_hash"] == "def456"
        assert data["certificate_status"] == "NOT_PRESENT"
        assert data["control_state"] == "NO_NEW_RISK"

    def test_audit_events_written_to_disk(self):
        """审计事件应写入磁盘。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence_dir = os.path.join(tmpdir, "evidence", "BD-00")
            guard = EnvironmentGuard(
                mode="paper",
                evidence_dir=evidence_dir,
            )
            guard.run_all_checks()
            audit_path = os.path.join(evidence_dir, "startup_audit.json")
            assert os.path.exists(audit_path)

            with open(audit_path) as f:
                events = json.load(f)
            assert len(events) > 0
            assert events[0]["environment"] == "paper"


class TestForbiddenClaimsScan:
    """禁止声明扫描。"""

    def test_forbidden_claims_detected(self):
        """检测禁止声明。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.md")
            with open(test_file, "w") as f:
                f.write("G7 L2-L5: ALL PASS")
            found = EnvironmentGuard.scan_for_forbidden_claims(tmpdir)
            assert len(found) > 0
            assert any("G7 L2-L5" in claim for claim in found)

    def test_clean_directory_no_claims(self):
        """清洁目录不应有禁止声明。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "clean.md")
            with open(test_file, "w") as f:
                f.write("Gate Status: PIVOT — NOT_VERIFIABLE")
            found = EnvironmentGuard.scan_for_forbidden_claims(tmpdir)
            assert len(found) == 0
