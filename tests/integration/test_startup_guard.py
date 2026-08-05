"""BD-00 集成测试 — 无密钥启动、Testnet 证书缺失、Mainnet URL 阻断。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

from beidou_core.guard import (
    EnvironmentGuard,
    EnvironmentMode,
    StartupGateStatus,
)


class TestStartupWithoutKeys:
    """无密钥启动场景。"""

    def test_paper_mode_starts_without_credentials(self):
        """Paper 模式无密钥应成功通过 Gate。"""
        guard = EnvironmentGuard(
            mode="paper",
            rest_url="https://testnet.binancefuture.com",
            api_key="",
            api_secret="",
        )
        result = guard.run_all_checks()
        assert result.status == StartupGateStatus.PASS
        assert len(result.failures) == 0

    def test_full_mode_requires_all_credentials(self):
        """full 模式缺失凭据应被阻断。"""
        guard = EnvironmentGuard(
            mode="testnet",
            rest_url="https://testnet.binancefuture.com",
            api_key="",
            api_secret="",
        )
        result = guard.run_all_checks()
        assert result.status == StartupGateStatus.FAIL
        assert "Trading credentials missing" in str(result.failures)


class TestTestnetCertificateMissing:
    """Testnet 证书缺失场景。"""

    def test_full_mode_without_g5_certificate_fails(self):
        """无 G5 证书时 full mode 要求应失败。"""
        guard = EnvironmentGuard(
            mode="testnet",
            rest_url="https://testnet.binancefuture.com",
            api_key="a" * 64,
            api_secret="b" * 64,
        )
        # G5 certificate not present by default
        assert not guard.check_full_mode_requirements()

    def test_paper_mode_without_certificate_passes(self):
        """Paper 模式不需要 G5 证书。"""
        guard = EnvironmentGuard(
            mode="paper",
            rest_url="https://testnet.binancefuture.com",
        )
        result = guard.run_all_checks()
        assert result.status == StartupGateStatus.PASS


class TestMainnetURLBlocked:
    """Mainnet URL 阻断场景。"""

    def test_mainnet_url_triggers_p0_audit(self):
        """Mainnet URL 触发 P0 审计事件。"""
        guard = EnvironmentGuard(
            mode="testnet",
            rest_url="https://fapi.binance.com",
            api_key="a" * 64,
            api_secret="b" * 64,
        )
        result = guard.run_all_checks()
        # Mainnet URL detection
        assert result.status == StartupGateStatus.FAIL
        # Check for mainnet-related failure
        mainnet_failures = [f for f in result.failures if "Mainnet" in f or "mainnet" in f.lower()]
        assert len(mainnet_failures) > 0

    def test_testnet_url_no_mainnet_block(self):
        """Testnet URL 不触发 Mainnet 阻断。"""
        guard = EnvironmentGuard(
            mode="testnet",
            rest_url="https://testnet.binancefuture.com",
            api_key="a" * 64,
            api_secret="b" * 64,
        )
        # Mainnet check should pass for testnet URLs
        assert guard.check_mainnet_url()


class TestStartupGateFailsGracefully:
    """启动 Gate 失败时优雅退出。"""

    def test_production_mode_fails_with_clear_message(self):
        """Production 模式应失败并给出清晰消息。"""
        guard = EnvironmentGuard(mode="production")
        result = guard.run_all_checks()
        assert result.status == StartupGateStatus.FAIL
        assert any("PRODUCTION" in str(f).upper() for f in result.failures)

    def test_audit_written_even_on_failure(self):
        """即使 Gate 失败，审计事件也应落盘。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence_dir = os.path.join(tmpdir, "evidence", "BD-00")
            guard = EnvironmentGuard(
                mode="testnet",
                rest_url="https://fapi.binance.com",
                evidence_dir=evidence_dir,
            )
            result = guard.run_all_checks()
            assert result.status == StartupGateStatus.FAIL
            audit_path = os.path.join(evidence_dir, "startup_audit.json")
            assert os.path.exists(audit_path)


class TestNoAutoResume:
    """不自动 RESUME 场景。"""

    def test_engine_starts_in_no_new_risk(self):
        """引擎启动时应保持 NO_NEW_RISK。"""
        from beidou_control.plane import ControlPlane, ControlAction
        cp = ControlPlane()
        # 初始状态就是 NO_NEW_RISK
        assert cp.get_status() == ControlAction.NO_NEW_RISK

    def test_resume_not_called_automatically(self):
        """不应有代码自动调用 RESUME。"""
        from beidou_control.plane import ControlPlane, ControlAction
        cp = ControlPlane()
        # 验证 RESUME 不是自动触发的
        initial = cp.get_status()
        assert initial != ControlAction.RESUME
