"""卡死标记: A 类裸露 ≥1800s 写 stuck, 消除时删除并写墓碑; 引擎侧纯函数可测。"""

import json
import os
from pathlib import Path

from beidou_core.engine import AutonomousEngine


class _ExposureStore:
    def __init__(self, exposures: list[dict] | None = None):
        self._rows = list(exposures or [])

    def _records(self, record_type: str) -> list[dict]:
        assert record_type == "protection_exposure"
        return [{"payload": dict(r)} for r in self._rows]


class _BareExposureStore:
    """生产 PG store 形状: _records 返回裸 payload 行 (无 payload 包裹)。

    测试替身默认只覆盖 wrapped 形状, R11 叠加缺陷正是只测 wrapped 导致
    脏裸行抛穿 float() 未被发现 —— 用裸形状替身钉住生产路径。
    """

    def __init__(self, exposures: list[dict] | None = None):
        self._rows = list(exposures or [])

    def _records(self, record_type: str) -> list[dict]:
        assert record_type == "protection_exposure"
        return [dict(r) for r in self._rows]


def test_stuck_marker_written_when_exposure_exceeds_threshold(tmp_path):
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _ExposureStore(
        [
            {
                "symbol": "BTCUSDT",
                "unprotectable_since": 1000.0,
                "last_reason": "X",
            }
        ]
    )
    state_dir = str(tmp_path)
    ok = engine._update_stuck_marker(now=1000.0 + 1800.0, state_dir=state_dir)
    assert ok is True
    p = os.path.join(state_dir, "stuck")
    assert os.path.exists(p)
    data = json.loads(Path(p).read_text(encoding="utf-8"))
    assert data["items"][0]["symbol"] == "BTCUSDT"


def test_stuck_marker_removed_when_exposure_clears(tmp_path):
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _ExposureStore(
        [
            {
                "symbol": "BTCUSDT",
                "unprotectable_since": 1000.0,
                "last_reason": "X",
            }
        ]
    )
    state_dir = str(tmp_path)
    p = os.path.join(state_dir, "stuck")
    Path(p).write_text("{}", encoding="utf-8")
    engine._store._rows = []
    engine._update_stuck_marker(now=1000.0, state_dir=state_dir)
    assert not os.path.exists(p)


def test_stuck_marker_writes_tombstone_on_clear(tmp_path):
    """R11: 引擎主动清除 (stuck 为空且此前存在 stuck 文件) → 写 stuck.cleared
    墓碑 {"cleared_at": ts} 并删除 stuck; watchdog 据此区分正常清除与冻结。"""
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _ExposureStore(
        [
            {
                "symbol": "BTCUSDT",
                "unprotectable_since": 1000.0,
                "last_reason": "X",
            }
        ]
    )
    state_dir = str(tmp_path)
    p = os.path.join(state_dir, "stuck")
    Path(p).write_text("{}", encoding="utf-8")
    engine._store._rows = []
    engine._update_stuck_marker(now=1500.0, state_dir=state_dir)
    assert not os.path.exists(p)
    cleared = os.path.join(state_dir, "stuck.cleared")
    assert os.path.exists(cleared)
    data = json.loads(Path(cleared).read_text(encoding="utf-8"))
    assert data["cleared_at"] == 1500.0


def test_stuck_marker_no_tombstone_when_no_marker_existed(tmp_path):
    """无 stuck 文件存在时 (从未卡死) 不写墓碑 —— 墓碑只在清除既有标记时写。"""
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _ExposureStore([])
    state_dir = str(tmp_path)
    engine._update_stuck_marker(now=1000.0, state_dir=state_dir)
    assert not os.path.exists(os.path.join(state_dir, "stuck"))
    assert not os.path.exists(os.path.join(state_dir, "stuck.cleared"))


def test_stuck_marker_dirty_bare_row_skipped_healthy_row_written(tmp_path):
    """R11: 生产 PG 返回裸 payload 行 —— 一条脏裸行 (非数值 unprotectable_since)
    不得让 float() 抛穿标记写出 (否则 mtime 过期 → watchdog 误判冻结删标记,
    两个 fail-open 边同链); 健康行仍须写出标记。"""
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _BareExposureStore(
        [
            {"symbol": "DIRTY", "unprotectable_since": "not-a-number", "last_reason": "X"},
            {"symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": "X"},
        ]
    )
    state_dir = str(tmp_path)
    ok = engine._update_stuck_marker(now=1000.0 + 1800.0, state_dir=state_dir)
    assert ok is True
    p = os.path.join(state_dir, "stuck")
    assert os.path.exists(p)
    data = json.loads(Path(p).read_text(encoding="utf-8"))
    assert [i["symbol"] for i in data["items"]] == ["BTCUSDT"]
    assert data["items"][0]["age_s"] == 1800.0
