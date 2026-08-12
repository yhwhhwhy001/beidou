"""版本化契约定义。所有模块间接口必须以版本化契约定义。"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ..types import SchemaVersion


class ContractCompatibility(str, Enum):
    BACKWARD = "BACKWARD"
    FORWARD = "FORWARD"
    FULL = "FULL"
    BREAKING = "BREAKING"


class ContractRegistration(BaseModel):
    contract_name: str
    schema_version: SchemaVersion
    previous_version: SchemaVersion | None = None
    compatibility: ContractCompatibility = ContractCompatibility.BACKWARD
    deprecation_date: datetime | None = None
    removal_date: datetime | None = None
    migration_script: str | None = None
    migration_description: str | None = None


class ContractRegistry:
    def __init__(self) -> None:
        self._contracts: dict[str, list[ContractRegistration]] = {}

    def register(self, registration: ContractRegistration) -> None:
        name = registration.contract_name
        if name not in self._contracts:
            self._contracts[name] = []
        existing_versions = {r.schema_version for r in self._contracts[name]}
        if registration.schema_version in existing_versions:
            raise ValueError(f"Contract {name} version {registration.schema_version} already registered")
        self._contracts[name].append(registration)

    def get_latest(self, contract_name: str) -> ContractRegistration | None:
        entries = self._contracts.get(contract_name, [])
        if not entries:
            return None
        return max(entries, key=lambda r: _schema_version_key(r.schema_version))

    def get_version(self, contract_name: str, version: SchemaVersion) -> ContractRegistration | None:
        for entry in self._contracts.get(contract_name, []):
            if entry.schema_version == version:
                return entry
        return None

    def list_contracts(self) -> dict[str, list[SchemaVersion]]:
        return {
            name: sorted(
                [r.schema_version for r in regs],
                key=_schema_version_key,
            )
            for name, regs in self._contracts.items()
        }


def _schema_version_key(version: SchemaVersion) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower()) for part in re.findall(r"\d+|[A-Za-z]+", str(version))
    )


_global_registry: ContractRegistry | None = None


def get_contract_registry() -> ContractRegistry:
    global _global_registry
    if _global_registry is None:
        _global_registry = ContractRegistry()
    return _global_registry
