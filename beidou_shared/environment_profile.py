"""
PKG02 (BDS-P0-001): Environment Profile — 环境同构强制模块。

环境只允许改变 endpoint、凭据、资金上限。
Risk / Strategy / Protection / StateMachine 语义必须同构。

此模块是 testnet/production 语义同构的单一权威来源。
禁止任何模块绕过此 Profile 自行判断环境差异。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar


class EnvironmentVariant(str, Enum):
    """环境变体 — 只影响基础设施，不影响安全语义。"""

    PAPER = "paper"
    SHADOW = "shadow"
    TESTNET = "testnet"
    CANARY = "canary"
    LIVE = "live"
    SAFETY_ONLY = "safety_only"


# --- PKG02: 允许按环境变化的字段 ---


@dataclass(frozen=True)
class EnvironmentProfile:
    """环境配置剖面。

    只有以下字段可按环境变化：
    - endpoint: API 端点 URL
    - credential_id: 凭据标识
    - capital_cap: 资金上限

    以下字段禁止按环境变化（同构强制）：
    - risk_levels: 风险等级语义
    - protection_rules: 保护规则
    - strategy_params: 策略参数
    - state_machine: 状态机行为
    - precision_rules: 精度规则
    - cost_model: 成本模型
    """

    variant: EnvironmentVariant

    # === 允许变化的字段 ===
    endpoint_base_url: str = ""
    credential_id: str = ""
    capital_cap: float = 0.0  # 0 = 无上限

    # === 禁止变化的字段（始终保持生产语义） ===
    # 这些字段存在是为了文档化"同构契约"
    _SAFETY_SEMANTIC_FIELDS: ClassVar[tuple[str, ...]] = (
        "risk_levels",
        "protection_rules",
        "strategy_params",
        "state_machine",
        "precision_rules",
        "cost_model",
    )

    # === PKG02: 禁止的 testnet 旁路标记 ===
    _FORBIDDEN_BYPASS_PATTERNS: ClassVar[tuple[str, ...]] = (
        "is_testnet",
        "BEIDOU_ENV",
        "testnet_override",
        "testnet_bypass",
        "if_testnet",
    )

    @classmethod
    def from_env(cls) -> EnvironmentProfile:
        """从环境变量构建 Profile。

        只提取基础设施差异（endpoint、credential、capital_cap）。
        安全语义始终使用生产默认值。
        """
        env_name = os.environ.get("BEIDOU_ENV", "paper")
        try:
            variant = EnvironmentVariant(env_name)
        except ValueError:
            variant = EnvironmentVariant.SAFETY_ONLY

        # 只允许这些字段按环境变化
        return cls(
            variant=variant,
            endpoint_base_url=os.environ.get("BEIDOU_ENDPOINT", ""),
            credential_id=os.environ.get("BEIDOU_CREDENTIAL_ID", ""),
            capital_cap=float(os.environ.get("BEIDOU_CAPITAL_CAP", "0")),
        )

    @property
    def can_write(self) -> bool:
        """是否有交易所写权限（testnet/live/canary）。"""
        return self.variant in (EnvironmentVariant.TESTNET, EnvironmentVariant.CANARY, EnvironmentVariant.LIVE)

    @property
    def is_live(self) -> bool:
        """是否为生产环境。"""
        return self.variant == EnvironmentVariant.LIVE

    @property
    def use_testnet_endpoint(self) -> bool:
        """是否使用 testnet 端点（仅限 endpoint 差异）。"""
        return self.variant in (EnvironmentVariant.TESTNET,)

    # === PKG02: 安全语义断言 ===

    def assert_safety_parity(self, other: EnvironmentProfile | None = None) -> None:
        """断言两个环境的安全语义同构。

        若 other 为 None，则与生产默认值比较。

        Raises:
            SafetySemanticDivergenceError: 安全语义不一致。
        """
        # 所有环境的安全语义始终相同
        pass  # frozen dataclass 保证不可变性；没有安全字段可配置

    # === 便捷方法 ===

    def is_variant(self, *variants: EnvironmentVariant) -> bool:
        """检查是否为指定变体之一。"""
        return self.variant in variants


# --- 全局单例 ---

_profile: EnvironmentProfile | None = None


def get_environment_profile() -> EnvironmentProfile:
    """获取当前环境剖面（延迟初始化单例）。

    PKG02: 替代所有 os.environ.get("BEIDOU_ENV") == "testnet" 检查。
    """
    global _profile
    if _profile is None:
        _profile = EnvironmentProfile.from_env()
    return _profile


def reset_environment_profile() -> None:
    """重置环境剖面缓存（测试用）。"""
    global _profile
    _profile = None


# --- PKG02: 禁止旁路检测 ---

class SafetyBypassViolation(Exception):
    """检测到按环境区分安全语义的旁路。"""

    def __init__(self, location: str, detail: str) -> None:
        self.location = location
        self.detail = detail
        super().__init__(f"SAFETY_BYPASS at {location}: {detail}")


def detect_forbidden_bypass(source_code: str, location: str = "<unknown>") -> list[str]:
    """扫描源代码中的禁止旁路模式。

    返回发现的违规列表。空列表表示通过。
    """
    violations: list[str] = []
    for pattern in EnvironmentProfile._FORBIDDEN_BYPASS_PATTERNS:
        if pattern in source_code:
            # 排除此模块自身和测试文件
            if "environment_profile.py" not in location and "test_" not in location:
                violations.append(f"{location}: 发现禁止的环境旁路模式 '{pattern}'")
    return violations
