"""保护所有权收敛回归测试(根因修复:消除 NO_NEW_RISK 永久死锁)。

背景(事故复盘):durable 保护行丢失/未持久化而 venue 上 SL/TP 条件单
仍然 ACTIVE 时,系统出现鸡生蛋死锁 —— 资格门要求 durable 覆盖
(→ NO_NEW_RISK),nearline 却因 venue 已有单而 covered skip(不补发、
不收养),两套事实永不收敛,所有新增风险意图被 ELIGIBILITY_NO_NEW_RISK
恒拒。本组测试锁定收敛语义:

1. 孤儿 bdp- 保护单可被收养进 durable 存储 → 覆盖恢复 → 资格 ELIGIBLE;
2. 收养对歧义(方向/数量/reduce-only/多单)保持 fail-closed;
3. 保护事实只有单一"放行"写入点,问题记录在库存验证通过后被清除;
4. _durable_fact_status 在问题未清除时持续保持门禁关闭;
5. nearline 补发循环在 covered skip 之前先执行收养,打破死锁;
6. outbox 过期租约在运行时按宽限窗口恢复,不再永久卡 SENDING。
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from beidou_control.truth import derive_eligibility
from beidou_core.engine import AutonomousEngine


class _Store:
    def __init__(self, protections: list[dict[str, Any]] | None = None) -> None:
        self.protections = {str(r["protection_id"]): dict(r) for r in (protections or [])}
        self.saved: list[dict[str, Any]] = []
        self.removed: list[str] = []

    def restore_protections(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.protections.values()]

    def save_protection(self, **kwargs: Any) -> None:
        row = dict(kwargs)
        self.protections[str(row["protection_id"])] = row
        self.saved.append(row)

    def remove_protection(self, position_id: str) -> None:
        self.removed.append(position_id)
        self.protections = {k: v for k, v in self.protections.items() if v.get("position_id") != position_id}

    def restore_position_projection(self) -> list[dict[str, Any]]:
        return []

    def restore_order_states(self) -> list[dict[str, Any]]:
        return []


class _Protection:
    def __init__(self, symbols: set[str] | None = None) -> None:
        self._symbols = set(symbols or ())
        self._projections: dict[str, Any] = {}

    def all_positions(self) -> dict[str, Any]:
        base = {sym: SimpleNamespace(instrument_id=sym, stop_loss=None, take_profits=[]) for sym in self._symbols}
        base.update(getattr(self, "_projections", {}))
        return base

    def restore_position_protection(self, protection: Any) -> Any:
        self._projections[str(protection.position_id)] = protection
        return protection

    def set_precision_from_rule(self, _rule: Any) -> None:
        return None

    def cancel_protection(self, position_id: str) -> list[Any]:
        return []

    def remove_position(self, position_id: str) -> None:
        self._projections.pop(position_id, None)

    def position_count(self) -> int:
        return len(self.all_positions())


def _venue_algo(
    algo_id: str,
    symbol: str,
    *,
    side: str,
    order_type: str,
    quantity: str,
    trigger_price: str,
    client_algo_id: str = "bdp-deadbeef",
    reduce_only: Any = True,
    status: str = "NEW",
) -> dict[str, Any]:
    return {
        "algoId": algo_id,
        "symbol": symbol,
        "side": side,
        "orderType": order_type,
        "quantity": quantity,
        "triggerPrice": trigger_price,
        "clientAlgoId": client_algo_id,
        "reduceOnly": reduce_only,
        "algoStatus": status,
    }


def _adopt_engine(
    *,
    protections: list[dict[str, Any]] | None = None,
    positions: dict[str, str] | None = None,
    local_symbols: set[str] | None = None,
    projection: dict[str, dict[str, Any]] | None = None,
    generation: dict[str, int] | None = None,
) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _Store(protections)
    engine._protection_owner_id = "owner-1"
    engine._session_id = "session-1"
    engine._protection = _Protection(local_symbols)
    engine._position_projection = dict(projection or {})
    engine._position_generation = dict(generation or {})
    engine._active_algo_ids = {}
    engine._last_algo_inventory_genuine = True
    engine._last_account = {
        "positions": [
            {"symbol": sym, "positionAmt": amt, "entryPrice": "1.0"} for sym, amt in (positions or {}).items()
        ]
    }
    engine._protection_issues = set()
    engine._last_protection_hash = hashlib.sha256(b"UNKNOWN").hexdigest()
    engine._last_protection_fact_at = 0.0
    engine._protection_owner_unknown = True
    return engine


def _durable_row(
    *,
    protection_id: str,
    symbol: str,
    side: str,
    order_type: str,
    quantity: str,
    position_id: str,
    generation: int,
    algo_id: str,
    status: str = "ACTIVE",
    owner_id: str = "owner-1",
    stop_type: str | None = "ATR_BASED",
    take_profit_type: str | None = None,
    trigger_price: str = "1.0",
) -> dict[str, Any]:
    return {
        "protection_id": protection_id,
        "position_id": position_id,
        "symbol": symbol,
        "side": side,
        "trigger_price": trigger_price,
        "order_price": None,
        "quantity": quantity,
        "order_type": order_type,
        "status": status,
        "stop_type": stop_type,
        "take_profit_type": take_profit_type,
        "owner_id": owner_id,
        "position_generation": generation,
        "session_id": "session-1",
        "exchange_order_id": algo_id,
    }


# ---------------------------------------------------------------------------
# 1. 孤儿保护单收养 → durable 覆盖恢复
# ---------------------------------------------------------------------------


def test_adoption_binds_orphaned_bdp_algos_into_durable_store_and_restores_coverage() -> None:
    engine = _adopt_engine(
        positions={"BTCUSDT": "1.0"},
        local_symbols={"BTCUSDT"},
    )
    algos = [
        _venue_algo(
            "algo-sl-1", "BTCUSDT", side="SELL", order_type="STOP_MARKET", quantity="1.0", trigger_price="99.0"
        ),
        _venue_algo(
            "algo-tp-1", "BTCUSDT", side="SELL", order_type="TAKE_PROFIT_MARKET", quantity="1.0", trigger_price="103.0"
        ),
    ]

    adopted, issues = engine._adopt_orphaned_protection_algos(algos)

    assert issues == []
    assert adopted == 2
    rows = engine._store.restore_protections()
    assert len(rows) == 2
    by_order_type = {r["order_type"]: r for r in rows}
    assert set(by_order_type) == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    for row in rows:
        assert row["status"] == "ACTIVE"
        assert row["owner_id"] == "owner-1"
        assert row["position_generation"] >= 1
        assert row["exchange_order_id"] in {"algo-sl-1", "algo-tp-1"}
    # 收养后 durable 覆盖成立(即使没有独立 projection 行)
    covered, evidence = engine._assess_protection_coverage(
        [{"symbol": "BTCUSDT", "positionAmt": "1.0"}],
        [r for r in rows if r["status"] == "ACTIVE" and r["owner_id"] == "owner-1" and r["exchange_order_id"]],
        {},
    )
    assert covered is True, evidence
    # 收养同时挂载内存保护投影(PKG-MON-04 与 nearline 覆盖判定读投影)
    mounted = [
        pp
        for pp in engine._protection.all_positions().values()
        if str(getattr(pp, "instrument_id", "")) == "BTCUSDT" and getattr(pp, "stop_loss", None) is not None
    ]
    assert len(mounted) == 1
    assert mounted[0].stop_loss is not None
    assert mounted[0].stop_loss.exchange_order_id == "algo-sl-1"
    assert [tp.exchange_order_id for tp in mounted[0].take_profits] == ["algo-tp-1"]


def test_adoption_is_idempotent_and_skips_already_owned_symbols() -> None:
    engine = _adopt_engine(
        protections=[
            _durable_row(
                protection_id="p-sl",
                symbol="BTCUSDT",
                side="SELL",
                order_type="STOP_MARKET",
                quantity="1.0",
                position_id="pos-btc",
                generation=3,
                algo_id="1000001",
            ),
        ],
        positions={"BTCUSDT": "1.0"},
        local_symbols={"BTCUSDT"},
    )
    algos = [
        _venue_algo("1000001", "BTCUSDT", side="SELL", order_type="STOP_MARKET", quantity="1.0", trigger_price="99.0"),
        _venue_algo(
            "1000002",
            "BTCUSDT",
            side="SELL",
            order_type="TAKE_PROFIT_MARKET",
            quantity="1.0",
            trigger_price="103.0",
        ),
    ]

    adopted, issues = engine._adopt_orphaned_protection_algos(algos)
    # 已有 durable 所有权的品种不收养(避免与既有代数/会话冲突)
    assert adopted == 0
    assert issues == []
    assert len(engine._store.restore_protections()) == 1


def test_adoption_skips_foreign_positions_and_foreign_namespaces() -> None:
    engine = _adopt_engine(
        positions={"BTCUSDT": "1.0", "ETHUSDT": "2.0"},
        local_symbols={"BTCUSDT"},  # ETHUSDT 是共享账户外部持仓
    )
    algos = [
        _venue_algo("sl-btc", "BTCUSDT", side="SELL", order_type="STOP_MARKET", quantity="1.0", trigger_price="99.0"),
        _venue_algo(
            "tp-btc", "BTCUSDT", side="SELL", order_type="TAKE_PROFIT_MARKET", quantity="1.0", trigger_price="103.0"
        ),
        # 外部持仓的保护单 + 非本引擎命名空间
        _venue_algo("sl-eth", "ETHUSDT", side="SELL", order_type="STOP_MARKET", quantity="2.0", trigger_price="9.0"),
        _venue_algo(
            "tp-eth", "ETHUSDT", side="SELL", order_type="TAKE_PROFIT_MARKET", quantity="2.0", trigger_price="11.0"
        ),
        _venue_algo(
            "sl-f",
            "BTCUSDT",
            side="SELL",
            order_type="STOP_MARKET",
            quantity="1.0",
            trigger_price="99.0",
            client_algo_id="someone-else",
        ),
    ]

    adopted, issues = engine._adopt_orphaned_protection_algos(algos)

    assert issues == []
    assert adopted == 2  # 只收养 BTCUSDT 的 bdp- 单
    rows = engine._store.restore_protections()
    assert all(r["symbol"] == "BTCUSDT" for r in rows)


def test_adoption_fail_closed_on_ambiguous_or_mismatched_algos() -> None:
    engine = _adopt_engine(positions={"BTCUSDT": "1.0"}, local_symbols={"BTCUSDT"})

    # 方向错误
    adopted, issues = engine._adopt_orphaned_protection_algos(
        [
            _venue_algo("sl-1", "BTCUSDT", side="BUY", order_type="STOP_MARKET", quantity="1.0", trigger_price="99.0"),
            _venue_algo(
                "tp-1", "BTCUSDT", side="SELL", order_type="TAKE_PROFIT_MARKET", quantity="1.0", trigger_price="103.0"
            ),
        ]
    )
    assert adopted == 0
    assert any("AMBIGUOUS" in i for i in issues)

    # 两个 SL(歧义)
    adopted, issues = engine._adopt_orphaned_protection_algos(
        [
            _venue_algo("sl-1", "BTCUSDT", side="SELL", order_type="STOP_MARKET", quantity="1.0", trigger_price="99.0"),
            _venue_algo("sl-2", "BTCUSDT", side="SELL", order_type="STOP_MARKET", quantity="1.0", trigger_price="98.0"),
            _venue_algo(
                "tp-1", "BTCUSDT", side="SELL", order_type="TAKE_PROFIT_MARKET", quantity="1.0", trigger_price="103.0"
            ),
        ]
    )
    assert adopted == 0
    assert any("AMBIGUOUS" in i for i in issues)

    # SL 数量不足以覆盖持仓
    adopted, issues = engine._adopt_orphaned_protection_algos(
        [
            _venue_algo("sl-1", "BTCUSDT", side="SELL", order_type="STOP_MARKET", quantity="0.5", trigger_price="99.0"),
            _venue_algo(
                "tp-1", "BTCUSDT", side="SELL", order_type="TAKE_PROFIT_MARKET", quantity="1.0", trigger_price="103.0"
            ),
        ]
    )
    assert adopted == 0
    assert any("AMBIGUOUS" in i for i in issues)

    # 非 NEW/WORKING 状态不收养:SL 缺失是真实覆盖缺口,记录问题(fail-closed)
    # 交由补发流程重建,而不是静默放行
    adopted, issues = engine._adopt_orphaned_protection_algos(
        [
            _venue_algo(
                "sl-1",
                "BTCUSDT",
                side="SELL",
                order_type="STOP_MARKET",
                quantity="1.0",
                trigger_price="99.0",
                status="CANCELLED",
            ),
            _venue_algo(
                "tp-1", "BTCUSDT", side="SELL", order_type="TAKE_PROFIT_MARKET", quantity="1.0", trigger_price="103.0"
            ),
        ]
    )
    assert adopted == 0
    assert any("AMBIGUOUS" in i for i in issues)

    # 收养失败不留下任何 durable 行(fail-closed)
    assert engine._store.restore_protections() == []


# ---------------------------------------------------------------------------
# 2. 保护事实单一写入点:_update_protection_fact
# ---------------------------------------------------------------------------


def test_update_protection_fact_clears_issues_only_on_verified_clean_inventory() -> None:
    engine = _adopt_engine(
        positions={"BTCUSDT": "1.0"},
        local_symbols={"BTCUSDT"},
        generation={"BTCUSDT": 2},
    )
    engine._protection_issues = {"OWNED_PROTECTION_MISSING:algo-x"}
    engine._protection_owner_unknown = True

    # 干净库存 + 收养后的覆盖 → 清除问题并放行
    algos = [
        _venue_algo(
            "algo-sl-1", "BTCUSDT", side="SELL", order_type="STOP_MARKET", quantity="1.0", trigger_price="99.0"
        ),
        _venue_algo(
            "algo-tp-1", "BTCUSDT", side="SELL", order_type="TAKE_PROFIT_MARKET", quantity="1.0", trigger_price="103.0"
        ),
    ]
    engine._adopt_orphaned_protection_algos(algos)
    engine._update_protection_fact(hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=True)

    assert engine._protection_issues == set()
    assert engine._protection_owner_unknown is False
    assert engine._last_protection_hash == hashlib.sha256(b"ACTIVE").hexdigest()

    # 语义问题存在时绝不放行
    engine._update_protection_fact(
        hard_issues=["PROTECTION_SIDE_MISMATCH:algo-sl-1"],
        venue_missing=[],
        unowned_ids=[],
        genuine_inventory=True,
    )
    assert engine._protection_owner_unknown is True
    assert engine._last_protection_hash == hashlib.sha256(b"UNKNOWN").hexdigest()
    assert "PROTECTION_SIDE_MISMATCH:algo-sl-1" in engine._protection_issues


def test_update_protection_fact_holds_closed_while_venue_rows_missing() -> None:
    engine = _adopt_engine(
        protections=[
            _durable_row(
                protection_id="p-sl",
                symbol="BTCUSDT",
                side="SELL",
                order_type="STOP_MARKET",
                quantity="1.0",
                position_id="pos-btc",
                generation=2,
                algo_id="algo-sl-gone",
            ),
        ],
        positions={"BTCUSDT": "1.0"},
        local_symbols={"BTCUSDT"},
        generation={"BTCUSDT": 2},
    )
    # durable 覆盖本身成立,但 venue 行缺失 → 门禁保持关闭
    engine._update_protection_fact(
        hard_issues=[],
        venue_missing=["PROTECTION_VENUE_ROW_MISSING:algo-sl-gone"],
        unowned_ids=[],
        genuine_inventory=True,
    )
    assert engine._protection_owner_unknown is True
    assert engine._last_protection_hash == hashlib.sha256(b"UNKNOWN").hexdigest()


def test_update_protection_fact_skips_decision_when_inventory_not_genuine() -> None:
    engine = _adopt_engine(positions={"BTCUSDT": "1.0"}, local_symbols={"BTCUSDT"})
    before_hash = engine._last_protection_hash
    before_owner = engine._protection_owner_unknown
    engine._update_protection_fact(hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=False)
    assert engine._last_protection_hash == before_hash
    assert engine._protection_owner_unknown is before_owner


def test_durable_fact_status_holds_gate_closed_while_issues_pending() -> None:
    engine = _adopt_engine(
        protections=[
            _durable_row(
                protection_id="p-sl",
                symbol="BTCUSDT",
                side="SELL",
                order_type="STOP_MARKET",
                quantity="1.0",
                position_id="pos-btc",
                generation=2,
                algo_id="algo-sl-1",
            ),
            _durable_row(
                protection_id="p-tp",
                symbol="BTCUSDT",
                side="SELL",
                order_type="TAKE_PROFIT_MARKET",
                quantity="1.0",
                position_id="pos-btc",
                generation=2,
                algo_id="algo-tp-1",
                stop_type=None,
                take_profit_type="FIXED_RR",
            ),
        ],
        positions={"BTCUSDT": "1.0"},
        local_symbols={"BTCUSDT"},
        generation={"BTCUSDT": 2},
    )
    engine._outbox = SimpleNamespace(stats=lambda: {"state_counts": {}})
    engine._active_order_ids = set()
    engine._owned_order_ids = set()
    # 覆盖本身成立 → durable 事实干净
    ok, reason, _ = engine._durable_fact_status()
    assert ok is True and reason == "DURABLE_FACTS_VERIFIED"
    # 所有权问题未清除 → 门禁保持关闭
    engine._protection_issues = {"PROTECTION_OWNER_MAPPING_INCOMPLETE"}
    ok, reason, _evidence = engine._durable_fact_status()
    assert ok is False
    assert reason == "DURABLE_PROTECTION_COVERAGE_UNKNOWN"
    # 问题清除后恢复
    engine._protection_issues = set()
    ok, reason, _ = engine._durable_fact_status()
    assert ok is True


def test_eligibility_converges_to_eligible_after_adoption_and_issue_clear() -> None:
    engine = _adopt_engine(positions={"BTCUSDT": "1.0"}, local_symbols={"BTCUSDT"})
    engine._last_protection_hash = hashlib.sha256(b"UNKNOWN").hexdigest()
    engine._protection_owner_unknown = True
    engine._protection_issues = {"STARTUP_BLOCK"}
    # 补齐资格推导所需的其余事实
    for attr in (
        "_last_market_hash",
        "_last_account_hash",
        "_last_order_hash",
        "_last_position_hash",
        "_last_ledger_hash",
        "_last_reconciliation_hash",
        "_last_risk_hash",
        "_config_hash",
        "_policy_hash",
    ):
        setattr(engine, attr, "hash-" + attr)
    engine._last_market_fact_at = 1e12
    engine._last_account_fact_at = 1e12
    engine._last_order_fact_at = 1e12
    engine._last_position_fact_at = 1e12
    engine._last_ledger_fact_at = 1e12
    engine._last_reconciliation_fact_at = 1e12
    engine._last_protection_fact_at = 1e12
    engine._last_risk_fact_at = 1e12
    engine._last_config_observed_at = 1e12
    engine._last_policy_observed_at = 1e12
    engine._last_reconciliation_result = SimpleNamespace(status="MATCHED")
    engine._env_mode = SimpleNamespace(value="testnet")
    engine._control = SimpleNamespace(_action="RESUME", update_truth_snapshot=lambda snap: None)

    assert derive_eligibility(engine.build_truth_snapshot()).value == "NO_NEW_RISK"

    algos = [
        _venue_algo(
            "algo-sl-1", "BTCUSDT", side="SELL", order_type="STOP_MARKET", quantity="1.0", trigger_price="99.0"
        ),
        _venue_algo(
            "algo-tp-1", "BTCUSDT", side="SELL", order_type="TAKE_PROFIT_MARKET", quantity="1.0", trigger_price="103.0"
        ),
    ]
    engine._adopt_orphaned_protection_algos(algos)
    engine._update_protection_fact(hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=True)

    snap = engine.build_truth_snapshot()
    assert snap.protection_status == "ACTIVE"
    assert derive_eligibility(snap).value == "ELIGIBLE"


# ---------------------------------------------------------------------------
# 3. nearline 补发循环先收养、后 covered skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_missing_protections_adopts_before_covered_skip() -> None:
    engine = _adopt_engine(
        positions={"BTCUSDT": "1.0"},
        local_symbols={"BTCUSDT"},
        projection={"BTCUSDT": {"signed_quantity": "1.0", "entry_price": "100.0"}},
    )
    engine._can_write = True
    engine._control = SimpleNamespace(
        get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"), execute_action=lambda action: None
    )
    engine._protection_retries = {}
    engine._pending_protection_persist = {}
    engine._venue_missing_streaks = {}
    engine._pending_protection_retry = set()
    engine._protection_exchange_attempted = set()
    engine._feed = SimpleNamespace()
    engine._diag_throttle = lambda _key: True

    algos = [
        _venue_algo(
            "algo-sl-1", "BTCUSDT", side="SELL", order_type="STOP_MARKET", quantity="1.0", trigger_price="99.0"
        ),
        _venue_algo(
            "algo-tp-1", "BTCUSDT", side="SELL", order_type="TAKE_PROFIT_MARKET", quantity="1.0", trigger_price="103.0"
        ),
    ]

    async def _fake_inventory() -> list[dict[str, Any]]:
        return [dict(a) for a in algos]

    engine._get_open_algo_inventory = _fake_inventory  # type: ignore[method-assign]
    engine._flush_pending_protection_persist = lambda: None  # type: ignore[method-assign]

    await engine._retry_missing_protections({"BTCUSDT"})

    rows = engine._store.restore_protections()
    assert len(rows) == 2
    assert all(r["status"] == "ACTIVE" for r in rows)
    # 收养进内存所有权映射,covered skip 的 owned_ids 可以命中
    assert engine._active_algo_ids.get("adopt-BTCUSDT") == {"algo-sl-1", "algo-tp-1"}
    # 单一放行写入点:问题被清除、事实 ACTIVE
    assert engine._protection_owner_unknown is False
    assert engine._last_protection_hash == hashlib.sha256(b"ACTIVE").hexdigest()


@pytest.mark.asyncio
async def test_retry_missing_protections_does_not_place_duplicates_after_adoption() -> None:
    """收养后,即使投影 SL 缺失,S41 去重也保证不重复下单(仅 durable 收养)。"""
    engine = _adopt_engine(
        positions={"BTCUSDT": "1.0"},
        local_symbols={"BTCUSDT"},
    )
    engine._can_write = True
    engine._control = SimpleNamespace(
        get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"), execute_action=lambda action: None
    )
    engine._protection_retries = {}
    engine._pending_protection_persist = {}
    engine._venue_missing_streaks = {}
    engine._pending_protection_retry = set()
    engine._protection_exchange_attempted = set()
    engine._feed = SimpleNamespace()
    engine._diag_throttle = lambda _key: True
    placement_calls: list[dict[str, Any]] = []

    async def _fake_create_algo(params: dict[str, Any]) -> dict[str, Any]:
        placement_calls.append(dict(params))
        return {"algoId": "algo-new"}

    engine._create_algo_order = _fake_create_algo  # type: ignore[method-assign]

    algos = [
        _venue_algo(
            "algo-sl-1", "BTCUSDT", side="SELL", order_type="STOP_MARKET", quantity="1.0", trigger_price="99.0"
        ),
        _venue_algo(
            "algo-tp-1", "BTCUSDT", side="SELL", order_type="TAKE_PROFIT_MARKET", quantity="1.0", trigger_price="103.0"
        ),
    ]

    async def _fake_inventory() -> list[dict[str, Any]]:
        return [dict(a) for a in algos]

    engine._get_open_algo_inventory = _fake_inventory  # type: ignore[method-assign]
    engine._flush_pending_protection_persist = lambda: None  # type: ignore[method-assign]

    await engine._retry_missing_protections({"BTCUSDT"})

    assert placement_calls == []  # venue 已有 2 单 → 不得重复下单
    assert len(engine._store.restore_protections()) == 2


# ---------------------------------------------------------------------------
# 4. 启动恢复:OWNED_PROTECTION_MISSING 宽限重查
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_exchange_position_protections_grace_requery_avoids_false_block() -> None:
    """刚下发的 algo 单未进库存快照(eventual consistency)时不立即阻断:
    宽限重查一次;第二次查询可见即视为已核验。"""
    engine = _adopt_engine(positions={"BTCUSDT": "1.0"}, local_symbols=set())
    engine._env_mode = SimpleNamespace(value="testnet")  # 共享账户外部持仓豁免
    engine._adapter = SimpleNamespace(reset_circuit_breaker=lambda: None)
    engine._can_write = True
    engine._control = SimpleNamespace(
        get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"), execute_action=lambda action: None
    )
    engine._active_algo_ids = {"pos-btc": {"algo-sl-1", "algo-tp-1"}}

    calls = 0

    async def _fake_inventory() -> list[dict[str, Any]] | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return []  # 首次快照未包含刚下发的 algo 单
        return [
            _venue_algo(
                "algo-sl-1", "BTCUSDT", side="SELL", order_type="STOP_MARKET", quantity="1.0", trigger_price="99.0"
            ),
            _venue_algo(
                "algo-tp-1",
                "BTCUSDT",
                side="SELL",
                order_type="TAKE_PROFIT_MARKET",
                quantity="1.0",
                trigger_price="103.0",
            ),
        ]

    engine._get_open_algo_inventory = _fake_inventory  # type: ignore[method-assign]

    await engine._ensure_exchange_position_protections()

    assert calls >= 2
    assert engine._protection_issues == set()
    assert engine._protection_owner_unknown is False


@pytest.mark.asyncio
async def test_retry_missing_protections_rebuilds_undersized_sl_after_position_growth() -> None:
    """分批加仓后旧 SL 数量 < 持仓量:covered skip / S41 不得跳过,
    必须取消陈旧保护并按当前持仓量重建 —— 否则资格门按数量覆盖判定
    恒拒(根因修复的加仓变体)。"""
    from beidou_safety.protection.engine import (
        ProtectionOrder,
        ProtectionStatus,
        StopLossType,
        TakeProfitType,
    )
    from beidou_shared.types import InstrumentId, OrderSide, Price, Quantity, VenueId

    engine = _adopt_engine(positions={"SUIUSDT": "44.3"}, local_symbols=set())
    engine._env_mode = SimpleNamespace(value="testnet")
    engine._can_write = True
    engine._control = SimpleNamespace(
        get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"), execute_action=lambda action: None
    )
    engine._protection_retries = {}
    engine._pending_protection_persist = {}
    engine._venue_missing_streaks = {}
    engine._pending_protection_retry = set()
    engine._protection_exchange_attempted = set()
    engine._feed = SimpleNamespace()
    engine._diag_throttle = lambda _key: True
    engine._symbol_precision = {"SUIUSDT": {"price": 4, "quantity": 4}}
    engine._stale_protection_algos = []
    engine._sl_unprotectable_streak = {}
    engine._position_generation = {"SUIUSDT": 2}
    engine._position_projection = {
        "SUIUSDT": {"position_generation": 2, "entry_price": "0.6473", "signed_quantity": "44.3"}
    }

    old_sl = ProtectionOrder(
        protection_id="sl-pos-1",
        position_id="pos-1",
        instrument_id=InstrumentId("SUIUSDT"),
        venue_id=VenueId("BINANCE"),
        side=OrderSide.SELL,
        trigger_price=Price(amount="0.66"),
        order_price=None,
        quantity=Quantity(amount="21.2"),
        order_type="STOP_MARKET",
        reduce_only=True,
        status=ProtectionStatus.ACTIVE,
        stop_type=StopLossType.ATR_BASED,
        take_profit_type=None,
        owner_id="owner-1",
        position_generation=2,
        session_id="session-1",
        exchange_order_id="1000001",
    )
    old_tp = ProtectionOrder(
        protection_id="tp-pos-1",
        position_id="pos-1",
        instrument_id=InstrumentId("SUIUSDT"),
        venue_id=VenueId("BINANCE"),
        side=OrderSide.SELL,
        trigger_price=Price(amount="0.70"),
        order_price=None,
        quantity=Quantity(amount="21.2"),
        order_type="TAKE_PROFIT_MARKET",
        reduce_only=True,
        status=ProtectionStatus.ACTIVE,
        stop_type=None,
        take_profit_type=TakeProfitType.FIXED_RR,
        owner_id="owner-1",
        position_generation=2,
        session_id="session-1",
        exchange_order_id="1000002",
    )
    pp = SimpleNamespace(
        position_id="pos-1",
        instrument_id="SUIUSDT",
        side=OrderSide.BUY,
        entry_price=0.6473,
        quantity=44.3,
        stop_loss=old_sl,
        take_profits=[old_tp],
        position_generation=2,
    )
    engine._protection._projections["pos-1"] = pp
    engine._store.protections = {
        "sl-pos-1": _durable_row(
            protection_id="sl-pos-1",
            symbol="SUIUSDT",
            side="SELL",
            order_type="STOP_MARKET",
            quantity="21.2",
            position_id="pos-1",
            generation=2,
            algo_id="1000001",
            trigger_price="0.66",
        ),
        "tp-pos-1": _durable_row(
            protection_id="tp-pos-1",
            symbol="SUIUSDT",
            side="SELL",
            order_type="TAKE_PROFIT_MARKET",
            quantity="21.2",
            position_id="pos-1",
            generation=2,
            algo_id="1000002",
            stop_type=None,
            take_profit_type="FIXED_RR",
            trigger_price="0.70",
        ),
    }

    cancelled_ids: list[int] = []
    created_params: list[dict[str, Any]] = []

    # 动态 venue 状态:取消即消失,创建即出现(镜像真实交易所行为)
    venue_state: list[dict[str, Any]] = [
        _venue_algo("1000001", "SUIUSDT", side="SELL", order_type="STOP_MARKET", quantity="21.2", trigger_price="0.66"),
        _venue_algo(
            "1000002", "SUIUSDT", side="SELL", order_type="TAKE_PROFIT_MARKET", quantity="21.2", trigger_price="0.70"
        ),
    ]

    async def _fake_cancel(symbol: str, algo_id: int) -> dict[str, Any]:
        cancelled_ids.append(algo_id)
        venue_state[:] = [a for a in venue_state if str(a["algoId"]) != str(algo_id)]
        return {"code": "200", "msg": "success"}

    engine._cancel_algo_order = _fake_cancel  # type: ignore[method-assign]

    async def _fake_create(params: dict[str, Any]) -> dict[str, Any]:
        created_params.append(dict(params))
        is_stop = str(params["type"]).startswith("STOP")
        new_id = "1000003" if is_stop else "1000004"
        venue_state.append(
            _venue_algo(
                new_id,
                "SUIUSDT",
                side="SELL",
                order_type=str(params["type"]),
                quantity=str(params["quantity"]),
                trigger_price=str(params["triggerPrice"]),
            )
        )
        return {"algoId": new_id}

    engine._create_algo_order = _fake_create  # type: ignore[method-assign]

    async def _fake_kline(_symbol: str) -> dict[str, Any]:
        return {}

    engine._feed.async_get_kline_features = _fake_kline  # type: ignore[attr-defined]

    async def _fake_inventory() -> list[dict[str, Any]]:
        return [dict(a) for a in venue_state]

    engine._get_open_algo_inventory = _fake_inventory  # type: ignore[method-assign]
    engine._flush_pending_protection_persist = lambda: None  # type: ignore[method-assign]

    import beidou_strategy.protection.adaptive as adaptive_mod

    class _Cfg:
        def __init__(self) -> None:
            self.metadata: dict[str, Any] = {}
            self.stop_pct = 2.0
            self.atr_pct = 1.0
            self.volatility_regime = SimpleNamespace(value="LOW")
            self.price_tier = SimpleNamespace(value="MID")
            self.market_regime = SimpleNamespace(value="RANGING")
            self.rr_ratio = 1.0
            self.stop_loss_config = {"type": "FIXED_PERCENT", "stop_pct": 2.0}
            self.take_profit_config = {"type": "FIXED_RR", "rr": 1.0}

    orig_calc = adaptive_mod.AdaptiveProtectionCalculator.calculate
    adaptive_mod.AdaptiveProtectionCalculator.calculate = staticmethod(lambda *_a, **_k: _Cfg())
    try:
        await engine._retry_missing_protections({"SUIUSDT"})
        # 第二轮:资格事实收敛为 ACTIVE(数量覆盖成立)
        await engine._retry_missing_protections({"SUIUSDT"})
    finally:
        adaptive_mod.AdaptiveProtectionCalculator.calculate = orig_calc

    # 旧 venue 条件单被取消
    assert {str(c) for c in cancelled_ids} == {"1000001", "1000002"}
    # 新 SL 按当前持仓量下发
    stop_creates = [c for c in created_params if str(c["type"]).startswith("STOP")]
    assert stop_creates and abs(float(stop_creates[0]["quantity"]) - 44.3) < 1e-6
    # durable 行收敛:SL 行数量覆盖持仓,状态 ACTIVE
    rows = engine._store.restore_protections()
    sl_rows = [r for r in rows if r["status"] == "ACTIVE" and r["order_type"].startswith("STOP")]
    assert any(float(r["quantity"]) + 1e-8 >= 44.3 for r in sl_rows), sl_rows
    assert engine._protection_owner_unknown is False
    assert engine._last_protection_hash == hashlib.sha256(b"ACTIVE").hexdigest()


# ---------------------------------------------------------------------------
# 5. 幽灵持仓去重(R8 20/21 死锁根因修复)
# ---------------------------------------------------------------------------


def _projection(
    position_id: str,
    symbol: str,
    *,
    sl_status: str | None = None,
    quantity: float = 1.0,
    algo_id: str | None = None,
) -> Any:
    """构造一个 ProtectionManager 风格的投影对象(与真实实现同构)。"""
    from beidou_shared.types import OrderSide as _OrderSide

    sl = None
    if sl_status is not None:
        sl = SimpleNamespace(
            protection_id=f"sl-{position_id}",
            position_id=position_id,
            instrument_id=symbol,
            status=SimpleNamespace(value=sl_status),
            is_active=lambda: sl_status == "ACTIVE",
            quantity=SimpleNamespace(amount=str(quantity)),
            exchange_order_id=algo_id,
            reduce_only=True,
        )
    return SimpleNamespace(
        position_id=position_id,
        instrument_id=symbol,
        venue_id="BINANCE",
        entry_price=100.0,
        quantity=quantity,
        side=_OrderSide.BUY,
        stop_loss=sl,
        take_profits=[],
        owner_id="owner-1",
        position_generation=1,
        session_id="session-1",
    )


def _dedup_engine(
    *,
    protections: list[dict[str, Any]],
    projections: list[Any],
    positions: dict[str, str] | None = None,
) -> AutonomousEngine:
    engine = _adopt_engine(protections=protections, positions=positions or {"BNBUSDT": "0.1"})
    engine._active_algo_ids = {}
    engine._position_entry_times = {}
    engine._pending_protection_retry = set()
    engine._protection_exchange_attempted = set()
    for pp in projections:
        engine._protection.restore_position_protection(pp)
    return engine


def test_dedup_removes_ghost_projection_and_keeps_active_one() -> None:
    """同品种真实(ACTIVE SL)+幽灵(CREATED SL)双投影 → 幽灵被移除。"""
    engine = _dedup_engine(
        protections=[
            _durable_row(
                protection_id="sl-pos-real",
                symbol="BNBUSDT",
                side="SELL",
                order_type="STOP_MARKET",
                quantity="0.1",
                position_id="pos-real",
                generation=3,
                algo_id="algo-real-sl",
            ),
            _durable_row(
                protection_id="tp-pos-real",
                symbol="BNBUSDT",
                side="SELL",
                order_type="TAKE_PROFIT_MARKET",
                quantity="0.1",
                position_id="pos-real",
                generation=3,
                algo_id="algo-real-tp",
                stop_type=None,
                take_profit_type="FIXED_RR",
            ),
            _durable_row(
                protection_id="sl-pos-ghost",
                symbol="BNBUSDT",
                side="SELL",
                order_type="STOP_MARKET",
                quantity="0.1",
                position_id="pos-ghost",
                generation=2,
                algo_id="",
                status="PENDING",
            ),
            _durable_row(
                protection_id="tp-pos-ghost",
                symbol="BNBUSDT",
                side="SELL",
                order_type="TAKE_PROFIT_MARKET",
                quantity="0.1",
                position_id="pos-ghost",
                generation=2,
                algo_id="",
                status="PENDING",
                stop_type=None,
                take_profit_type="FIXED_RR",
            ),
        ],
        projections=[
            _projection("pos-real", "BNBUSDT", sl_status="ACTIVE", algo_id="algo-real-sl"),
            _projection("pos-ghost", "BNBUSDT", sl_status="CREATED"),
        ],
    )
    engine._active_algo_ids = {"pos-real": {"algo-real-sl", "algo-real-tp"}}

    removed = engine._dedup_ghost_protection_positions()

    assert removed == 1
    remaining = engine._protection.all_positions()
    assert "pos-ghost" not in remaining
    assert "pos-real" in remaining
    rows = engine._store.restore_protections()
    assert all(r["position_id"] != "pos-ghost" for r in rows)
    assert "pos-ghost" not in engine._active_algo_ids
    # R8 覆盖计数收敛:剩余持仓全部 SL ACTIVE
    active = sum(
        1
        for pp in remaining.values()
        if getattr(pp, "stop_loss", None) is not None
        and callable(getattr(pp.stop_loss, "is_active", None))
        and pp.stop_loss.is_active()
    )
    assert active == len(remaining) == 1


def test_dedup_noop_when_no_duplicates() -> None:
    engine = _dedup_engine(
        protections=[],
        projections=[_projection("pos-a", "BTCUSDT", sl_status="ACTIVE", algo_id="algo-a")],
        positions={"BTCUSDT": "1.0"},
    )
    assert engine._dedup_ghost_protection_positions() == 0
    assert set(engine._protection.all_positions()) == {"pos-a"}


def test_dedup_fail_closed_when_multiple_active_projections() -> None:
    """两个投影 SL 均 ACTIVE → 歧义,不自动裁决(两个都保留)。"""
    engine = _dedup_engine(
        protections=[],
        projections=[
            _projection("pos-a", "BNBUSDT", sl_status="ACTIVE", algo_id="algo-a"),
            _projection("pos-b", "BNBUSDT", sl_status="ACTIVE", algo_id="algo-b"),
        ],
    )
    assert engine._dedup_ghost_protection_positions() == 0
    assert set(engine._protection.all_positions()) == {"pos-a", "pos-b"}


def test_dedup_fail_closed_when_no_active_projection() -> None:
    """全为幽灵(无 ACTIVE SL)→ 交 S33 重建,不裁决。"""
    engine = _dedup_engine(
        protections=[],
        projections=[
            _projection("pos-a", "BNBUSDT", sl_status="CREATED"),
            _projection("pos-b", "BNBUSDT", sl_status=None),
        ],
    )
    assert engine._dedup_ghost_protection_positions() == 0
    assert set(engine._protection.all_positions()) == {"pos-a", "pos-b"}


@pytest.mark.asyncio
async def test_retry_s41_skip_requires_own_active_sl() -> None:
    """S41 品种级算法单计数不能替本持仓自己的 SL ACK 背书。

    幽灵投影(SL CREATED 未 ACK,品种 2 个算法单属于他人/其他投影)过去
    被 S41 恒跳过 → R8 覆盖 N/(N+1) 死锁。现在必须重试本持仓 SL。
    """
    engine = _adopt_engine(
        positions={"BTCUSDT": "1.0"},
        projection={"BTCUSDT": {"signed_quantity": "1.0", "entry_price": "100.0"}},
        protections=[
            _durable_row(
                protection_id="sl-pos-p",
                symbol="BTCUSDT",
                side="SELL",
                order_type="STOP_MARKET",
                quantity="1.0",
                position_id="pos-p",
                generation=1,
                algo_id="",
                status="PENDING",
            ),
            _durable_row(
                protection_id="tp-pos-p",
                symbol="BTCUSDT",
                side="SELL",
                order_type="TAKE_PROFIT_MARKET",
                quantity="1.0",
                position_id="pos-p",
                generation=1,
                algo_id="",
                status="PENDING",
                stop_type=None,
                take_profit_type="FIXED_RR",
            ),
        ],
    )
    engine._can_write = True
    engine._control = SimpleNamespace(
        get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"), execute_action=lambda action: None
    )
    engine._protection_retries = {}
    engine._pending_protection_persist = {}
    engine._venue_missing_streaks = {}
    engine._pending_protection_retry = set()
    engine._protection_exchange_attempted = set()
    engine._feed = SimpleNamespace()
    engine._diag_throttle = lambda _key: True
    engine._symbol_precision = {}
    engine._last_retry_diag = 0.0
    placement_calls: list[dict[str, Any]] = []

    async def _fake_create_algo(params: dict[str, Any]) -> dict[str, Any]:
        placement_calls.append(dict(params))
        return {"algoId": f"algo-new-{len(placement_calls)}"}

    engine._create_algo_order = _fake_create_algo  # type: ignore[method-assign]
    engine._protection_algo_params = lambda order, **kw: {"type": getattr(order, "order_type", "STOP_MARKET"), **kw}  # type: ignore[method-assign]

    from beidou_shared.types import OrderSide as _OrderSide

    sl = SimpleNamespace(
        protection_id="sl-pos-p",
        position_id="pos-p",
        instrument_id="BTCUSDT",
        status=SimpleNamespace(value="CREATED"),
        is_active=lambda: False,
        quantity=SimpleNamespace(amount="1.0"),
        exchange_order_id=None,
        stop_type=SimpleNamespace(value="ATR_BASED"),
        take_profit_type=None,
        order_type="STOP_MARKET",
        trigger_price=SimpleNamespace(amount="99.0"),
    )
    tp = SimpleNamespace(
        protection_id="tp-pos-p",
        position_id="pos-p",
        instrument_id="BTCUSDT",
        status=SimpleNamespace(value="CREATED"),
        is_active=lambda: False,
        quantity=SimpleNamespace(amount="1.0"),
        exchange_order_id=None,
        stop_type=None,
        take_profit_type=SimpleNamespace(value="FIXED_RR"),
        order_type="TAKE_PROFIT_MARKET",
        trigger_price=SimpleNamespace(amount="103.0"),
    )
    engine._protection.restore_position_protection(
        SimpleNamespace(
            position_id="pos-p",
            instrument_id="BTCUSDT",
            venue_id="BINANCE",
            entry_price=100.0,
            quantity=1.0,
            side=_OrderSide.BUY,
            stop_loss=sl,
            take_profits=[tp],
            owner_id="owner-1",
            position_generation=1,
            session_id="session-1",
        )
    )

    # 品种有 2 个算法单,但属于其他命名空间/所有权 —— 不得为本持仓背书
    algos = [
        _venue_algo(
            "algo-other-sl",
            "BTCUSDT",
            side="SELL",
            order_type="STOP_MARKET",
            quantity="1.0",
            trigger_price="99.0",
            client_algo_id="shared-ext",
        ),
        _venue_algo(
            "algo-other-tp",
            "BTCUSDT",
            side="SELL",
            order_type="TAKE_PROFIT_MARKET",
            quantity="1.0",
            trigger_price="103.0",
            client_algo_id="shared-ext",
        ),
    ]

    async def _fake_inventory() -> list[dict[str, Any]]:
        return [dict(a) for a in algos]

    engine._get_open_algo_inventory = _fake_inventory  # type: ignore[method-assign]
    engine._flush_pending_protection_persist = lambda: None  # type: ignore[method-assign]

    await engine._retry_missing_protections({"BTCUSDT"})

    # 幽灵 SL/TP 必须重试提交,不得被 S41 跳过
    assert len(placement_calls) >= 1
    assert any("STOP_MARKET" in str(c.get("type")) for c in placement_calls)
    # SL 获得 ACK 后状态推进 ACTIVE,覆盖计数收敛
    assert engine._protection.all_positions()["pos-p"].stop_loss.status.value == "ACTIVE"


@pytest.mark.asyncio
async def test_retry_sweep_skips_symbols_with_durable_active_rows() -> None:
    """投影缺口自愈不再为已有 durable ACTIVE 行的品种制造 recovered-* 幽灵投影。"""
    engine = _adopt_engine(
        positions={"BTCUSDT": "1.0"},
        projection={"BTCUSDT": {"signed_quantity": "1.0", "entry_price": "100.0"}},
        protections=[
            _durable_row(
                protection_id="sl-pos-x",
                symbol="BTCUSDT",
                side="SELL",
                order_type="STOP_MARKET",
                quantity="1.0",
                position_id="pos-x",
                generation=1,
                algo_id="algo-x-sl",
            ),
            _durable_row(
                protection_id="tp-pos-x",
                symbol="BTCUSDT",
                side="SELL",
                order_type="TAKE_PROFIT_MARKET",
                quantity="1.0",
                position_id="pos-x",
                generation=1,
                algo_id="algo-x-tp",
                stop_type=None,
                take_profit_type="FIXED_RR",
            ),
        ],
    )
    engine._can_write = True
    engine._control = SimpleNamespace(
        get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"), execute_action=lambda action: None
    )
    engine._protection_retries = {}
    engine._pending_protection_persist = {}
    engine._venue_missing_streaks = {}
    engine._pending_protection_retry = set()
    engine._protection_exchange_attempted = set()
    engine._feed = SimpleNamespace()
    engine._diag_throttle = lambda _key: True
    engine._active_algo_ids = {"pos-x": {"algo-x-sl", "algo-x-tp"}}

    algos = [
        _venue_algo(
            "algo-x-sl",
            "BTCUSDT",
            side="SELL",
            order_type="STOP_MARKET",
            quantity="1.0",
            trigger_price="99.0",
            client_algo_id="bdp-x",
        ),
        _venue_algo(
            "algo-x-tp",
            "BTCUSDT",
            side="SELL",
            order_type="TAKE_PROFIT_MARKET",
            quantity="1.0",
            trigger_price="103.0",
            client_algo_id="bdp-x",
        ),
    ]

    async def _fake_inventory() -> list[dict[str, Any]]:
        return [dict(a) for a in algos]

    engine._get_open_algo_inventory = _fake_inventory  # type: ignore[method-assign]
    engine._flush_pending_protection_persist = lambda: None  # type: ignore[method-assign]

    await engine._retry_missing_protections({"BTCUSDT"})

    # durable 已有 ACTIVE 所有权 → 收养跳过、自愈投影缺口跳过:
    # 不得新增 recovered-* 投影,不得新增 PENDING 行
    assert set(engine._protection.all_positions()) == set()
    rows = engine._store.restore_protections()
    assert all(r["status"] == "ACTIVE" for r in rows)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# 6. fast-fill 竞态:子命令已终态时父意图补 ACK
# ---------------------------------------------------------------------------


def test_reconcile_terminal_child_parent_acks_when_children_confirmed() -> None:
    engine = _adopt_engine(positions={})
    acks: list[tuple[str, str]] = []

    def _ack(intent_id: str, idempotency_key: str = "") -> None:
        acks.append((intent_id, idempotency_key))

    engine._outbox = SimpleNamespace(
        restore_execution_plan=lambda _iid: SimpleNamespace(all_children_acknowledged=True),
        ack=_ack,
    )
    intent = SimpleNamespace(intent_id="intent-X", idempotency_key="idem-X")

    assert engine._reconcile_terminal_child_parent(intent) is True
    assert acks == [("intent-X", "idem-X")]


def test_reconcile_terminal_child_parent_fail_closed_on_incomplete_aggregate() -> None:
    engine = _adopt_engine(positions={})
    acks: list[str] = []

    def _ack(intent_id: str, idempotency_key: str = "") -> None:
        acks.append(intent_id)

    engine._outbox = SimpleNamespace(
        restore_execution_plan=lambda _iid: SimpleNamespace(all_children_acknowledged=False),
        ack=_ack,
    )
    intent = SimpleNamespace(intent_id="intent-X", idempotency_key="idem-X")

    assert engine._reconcile_terminal_child_parent(intent) is False
    assert acks == []


def test_reconcile_terminal_child_parent_fail_closed_on_missing_plan_or_outbox() -> None:
    engine = _adopt_engine(positions={})
    intent = SimpleNamespace(intent_id="intent-X", idempotency_key="idem-X")

    # 执行聚合缺失 → 不 ACK
    engine._outbox = SimpleNamespace(
        restore_execution_plan=lambda _iid: None,
        ack=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not ack")),
    )
    assert engine._reconcile_terminal_child_parent(intent) is False

    # outbox 未装配 → 不 ACK
    engine._outbox = None
    assert engine._reconcile_terminal_child_parent(intent) is False


# ---------------------------------------------------------------------------
# 7. TWAP 竞态:子命令终态重复转换不得中止执行计划
# ---------------------------------------------------------------------------


def test_transition_child_race_safe_continues_when_child_already_terminal() -> None:
    engine = _adopt_engine(positions={})
    raw_calls: list[dict[str, Any]] = []
    fresh = SimpleNamespace(marker="fresh-aggregate")

    class _Outbox:
        def transition_execution_child(self, intent_id, sequence, state, **kw):
            raw_calls.append({"intent_id": intent_id, "sequence": sequence, "state": state, **kw})
            raise ValueError(f"TERMINAL_CHILD_STATE:{'FILLED'}")

        def restore_execution_plan(self, intent_id):
            return fresh

    engine._outbox = _Outbox()

    agg = engine._transition_execution_child_race_safe(
        "intent-X", 0, "SENDING", event_id="send:intent-X:0"
    )

    assert agg is fresh
    assert len(raw_calls) == 1


def test_transition_child_race_safe_reraises_other_errors() -> None:
    engine = _adopt_engine(positions={})

    class _Outbox:
        def transition_execution_child(self, intent_id, sequence, state, **kw):
            raise ValueError("EXECUTION_COMMAND_FENCED_OR_CONCURRENT")

        def restore_execution_plan(self, intent_id):
            raise AssertionError("must not restore on non-terminal errors")

    engine._outbox = _Outbox()

    with pytest.raises(ValueError, match="EXECUTION_COMMAND_FENCED_OR_CONCURRENT"):
        engine._transition_execution_child_race_safe("intent-X", 0, "SENDING", event_id="e")


def test_transition_child_race_safe_reraises_when_aggregate_unavailable() -> None:
    engine = _adopt_engine(positions={})

    class _Outbox:
        def transition_execution_child(self, intent_id, sequence, state, **kw):
            raise ValueError("TERMINAL_CHILD_STATE:CANCELED")

        def restore_execution_plan(self, intent_id):
            return None

    engine._outbox = _Outbox()

    with pytest.raises(ValueError, match="TERMINAL_CHILD_STATE"):
        engine._transition_execution_child_race_safe("intent-X", 0, "SENDING", event_id="e")


# ---------------------------------------------------------------------------
# 8. 启动恢复挂载 durable 行时必须同步登记所有权映射
# ---------------------------------------------------------------------------


def test_restore_durable_protection_registers_active_algo_mapping() -> None:
    """_restore_durable_protection_projection 挂载投影后必须登记
    _active_algo_ids —— 否则 _cleanup_excess_orders 每轮
    PROTECTION_OWNER_MAPPING_INCOMPLETE → NO_NEW_RISK 钉死资格门
    (实测重启后 44/61 意图被 ELIGIBILITY_NO_NEW_RISK 拒绝)。"""
    engine = _adopt_engine(
        protections=[
            _durable_row(
                protection_id="sl-pos-x",
                symbol="BTCUSDT",
                side="SELL",
                order_type="STOP_MARKET",
                quantity="1.0",
                position_id="pos-x",
                generation=1,
                algo_id="algo-x-sl",
                trigger_price="99.0",
            ),
            _durable_row(
                protection_id="tp-pos-x",
                symbol="BTCUSDT",
                side="SELL",
                order_type="TAKE_PROFIT_MARKET",
                quantity="1.0",
                position_id="pos-x",
                generation=1,
                algo_id="algo-x-tp",
                trigger_price="103.0",
                stop_type=None,
                take_profit_type="FIXED_RR",
            ),
        ],
        positions={"BTCUSDT": "1.0"},
    )
    engine._protection_owner_unknown = False
    engine._position_entry_times = {}
    account = {
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "1.0", "entryPrice": "100.0"}]
    }
    inventory = [
        _venue_algo(
            "algo-x-sl",
            "BTCUSDT",
            side="SELL",
            order_type="STOP_MARKET",
            quantity="1.0",
            trigger_price="99.0",
            client_algo_id="bdp-x",
        ),
        _venue_algo(
            "algo-x-tp",
            "BTCUSDT",
            side="SELL",
            order_type="TAKE_PROFIT_MARKET",
            quantity="1.0",
            trigger_price="103.0",
            client_algo_id="bdp-x",
        ),
    ]

    ok = engine._restore_durable_protection_projection(account, inventory)

    assert ok is True
    assert engine._active_algo_ids.get("pos-x") == {"algo-x-sl", "algo-x-tp"}
    # 与 durable ACTIVE 行完全一致 → cleanup 的映射等式成立
    durable_ids = {
        str(r["exchange_order_id"])
        for r in engine._store.restore_protections()
        if r["status"] == "ACTIVE" and r["exchange_order_id"]
    }
    known_ids = {a for ids in engine._active_algo_ids.values() for a in ids}
    assert known_ids == durable_ids
