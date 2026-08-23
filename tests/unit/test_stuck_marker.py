"""卡死标记: A 类裸露 ≥1800s 写 stuck, 消除时删除; 引擎侧纯函数可测。"""

import json
import os

from beidou_core.engine import AutonomousEngine


class _ExposureStore:
    def __init__(self, exposures: list[dict] | None = None):
        self._rows = list(exposures or [])

    def _records(self, record_type: str) -> list[dict]:
        assert record_type == "protection_exposure"
        return [{"payload": dict(r)} for r in self._rows]


def test_stuck_marker_written_when_exposure_exceeds_threshold(tmp_path):
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _ExposureStore([{
        "symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": "X",
    }])
    state_dir = str(tmp_path)
    ok = engine._update_stuck_marker(now=1000.0 + 1800.0, state_dir=state_dir)
    assert ok is True
    p = os.path.join(state_dir, "stuck")
    assert os.path.exists(p)
    data = json.loads(open(p).read())
    assert data["items"][0]["symbol"] == "BTCUSDT"


def test_stuck_marker_removed_when_exposure_clears(tmp_path):
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _ExposureStore([{
        "symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": "X",
    }])
    state_dir = str(tmp_path)
    p = os.path.join(state_dir, "stuck")
    open(p, "w").write("{}")
    engine._store._rows = []
    engine._update_stuck_marker(now=1000.0, state_dir=state_dir)
    assert not os.path.exists(p)
