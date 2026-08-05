"""
Pytest 全局配置与 fixtures。

提供测试所需的基础设施：临时目录、测试数据、mock 对象等。
测试替身只能位于 tests/ 目录，并标记 TEST_SYNTHETIC。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Generator

import pytest

from beidou_shared.types import (
    AccountId,
    ClockDomain,
    CorrelationId,
    InstrumentId,
    SchemaVersion,
    VenueId,
    VenueInstrument,
)


@pytest.fixture
def synthetic_correlation_id() -> CorrelationId:
    """生成测试用的 Correlation ID。标记为 TEST_SYNTHETIC。"""
    return CorrelationId(f"TEST_SYNTHETIC_{uuid.uuid4().hex[:12]}")


@pytest.fixture
def synthetic_venue_instrument() -> VenueInstrument:
    """生成测试用的 VenueInstrument。标记为 TEST_SYNTHETIC。"""
    return VenueInstrument(
        venue_id=VenueId("TEST_SYNTHETIC_BINANCE"),
        instrument_id=InstrumentId("TEST_SYNTHETIC_BTCUSDT"),
    )


@pytest.fixture
def temp_evidence_dir(tmp_path: Path) -> Generator[Path, None, None]:
    """临时证据目录。"""
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    yield evidence


@pytest.fixture
def schema_version_v2() -> SchemaVersion:
    return SchemaVersion("2.0.0")


@pytest.fixture
def realtime_clock() -> ClockDomain:
    return ClockDomain.REALTIME


@pytest.fixture
def nearline_clock() -> ClockDomain:
    return ClockDomain.NEARLINE


@pytest.fixture
def offline_clock() -> ClockDomain:
    return ClockDomain.OFFLINE
