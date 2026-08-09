"""版本化数据集、Lineage、退市样本与真实 Replay 基线。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from beidou_shared.types import InstrumentId, SchemaVersion


@dataclass(frozen=True, slots=True)
class DatasetVersion:
    dataset_id: str
    version: SchemaVersion
    lineage: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    instrument_ids: frozenset[InstrumentId] = field(default_factory=frozenset)
    sample_count: int = 0
    checksum: str = ""
    is_delisted_aware: bool = True
    parent_version: str | None = None


@dataclass
class DatasetManager:
    """数据集版本管理器。Lineage 追踪，退市样本保留。"""

    def __init__(self) -> None:
        self._datasets: dict[str, list[DatasetVersion]] = {}

    def create_version(self, dataset_id: str, prev_version: SchemaVersion | None, **kwargs: Any) -> DatasetVersion:
        v = DatasetVersion(
            dataset_id=dataset_id,
            version=SchemaVersion(f"{dataset_id}-{len(self._datasets.get(dataset_id, [])) + 1}"),
            parent_version=str(prev_version) if prev_version else None,
            **kwargs,
        )
        if dataset_id not in self._datasets:
            self._datasets[dataset_id] = []
        self._datasets[dataset_id].append(v)
        return v

    def get_latest(self, dataset_id: str) -> DatasetVersion | None:
        versions = self._datasets.get(dataset_id, [])
        return versions[-1] if versions else None

    def get_version(self, dataset_id: str, version: SchemaVersion) -> DatasetVersion | None:
        for v in self._datasets.get(dataset_id, []):
            if v.version == version:
                return v
        return None

    def compute_checksum(self, data: bytes) -> str:
        return sha256(data).hexdigest()
