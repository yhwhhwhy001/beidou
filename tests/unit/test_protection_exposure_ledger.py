"""protection_exposure 记录: 重启不清除、代数变化清零、E2 需跨度≥60s。"""

import time
from types import SimpleNamespace

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


def test_startup_backfills_generation_from_projection() -> None:
    """启动代数回填: 裸仓品种 (无 ACTIVE 保护行) 从投影行恢复代数。"""
    engine = _engine()
    engine._position_generation = {}
    engine._position_projection = {
        "BTCUSDT": {"signed_quantity": "-0.0008", "position_generation": 5},
        "ETHUSDT": {"signed_quantity": "0", "position_generation": 3},
        "NOGEN": {"signed_quantity": "1.0"},
    }
    engine._backfill_position_generation_from_projection()
    assert engine._position_generation["BTCUSDT"] == 5
    assert engine._position_generation["ETHUSDT"] == 3
    # 投影行无代数 → 不回填 (保持缺失, 交由后续代数解析)。
    assert "NOGEN" not in engine._position_generation


def test_exposure_survives_restart_with_backfilled_generation() -> None:
    """重启实例代数从投影回填而非归 0 → 记录不清除、attempts 递增。"""
    engine = _engine()
    rec = engine._persist_protection_exposure("BTCUSDT", "VENUE_REJECT_-2021", now=time.time)
    # 模拟重启: 启动时无 ACTIVE 保护行可恢复代数, 但投影行有代数 → 回填。
    engine2 = _engine()
    engine2._store = engine._store
    engine2._position_generation = {}
    engine2._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008", "position_generation": 5}}
    engine2._backfill_position_generation_from_projection()
    rec2 = engine2._persist_protection_exposure("BTCUSDT", "VENUE_REJECT_-2021", now=time.time)
    assert rec2["attempts"] == 2
    assert rec2["position_generation"] == 5
    assert rec2["unprotectable_since"] == rec["unprotectable_since"]


def test_active_protection_confirmation_clears_exposure() -> None:
    """保护 ACTIVE 确认落库 → 清除裸露时钟 (陈年记录不得污染下轮裸仓)。"""
    engine = _engine()
    engine._persist_protection_exposure("BTCUSDT", "VENUE_REJECT_-2021", now=time.time)
    engine._protection_owner_id = "owner-1"
    engine._session_id = "s1"
    engine._store.save_protection = lambda **kwargs: None
    p_order = SimpleNamespace(
        protection_id="sl-1",
        position_id="pos-1",
        instrument_id="BTCUSDT",
        side=SimpleNamespace(value="SELL"),
        trigger_price=SimpleNamespace(amount="60000"),
        order_price=None,
        quantity=SimpleNamespace(amount="0.0008"),
        order_type="STOP_MARKET",
        stop_type=SimpleNamespace(value="ATR_BASED"),
        take_profit_type=None,
        owner_id="owner-1",
        position_generation=5,
        session_id="s1",
        exchange_order_id="1000000001",
    )
    engine._persist_protection_order(p_order, status="ACTIVE")
    assert engine._store._get_record("protection_exposure", "exposure:BTCUSDT") is None


def test_projection_zero_clears_exposure() -> None:
    """持仓归零 → 清除裸露时钟 (旧仓的账不得杀新仓)。"""
    engine = _engine()
    engine._persist_protection_exposure("BTCUSDT", "VENUE_REJECT_-2021", now=time.time)
    engine._store.save_position_projection = lambda *args, **kwargs: None
    # 完全平仓: 数量归零 (short 0.0008 被 BUY 0.0008 平掉)。
    engine._update_position_projection("BTCUSDT", "BUY", delta_qty=0.0008, price=60000, source_event_id="evt-1")
    assert engine._store._get_record("protection_exposure", "exposure:BTCUSDT") is None


def test_gap_refresh_without_increment_keeps_attempts_and_since() -> None:
    """R9: 纯卡死缺口刷新 — increment=False 创建 attempts=1, 刷新不膨胀、
    since 不重置 (保留原始裸露起点), last_reason 仅在值不同时更新。"""
    engine = _engine()
    first = engine._persist_protection_exposure(
        "BTCUSDT", "STOP_LOSS_QUANTITY_UNCOVERED", now=time.time, increment=False
    )
    assert first["attempts"] == 1
    since = first["unprotectable_since"]
    # 30s 后同 reason 刷新: attempts 仍 1, since 不重置
    later = engine._persist_protection_exposure(
        "BTCUSDT",
        "STOP_LOSS_QUANTITY_UNCOVERED",
        now=lambda: time.time() + 30,
        increment=False,
    )
    assert later["attempts"] == 1
    assert later["unprotectable_since"] == since
    assert later["last_reason"] == "STOP_LOSS_QUANTITY_UNCOVERED"
    # reason 变化 → 更新 last_reason, 但 attempts/since 仍不动
    changed = engine._persist_protection_exposure(
        "BTCUSDT",
        "VENUE_POSITION_QUANTITY_UNKNOWN",
        now=lambda: time.time() + 60,
        increment=False,
    )
    assert changed["attempts"] == 1
    assert changed["unprotectable_since"] == since
    assert changed["last_reason"] == "VENUE_POSITION_QUANTITY_UNKNOWN"


def test_e2_increment_true_unchanged_after_gap_records() -> None:
    """R9: E2 路径 increment=True 行为不变 — gap 记录之上继续累计连击确认,
    并以 SL_UNPROTECTABLE 覆盖 reason (E3 随即交接 E2, 语义闭环)。"""
    engine = _engine()
    engine._persist_protection_exposure("BTCUSDT", "STOP_LOSS_QUANTITY_UNCOVERED", now=time.time, increment=False)
    rec = engine._persist_protection_exposure("BTCUSDT", "SL_UNPROTECTABLE", now=time.time)
    assert rec["attempts"] == 2
    assert rec["last_reason"] == "SL_UNPROTECTABLE"
    # E2 再次确认 → attempts 继续累计 (既有 3 连确认语义不变)
    rec2 = engine._persist_protection_exposure("BTCUSDT", "SL_UNPROTECTABLE", now=time.time)
    assert rec2["attempts"] == 3
