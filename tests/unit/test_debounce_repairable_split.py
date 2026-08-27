"""A 类(可修复)阻断永不 LOCKED; B 类行为与旧版逐位一致; A+B 并存按 B。

分类规则 (spec, 2026-08-24):
- A 可修复 = 所有 gap reason ∈ {STOP_LOSS_QUANTITY_UNCOVERED, MISSING_SL,
  MISSING_TP, ORPHAN_PROTECTION_WITHOUT_VENUE_POSITION};
- coverage 检查 (R2): message issue token 全部 ∈ {MISSING_SL, MISSING_TP}
  且全部 entity_id ∈ 引擎本地所有权符号集才可修复;
- R12: 分类只吃每 tick 新鲜事实 —— 仅 gap_detail 的 evidence.gaps 提供
  A 证据; incidents 的 gap_reasons 是重发时的陈旧快照, 不参与分类
  (只作可观测性展示), 任何 incidents blocker 有它在即整体按 B;
- 其余 (含 LOCAL_POSITION_WITHOUT_VENUE_FACT、execution_fact、reconciliation、
  supervisor、realtime) 全按 B; A+B 并存按 B; reason 缺失/无法分类按 B。
"""

from types import SimpleNamespace

from beidou_core.engine import AutonomousEngine
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus, HealthDebounce
from beidou_launcher.supervisor import _classify_repairable


def _feed_n(deb: HealthDebounce, n: int, repairable: bool) -> None:
    for _ in range(n):
        deb.feed(True, repairable=repairable)


def test_repairable_blocks_never_lock() -> None:
    deb = HealthDebounce(degrade_after=6, lock_after=12, window_seconds=200.0)
    _feed_n(deb, 100, repairable=True)
    # 永不 LOCKED: A 类阻断仍走 DEGRADED 路径 (6 轮后降级), 但不升级
    assert deb.feed(True, repairable=True) == "DEGRADED"
    # 恢复路径不受影响
    for _ in range(3):
        deb.feed(False, repairable=False)
    assert deb.feed(False, repairable=False) == "RUNNING"


def test_non_repairable_locks_at_threshold_unchanged() -> None:
    deb = HealthDebounce(degrade_after=6, lock_after=12, window_seconds=200.0)
    for _ in range(11):
        deb.feed(True, repairable=False)
    assert deb.feed(True, repairable=False) == "LOCKED"  # 第 12 轮, 旧语义不变


def test_mixed_blocks_count_as_non_repairable() -> None:
    deb = HealthDebounce(degrade_after=6, lock_after=12, window_seconds=200.0)
    _feed_n(deb, 11, repairable=True)
    # 第 12 轮混入 B 类 → 按 B 计, 立即 LOCKED
    assert deb.feed(True, repairable=False) == "LOCKED"


def test_mixed_window_with_older_b_still_locks() -> None:
    """A+B 并存按 B: 阻断连击窗口内出现过 B 样本, 后续 A 轮整体按 B 计数。"""
    deb = HealthDebounce(degrade_after=6, lock_after=12, window_seconds=200.0)
    _feed_n(deb, 11, repairable=False)
    assert deb.feed(True, repairable=True) == "LOCKED"


def test_repairable_only_degrades_after_threshold() -> None:
    deb = HealthDebounce(degrade_after=6, lock_after=12, window_seconds=200.0)
    for _ in range(5):
        deb.feed(True, repairable=True)
    assert deb.feed(True, repairable=True) == "DEGRADED"


def test_classifier_accepts_repairable_gap_and_rejects_unknown() -> None:
    """_classify_repairable: A 类 reason → True; 未知/缺失 → False (按 B)。"""
    supervisor = _bind_classifier()

    a_only = CheckResult(
        check_id="runtime.safety.protection_gap_detail",
        name="x",
        status=CheckStatus.WARN,
        severity=CheckSeverity.P1,
        message="x",
        evidence={"gaps": [{"symbol": "BTCUSDT", "reason": "STOP_LOSS_QUANTITY_UNCOVERED"}], "repairable": True},
    )
    assert supervisor._classify_repairable([a_only]) is True

    b_gap = CheckResult(
        check_id="runtime.safety.protection_gap_detail",
        name="x",
        status=CheckStatus.WARN,
        severity=CheckSeverity.P1,
        message="x",
        evidence={"gaps": [{"symbol": "BTCUSDT", "reason": "LOCAL_POSITION_WITHOUT_VENUE_FACT"}], "repairable": False},
    )
    assert supervisor._classify_repairable([b_gap]) is False

    unknown = CheckResult(
        check_id="runtime.health.market_data",
        name="x",
        status=CheckStatus.FAIL,
        severity=CheckSeverity.P0,
        message="x",
        evidence={"source": "exchange"},
    )
    assert supervisor._classify_repairable([unknown]) is False


def test_classifier_empty_blockers_is_non_repairable() -> None:
    supervisor = _bind_classifier()
    assert supervisor._classify_repairable([]) is False


def test_classifier_gap_reason_missing_is_b() -> None:
    """reason 缺失 → 按 B: 任一 gap 缺 reason, 即使其余为 A 也整体 False。"""
    supervisor = _bind_classifier()
    gap_missing_reason = CheckResult(
        check_id="runtime.safety.protection_gap_detail",
        name="x",
        status=CheckStatus.FAIL,
        severity=CheckSeverity.P1,
        message="x",
        evidence={
            "gaps": [
                {"symbol": "BTCUSDT", "reason": "MISSING_SL"},
                {"symbol": "ETHUSDT", "reason": ""},
            ]
        },
    )
    assert supervisor._classify_repairable([gap_missing_reason]) is False


def _bind_classifier(engine: object | None = None) -> AutonomousEngine:
    """把模块级 _classify_repairable 绑定到裸 AutonomousEngine 容器上。"""
    supervisor = AutonomousEngine.__new__(AutonomousEngine)  # 仅作绑定容器
    if engine is not None:
        supervisor.engine = engine
    supervisor._classify_repairable = _classify_repairable.__get__(supervisor, type(supervisor))
    return supervisor


def _coverage_blocker(
    *, entity_id: str = "BTCUSDT", message: str = "Protection: MISSING_SL, MISSING_TP"
) -> CheckResult:
    return CheckResult(
        check_id="runtime.safety.protection_coverage",
        name="持仓保护覆盖",
        status=CheckStatus.FAIL,
        severity=CheckSeverity.P0,
        message=message,
        evidence={"entity_type": "position", "entity_id": entity_id},
    )


def _local_engine() -> SimpleNamespace:
    return SimpleNamespace(
        _position_generation={"BTCUSDT": 1},
        _position_projection={"ETHUSDT": {"symbol": "ETHUSDT"}},
    )


def test_coverage_locally_owned_missing_sl_tp_is_repairable() -> None:
    """R2: coverage 阻断 — MISSING_SL/MISSING_TP 且 entity 本地所有 → A。"""
    supervisor = _bind_classifier(_local_engine())
    assert supervisor._classify_repairable([_coverage_blocker()]) is True
    assert supervisor._classify_repairable([_coverage_blocker(message="Protection: MISSING_SL")]) is True


def test_coverage_with_dup_or_ghost_is_non_repairable() -> None:
    """R2: 含 DUP(...)/GHOST/未知 token → fail-closed 按 B。"""
    supervisor = _bind_classifier(_local_engine())
    assert supervisor._classify_repairable([_coverage_blocker(message="Protection: MISSING_SL, DUP(2)")]) is False
    assert supervisor._classify_repairable([_coverage_blocker(message="Protection: GHOST")]) is False
    assert supervisor._classify_repairable([_coverage_blocker(message="Protection: WEIRD_ISSUE")]) is False
    assert (
        supervisor._classify_repairable([_coverage_blocker(message="MONITORING_CHECK_ERROR:TimeoutError:x")]) is False
    )


def test_coverage_non_local_entity_is_non_repairable() -> None:
    """R2: 共享 testnet 账户的外部持仓 (entity 非本地所有) → fail-closed。"""
    supervisor = _bind_classifier(_local_engine())
    assert supervisor._classify_repairable([_coverage_blocker(entity_id="DOGEUSDT")]) is False
    assert supervisor._classify_repairable([_coverage_blocker(entity_id="")]) is False


def test_coverage_without_engine_is_non_repairable() -> None:
    """引擎未就绪 (engine 缺失) → 所有权不可证明 → 按 B。"""
    supervisor = _bind_classifier(None)
    assert supervisor._classify_repairable([_coverage_blocker()]) is False


def test_mixed_gap_and_coverage_both_repairable_is_true() -> None:
    """gap_detail 与 coverage 同时存在且各自可修复 → 整体 True。"""
    supervisor = _bind_classifier(_local_engine())
    gap = CheckResult(
        check_id="runtime.safety.protection_gap_detail",
        name="x",
        status=CheckStatus.FAIL,
        severity=CheckSeverity.P1,
        message="x",
        evidence={"gaps": [{"symbol": "BTCUSDT", "reason": "MISSING_SL"}], "repairable": True},
    )
    assert supervisor._classify_repairable([gap, _coverage_blocker()]) is True


def test_mixed_gap_and_coverage_any_non_repairable_is_false() -> None:
    """gap_detail 与 coverage 并存时任一非可修复 → 整体 False (R2 注)。"""
    supervisor = _bind_classifier(_local_engine())
    b_gap = CheckResult(
        check_id="runtime.safety.protection_gap_detail",
        name="x",
        status=CheckStatus.FAIL,
        severity=CheckSeverity.P1,
        message="x",
        evidence={"gaps": [{"symbol": "BTCUSDT", "reason": "LOCAL_POSITION_WITHOUT_VENUE_FACT"}], "repairable": False},
    )
    assert supervisor._classify_repairable([b_gap, _coverage_blocker()]) is False
    # coverage 非可修复 (外部持仓) + gap_detail 可修复 → 整体 False
    gap = CheckResult(
        check_id="runtime.safety.protection_gap_detail",
        name="x",
        status=CheckStatus.FAIL,
        severity=CheckSeverity.P1,
        message="x",
        evidence={"gaps": [{"symbol": "BTCUSDT", "reason": "MISSING_SL"}], "repairable": True},
    )
    assert supervisor._classify_repairable([gap, _coverage_blocker(entity_id="DOGEUSDT")]) is False
