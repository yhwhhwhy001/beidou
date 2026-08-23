"""protection_exposure 记录: 重启不清除、代数变化清零、E2 需跨度≥60s。"""

import time

from beidou_core.engine import AutonomousEngine


class _MemStore:
    """最小 durable 替身: 记录活在其内部 dict, 模拟跨重启保留。"""

    def __init__(self):
        self.records: dict[tuple[str, str], dict] = {}

    def _get_record(self, record_type: str, record_id: str):
        return self.records.get((record_type, record_id))

    def _write_record(self, record_type: str, record_id: str, payload: dict, **kw) -> bool:
        self.records[(record_type, record_id)] = dict(payload)
        return True

    def _delete_record(self, record_type: str, record_id: str) -> None:
        self.records.pop((record_type, record_id), None)


def _engine() -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _MemStore()
    engine._position_generation = {"BTCUSDT": 5}
    engine._sl_unprotectable_streak = {}
    engine._policy_id_active = "pol-1"
    engine._policy_version = "v1"
    engine._policy_signature = "sig"
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008"}}
    return engine


def test_exposure_persists_and_survives_reinstantiation() -> None:
    engine = _engine()
    rec = engine._persist_protection_exposure("BTCUSDT", "VENUE_REJECT_-2021", now=time.time)
    assert rec["attempts"] == 1 and rec["position_generation"] == 5

    # 模拟重启: 新实例共享同一 store
    engine2 = _engine()
    engine2._store = engine._store
    rec2 = engine2._persist_protection_exposure("BTCUSDT", "VENUE_REJECT_-2021", now=time.time)
    assert rec2["attempts"] == 2
    assert rec2["unprotectable_since"] == rec["unprotectable_since"]


def test_exposure_cleared_on_generation_change() -> None:
    engine = _engine()
    engine._persist_protection_exposure("BTCUSDT", "X", now=time.time)
    engine._position_generation["BTCUSDT"] = 6
    rec = engine._persist_protection_exposure("BTCUSDT", "Y", now=time.time)
    assert rec["attempts"] == 1 and rec["position_generation"] == 6
    assert rec["last_reason"] == "Y"


def test_clear_removes_record() -> None:
    engine = _engine()
    engine._persist_protection_exposure("BTCUSDT", "X", now=time.time)
    engine._clear_protection_exposure("BTCUSDT")
    assert engine._store._get_record("protection_exposure", "exposure:BTCUSDT") is None
