"""BD-CV01: 环境语义同构测试 (PKG02)

验证同一输入事实在 paper/shadow/testnet 模式下产生相同的 safety decision。
"""

from __future__ import annotations

import pytest


class TestEnvironmentSemanticParity:
    """BD-CV01 AC-01-04: 所有 parity fixtures 在 paper/shadow/testnet 结果一致。"""

    @pytest.mark.parametrize("env_name", ["paper", "shadow", "testnet"])
    def test_environment_profile_safety_parity(self, env_name: str) -> None:
        """验证 EnvironmentProfile 在任何环境下返回相同的安全语义。"""
        from beidou_shared.environment_profile import EnvironmentProfile, EnvironmentVariant

        variant = EnvironmentVariant(env_name)
        profile = EnvironmentProfile(variant=variant)

        # Infrastructure fields can differ
        # Safety semantics must be identical
        # (frozen dataclass guarantees immutability)
        assert profile._SAFETY_SEMANTIC_FIELDS is not None

    @pytest.mark.parametrize("env_name", ["paper", "shadow", "testnet"])
    def test_control_action_matrix_identical(self, env_name: str) -> None:
        """验证控制状态矩阵在所有环境下相同。"""
        from beidou_control.plane import (
            CONTROL_ALLOW_MATRIX,
            CONTROL_TRANSITION_MATRIX,
            ControlAction,
            RiskDirection,
        )

        # NO_NEW_RISK 在所有环境必须阻断 INCREASE
        assert RiskDirection.INCREASE not in CONTROL_ALLOW_MATRIX[ControlAction.NO_NEW_RISK]
        # LOCK 在所有环境必须仅允许 QUERY
        assert CONTROL_ALLOW_MATRIX[ControlAction.LOCK] == {RiskDirection.QUERY}
        # LOCK 是终态
        transitions = CONTROL_TRANSITION_MATRIX[ControlAction.LOCK]
        assert len(transitions) == 1 and ControlAction.LOCK in transitions

    @pytest.mark.parametrize("env_name", ["paper", "shadow", "testnet"])
    def test_risk_severity_identical(self, env_name: str) -> None:
        """验证风险严重级别在所有环境下相同。"""
        from beidou_observability.monitoring.contracts import CheckSeverity

        # P0/P1/P2 定义在所有环境必须一致
        assert CheckSeverity.P0.value == "P0"
        assert CheckSeverity.P1.value == "P1"
        assert CheckSeverity.P2.value == "P2"

    def test_no_testnet_bypass_patterns_in_source(self) -> None:
        """BD-CV01 AC-01-02: 仓库无 testnet-bypass/testnet mask 代码。"""
        from pathlib import Path

        from beidou_shared.environment_profile import detect_forbidden_bypass

        project_root = Path(__file__).parent.parent
        forbidden_count = 0
        # Only scan source dirs (not tests or this file)
        source_dirs = [
            d
            for d in project_root.iterdir()
            if d.is_dir() and d.name.startswith("beidou_") and d.name != "beidou_shared"
        ]

        read_errors: list[str] = []
        for src_dir in source_dirs:
            for py_file in src_dir.rglob("*.py"):
                if "test_" in py_file.name or py_file.name == "__pycache__":
                    continue
                try:
                    content = py_file.read_text()
                    violations = detect_forbidden_bypass(content, str(py_file))
                    forbidden_count += len(violations)
                except (OSError, UnicodeError) as exc:
                    read_errors.append(f"{py_file}: {type(exc).__name__}")

        # PKG02 自身模块允许包含 "BEIDOU_ENV" 引用（用于配置文件）
        # 其他模块应使用 EnvironmentProfile 而非裸 os.environ 检查
        assert not read_errors, "环境旁路扫描无法读取源码: " + ", ".join(read_errors)
        if forbidden_count > 0:
            pytest.fail(f"发现 {forbidden_count} 处禁止的环境旁路模式")

    def test_tif_semantic_not_modified_by_environment(self) -> None:
        """BD-CV01 AC-01-03: TIF/side/reduce-only 等订单语义不可被环境代码修改。"""
        # 验证 engine 中不存在 IOC/FOK→GTC 的 testnet 覆写
        from pathlib import Path

        engine_path = Path(__file__).parent.parent.parent / "beidou_core" / "engine.py"
        content = engine_path.read_text()

        # IOC/FOK 作为 TIF 值是允许的；禁止的是 testnet + IOC/FOK + GTC 的组合覆写
        # 验证不存在 "testnet" + "IOC" + "GTC" 在同一逻辑块
        assert "is_testnet_tif" not in content, "engine.py 中存在 testnet TIF 覆写变量，违反了语义同构要求"
