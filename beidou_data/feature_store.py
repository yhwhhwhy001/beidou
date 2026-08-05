"""在线/离线统一特征图、时间语义与特征仓。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from beidou_shared.types import InstrumentId, SchemaVersion, VenueId


@dataclass(frozen=True, slots=True)
class FeatureVector:
    name: str
    values: dict[str, float]
    timestamp: datetime
    instrument_id: InstrumentId
    venue_id: VenueId
    version: SchemaVersion
    feature_metadata: dict[str, str] = field(default_factory=dict)


class FeatureStore:
    """统一特征仓。在线/离线一致的时间语义。"""

    def __init__(self) -> None:
        self._features: dict[str, list[FeatureVector]] = {}

    def _key(self, name: str, instrument_id: InstrumentId) -> str:
        return f"{name}:{instrument_id}"

    def store(self, fv: FeatureVector) -> None:
        key = self._key(fv.name, fv.instrument_id)
        if key not in self._features:
            self._features[key] = []
        self._features[key].append(fv)

    def get_latest(self, name: str, instrument_id: InstrumentId) -> FeatureVector | None:
        key = self._key(name, instrument_id)
        vals = self._features.get(key, [])
        return vals[-1] if vals else None

    def get_range(self, name: str, instrument_id: InstrumentId, start: datetime, end: datetime) -> list[FeatureVector]:
        key = self._key(name, instrument_id)
        return [v for v in self._features.get(key, []) if start <= v.timestamp <= end]

    def check_consistency(self, name: str, instrument_id: InstrumentId) -> bool:
        """验证在线/离线特征一致性。"""
        vals = self._features.get(self._key(name, instrument_id), [])
        versions = {v.version for v in vals}
        return len(versions) <= 1
