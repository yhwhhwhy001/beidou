"""M11-R2: 对抗审查反例回归 —— S33 止损持久化代数的完整性与补写队列。

审查 CONFIRMED_BUG: M11-F02 把 position_generation=0 的 S33 重建止损
持久化 → 重启投影恢复硬阻断(engine.py:3990)+ 覆盖门恒跳过(2923)
→ NO_NEW_RISK 死锁。以及 persist 失败静默吞掉 → 内存/PG 分叉。
"""

from __future__ import annotations

from types import SimpleNamespace

from beidou_core.engine import AutonomousEngine


def _engine() -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._position_generation = {}
    return engine


class TestResolvePositionGeneration:
    """S33 重建代数的解析接缝。"""

    def test_inherits_projection_generation(self):
        engine = _engine()
        pp = SimpleNamespace(position_generation=5)
        assert engine._resolve_position_generation("BTCUSDT", pp) == 5

    def test_falls_back_to_engine_tracking(self):
        engine = _engine()
        engine._position_generation["BTCUSDT"] = 3
        pp = SimpleNamespace(position_generation=0)
        assert engine._resolve_position_generation("BTCUSDT", pp) == 3

    def test_allocates_new_generation_when_both_missing(self):
        engine = _engine()
        pp = SimpleNamespace(position_generation=0)
        gen = engine._resolve_position_generation("BTCUSDT", pp)
        assert gen >= 1
        assert engine._position_generation["BTCUSDT"] == gen

    def test_never_returns_zero(self):
        engine = _engine()
        pp = SimpleNamespace(position_generation=0)
        for _ in range(3):
            gen = engine._resolve_position_generation("BTCUSDT", pp)
            assert gen >= 1


class TestFlushPendingProtectionPersist:
    """persist 失败补写队列 —— 内存 ACTIVE 保留,每轮重试持久化。"""

    def test_flush_retries_and_clears_on_success(self):
        from beidou_safety.protection.engine import ProtectionStatus

        engine = _engine()
        calls: list[str] = []
        failures = {"remaining": 1}

        def _persist(p_order, status=None):
            if failures["remaining"] > 0:
                failures["remaining"] -= 1
                raise OSError("pg down")
            calls.append(p_order.protection_id)

        engine._persist_protection_order = _persist
        p_order = SimpleNamespace(
            protection_id="sl-pos-1",
            status=ProtectionStatus.ACTIVE,
        )
        engine._pending_protection_persist = {"pos-1": [p_order]}

        engine._flush_pending_protection_persist()  # 第一次失败,保留队列
        assert "pos-1" in engine._pending_protection_persist
        assert calls == []

        engine._flush_pending_protection_persist()  # 第二次成功,清空
        assert "pos-1" not in engine._pending_protection_persist
        assert calls == ["sl-pos-1"]

    def test_flush_skips_non_active_orders(self):
        from beidou_safety.protection.engine import ProtectionStatus

        engine = _engine()
        persisted: list[str] = []

        def _persist(p_order, status=None):
            persisted.append(p_order.protection_id)

        engine._persist_protection_order = _persist
        created = SimpleNamespace(protection_id="sl-created", status=ProtectionStatus.CREATED)
        engine._pending_protection_persist = {"pos-1": [created]}
        engine._flush_pending_protection_persist()
        assert persisted == []  # 非 ACTIVE 不补写
        assert "pos-1" not in engine._pending_protection_persist
