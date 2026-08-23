"""E3 慢引信: 纯卡死 ≥7200s 才平仓; testnet 默认开, live/canary 默认关。"""

from beidou_core.engine import AutonomousEngine


class _ExposureStore:
    def __init__(self, rows: list[dict] | None = None):
        self._rows = list(rows or [])

    def _records(self, record_type: str) -> list[dict]:
        return [{"payload": dict(r)} for r in self._rows]

    def _delete_record(self, record_type: str, record_id: str) -> None:
        self._rows = [r for r in self._rows if r.get("symbol") != record_id.split(":", 1)[-1]]


class _EnvMode:
    value = "testnet"


def _engine(rows, mode="testnet") -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _ExposureStore(rows)
    engine._env_mode = _EnvMode() if mode == "testnet" else type("M", (), {"value": "live"})()
    engine._position_generation = {}
    engine._position_projection = {}  # 测试默认空; 触发用例需显式给 signed_quantity
    engine._policy_id_active = "p"
    engine._policy_version = "v"
    engine._policy_signature = "s"
    engine._sl_unprotectable_streak = {}
    engine._slow_fuse_fired = []
    async def _fake_close(pos_id, symbol, pp, **kw):
        engine._slow_fuse_fired.append(symbol)
        return True
    engine._maybe_emergency_close_unprotectable = _fake_close
    return engine


def test_slow_fuse_does_not_fire_before_7200s():
    engine = _engine([{"symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": ""}])
    import asyncio
    n = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7199.0))
    assert n == 0 and not engine._slow_fuse_fired


def test_slow_fuse_fires_after_7200s_on_testnet():
    engine = _engine([{"symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": ""}])
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008"}}
    import asyncio
    n = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0))
    assert n == 1 and engine._slow_fuse_fired == ["BTCUSDT"]


def test_slow_fuse_off_by_default_in_live():
    engine = _engine([{"symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": ""}], mode="live")
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008"}}
    import asyncio
    n = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0))
    assert n == 0


def test_slow_fuse_skips_when_projection_missing():
    engine = _engine([{"symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": ""}])
    import asyncio
    n = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0))
    assert n == 0  # 方向不可知 → 不动作 (fail-closed)


def test_explicit_rejection_hands_off_to_e2_not_slow_fuse():
    # 显式拒绝 (-2021 等) 走 E2 快路径, 慢引信不再重复处置
    engine = _engine([{"symbol": "BTCUSDT", "unprotectable_since": 1000.0,
                       "last_reason": "SL_UNPROTECTABLE"}])
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008"}}
    import asyncio
    n = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0))
    assert n == 0
