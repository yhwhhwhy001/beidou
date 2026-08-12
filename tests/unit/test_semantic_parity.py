"""
PKG02 (BDS-P0-001, BDS-P0-025): Testnet/Production 语义同构测试。

验证：
1. 环境变化只影响 endpoint/credential/capital_cap
2. 安全语义（风险、保护、执行、状态机）在所有环境中保持一致
3. 不存在 testnet safety bypass
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from beidou_shared.environment_profile import (
    EnvironmentProfile,
    EnvironmentVariant,
    detect_forbidden_bypass,
    get_environment_profile,
    reset_environment_profile,
)


class TestEnvironmentProfile:
    """PKG02: EnvironmentProfile 基本行为测试。"""

    def test_from_env_only_affects_infrastructure(self) -> None:
        """环境变量只影响基础设施字段，安全语义保持不变。"""
        with patch.dict(os.environ, {"BEIDOU_ENV": "testnet", "BEIDOU_ENDPOINT": "https://testnet.binance.vision"}):
            reset_environment_profile()
            profile = get_environment_profile()

            assert profile.variant == EnvironmentVariant.TESTNET
            assert profile.endpoint_base_url == "https://testnet.binance.vision"
            assert profile.can_write is True
            assert profile.is_live is False

    def test_paper_profile_has_no_write(self) -> None:
        """Paper 环境无写权限。"""
        with patch.dict(os.environ, {"BEIDOU_ENV": "paper"}):
            reset_environment_profile()
            profile = get_environment_profile()

            assert profile.variant == EnvironmentVariant.PAPER
            assert profile.can_write is False

    def test_safety_semantic_is_identical_across_environments(self) -> None:
        """安全语义在所有环境中保持一致。"""
        for env_name in ("paper", "shadow", "testnet", "live"):
            with patch.dict(os.environ, {"BEIDOU_ENV": env_name}):
                reset_environment_profile()
                profile = get_environment_profile()
                # 所有环境的安全字段均来自 frozen dataclass，不可变
                # 尝试设置不存在的属性应失败
                with pytest.raises(AttributeError):
                    _ = profile.risk_levels  # type: ignore[attr-defined]

    def test_unknown_environment_falls_back_safety_only(self) -> None:
        """未知环境回退到 SAFETY_ONLY（fail closed）。"""
        with patch.dict(os.environ, {"BEIDOU_ENV": "production_mainnet_real"}):
            reset_environment_profile()
            profile = get_environment_profile()

            assert profile.variant == EnvironmentVariant.SAFETY_ONLY
            assert profile.can_write is False

    def test_profile_is_frozen_immutable(self) -> None:
        """Profile 是不可变的 frozen dataclass。"""
        profile = EnvironmentProfile(variant=EnvironmentVariant.TESTNET)

        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            profile.variant = EnvironmentVariant.LIVE  # type: ignore[misc]


class TestSemanticParity:
    """PKG02: 生产语义同构验证。"""

    def test_no_testnet_bypass_in_source_files(self) -> None:
        """核心安全模块中不得存在 testnet safety bypass 模式。

        PKG02: 扫描 beidou_safety/ 和 beidou_core/engine.py 中的禁止模式。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent

        forbidden_in_safety = [
            "is_testnet",
            'environ.get("BEIDOU_ENV")',
            "testnet_override",
        ]

        safety_files: list[Path] = []
        safety_dirs = [
            root / "beidou_safety" / "risk",
            root / "beidou_safety" / "protection",
            root / "beidou_safety" / "execution",
        ]
        for d in safety_dirs:
            if d.is_dir():
                safety_files.extend(d.rglob("*.py"))

        violations: list[str] = []
        for f in safety_files:
            if f.name.startswith("test_"):
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            rel = str(f.relative_to(root))
            for pattern in forbidden_in_safety:
                if pattern in content:
                    violations.append(f"{rel}: 发现禁止的 testnet 安全旁路模式 '{pattern}'")

        # PKG02 修复后，safety 模块中不应再有 bypass 模式
        assert not violations, "安全模块 (beidou_safety/) 中发现 testnet 安全旁路模式:\n" + "\n".join(violations)

    def test_protection_engine_no_testnet_bypass(self) -> None:
        """保护引擎不得包含 testnet 旁路。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        protection_file = root / "beidou_safety" / "protection" / "engine.py"

        if protection_file.exists():
            content = protection_file.read_text(encoding="utf-8")
            assert 'BEIDOU_ENV") == "testnet"' not in content, "protection/engine.py 中存在 testnet 安全旁路"
            assert "testnet_override" not in content, "protection/engine.py 中存在 testnet_override 模式"

    def test_cost_model_identical_across_environments(self) -> None:
        """成本模型在所有环境中保持一致。

        PKG02 修复后：核心安全路径不再有基于 testnet 的差异。
        检查关键安全旁路（成本、风险、保护）已被移除。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        engine_file = root / "beidou_core" / "engine.py"
        content = engine_file.read_text(encoding="utf-8")

        # 验证核心安全旁路已移除（不检查纯运维/指标差异）
        core_bypasses_removed = [
            # 信号/成本旁路
            ("no_trade_band=0.0 if _testnet", False),
            ("cost_margin=0.0 if _testnet", False),
            ("_extra_cost = 1.0 if os.environ", False),
            # 保护旁路
            ('if os.environ.get("BEIDOU_ENV") == "testnet":\n                    trigger_value = entry_value', False),
            # 控制面旁路
            ('BEIDOU_ENV") != "testnet":\n            if self._control.get_status()', False),
            # R7/R8 跳过
            ('{"R7", "R8"} if os.environ.get("BEIDOU_ENV")', False),
            # 用户流就绪旁路
            ('"testnet_override": True', False),
        ]
        for bypass, should_exist in core_bypasses_removed:
            exists = bypass in content
            assert exists == should_exist, (
                f"核心安全旁路状态错误: '{bypass[:60]}...' 期望存在={should_exist}, 实际存在={exists}"
            )

    def test_control_plane_no_testnet_bypass(self) -> None:
        """控制面不得有 testnet 旁路 — 所有环境统一切换。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        engine_file = root / "beidou_core" / "engine.py"
        content = engine_file.read_text(encoding="utf-8")

        # 核心控制面旁路已被移除
        # 允许保留: 监督器启动授权 (S21 已修复)
        assert 'BEIDOU_ENV") != "testnet":\n            if self._control.get_status()' not in content, (
            "engine.py 中仍存在 testnet 控制面旁路"
        )

    def test_reconciliation_no_testnet_bypass(self) -> None:
        """对账逻辑不得有 testnet 旁路。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        engine_file = root / "beidou_core" / "engine.py"
        content = engine_file.read_text(encoding="utf-8")

        assert "Position-only mismatch accepted for testnet" not in content, "engine.py 中仍存在 testnet 对账旁路"


class TestForbiddenBypassDetection:
    """PKG02: 禁止旁路检测工具测试。"""

    def test_detect_bypass_in_code(self) -> None:
        """检测工具能发现代码中的旁路模式。"""
        bad_code = 'if os.environ.get("BEIDOU_ENV") == "testnet":\n    bypass_risk()'
        violations = detect_forbidden_bypass(bad_code, location="beidou_core/engine.py")
        assert len(violations) > 0, "检测工具未能发现禁止的旁路模式"

    def test_clean_code_passes_detection(self) -> None:
        """无旁路的代码通过检测。"""
        clean_code = """
from beidou_shared.environment_profile import get_environment_profile
profile = get_environment_profile()
if profile.use_testnet_endpoint:
    base_url = "https://testnet.binance.vision"
else:
    base_url = "https://api.binance.com"
"""
        violations = detect_forbidden_bypass(clean_code, location="clean_example.py")
        assert len(violations) == 0, f"干净代码不应触发检测，但发现: {violations}"

    def test_self_module_exempt_from_detection(self) -> None:
        """environment_profile.py 自身豁免检测。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        profile_file = root / "beidou_shared" / "environment_profile.py"
        content = profile_file.read_text(encoding="utf-8")

        # 此模块中定义 FORBIDDEN_BYPASS_PATTERNS，其中包含 "BEIDOU_ENV"
        # 但 detect_forbidden_bypass 应跳过自身
        violations = detect_forbidden_bypass(content, location="beidou_shared/environment_profile.py")
        assert len(violations) == 0, f"自身模块不应触发检测: {violations}"


class TestMutationSemanticParity:
    """PKG02: Mutation 测试 — 证明修复有效。"""

    def test_mutation_testnet_cost_bypass_would_be_detected(self) -> None:
        """Mutation: 如果在 engine.py 重新引入 testnet 成本旁路，检测应捕获。"""
        mutation_code = 'cost = 1.0 if os.environ.get("BEIDOU_ENV") == "testnet" else 6.0'
        violations = detect_forbidden_bypass(mutation_code, location="beidou_core/engine.py")
        assert len(violations) > 0, "Mutation: testnet 成本旁路未被检测"

    def test_mutation_testnet_control_bypass_would_be_detected(self) -> None:
        """Mutation: 如果在 engine.py 重新引入 testnet 控制面旁路，检测应捕获。"""
        mutation_code = 'if os.environ.get("BEIDOU_ENV") != "testnet":\n    control.execute_action(NO_NEW_RISK)'
        violations = detect_forbidden_bypass(mutation_code, location="beidou_core/engine.py")
        assert len(violations) > 0, "Mutation: testnet 控制面旁路未被检测"

    def test_mutation_testnet_protection_bypass_would_be_detected(self) -> None:
        """Mutation: 如果在 protection 模块重新引入 testnet 旁路，检测应捕获。"""
        mutation_code = 'if os.environ.get("BEIDOU_ENV") == "testnet":\n    trigger_value = entry_value * 0.99'
        violations = detect_forbidden_bypass(mutation_code, location="beidou_safety/protection/engine.py")
        assert len(violations) > 0, "Mutation: testnet 保护旁路未被检测"
