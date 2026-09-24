"""清单 #10.9 / #10.10：每笔成交的事前成本估计摆在它的事后成本旁边。

`report_execution.per_order_tca` 只报告，不告警。这个文件钉住五件会静默坏掉的事：

一、事前估计只用下单那一刻已经收盘的 bar。把决策 bar 之后的数据改掉，估计必须逐位不变。
二、事后滑点与 M-Q08 是同一批成交、同一套算术。全书与主书两个读数都要对得上；对不上时块里要说差在哪。
三、归档定不了价的成交照样计入实际滑点。它只是不拆、不估，并按原因计数。
四、手续费另列，读 attribution 入账的数。一个 (bar, symbol) 对不上唯一一行就不读。
五、这一块坏了，日报的其余部分与告警一字不变。

第一条测试的 fixture 是真实行。成交是 `trades.jsonl` 2026-09-24 那份的第 256 行（16:00:28Z，
AKEUSDT BUY），手续费是 `attribution.jsonl` 第 100 行。决策 bar 与前后各一根取 AKEUSDT 归档的原值。
更早的历史是合成的，只用来让 720 根窗口的 sigma 与 ADV 有数可算。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from beidou_data.store import KlineStore
from beidou_live import report_execution
from beidou_live.report_execution import TCA_PARTICIPATION_EDGES, _by_participation, per_order_tca, tca_lines
from beidou_live.reports import daily_alerts, daily_markdown, daily_payload
from beidou_live.risk_budget import RiskBudgetParams, books_by_bar, slippage_bps
from beidou_live.state import StateStore
from beidou_shared.config import load_yaml

ROOT = Path(__file__).resolve().parents[2]
HOUR = 3_600_000
BAR = 1_790_262_000_000  # 2026-09-24T15:00Z, the decision bar of the real fill below
COSTS = load_yaml(ROOT / "config/costs.yaml")

# `.beidou/live/trades.jsonl`, line 256 of the 2026-09-24 copy: every field as written.
REAL_FILL: dict[str, Any] = {
    "at": "2026-09-24T16:00:28+00:00",
    "avg_price": 0.044624,
    "bar_open_ms": 1790262000000,
    "client_order_id": "bd-1790262000000-AKEUSDT",
    "current_notional": 0.0,
    "decision_close": 0.044588,
    "error": "",
    "executed_qty": "2943",
    "late_seconds": 28.192,
    "note": "",
    "order_id": "322947312",
    "price": 0.04458192,
    "quantity": "2943",
    "reduce_only": False,
    "side": "BUY",
    "status": "FILLED",
    "symbol": "AKEUSDT",
    "target_notional": 131.23442731096677,
    "target_weight": 0.010033114803632767,
    "venue_status": "FILLED",
}
# `.beidou/live/attribution.jsonl`, line 100: the next cycle's income window, keyed by the bar that traded.
REAL_INCOME: dict[str, Any] = {
    "at": "2026-09-24T17:00:25+00:00",
    "bar_open_ms": 1790262000000,
    "basis": "net_exposure",
    "by_strategy": {"tsmom": -0.06566421},
    "by_symbol": {
        "AKEUSDT": {
            "COMMISSION": -0.06566421,
            "FUNDING_FEE": 0.0,
            "INSURANCE_CLEAR": 0.0,
            "REALIZED_PNL": 0.0,
            "total": -0.06566421,
        }
    },
    "cancelled": {},
    "external_flows": {"by_type": {}, "rows": 0, "total": 0.0},
    "foreign": {"by_symbol": {}, "reconciled": True, "rows": 0, "total": 0},
    "since_ms": 1790265625997,
    "total": -0.06566421,
    "unattributed": 0.0,
    "until_ms": 1790269226956,
}
# AKEUSDT's archive, 14:00 to 16:00 on 2026-09-24: the decision bar is the middle one.
REAL_BARS = [
    (1790258400000, 0.043546, 0.045875, 0.043476, 0.045525, 202807875.0, 1790261999999, 9075448.32902, 238455),
    (1790262000000, 0.045526, 0.045986, 0.043829, 0.044588, 116425207.0, 1790265599999, 5242097.489977, 155373),
    (1790265600000, 0.044591, 0.044728, 0.04187, 0.042107, 138959339.0, 1790269199999, 6004885.742603, 146125),
]


def _archive(
    root: Path, *, symbol: str = "AKEUSDT", history: int = 800, after: list[tuple[Any, ...]] | None = None
) -> None:
    """Synthetic history up to the real bars, then the real bars, then whatever `after` adds."""
    rng = np.random.default_rng(7)
    first = REAL_BARS[0][0] - history * HOUR
    closes = 0.04 * np.cumprod(1.0 + rng.normal(0.0, 0.01, history))
    volumes = 5e6 * (1.0 + 0.5 * np.sin(np.arange(history) / 10.0))
    synthetic = [
        (first + i * HOUR, c, c, c, c, v / c, first + (i + 1) * HOUR - 1, v, 100)
        for i, (c, v) in enumerate(zip(closes, volumes, strict=True))
    ]
    rows = [(*row, 0.0, 0.0) for row in [*synthetic, *REAL_BARS, *(after or [])]]
    frame = pd.DataFrame(
        rows,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trades",
            "taker_buy_base",
            "taker_buy_quote",
        ],
    )
    KlineStore(root).append(symbol, "1h", frame)


def _store(root: Path, trades: Sequence[dict[str, Any]], income: Sequence[dict[str, Any]] = ()) -> StateStore:
    store = StateStore(root / "live")
    for i in range(3):
        bar = BAR - (2 - i) * HOUR
        store.append_cycle({"bar_open_ms": bar, "equity": 13_000.0, "construction": "c", "registry": "r"})
    for trade in trades:
        store.append_trade(trade)
    for row in income:
        store.append_attribution(row)
    return store


def test_a_real_fill_is_split_at_the_next_open_and_priced_from_what_had_closed(tmp_path: Path) -> None:
    _archive(tmp_path / "archive")
    store = _store(tmp_path, [REAL_FILL], [REAL_INCOME])
    block = per_order_tca(store, RiskBudgetParams(), data_root=tmp_path / "archive", costs=COSTS)

    reference, filled = 0.044588, 0.044624
    slippage = (filled - reference) / reference * 1e4  # a BUY above the decision close costs
    gap = (0.044591 - reference) / reference * 1e4  # the next bar's archived open
    post = block["post_trade"]
    assert post["slippage"]["value"] == pytest.approx(slippage, rel=1e-12)
    assert post["gap"]["value"] == pytest.approx(gap, rel=1e-12)
    assert post["fill"]["value"] == pytest.approx(slippage - gap, rel=1e-12)
    assert post["gap"]["value"] + post["fill"]["value"] == pytest.approx(post["split_slippage"]["value"], abs=1e-12)
    assert post["late_seconds_median"] == pytest.approx(28.192)

    # The estimate, computed the way `impact_costs` and the 2026-09-09 VWAP measurement compute it -
    # pandas rolling windows over the whole series - read at the decision bar.  Independent of the
    # slicing the reading does, which is the point of computing it twice.
    frame = KlineStore(tmp_path / "archive").load("AKEUSDT", "1h").set_index("open_time")
    sigma = frame["close"].pct_change().rolling(720, min_periods=180).std().loc[BAR] * math.sqrt(24)
    adv = frame["quote_volume"].rolling(720, min_periods=1).mean().loc[BAR] * 24
    planned = 2943 * 0.04458192  # quantity x price: what the order was sized at before it went out
    impact = 1.0 * sigma * math.sqrt(planned / adv) * 1e4
    pre = block["pre_trade"]
    assert pre["impact"]["value"] == pytest.approx(impact, rel=1e-9)
    assert pre["predicted"]["value"] == pytest.approx(2.0 + impact, rel=1e-9)
    assert pre["participation"]["median"] == pytest.approx(planned / 5242097.489977, rel=1e-12)
    assert block["predicted_minus_actual"]["value"] == pytest.approx(2.0 + impact - slippage, rel=1e-9)
    assert pre["unestimated"] == {} and post["unsplit"] == {}

    # 0.06566421 USDT on 131.33 USDT: the name's booked 5.0 bps, not a model number.
    assert post["fee"]["booked"]["value"] == pytest.approx(0.06566421 / (filled * 2943) * 1e4, rel=1e-12)
    assert post["fee"]["booked"]["value"] == pytest.approx(5.0, abs=1e-3)
    assert post["fee"]["model_bps"] == 5.0

    recon = block["reconciliation"]["combined"]
    assert recon["agrees"] is True and recon["m_q08_fills"] == 1
    assert recon["difference"] == pytest.approx(0.0, abs=1e-9)


def test_the_estimate_reads_nothing_after_the_decision_bar(tmp_path: Path) -> None:
    """Rewrite every bar after the decision bar: the estimate may not move, and the split must."""
    _archive(tmp_path / "as_traded")
    later = [(BAR + 2 * HOUR, 9.9, 9.9, 9.9, 9.9, 1.0, BAR + 3 * HOUR - 1, 1e12, 1)]
    _archive(tmp_path / "rewritten", after=later)
    rewritten = KlineStore(tmp_path / "rewritten").load("AKEUSDT", "1h")
    rewritten.loc[rewritten["open_time"] == BAR + HOUR, ["open", "close", "quote_volume"]] = [0.05, 0.05, 1e12]
    KlineStore(tmp_path / "rewritten").append("AKEUSDT", "1h", rewritten)

    store = _store(tmp_path, [REAL_FILL], [REAL_INCOME])
    before = per_order_tca(store, data_root=tmp_path / "as_traded", costs=COSTS)
    after = per_order_tca(store, data_root=tmp_path / "rewritten", costs=COSTS)

    assert after["pre_trade"] == before["pre_trade"], "the estimate saw a bar the order could not have"
    assert after["predicted_minus_actual"] == before["predicted_minus_actual"]
    assert after["post_trade"]["gap"]["value"] != before["post_trade"]["gap"]["value"]


def _fill(bar: int, symbol: str, side: str, price: float, **extra: Any) -> dict[str, Any]:
    return {
        **REAL_FILL,
        "bar_open_ms": bar,
        "client_order_id": f"bd-{bar}-{symbol}",
        "symbol": symbol,
        "side": side,
        "avg_price": price,
        "decision_close": 0.044588,
        **extra,
    }


def test_the_mean_is_mq08s_on_the_same_fills_and_says_where_it_would_part(tmp_path: Path) -> None:
    _archive(tmp_path / "archive")
    trades = [
        _fill(BAR, "AKEUSDT", "BUY", 0.044624),
        _fill(BAR - HOUR, "AKEUSDT", "SELL", 0.044500, executed_qty="1000"),
        _fill(BAR - HOUR, "NOARCHIVEUSDT", "BUY", 0.044700),  # counted, not split, not estimated
        _fill(BAR, "AKEUSDT", "BUY", 0.046, decision_close=None),  # M-Q08 counts it apart; so does this
        {**_fill(BAR, "AKEUSDT", "SELL", 0.04), "bar_open_ms": None, "flatten": True},  # not the book's execution
        _fill(BAR - 40 * 24 * HOUR, "AKEUSDT", "BUY", 0.05),  # outside M-Q08's 30 days
    ]
    store = _store(tmp_path, trades)
    block = per_order_tca(store, RiskBudgetParams(), data_root=tmp_path / "archive", costs=COSTS)

    mq08 = slippage_bps(store.read_jsonl(store.trades_path), RiskBudgetParams(), latest_ms=BAR)
    assert block["post_trade"]["slippage"] == mq08["combined"], "same fills, same arithmetic, same dict"
    assert block["fills"] == 3 and block["without_reference"] == mq08["without_reference"] == 1
    assert block["reconciliation"]["combined"]["agrees"] is True
    assert block["pre_trade"]["unestimated"] == {"NOARCHIVEUSDT 归档没有这个标的": 1}
    assert block["post_trade"]["unsplit"] == {"NOARCHIVEUSDT 归档没有这个标的": 1}
    assert block["post_trade"]["gap"]["fills"] == 2, "the unpriced fill stays in the mean and leaves the split"

    # Where the two would part, said in the block rather than left for a reader to find.
    parted = report_execution._reconciled({"value": 5.0, "fills": 3}, {"value": 5.0, "fills": 2})
    assert parted["agrees"] is False and "成交笔数不同" in str(parted["why"])
    drifted = report_execution._reconciled({"value": 5.0, "fills": 3}, {"value": 5.1, "fills": 3})
    assert drifted["agrees"] is False and "算术" in str(drifted["why"])


def test_the_split_by_book_reconciles_with_the_reading_mq08_judges(tmp_path: Path) -> None:
    """G9's split, per fill, on the report's own rows - where a SKIPPED row is not a bar that traded.

    Read from every row, the restart's skip below would end the window an hour late and drop the fill
    on its first bar: M-Q08 would count three fills and this block two.
    """
    _archive(tmp_path / "archive")
    store = StateStore(tmp_path / "live")
    carried = {"tsmom": {"AKEUSDT": 1.0, "BBBUSDT": -1.0}, "flow": {"BBBUSDT": -1.0}}
    for i in range(3):
        bar = BAR - (2 - i) * HOUR
        books = {"tsmom": "main", "flow": "flow_short"}
        store.append_cycle({"bar_open_ms": bar, "equity": 13_000.0, "books": books, "contributions": carried})
    # A restart that woke outside the next bar's window, with the keys a real one carries (2026-09-23T16:41Z).
    store.append_cycle(
        {
            "at": "2026-09-24T17:41:45+00:00",
            "bar": "2026-09-24T16:00:00+00:00",
            "bar_open_ms": BAR + HOUR,
            "dry_run": False,
            "late_seconds": 2505.204,
            "missed_rebalances": 1,
            "orders": [],
            "phase": "SKIPPED",
            "reason": "restart outside the rebalance window",
            "targets": {},
            "window_seconds": 85.861,
        }
    )
    store.append_trade(_fill(BAR, "AKEUSDT", "BUY", 0.044624))  # the main book alone carried it
    store.append_trade(_fill(BAR, "BBBUSDT", "SELL", 0.044500))  # both books carried it
    store.append_trade(_fill(BAR - 30 * 24 * HOUR, "AKEUSDT", "BUY", 0.0446))  # M-Q08's first bar; no books
    block = per_order_tca(store, RiskBudgetParams(), data_root=tmp_path / "archive", costs=COSTS)

    rows = [row for row in store.read_jsonl(store.cycles_path) if row.get("equity") is not None]
    mq08 = slippage_bps(
        store.read_jsonl(store.trades_path), RiskBudgetParams(), latest_ms=BAR, books_at=books_by_bar(rows)
    )
    recon = block["reconciliation"]
    assert recon["judged"] == mq08["judged"] == "main_only"
    assert recon["main_only"]["fills"] == mq08["by_group"]["main_only"]["fills"] == 1
    assert recon["main_only"]["value"] == pytest.approx(mq08["by_group"]["main_only"]["value"], abs=1e-9)
    assert recon["combined"]["fills"] == mq08["combined"]["fills"] == 3
    assert recon["main_only"]["agrees"] and recon["combined"]["agrees"]


def test_the_fee_is_read_only_where_one_commission_meets_one_fill(tmp_path: Path) -> None:
    _archive(tmp_path / "archive")
    store = _store(tmp_path, [REAL_FILL], [REAL_INCOME, {**REAL_INCOME, "at": "2026-09-24T17:30:00+00:00"}])
    block = per_order_tca(store, data_root=tmp_path / "archive", costs=COSTS)
    assert block["post_trade"]["fee"]["booked"]["fills"] == 0, "two income rows on one key: ambiguous, not summed"
    assert block["post_trade"]["fee"]["unmatched"] == 1 and block["post_trade"]["fee"]["not_yet_read"] == 0
    assert "1 笔在 attribution 里对不上唯一一行" in tca_lines(block)["手续费（另列，不在滑点里）"]

    # The newest bar's commission is read by the next cycle: not missing, only not read yet.
    newest = per_order_tca(_store(tmp_path / "newest", [REAL_FILL]), data_root=tmp_path / "archive", costs=COSTS)
    assert newest["post_trade"]["fee"]["not_yet_read"] == 1 and newest["post_trade"]["fee"]["unmatched"] == 0
    assert "1 笔下个周期才读到手续费" in tca_lines(newest)["手续费（另列，不在滑点里）"]


def test_participation_buckets_are_fixed_decades() -> None:
    readings = [
        {"participation": p, "error": e, "slippage": s, "predicted": s + e, "notional": 100.0}
        for p, e, s in ((5e-6, -1.0, 3.0), (1e-5, -2.0, 4.0), (2e-4, 0.5, 1.0), (1e-3, -6.0, 8.0))
    ]
    buckets = _by_participation(readings)
    assert TCA_PARTICIPATION_EDGES == (1e-5, 1e-4, 1e-3)
    assert [row["bucket"] for row in buckets] == ["<1e-05", "1e-05–1e-04", "1e-04–1e-03", "≥1e-03"]
    fills = [row["predicted_minus_actual"]["fills"] for row in buckets]
    assert fills == [1, 1, 1, 1], "an edge belongs to the bucket above it"
    assert buckets[1]["predicted_minus_actual"]["value"] == pytest.approx(-2.0)
    assert buckets[1]["slippage"] == pytest.approx(4.0) and buckets[1]["predicted"] == pytest.approx(2.0)


def _day_store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "live")
    for i in range(24):
        bar = BAR - (23 - i) * HOUR
        store.append_cycle(
            {
                "bar_open_ms": bar,
                "equity": 13_000.0,
                "registry": "r",
                "construction": "c",
                "skip": False,
                "guard_reasons": [],
                "targets": {},
                "orders": [],
            }
        )
    store.append_trade(REAL_FILL)
    store.append_attribution(REAL_INCOME)
    return store


def test_the_daily_report_carries_it_after_mq08_and_says_what_the_estimate_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(ROOT)  # the cost model is read from `config/costs.yaml`, as the loop's profile names it
    _archive(tmp_path / "archive")
    payload = daily_payload(_day_store(tmp_path), "2026-09-24", {}, data_root=tmp_path / "archive")
    keys = list(payload)
    assert keys.index("tca") == keys.index("execution_fidelity") + 1
    recon, slippage = payload["tca"]["reconciliation"], payload["risk_budget"]["slippage"]
    assert recon["combined"]["m_q08"] == slippage["combined"]["value"] and recon["combined"]["agrees"]
    markdown = daily_markdown(payload)
    headings = [line for line in markdown.splitlines() if line.startswith("## ")]
    at = headings.index("## Execution fidelity (M-Q08, four clauses)")
    assert headings[at + 1] == "## Per-order TCA (#10.9 / #10.10, reported only)"
    section = markdown.split("## Per-order TCA (#10.9 / #10.10, reported only)")[1].split("\n## ")[0]
    for said in ("离线重建", "只用决策时刻已收盘的 bar", "没校准", "对得上", "bookTicker 归档止于 2024-04", "合成的"):
        assert said in section, said
    # Reported only: taking the block out moves no alert and no notice.
    assert daily_alerts(payload) == daily_alerts({k: v for k, v in payload.items() if k != "tca"})


def test_a_broken_block_leaves_the_rest_of_the_report_and_its_alerts_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(ROOT)
    _archive(tmp_path / "archive")
    store = _day_store(tmp_path)
    clean = daily_payload(store, "2026-09-24", {}, data_root=tmp_path / "archive")

    def broken(*_args: object, **_kwargs: object) -> Any:
        raise ZeroDivisionError("injected by the test")

    monkeypatch.setattr(report_execution, "_tca", broken)
    payload = daily_payload(store, "2026-09-24", {}, data_root=tmp_path / "archive")
    assert payload["tca"] == {"error": "ZeroDivisionError: injected by the test"}
    assert daily_alerts(payload) == daily_alerts(clean)
    markdown = daily_markdown(payload)
    sections = [line for line in markdown.splitlines() if line.startswith("## ")]
    assert sections == [line for line in daily_markdown(clean).splitlines() if line.startswith("## ")]
    section = markdown.split("## Per-order TCA (#10.9 / #10.10, reported only)")[1].split("\n## ")[0]
    assert "读不出，这一块整块失败" in section and "ZeroDivisionError: injected by the test" in section
