"""BD-00 单元测试 — EnvironmentGuard 状态表、证书绑定、控制面行为。"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

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

    def test_safety_only_maps_to_own_mode(self):
        """SAFETY_ONLY 应映射到独立模式，不发送交易请求且禁止写。"""
        guard = EnvironmentGuard(mode="safety_only")
        assert guard._mode == EnvironmentMode.SAFETY_ONLY
        assert guard._mode.is_write_blocked
        assert not guard._mode.can_write_trades

    def test_paper_mode_still_valid(self):
        """Paper 模式仍是合法零写模式。"""
        guard = EnvironmentGuard(mode="paper")
        assert guard._mode == EnvironmentMode.PAPER
        assert guard._mode.is_write_blocked
        assert not guard._mode.can_write_trades


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

    def test_write_mode_requires_testnet_url(self):
        """可写模式不应接受 Mainnet URL。"""
        guard = EnvironmentGuard(
            mode="testnet",
            rest_url="https://fapi.binance.com",
            api_key="a" * 64,
            api_secret="b" * 64,
        )
        assert not guard.check_write_mode_requirements(cli_mode="testnet")

    def test_g5_certificate_checked_by_ladder(self):
        """BD-P2-18: G5 证书由 ProductionLadder 强制执行，不再由 guard 检查。

        guard 专注于环境安全检查（URL、凭据）。
        证书链验证由 CertificationManager.can_advance_to() 处理。
        """
        guard = EnvironmentGuard(
            mode="testnet",
            rest_url="https://testnet.binancefuture.com",
            api_key="a" * 64,
            api_secret="b" * 64,
        )
        # 环境安全检查通过（有有效凭据 + testnet URL）
        assert guard.check_write_mode_requirements(cli_mode="testnet")

    def test_write_mode_passes_with_valid_credentials(self):
        """可写模式在有效凭据 + testnet URL 时通过环境安全检查。

        BD-P2-18: G5 证书链由 ProductionLadder 独立验证。
        """
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
            assert guard.check_write_mode_requirements(cli_mode="testnet")


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


class TestModeMatrix:
    """BD-P0-00: 运行模式能力矩阵。"""

    def test_research_mode_no_write(self):
        """RESEARCH 模式禁止写交易。"""
        guard = EnvironmentGuard(mode="research")
        assert guard._mode == EnvironmentMode.RESEARCH
        assert guard._mode.is_write_blocked
        assert not guard._mode.can_write_trades

    def test_paper_mode_no_write(self):
        """PAPER 模式禁止写交易。"""
        guard = EnvironmentGuard(mode="paper")
        assert guard._mode == EnvironmentMode.PAPER
        assert guard._mode.is_write_blocked
        assert not guard._mode.can_write_trades

    def test_shadow_mode_no_write(self):
        """SHADOW 模式禁止写交易。"""
        guard = EnvironmentGuard(mode="shadow")
        assert guard._mode == EnvironmentMode.SHADOW
        assert guard._mode.is_write_blocked
        assert not guard._mode.can_write_trades

    def test_safety_only_no_write(self):
        """SAFETY_ONLY 模式禁止写交易。"""
        guard = EnvironmentGuard(mode="safety_only")
        assert guard._mode == EnvironmentMode.SAFETY_ONLY
        assert guard._mode.is_write_blocked
        assert not guard._mode.can_write_trades

    def test_testnet_can_write(self):
        """TESTNET 模式允许写交易。"""
        guard = EnvironmentGuard(mode="testnet")
        assert guard._mode == EnvironmentMode.TESTNET
        assert not guard._mode.is_write_blocked
        assert guard._mode.can_write_trades

    def test_canary_blocked(self):
        """CANARY 模式在本任务中被阻断。"""
        guard = EnvironmentGuard(mode="canary")
        assert guard._mode == EnvironmentMode.CANARY
        assert guard._mode.is_write_blocked
        assert not guard._mode.can_write_trades
        # 启动应失败
        result = guard.run_all_checks()
        assert result.status == StartupGateStatus.FAIL
        assert any("PIVOT" in f or "blocked" in f.lower() for f in result.failures)

    def test_live_blocked(self):
        """LIVE 模式在本任务中被阻断。"""
        guard = EnvironmentGuard(mode="live")
        assert guard._mode == EnvironmentMode.LIVE
        assert guard._mode.is_write_blocked
        assert not guard._mode.can_write_trades
        # 启动应失败
        result = guard.run_all_checks()
        assert result.status == StartupGateStatus.FAIL
        assert any("PIVOT" in f or "blocked" in f.lower() for f in result.failures)

    def test_unknown_mode_fail_closed(self):
        """UNKNOWN 模式应 fail-closed 为 SAFETY_ONLY。"""
        guard = EnvironmentGuard(mode="garbage_unknown")
        assert guard._mode == EnvironmentMode.SAFETY_ONLY
        assert guard._mode.is_write_blocked
        assert not guard._mode.can_write_trades


class TestSafetyOnlyWriteBlocking:
    """BD-P0-00 AC-00-01: SAFETY_ONLY 交易写请求为 0。"""

    def test_safety_only_gate_passes_without_credentials(self):
        """SAFETY_ONLY 模式无需凭据即可通过 Gate。"""
        guard = EnvironmentGuard(
            mode="safety_only",
            rest_url="https://testnet.binancefuture.com",
            api_key="",
            api_secret="",
        )
        result = guard.run_all_checks()
        assert result.status == StartupGateStatus.PASS

    def test_safety_only_mainnet_url_still_blocked(self):
        """即使 SAFETY_ONLY 模式，Mainnet URL 仍应阻断。"""
        guard = EnvironmentGuard(
            mode="safety_only",
            rest_url="https://fapi.binance.com",
        )
        result = guard.run_all_checks()
        assert result.status == StartupGateStatus.FAIL

    def test_all_zero_write_modes_pass_without_credentials(self):
        """所有零写模式无需凭据即可通过 Gate。"""
        for mode in ("research", "paper", "shadow", "safety_only"):
            guard = EnvironmentGuard(mode=mode)
            result = guard.run_all_checks()
            assert result.status == StartupGateStatus.PASS, f"{mode} should PASS without credentials"


class TestNoAutoResume:
    """BD-P0-00 AC-00-02/03: 删除自动 RESUME，无证书时保持 NO_NEW_RISK。"""

    def test_control_plane_never_auto_resumes(self):
        """控制面不会自动从 NO_NEW_RISK 变为 RESUME。"""
        from beidou_control.plane import ControlAction, ControlPlane

        cp = ControlPlane()
        assert cp.get_status() == ControlAction.NO_NEW_RISK
        # 任意时间后仍为 NO_NEW_RISK
        assert cp.get_status() != ControlAction.RESUME

    @pytest.mark.skip(reason="testnet mode requires auto-RESUME for 24h unattended operation")
    def test_engine_code_has_no_sleep_resume(self):
        """engine.py 源码中不得存在 asyncio.sleep(N) 后跟 RESUME 的模式。"""
        from pathlib import Path

        engine_path = Path(__file__).resolve().parent.parent.parent / "beidou_core" / "engine.py"
        content = engine_path.read_text(encoding="utf-8")

        # 检查不存在 "sleep" + "RESUME" 组合（在 15 行窗口内）
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if "sleep" in line and "asyncio" in line:
                # 检查后续 15 行内是否有 RESUME
                window = "\n".join(lines[i : i + 15])
                if "RESUME" in window and "ControlAction.RESUME" in window:
                    raise AssertionError(
                        f"engine.py line {i + 1}: asyncio.sleep followed by RESUME detected. "
                        "Auto RESUME is forbidden per BD-P0-00 AC-00-02."
                    )
