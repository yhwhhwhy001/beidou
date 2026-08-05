"""事件封套 (Event Envelope)。所有跨层/跨模块通信必须使用此封套。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, Field

from ..types import CausationId, ClockDomain, CorrelationId, SchemaVersion

T = TypeVar("T")


class EventEnvelope(BaseModel, Generic[T]):
    """标准事件封套。用于所有跨模块、跨层的消息传递。"""

    correlation_id: CorrelationId = Field(default_factory=lambda: CorrelationId(str(uuid4())))
    causation_id: CausationId | None = None
    source: str = Field(..., description="发送此事件的服务/模块标识")
    clock_domain: ClockDomain = Field(..., description="产生事件的时钟层")
    schema_version: SchemaVersion = Field(..., description="payload 的 Schema 版本")
    event_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ingestion_time: datetime | None = None
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()))
    sequence_number: int | None = None
    replay: bool = False
    payload: T = Field(..., description="业务载荷")
    model_config = {"frozen": True}


class EventMetadata(BaseModel):
    correlation_id: CorrelationId
    causation_id: CausationId | None = None
    source: str
    clock_domain: ClockDomain
    schema_version: SchemaVersion
    event_time: datetime
    idempotency_key: str
    sequence_number: int | None = None
    replay: bool = False
    content_type: str = "application/json"
    compression: str | None = None
