"""A slippage breach pages only when it can be told from noise (operator ruling 2026-10-02).

From 2026-09-17 the hourly check paged `滑点（主书）5.1 bps 高于假设的 4 bps（±2.0，与噪声不可分辨）` every hour,
twice.  The line said itself that it could not be told from noise.  The 09-30 system audit priced the options
(`docs/analysis/2026-09-30-system-audit.md`, D2) and the operator chose b2: page only when
|reading - limit| > 2 SE - the instrument's own `decisive` - and read the rest at review.  M-Q08 reads the
`slippage` block itself, so neither the instrument nor its judgement moves.
"""

from __future__ import annotations

from typing import Any

from beidou_live.reports import daily_alerts
from beidou_live.risk_budget import RiskBudgetParams, risk_budget_status
from tests.live.test_risk_budget_monitor import _cycle

PARAMS = RiskBudgetParams(min_slippage_fills=1)
ROWS = [_cycle(i, 100.0) for i in range(3)]


def _status(*prices: float) -> dict[str, Any]:
    """Buys against a 100.0 decision close, one per price, all on the newest bar."""
    trades = [
        {
            "bar_open_ms": ROWS[-1]["bar_open_ms"],
            "side": "BUY",
            "decision_close": 100.0,
            "avg_price": price,
            "executed_qty": "1",
        }
        for price in prices
    ]
    return risk_budget_status(ROWS, trades, PARAMS)


def test_a_breach_inside_its_error_bar_is_a_notice() -> None:
    out = _status(100.16, 99.95)  # +16 and -5 bps: 5.5 against 4, with an error bar of about 10
    slippage = out["slippage"]
    assert slippage["inside"] is False and slippage["decisive"] is False, "the instrument still reads the breach"
    assert out["status"] != "ALERT" and not any("滑点" in r for r in out["reasons"])
    assert len(out["notices"]) == 1 and "与噪声不可分辨" in out["notices"][0]

    alerts, notices = daily_alerts({"risk_budget": out})
    assert not any("滑点" in a for a in alerts), "nothing pages"
    assert [n for n in notices if n.startswith("风险预算提示：")] == ["风险预算提示：" + out["notices"][0]]


def test_a_breach_that_can_be_told_from_noise_still_pages() -> None:
    out = _status(100.2, 100.2, 100.2, 100.2)  # 20 bps four times: no disagreement at all
    assert out["slippage"]["decisive"] is True
    assert out["status"] == "ALERT" and out["notices"] == []
    alerts, _notices = daily_alerts({"risk_budget": out})
    assert any(a.startswith("风险预算告警：滑点") for a in alerts)


def test_a_breach_with_no_spread_at_all_pages_as_before() -> None:
    """`se` is None only when there is no reading; one fill has a spread of zero, and zero is decisive."""
    out = _status(101.0)
    assert out["slippage"]["se"] == 0.0 and out["slippage"]["decisive"] is True
    assert out["status"] == "ALERT" and any("滑点" in r for r in out["reasons"]) and out["notices"] == []
