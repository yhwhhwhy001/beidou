"""gap reason 必须从引擎贯通到 runtime 检查 (A/B 分流的输入)。

链路: 引擎 `_last_protection_gap_detail` → runtime 检查
`runtime.safety.protection_gap_detail` 的 evidence,
供 supervisor 区分"引擎可修复的缺口"与"真故障"。
"""

from __future__ import annotations

from beidou_core.engine import AutonomousEngine
from beidou_launcher.models import CheckStatus
from beidou_launcher.runtime import collect_runtime_checks

# collect_runtime_checks 的其余必需参数与 _runtime_engine 无关, 直接用默认替身
_RUNTIME_KWARGS = {
    "mode": "testnet",
    "port": 9090,
    "resume_authorized": True,
    "algorithm_probe": {"ok": True},
    "last_error_count": 0,
}


def _bare_engine(gap_detail: list[dict[str, str]]) -> AutonomousEngine:
    """最小引擎替身: 只提供 gap 明细，其余走 getattr 默认。"""
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._last_protection_gap_detail = list(gap_detail)
    return engine


def test_gap_reasons_appear_in_runtime_check() -> None:
    engine = _bare_engine([{"symbol": "BTCUSDT", "reason": "STOP_LOSS_QUANTITY_UNCOVERED"}])

    checks, _ = collect_runtime_checks(engine=engine, **_RUNTIME_KWARGS)
    gap_checks = [c for c in checks if c.check_id == "runtime.safety.protection_gap_detail"]
    assert gap_checks, "runtime checks 必须包含 protection_gap_detail"
    # R1: 有缺口必须是 FAIL —— 只有 FAIL/UNKNOWN 的 P0/P1 检查才进
    # supervisor blockers 与防抖, WARN 永远进不了 blockers, A/B 分流会
    # 静默失效。此断言钉住该语义, 防止回归到简报初稿的 WARN。
    assert gap_checks[0].status == CheckStatus.FAIL
    evidence = gap_checks[0].evidence
    assert evidence["gaps"][0]["reason"] == "STOP_LOSS_QUANTITY_UNCOVERED"
    assert evidence["repairable"] is True


def test_unknown_reason_defaults_repairable_false() -> None:
    engine = _bare_engine([{"symbol": "X", "reason": "LOCAL_POSITION_WITHOUT_VENUE_FACT"}])

    checks, _ = collect_runtime_checks(engine=engine, **_RUNTIME_KWARGS)
    gap_checks = [c for c in checks if c.check_id == "runtime.safety.protection_gap_detail"]
    assert gap_checks and gap_checks[0].evidence["repairable"] is False


def test_no_gaps_reports_pass_and_not_repairable() -> None:
    engine = _bare_engine([])

    checks, _ = collect_runtime_checks(engine=engine, **_RUNTIME_KWARGS)
    gap_checks = [c for c in checks if c.check_id == "runtime.safety.protection_gap_detail"]
    assert gap_checks
    assert gap_checks[0].status == CheckStatus.PASS
    assert gap_checks[0].evidence["repairable"] is False
