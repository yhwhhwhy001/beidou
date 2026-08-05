"""能力注册与契约版本协商。不兼容版本不能加入生产消费组。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from beidou_shared.types import SchemaVersion


class CompatibilityResult(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    BACKWARD_COMPATIBLE = "BACKWARD_COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class CapabilityVersion:
    capability_name: str
    schema_version: SchemaVersion
    min_compatible_version: SchemaVersion
    contract_hash: str


@dataclass
class CapabilityNegotiation:
    consumer_version: CapabilityVersion
    provider_version: CapabilityVersion | None
    result: CompatibilityResult = CompatibilityResult.UNKNOWN
    detail: str = ""

    def can_join_consumer_group(self) -> bool:
        return self.result in (CompatibilityResult.COMPATIBLE, CompatibilityResult.BACKWARD_COMPATIBLE)


class CapabilityRegistry:
    """能力注册中心 — 管理模块能力发现和契约版本协商。"""

    def __init__(self) -> None:
        self._capabilities: dict[str, dict[SchemaVersion, CapabilityVersion]] = {}

    def register_provider(self, module: str, version: CapabilityVersion) -> None:
        if module not in self._capabilities:
            self._capabilities[module] = {}
        self._capabilities[module][version.schema_version] = version

    def get_provider_versions(self, module: str) -> list[CapabilityVersion]:
        return list(self._capabilities.get(module, {}).values())

    def negotiate(self, module: str, consumer_version: CapabilityVersion) -> CapabilityNegotiation:
        providers = self.get_provider_versions(module)
        if not providers:
            return CapabilityNegotiation(
                consumer_version=consumer_version,
                provider_version=None,
                result=CompatibilityResult.UNKNOWN,
                detail="No provider found",
            )

        for provider in sorted(providers, key=lambda v: v.schema_version, reverse=True):
            if provider.schema_version == consumer_version.schema_version:
                return CapabilityNegotiation(
                    consumer_version=consumer_version,
                    provider_version=provider,
                    result=CompatibilityResult.COMPATIBLE,
                    detail="Exact match",
                )
            # Provider must be >= consumer and provider's min_compatible <= consumer
            if (
                provider.schema_version >= consumer_version.schema_version
                and provider.min_compatible_version <= consumer_version.schema_version
            ):
                return CapabilityNegotiation(
                    consumer_version=consumer_version,
                    provider_version=provider,
                    result=CompatibilityResult.BACKWARD_COMPATIBLE,
                    detail=f"Provider {provider.schema_version} compatible with consumer {consumer_version.schema_version}",
                )

        return CapabilityNegotiation(
            consumer_version=consumer_version,
            provider_version=providers[0],
            result=CompatibilityResult.INCOMPATIBLE,
            detail="No compatible version found",
        )

    def register_capability(
        self, name: str, schema_version: SchemaVersion, min_compatible_version: SchemaVersion
    ) -> None:
        """便捷注册 — 创建 CapabilityVersion 并注册为 provider。"""
        from hashlib import sha256

        contract = f"{name}:{schema_version}:{min_compatible_version}"
        cv = CapabilityVersion(
            capability_name=name,
            schema_version=schema_version,
            min_compatible_version=min_compatible_version,
            contract_hash=sha256(contract.encode()).hexdigest()[:16],
        )
        self.register_provider(name, cv)

    def check_compatibility(self, name: str, consumer_version: SchemaVersion) -> CompatibilityResult:
        """便捷兼容性检查 — 以 consumer schema_version 协商。"""
        from hashlib import sha256

        contract = f"{name}:{consumer_version}:{SchemaVersion('0.0.0')}"
        consumer_cv = CapabilityVersion(
            capability_name=name,
            schema_version=consumer_version,
            min_compatible_version=consumer_version,
            contract_hash=sha256(contract.encode()).hexdigest()[:16],
        )
        return self.negotiate(name, consumer_cv).result
