"""E3 慢引信: 纯卡死 ≥7200s 才平仓; testnet 默认开, live/canary 默认关。"""

import asyncio

from beidou_core.engine import AutonomousEngine, OrderSide


class _ExposureStore:
    def __init__(self, rows: list[dict] | None = None):
        self._rows = list(rows or [])
        # 种子行同时可见于 _records 与 _get_record (真实 E2 阶梯测试需要
        # 读到 attempts/unprotectable_since)。
        self._records_by_key: dict[str, dict] = {
            f"exposure:{r['symbol']}": dict(r) for r in self._rows if r.get("symbol")
        }

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


def test_slow_fuse_fires_on_explicit_rejection_row_advancing_e2_ladder():
    # R10(b): E3 纯按 age≥7200 触发, 忽略 last_reason —— SL_UNPROTECTABLE
    # 行同样触发, 把 E2 的 attempts 确认阶梯向前推进 (enqueue 由 E2 门控)。
    engine = _engine([{"symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": "SL_UNPROTECTABLE"}])
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008"}}
    import asyncio

    n = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0))
    assert n == 1 and engine._slow_fuse_fired == ["BTCUSDT"]


def test_gap_persist_end_to_end_slow_fuse_fires_on_gap_reason():
    """R9 端到端: 覆盖缺口评估落 gap-reason 记录 → 7200s 后慢引信触发 → 交接 E2。

    纯卡死场景 (SL 重试失败但从未触达 E2 调用点) 的 exposure 记录由
    _update_protection_fact 非 clean 分支持久化, reason=gap reason
    (≠SL_UNPROTECTABLE) → E3 可见, 不再是被 E2 独占的死代码。
    """
    from types import SimpleNamespace

    engine = _engine([])
    engine._last_account = {}
    engine._protection = SimpleNamespace(all_positions=lambda: {"pos-1": SimpleNamespace(instrument_id="BTCUSDT")})
    # 第一轮覆盖评估: 本地持仓无 venue 事实 → gap → 落记录
    engine._update_protection_fact(hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=True)
    rec = engine._store._get_record("protection_exposure", "exposure:BTCUSDT")
    assert rec is not None
    assert rec["last_reason"] == "LOCAL_POSITION_WITHOUT_VENUE_FACT"
    assert rec["attempts"] == 1
    since = rec["unprotectable_since"]
    # 第二轮评估: attempts 不膨胀、since 不重置 (保留原始裸露起点)
    engine._update_protection_fact(hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=True)
    rec2 = engine._store._get_record("protection_exposure", "exposure:BTCUSDT")
    assert rec2["attempts"] == 1
    assert rec2["unprotectable_since"] == since

    engine._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008"}}
    import asyncio

    n = asyncio.run(engine._run_slow_fuse(now=since + 7200.0))
    assert n == 1 and engine._slow_fuse_fired == ["BTCUSDT"]


def test_gap_refresh_does_not_rewrite_e2_owned_reason():
    """R10(a): E2 接管后 (last_reason=SL_UNPROTECTABLE), gap 刷新不回写
    gap reason —— 记录归属 E2, 不打败 E2 确认阶梯语义。"""
    from types import SimpleNamespace

    engine = _engine([])
    engine._last_account = {}
    engine._protection = SimpleNamespace(all_positions=lambda: {"pos-1": SimpleNamespace(instrument_id="BTCUSDT")})
    engine._update_protection_fact(hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=True)
    rec = engine._store._get_record("protection_exposure", "exposure:BTCUSDT")
    assert rec["last_reason"] == "LOCAL_POSITION_WITHOUT_VENUE_FACT"
    # E2 接管: 首触翻 reason 为 SL_UNPROTECTABLE (attempts 1→2)
    engine._persist_protection_exposure("BTCUSDT", "SL_UNPROTECTABLE", now=lambda: 2000.0)
    # 下一轮 gap 刷新: 不得回写 gap reason
    engine._update_protection_fact(hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=True)
    rec2 = engine._store._get_record("protection_exposure", "exposure:BTCUSDT")
    assert rec2["last_reason"] == "SL_UNPROTECTABLE"
    assert rec2["attempts"] == 2


def test_real_e2_handoff_position_side_and_ladder_enqueue_once():
    """R10 端到端 (真实 E2): pp.side 传持仓方向 (多头=BUY), E2 发射减仓方向
    (SELL); attempts=1 记录经 3 个 tick 后 enqueue 恰好调用一次 (E2 门控)。"""
    engine = _engine(
        [
            {
                "symbol": "BTCUSDT",
                "unprotectable_since": 1000.0,
                "last_reason": "STOP_LOSS_QUANTITY_UNCOVERED",
                "attempts": 1,
            }
        ]
    )
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "0.0008"}}
    enqueue_calls: list[dict] = []

    async def _fake_enqueue(**kw):
        enqueue_calls.append(dict(kw))
        return True

    engine.enqueue_reduce_only_market = _fake_enqueue

    captured: dict = {}
    _real_e2 = AutonomousEngine._maybe_emergency_close_unprotectable

    async def _spy_e2(pos_id, symbol, pp, **kw):
        captured["side"] = pp.side
        captured["quantity"] = pp.quantity
        return await _real_e2(engine, pos_id, symbol, pp, **kw)

    engine._maybe_emergency_close_unprotectable = _spy_e2

    n1 = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0))
    assert n1 == 0  # attempts 1→2, 阶梯未到 3
    n2 = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0 + 30.0))
    assert n2 == 1  # attempts 2→3 且跨度 ≥60s → enqueue → 记录清除
    n3 = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0 + 90.0))
    assert n3 == 0  # 记录已清除, 自然停止
    assert len(enqueue_calls) == 1  # 3 个 tick 恰好 enqueue 一次
    assert captured["side"] == OrderSide.BUY  # pp.side = 持仓方向 (多头)
    assert captured["quantity"] == 0.0008
    assert enqueue_calls[0]["side"] == "SELL"  # E2 发射减仓方向
    assert enqueue_calls[0]["quantity"] == 0.0008


def test_slow_fuse_passes_position_side_for_short():
    """R10 Critical 1: 空头持仓 → pp.side=SELL (持仓方向), 由 E2 翻转为 BUY 减仓。"""
    engine = _engine([{"symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": ""}])
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008"}}
    captured: dict = {}

    async def _capture_close(pos_id, symbol, pp, **kw):
        captured["side"] = pp.side
        captured["quantity"] = pp.quantity
        return True

    engine._maybe_emergency_close_unprotectable = _capture_close

    n = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0))
    assert n == 1
    assert captured["side"] == OrderSide.SELL
    assert captured["quantity"] == 0.0008


def test_slow_fuse_dirty_row_does_not_break_sweep():
    """R10 minor 4: unprotectable_since 脏行 (非数值) 跳过, 不炸整个 sweep。"""
    engine = _engine(
        [
            {"symbol": "DIRTY", "unprotectable_since": "not-a-number", "last_reason": ""},
            {"symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": ""},
        ]
    )
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008"}}
    import asyncio

    n = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0))
    assert n == 1 and engine._slow_fuse_fired == ["BTCUSDT"]


def test_persist_protection_exposure_accepts_float_now():
    """M-6: _persist_protection_exposure 的 now 参数接受 float 时间戳
    (与 _run_slow_fuse/_update_stuck_marker 一致), 不再默默忽略非 callable。"""
    engine = _engine([])
    rec = engine._persist_protection_exposure("BTCUSDT", "SL_UNPROTECTABLE", now=1234.5)
    assert rec["unprotectable_since"] == 1234.5
    assert rec["attempts"] == 1
    # 再次传 float: 更新路径同样吃 float 时间戳
    rec2 = engine._persist_protection_exposure("BTCUSDT", "SL_UNPROTECTABLE", now=2234.5)
    assert rec2["unprotectable_since"] == 1234.5  # 起点不重置
    assert rec2["attempts"] == 2
