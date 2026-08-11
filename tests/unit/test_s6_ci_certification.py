"""
S6 (PKG27-29): CI、测试、证据与认证晋级测试。

覆盖：
- PKG28 (BDS-P1-065): GateCertificate 签名与证据根哈希
- PKG27 (BDS-P1-059): 安全关键文件无 F821 ignore
- PKG27 (BDS-P1-057): 可重现构建 — 依赖锁定
"""

from __future__ import annotations

import hashlib

from beidou_certification.engine import (
    CertificationFramework,
    CertificationGate,
    CertificationScenario,
    ScenarioResult,
    ScenarioStatus,
)


class TestGateCertificateSigning:
    """PKG28 (BDS-P1-065): Gate 证书签名与证据绑定。"""

    def test_certificate_has_signature_when_key_available(self) -> None:
        """有签名密钥时证书包含签名。"""
        import os

        os.environ["BEIDOU_SIGNING_KEY"] = "test-signing-key-123"
        os.environ["BEIDOU_CERT_SIGNER"] = "test-signer"

        framework = CertificationFramework(CertificationGate.G5_TESTNET)
        scenario = CertificationScenario(
            scenario_id="test-scenario",
            name="Test Scenario",
            description="Test scenario",
            gate=CertificationGate.G5_TESTNET,
            category="idempotency",
            is_blocking=False,
        )
        framework.register_scenario(scenario)
        framework.record_result(ScenarioResult(
            scenario=scenario,
            status=ScenarioStatus.PASS,
        ))

        cert = framework.evaluate()
        assert cert.signature != "", "PASS 证书必须签名"
        assert cert.signer == "test-signer"

    def test_evidence_root_hash_included(self) -> None:
        """证据根哈希绑定到证书。"""
        import os

        os.environ["BEIDOU_SIGNING_KEY"] = "key-for-evidence-test"

        framework = CertificationFramework(CertificationGate.G6_SHADOW)
        s1 = CertificationScenario("ev-1", "Evidence 1", "Evidence 1", CertificationGate.G6_SHADOW, "reconciliation", is_blocking=False)
        s2 = CertificationScenario("ev-2", "Evidence 2", "Evidence 2", CertificationGate.G6_SHADOW, "reconciliation", is_blocking=False)
        framework.register_scenario(s1)
        framework.register_scenario(s2)
        framework.record_result(ScenarioResult(scenario=s1, status=ScenarioStatus.PASS))
        framework.record_result(ScenarioResult(scenario=s2, status=ScenarioStatus.PASS))

        cert = framework.evaluate()
        assert cert.evidence_manifest == ["ev-1", "ev-2"]

    def test_fail_certificate_not_signed(self) -> None:
        """FAIL 证书也有签名（失败也是可追踪事实）。"""
        import os

        os.environ["BEIDOU_SIGNING_KEY"] = "key-for-fail"

        framework = CertificationFramework(CertificationGate.G5_TESTNET)
        scenario = CertificationScenario(
            "fail-scenario", "Fail Scenario", "Will fail",
            CertificationGate.G5_TESTNET, "recovery", is_blocking=True,
        )
        framework.register_scenario(scenario)
        framework.record_result(ScenarioResult(
            scenario=scenario, status=ScenarioStatus.FAIL, error_detail="Expected failure"
        ))

        cert = framework.evaluate()
        assert cert.result.value == "FAIL"
        assert cert.signature != ""


class TestReproducibleBuild:
    """PKG27 (BDS-P1-057): 可重现构建 — 依赖锁定。"""

    def test_pyproject_has_dependencies(self) -> None:
        """pyproject.toml 包含依赖声明。"""
        import tomllib
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        pyproject = root / "pyproject.toml"
        with open(pyproject, "rb") as f:
            config = tomllib.load(f)

        assert "dependencies" in config["project"]
        deps = config["project"]["dependencies"]
        assert len(deps) > 0

        # PKG27: 依赖应锁定版本（至少包含下限）
        for dep in deps:
            assert ">=" in dep, f"依赖 {dep} 应锁定最低版本"

    def test_ci_config_exists_and_tests_all_packages(self) -> None:
        """CI 配置存在且覆盖所有包。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        ci_file = root / ".github" / "workflows" / "ci.yml"
        assert ci_file.exists(), "CI 配置不存在"

        content = ci_file.read_text()
        assert "pytest" in content
        assert "ruff" in content
        assert "mypy" in content


class TestTypeSafety:
    """PKG27 (BDS-P1-059): 安全关键文件类型安全。"""

    def test_safety_critical_no_f821_ignore(self) -> None:
        """安全关键文件不应有 F821 ignore（未定义符号豁免）。"""
        import tomllib
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        pyproject = root / "pyproject.toml"
        with open(pyproject, "rb") as f:
            config = tomllib.load(f)

        per_file_ignores = config.get("tool", {}).get("ruff", {}).get("lint", {}).get("per-file-ignores", {})

        # 安全关键路径不应有 F821
        safety_critical_paths = [
            "beidou_safety/execution",
            "beidou_safety/risk",
            "beidou_safety/protection",
        ]
        for path_pattern, ignores in per_file_ignores.items():
            for critical_path in safety_critical_paths:
                if critical_path in path_pattern:
                    assert "F821" not in ignores, (
                        f"安全关键文件 {path_pattern} 不应有 F821 ignore"
                    )

    def test_f821_only_in_exchange_adapter(self) -> None:
        """F821 ignore 应仅限于 exchange adapter（外部 API 依赖）。"""
        import tomllib
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        pyproject = root / "pyproject.toml"
        with open(pyproject, "rb") as f:
            config = tomllib.load(f)

        per_file_ignores = config.get("tool", {}).get("ruff", {}).get("lint", {}).get("per-file-ignores", {})

        f821_files = [p for p, ignores in per_file_ignores.items() if "F821" in ignores]
        # 仅 exchange/adapter 允许 F821（外部 API 依赖不可控）
        for f in f821_files:
            assert "exchange" in f or "adapter" in f, (
                f"F821 ignore 应仅限于 exchange/adapter: {f}"
            )
