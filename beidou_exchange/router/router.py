"""ExchangeRouter 与 CapabilityMatrix 实现。"""

from __future__ import annotations

from dataclasses import dataclass, field

from beidou_shared.types import InstrumentId, ResultStatus, VenueId

from ..core.protocol import Capability, ExchangeAdapter, VenueInstrument


@dataclass
class CapabilityMatrix:
    """交易所能力矩阵 — 查询哪些交易所支持哪些能力和品种。"""

    _venue_capabilities: dict[VenueId, frozenset[Capability]] = field(default_factory=dict)
    _venue_instruments: dict[VenueId, frozenset[InstrumentId]] = field(default_factory=dict)
    _instrument_to_venues: dict[InstrumentId, set[VenueId]] = field(default_factory=dict)

    def register_venue(
        self, venue_id: VenueId, capabilities: frozenset[Capability], instruments: frozenset[InstrumentId]
    ) -> None:
        self._venue_capabilities[venue_id] = capabilities
        self._venue_instruments[venue_id] = instruments
        for inst in instruments:
            if inst not in self._instrument_to_venues:
                self._instrument_to_venues[inst] = set()
            self._instrument_to_venues[inst].add(venue_id)

    def venue_supports(self, venue_id: VenueId, capability: Capability) -> ResultStatus:
        caps = self._venue_capabilities.get(venue_id)
        if caps is None:
            return ResultStatus.UNKNOWN
        if capability not in caps:
            return ResultStatus.UNSUPPORTED
        return ResultStatus.SUCCESS

    def instrument_available_on(self, instrument_id: InstrumentId) -> frozenset[VenueId]:
        return frozenset(self._instrument_to_venues.get(instrument_id, set()))

    def venue_has_instrument(self, venue_id: VenueId, instrument_id: InstrumentId) -> bool:
        instruments = self._venue_instruments.get(venue_id, frozenset())
        return instrument_id in instruments

    def get_venue_capabilities(self, venue_id: VenueId) -> frozenset[Capability]:
        return self._venue_capabilities.get(venue_id, frozenset())


class ExchangeRouter:
    """交易所路由器 — 根据品种、能力和账户路由请求。"""

    def __init__(self, capability_matrix: CapabilityMatrix) -> None:
        self._matrix = capability_matrix
        self._adapters: dict[VenueId, ExchangeAdapter] = {}

    def register_adapter(self, adapter: ExchangeAdapter) -> None:
        if adapter.venue_id in self._adapters:
            raise ValueError(f"Adapter for venue {adapter.venue_id} already registered")
        self._adapters[adapter.venue_id] = adapter
        self._matrix.register_venue(
            adapter.venue_id,
            adapter.capabilities,
            frozenset(),  # instruments filled by specific adapter
        )

    def get_adapter(self, venue_id: VenueId) -> ExchangeAdapter | None:
        return self._adapters.get(venue_id)

    def resolve(self, venue_instrument: VenueInstrument, capability: Capability) -> ResultStatus:
        adapter = self._adapters.get(venue_instrument.venue_id)
        if adapter is None:
            return ResultStatus.UNKNOWN
        status = self._matrix.venue_supports(venue_instrument.venue_id, capability)
        if status != ResultStatus.SUCCESS:
            return status
        return ResultStatus.SUCCESS

    def get_best_venue(self, instrument_id: InstrumentId, capability: Capability) -> VenueId | None:
        venues = self._matrix.instrument_available_on(instrument_id)
        for venue_id in venues:
            if self._matrix.venue_supports(venue_id, capability) == ResultStatus.SUCCESS:
                return venue_id
        return None
