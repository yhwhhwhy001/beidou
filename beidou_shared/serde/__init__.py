"""序列化/反序列化工具。支持 JSON 和 MessagePack 格式。"""
from __future__ import annotations
import json
from datetime import datetime
from typing import Any
import msgpack
from ..envelope import EventEnvelope, EventMetadata

class DatetimeEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

def serialize_json(envelope: EventEnvelope[Any]) -> bytes:
    return json.dumps(envelope.model_dump(mode="json"), cls=DatetimeEncoder, ensure_ascii=False).encode("utf-8")

def deserialize_json(data: bytes | str) -> dict[str, Any]:
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return json.loads(data)

def serialize_msgpack(envelope: EventEnvelope[Any]) -> bytes:
    return msgpack.packb(envelope.model_dump(mode="json"), default=lambda o: o.isoformat() if isinstance(o, datetime) else o)

def deserialize_msgpack(data: bytes) -> dict[str, Any]:
    return msgpack.unpackb(data, raw=False)

def create_metadata(envelope: EventEnvelope[Any]) -> EventMetadata:
    return EventMetadata(
        correlation_id=envelope.correlation_id,
        causation_id=envelope.causation_id,
        source=envelope.source,
        clock_domain=envelope.clock_domain,
        schema_version=envelope.schema_version,
        event_time=envelope.event_time,
        idempotency_key=envelope.idempotency_key,
        sequence_number=envelope.sequence_number,
        replay=envelope.replay,
    )
