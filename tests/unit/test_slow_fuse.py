"""E3 慢引信: 纯卡死 ≥7200s 才平仓; testnet 默认开, live/canary 默认关。"""

from beidou_core.engine import AutonomousEngine


class _ExposureStore:
    def __init__(self, rows: list[dict] | None = None):
        self._rows = list(rows or [])
        self._records_by_key: dict[str, dict] = {}

    def _records(self, record_type: str) -> list[dict]:
        return [{"payload": dict(r)} for r in self._rows]

    def _get_record(self, record_type: str, record_id: str):
        return self._records_by_key.get(record_id)

    def _write_record(self, record_type: str, record_id: str, payload: dict, **kw) -> bool:
        self._records_by_key[record_id] = dict(payload)
        sym = record_id.split(":", 1)[-1]
        self._rows = [r for r in self._rows if r.get("symbol") != sym]
        self._rows.append(dict(payload))
        return True

    def _delete_record(self, record_type: str, record_id: str) -> None:
        self._rows = [r for r in self._rows if r.get("symbol") != record_id.split(":", 1)[-1]]
        self._records_by_key.pop(record_id, None)

    def restore_protections(self) -> list[dict]:
        return []


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


def test_gap_persist_end_to_end_slow_fuse_fires_on_gap_reason():
    """R9 端到端: 覆盖缺口评估落 gap-reason 记录 → 7200s 后慢引信触发 → 交接 E2。

    纯卡死场景 (SL 重试失败但从未触达 E2 调用点) 的 exposure 记录由
    _update_protection_fact 非 clean 分支持久化, reason=gap reason
    (≠SL_UNPROTECTABLE) → E3 可见, 不再是被 E2 独占的死代码。
    """
    from types import SimpleNamespace

    engine = _engine([])
    engine._last_account = {}
    engine._protection = SimpleNamespace(
        all_positions=lambda: {"pos-1": SimpleNamespace(instrument_id="BTCUSDT")}
    )
    # 第一轮覆盖评估: 本地持仓无 venue 事实 → gap → 落记录
    engine._update_protection_fact(
        hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=True
    )
    rec = engine._store._get_record("protection_exposure", "exposure:BTCUSDT")
    assert rec is not None
    assert rec["last_reason"] == "LOCAL_POSITION_WITHOUT_VENUE_FACT"
    assert rec["attempts"] == 1
    since = rec["unprotectable_since"]
    # 第二轮评估: attempts 不膨胀、since 不重置 (保留原始裸露起点)
    engine._update_protection_fact(
        hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=True
    )
    rec2 = engine._store._get_record("protection_exposure", "exposure:BTCUSDT")
    assert rec2["attempts"] == 1
    assert rec2["unprotectable_since"] == since

    engine._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008"}}
    import asyncio
    n = asyncio.run(engine._run_slow_fuse(now=since + 7200.0))
    assert n == 1 and engine._slow_fuse_fired == ["BTCUSDT"]
