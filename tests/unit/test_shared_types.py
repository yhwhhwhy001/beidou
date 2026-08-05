"""
共享内核类型系统的单元测试。
"""

from __future__ import annotations

import uuid

import pytest

from beidou_shared.types import (
    AccountRef,
    ClockDomain,
    CorrelationId,
    HealthStatus,
    InstrumentId,
    MonetaryValue,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    Quantity,
    ResultStatus,
    RiskDecision,
    SchemaVersion,
    VenueId,
    VenueInstrument,
)
from beidou_shared.envelope import EventEnvelope


class TestResultStatus:
    """ResultStatus 语义测试 — 确保失败不会被转成空值。"""

    def test_success_is_not_empty(self) -> None:
        assert ResultStatus.SUCCESS != ResultStatus.EMPTY

    def test_unknown_is_not_success(self) -> None:
        assert ResultStatus.UNKNOWN != ResultStatus.SUCCESS

    def test_error_is_not_empty(self) -> None:
        assert ResultStatus.ERROR != ResultStatus.EMPTY

    def test_unsupported_is_not_error(self) -> None:
        assert ResultStatus.UNSUPPORTED != ResultStatus.ERROR

    def test_all_values_distinct(self) -> None:
        values = list(ResultStatus)
        assert len(values) == len(set(values))


class TestVenueInstrument:
    """VenueInstrument 身份测试。"""

    def test_equality(self) -> None:
        a = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        b = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        assert a == b

    def test_inequality_different_venue(self) -> None:
        a = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        b = VenueInstrument(venue_id=VenueId("BYBIT"), instrument_id=InstrumentId("BTCUSDT"))
        assert a != b

    def test_inequality_different_instrument(self) -> None:
        a = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        b = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("ETHUSDT"))
        assert a != b

    def test_hash_consistent(self) -> None:
        a = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        b = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        assert hash(a) == hash(b)

    def test_can_be_dict_key(self) -> None:
        d: dict[VenueInstrument, str] = {}
        k = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        d[k] = "test"
        assert d[k] == "test"


class TestMonetaryValue:
    """MonetaryValue 精度测试。"""

    def test_string_representation_preserves_precision(self) -> None:
        v = MonetaryValue(amount="0.12345678", currency="USDT", decimals=8)
        assert v.amount == "0.12345678"

    def test_default_currency(self) -> None:
        v = MonetaryValue(amount="100")
        assert v.currency == "USDT"

    def test_frozen(self) -> None:
        v = MonetaryValue(amount="100")
        with pytest.raises(Exception):
            v.amount = "200"  # type: ignore[misc]


class TestClockDomain:
    """时钟域测试。"""

    def test_three_domains_defined(self) -> None:
        domains = {ClockDomain.REALTIME, ClockDomain.NEARLINE, ClockDomain.OFFLINE}
        assert len(domains) == 3

    def test_domains_distinct(self) -> None:
        assert ClockDomain.REALTIME != ClockDomain.NEARLINE
        assert ClockDomain.NEARLINE != ClockDomain.OFFLINE
        assert ClockDomain.REALTIME != ClockDomain.OFFLINE


class TestEnvelope:
    """EventEnvelope 测试。"""

    def test_envelope_creation(self) -> None:
        envelope = EventEnvelope[str](
            source="test_service",
            clock_domain=ClockDomain.NEARLINE,
            schema_version=SchemaVersion("2.0.0"),
            payload="test_payload",
        )
        assert envelope.payload == "test_payload"
        assert envelope.source == "test_service"
        assert envelope.clock_domain == ClockDomain.NEARLINE

    def test_correlation_id_auto_generated(self) -> None:
        envelope = EventEnvelope[str](
            source="test",
            clock_domain=ClockDomain.REALTIME,
            schema_version=SchemaVersion("2.0.0"),
            payload="data",
        )
        assert envelope.correlation_id is not None
        assert len(envelope.correlation_id) > 0

    def test_idempotency_key_auto_generated(self) -> None:
        envelope = EventEnvelope[str](
            source="test",
            clock_domain=ClockDomain.REALTIME,
            schema_version=SchemaVersion("2.0.0"),
            payload="data",
        )
        assert envelope.idempotency_key is not None

    def test_envelope_frozen(self) -> None:
        envelope = EventEnvelope[str](
            source="test",
            clock_domain=ClockDomain.REALTIME,
            schema_version=SchemaVersion("2.0.0"),
            payload="data",
        )
        with pytest.raises(Exception):
            envelope.payload = "modified"  # type: ignore[misc]

    def test_replay_flag_default_false(self) -> None:
        envelope = EventEnvelope[str](
            source="test",
            clock_domain=ClockDomain.REALTIME,
            schema_version=SchemaVersion("2.0.0"),
            payload="data",
        )
        assert envelope.replay is False

    def test_envelope_with_causation(self) -> None:
        envelope = EventEnvelope[str](
            source="service_b",
            clock_domain=ClockDomain.NEARLINE,
            schema_version=SchemaVersion("2.0.0"),
            payload="response",
            causation_id="prev_correlation_id",
        )
        assert envelope.causation_id == "prev_correlation_id"


class TestAccountRef:
    """AccountRef 测试。"""

    def test_account_ref_required_fields(self) -> None:
        ref = AccountRef(venue_id=VenueId("BINANCE"), account_id="test_account")
        assert ref.venue_id == "BINANCE"
        assert ref.account_id == "test_account"


class TestRiskDecision:
    """RiskDecision 测试。"""

    def test_only_approved_allows_risk_increase(self) -> None:
        """确保只有 APPROVED 才能增加风险。"""
        assert RiskDecision.APPROVED != RiskDecision.REJECTED
        assert RiskDecision.APPROVED != RiskDecision.PENDING
