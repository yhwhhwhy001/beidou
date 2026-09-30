"""日报的盈亏按可动用 USDT 读，起点是账户重置后的 5000 U（操作者 2026-09-30 要求）。

原来日报第一段「Equity」印的是总权益：`totalMarginBalance`，含 USDC 与 BTC 抵押品按标记价折算的部分。
多资产保证金账户上，BTC 一动，这一行就跟着动，那不是书的盈亏。回撤早在 2026-09-20 改成 USDT 自己的尺子
（`usdt_drawdown_state`）；盈亏还停在总权益上，同一份日报的头两个数量在两条序列上。

两处口径都钉在这里：

- 当日变化从**前一天最后一个周期**算起。只从当天第一个周期算，会漏掉当天第一根 bar 的变动。
  下面的夹具就是这种情况：00:00Z 那根 bar 的收盘周期记在 09-28 名下，09-29 的第一行已是 01:00Z，
  中间差 −27.08 U。
- 累计盈亏对 profile 里的 `risk_budget.usdt_baseline`。它是账户事实，不从数据里推：demo 重置表现为 TRANSFER 行，
  而 TRANSFER 也可能是真充值。

夹具是 `.beidou/live/cycles.jsonl` 的三行（09-28 最后一行、09-29 第一行与最后一行），只抄这里读的字段，数值逐位照抄。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from beidou_live.report_risk import usdt_pnl
from beidou_live.reports import daily_markdown, daily_payload, weekly_markdown, weekly_payload
from beidou_live.risk_budget import RiskBudgetParams
from beidou_live.state import StateStore
from beidou_shared.config import load_yaml

ROOT = Path(__file__).resolve().parents[2]

LAST_OF_0928: dict[str, Any] = {
    "at": "2026-09-29T00:01:05+00:00",
    "as_of_ms": 1790636400000,
    "bar_open_ms": 1790636400000,
    "equity": 13317.30023633,
    "collateral": {
        "collateral": 5737.73596358,
        "equity": 13317.30023633,
        "share": 0.43084828469416664,
        "usdt_equity": 7579.56427275,
    },
}
FIRST_OF_0929: dict[str, Any] = {
    "at": "2026-09-29T01:00:29+00:00",
    "as_of_ms": 1790640000000,
    "bar_open_ms": 1790640000000,
    "equity": 13286.89583522,
    "collateral": {
        "collateral": 5734.4095737200005,
        "equity": 13286.89583522,
        "share": 0.4315838435731254,
        "usdt_equity": 7552.4862615,
    },
}
LAST_OF_0929: dict[str, Any] = {
    "at": "2026-09-29T14:46:26+00:00",
    "as_of_ms": 1790686800000,
    "bar_open_ms": 1790686800000,
    "equity": 13379.29129625,
    "collateral": {
        "collateral": 5743.574900619999,
        "equity": 13379.29129625,
        "share": 0.42928842592954314,
        "usdt_equity": 7635.71639563,
    },
}
ROWS = [LAST_OF_0928, FIRST_OF_0929, LAST_OF_0929]


def test_the_day_starts_where_the_previous_day_ended() -> None:
    out = usdt_pnl(ROWS, "2026-09-29", baseline=5000.0)
    assert out["usdt_start"] == 7579.56427275
    assert out["usdt_start_from"] == "2026-09-29T00:01:05+00:00"
    assert out["usdt_end"] == 7635.71639563
    assert out["usdt_change"] == pytest.approx(7635.71639563 - 7579.56427275, abs=1e-9)
    assert out["usdt_change_pct"] == pytest.approx(7635.71639563 / 7579.56427275 - 1.0, abs=1e-15)


def test_the_cumulative_pnl_is_read_against_the_declared_baseline() -> None:
    out = usdt_pnl(ROWS, "2026-09-29", baseline=5000.0)
    assert out["usdt_baseline"] == 5000.0
    assert out["usdt_since_baseline"] == pytest.approx(2635.71639563, abs=1e-9)
    assert out["usdt_since_baseline_pct"] == pytest.approx(0.527143279126, abs=1e-12)


def test_without_a_previous_day_the_first_cycle_of_the_day_is_the_start() -> None:
    out = usdt_pnl(ROWS[1:], "2026-09-29", baseline=5000.0)
    assert out["usdt_start"] == 7552.4862615
    assert out["usdt_start_from"] == "2026-09-29T01:00:29+00:00"


def test_no_declared_baseline_prints_none_not_zero() -> None:
    """零会读成「从零赚到 7635」。"""
    out = usdt_pnl(ROWS, "2026-09-29", baseline=None)
    assert out["usdt_since_baseline"] is None
    assert out["usdt_since_baseline_pct"] is None


def test_rows_without_a_usdt_reading_are_skipped_and_an_empty_day_says_so() -> None:
    bare = {k: v for k, v in LAST_OF_0929.items() if k != "collateral"}
    assert usdt_pnl([LAST_OF_0928, FIRST_OF_0929, bare], "2026-09-29", baseline=5000.0)["usdt_end"] == 7552.4862615
    empty = usdt_pnl([LAST_OF_0928], "2026-09-29", baseline=5000.0)
    assert empty["usdt_end"] is None
    assert empty["why"]


def test_the_shipped_profile_declares_the_5000_baseline() -> None:
    profile = load_yaml(ROOT / "config" / "live.demo.yaml")
    assert RiskBudgetParams.from_mapping(profile["risk_budget"]).usdt_baseline == 5000.0


def test_the_report_leads_with_usdt_and_no_longer_prints_total_equity_as_pnl(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live")
    for row in ROWS:
        store.append_cycle(row)
    payload = daily_payload(
        store, "2026-09-29", risk_budget=RiskBudgetParams(usdt_baseline=5000.0), data_root=tmp_path / "data"
    )
    assert payload["usdt_pnl"]["usdt_end"] == 7635.71639563
    markdown = daily_markdown(payload)
    head = markdown.split("\n## ")[1]
    assert head.startswith("PnL (USDT")
    assert "| usdt_end | 7635.72 |" in head
    assert "| usdt_since_baseline | +2635.72 |" in head
    assert "equity_start" not in head
    assert "equity_change_pct" not in head


def test_a_range_of_days_starts_before_its_first_day() -> None:
    """周报读七天：起点是第一天之前的最后一个读数，终点是最后一天的最后一个读数。"""
    out = usdt_pnl(ROWS, "2026-09-23", "2026-09-29", baseline=5000.0)
    assert out["usdt_start"] == 7579.56427275  # 09-28 的最后一行落在区间里，没有更早的：取区间第一行
    assert out["usdt_end"] == 7635.71639563
    assert usdt_pnl(ROWS, "2026-09-29", "2026-10-05", baseline=5000.0)["usdt_start"] == 7579.56427275


def test_the_weekly_report_leads_with_usdt_too(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live")
    for row in ROWS:
        store.append_cycle(row)
    payload = weekly_payload(store, "2026-09-29", usdt_baseline=5000.0)
    assert payload["usdt_pnl"]["usdt_end"] == 7635.71639563
    head = weekly_markdown(payload).split("\n## ")[1]
    assert head.startswith("PnL (USDT")
    assert "| usdt_since_baseline | +2635.72 |" in head
    assert "equity_start" not in head


# --- Drift, Probe, Noise scale on USDT (operator, 2026-09-30) ------------------------------------------------


def _usdt_rows(usdt: list[float], factor: float = 2.0) -> list[dict[str, Any]]:
    """Hourly rows whose total equity is `factor` times the USDT, collateral constant in between."""
    base = 1_790_640_000_000  # 2026-09-29T01:00Z
    collateral = usdt[0] * (factor - 1.0)
    return [
        {
            "bar_open_ms": base + i * 3_600_000,
            "at": f"2026-09-29T{i % 24:02d}:00:30+00:00",
            "equity": value + collateral,
            "collateral": {"usdt_equity": value, "equity": value + collateral},
        }
        for i, value in enumerate(usdt)
    ]


def _store_of(tmp_path: Path, rows: list[dict[str, Any]]) -> StateStore:
    store = StateStore(tmp_path / "live")
    for row in rows:
        store.append_cycle(row)
    return store


def test_drift_reads_usdt_and_converts_the_drawdown_bar(tmp_path: Path) -> None:
    """USDT −30% at factor 2 is a −15% book loss: under 1.5 × 13%, so no alert.  Unconverted it would page.

    The factor is peak over peak.  The last row's ratio would be 8500/3500 = 2.43 here and 3.22 below, where a
    −27.5% book loss (past 1.5 × 13%) would then read as inside the bar.
    """
    from beidou_live.report_decay import drift_check

    expectations = {"tsmom": {"oos_sharpe": 1.5, "full_sample_max_drawdown": -0.13}}
    falling = [5_000.0 * (1 - 0.30 * i / 59) for i in range(60)]
    drift = drift_check(_store_of(tmp_path / "a", _usdt_rows(falling)), expectations)
    assert drift["series"] == "usdt_equity"
    assert drift["trailing_drawdown"] == pytest.approx(-0.30, abs=1e-9)
    assert drift["usdt_factor"] == pytest.approx(2.0, abs=1e-12)
    assert drift["validated_drawdown_in_usdt"] == pytest.approx(-0.13 * drift["usdt_factor"], abs=1e-12)
    assert not any("回撤" in reason for reason in drift["reasons"])

    deeper = [5_000.0 * (1 - 0.55 * i / 59) for i in range(60)]
    alert = drift_check(_store_of(tmp_path / "b", _usdt_rows(deeper)), expectations)
    assert any("USDT 滚动回撤" in reason for reason in alert["reasons"])


def test_the_probe_row_carries_its_pnl_over_usdt_and_the_stop_still_reads_total_equity(tmp_path: Path) -> None:
    from beidou_live.probe import ProbeParams
    from beidou_live.report_decay import probe_rows

    store = _store_of(tmp_path, _usdt_rows([5_000.0, 5_000.0]))
    store.append_attribution(
        {
            "bar_open_ms": 1_790_640_000_000,
            "until_ms": 1_790_643_600_000,
            "basis": "net_exposure",
            "by_strategy": {"flow": -50.0},
        }
    )
    probe = ProbeParams(book="flow_short", strategy="flow", window_days=30, max_loss=0.02, accepted_on="2026-09-01")
    row = probe_rows(store, (probe,), equity=10_000.0, now_ms=1_790_650_000_000, usdt_equity=5_000.0)[0]
    assert row["pnl_pct_usdt"] == pytest.approx(-0.01, abs=1e-12)
    assert row["pnl_pct"] == pytest.approx(-0.005, abs=1e-12), "the stop's own reading is unchanged"


def test_noise_scale_measures_the_giveback_from_the_usdt_series_own_high(tmp_path: Path) -> None:
    """Collateral falls while USDT peaks: the two highs are at different bars, and the page reads USDT's."""
    from beidou_live.report_risk import noise_scale

    rows = _usdt_rows([5_000.0, 5_200.0, 5_100.0, 5_050.0])
    rows[0]["equity"] = 20_000.0  # the total-equity high, from collateral, at bar 0
    out = noise_scale(_store_of(tmp_path, rows), "2026-09-29", vol_target=0.175)
    assert out["usdt_hwm_u"] == 5_200.0
    assert out["usdt_hwm_at"] == "2026-09-29T01:00:30+00:00"
    assert out["usdt_giveback_since_hwm_u"] == pytest.approx(150.0, abs=1e-9)
    assert out["usdt_giveback_since_hwm_hours"] == pytest.approx(2.0, abs=1e-9)
    assert out["usdt_drawdown_vs_hwm_pct"] == pytest.approx(150.0 / 5_200.0, abs=1e-12)
    assert out["usdt_peak_giveback_u"] == pytest.approx(150.0, abs=1e-9)
