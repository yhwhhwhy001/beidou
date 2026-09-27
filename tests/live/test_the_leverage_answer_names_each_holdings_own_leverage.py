"""白话那一段（`plain_leverage_lines`）里的每个数，都取自 M-015 读的同一份周期记录。

2026-09-26，「所有持仓都是 5 倍、没有区分」第六次被问到。M-015 与 D-037 早就答过，用的却是系统的话
（「吸收了市场波动离散度的 91%」「在这本书里不承担风险」），也都没印出这个问题问的那个数：每个持仓
自己的杠杆。于是屏幕上唯一按币列出的杠杆，仍是交易所那一栏的统一 5x。

这里钉住白话那一段的三件事：

- 每个持仓的真实杠杆就是记录里的 |目标|，波动就是记录里的 `asset_vol`，风险份额按 |目标| x 波动在持仓
  之间分，合计 100%；
- 最小仓位规则（D3）按记录里的构造判：目标不到 `no_trade_band x band_entry_multiple` 的名字列为不持有，
  规则关着或记录里没有构造时一个都不列；
- 交易所那一栏照 `leverage_set` 写：统一时写「5x」与「名义的 1/5」，分档后写区间；读不出持仓时也照写。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from beidou_live.reports import daily_markdown, daily_payload, latest_risk_adaptation, plain_leverage_lines
from beidou_live.state import LiveState, StateStore

BASE = 1_788_000_000_000
DAY = datetime.fromtimestamp(BASE / 1000, tz=UTC).strftime("%Y-%m-%d")

# 10-13 之后 k=0.175 那本书的形状：逆波动率定仓，最波动的两个名字目标落到权益的 1% 以下；BTCUSDT 被单币
# 上限截过，所以各持仓的风险份额不相等，份额那一项才有东西可核。
VOLS = {"BTCUSDT": 0.306, "ETHUSDT": 0.561, "SOLUSDT": 0.536, "UNIUSDT": 1.20, "NEARUSDT": 1.60, "AKEUSDT": 3.226}
TARGETS = {**{symbol: 0.014 / vol for symbol, vol in VOLS.items()}, "BTCUSDT": 0.03, "SOLUSDT": -0.014 / 0.536}
HELD = ["BTCUSDT", "SOLUSDT", "ETHUSDT", "UNIUSDT"]
D3_ON = {"no_trade_band": 0.005, "band_entry_multiple": 2.0, "flat_inside_band": True}


def _store(
    tmp_path: Path,
    *,
    rebalance: dict[str, Any] | None = D3_ON,
    vols: dict[str, float] | None = VOLS,
    leverage: dict[str, int] | None = None,
) -> StateStore:
    store = StateStore(tmp_path / "live")
    record: dict[str, Any] = {"as_of_ms": BASE, "equity": 10_000.0, "targets": TARGETS}
    if vols is not None:
        record["asset_vol"] = vols
    if rebalance is not None:
        record["construction_full"] = {"rebalance": rebalance}
    store.append_cycle(record)
    store.save(LiveState(leverage_set=leverage or dict.fromkeys(TARGETS, 5)))
    return store


def _holdings(lines: list[str]) -> dict[str, str]:
    return {line.split("：")[0]: line for line in lines if "：年化波动" in line}


def test_each_holding_carries_the_records_own_leverage_volatility_and_risk_share(tmp_path: Path) -> None:
    """T-2：与同一份记录里的数逐个相等，而不是另算一遍。"""
    lines = plain_leverage_lines(latest_risk_adaptation(_store(tmp_path)))

    holdings = _holdings(lines)
    assert list(holdings) == HELD, "真实杠杆从大到小，空头按绝对值排"
    risk = {symbol: abs(TARGETS[symbol]) * VOLS[symbol] for symbol in HELD}
    for symbol, line in holdings.items():
        assert f"年化波动 {VOLS[symbol]:.0%}" in line
        assert f"真实杠杆 {abs(TARGETS[symbol]):.3f}x" in line
        assert f"风险份额 {risk[symbol] / sum(risk.values()):.1%}" in line
    shares = [float(re.findall(r"风险份额 ([0-9.]+)%", line)[0]) for line in holdings.values()]
    assert abs(sum(shares) - 100.0) < 0.2, "份额只在持仓之间分，不持有的名字不占"
    summary = next(line for line in lines if line.startswith("合计"))
    assert f"合计 4 个持仓，真实杠杆合计 {sum(abs(TARGETS[symbol]) for symbol in HELD):.2f}x" in summary
    assert f"是最低的 UNIUSDT 的 {abs(TARGETS['BTCUSDT'] / TARGETS['UNIUSDT']):.1f} 倍" in summary


def test_the_minimum_position_rule_is_read_off_the_recorded_construction(tmp_path: Path) -> None:
    """D3 把最波动的名字拿掉时要说出来，否则第七次提问是「高波动币为什么没了」（RISK-009）。"""
    lines = plain_leverage_lines(latest_risk_adaptation(_store(tmp_path)))

    dropped = [line for line in lines if line.startswith("不持有")]
    assert dropped == [
        "不持有 2 个：AKEUSDT、NEARUSDT。目标不到权益的 1.0%，按最小仓位规则（D3）不开仓："
        "这么小的仓位日后会被不交易带卡住、平不掉"
    ]
    assert not {"AKEUSDT", "NEARUSDT"} & set(_holdings(lines))


def test_with_the_rule_off_or_unrecorded_every_name_with_a_target_is_held(tmp_path: Path) -> None:
    """关着的规则不拿掉任何名字；读不出构造时也不猜一个门槛。"""
    off = plain_leverage_lines(
        latest_risk_adaptation(_store(tmp_path / "off", rebalance={**D3_ON, "flat_inside_band": False}))
    )
    unrecorded = plain_leverage_lines(latest_risk_adaptation(_store(tmp_path / "none", rebalance=None)))

    for lines in (off, unrecorded):
        assert not any(line.startswith("不持有") for line in lines)
        assert set(_holdings(lines)) == set(VOLS)


def test_the_venue_sentence_follows_leverage_set(tmp_path: Path) -> None:
    """统一 5x 写「名义的 1/5」；分档之后（若做 R1）写区间，不能还说 1/5。"""
    uniform = plain_leverage_lines(latest_risk_adaptation(_store(tmp_path / "uniform")))
    tiered = {**dict.fromkeys(TARGETS, 5), "BTCUSDT": 15, "AKEUSDT": 3}
    mixed = plain_leverage_lines(latest_risk_adaptation(_store(tmp_path / "tiered", leverage=tiered)))

    assert "交易所那一栏的 5x 只决定开仓占用多少保证金（名义的 1/5），不决定盈亏，也不决定强平" in uniform[0]
    assert "交易所那一栏的 3x–15x 只决定开仓占用多少保证金，不决定盈亏，也不决定强平" in mixed[0]
    assert "1/" not in mixed[0]


def test_a_cycle_without_volatility_still_says_what_the_venue_number_does(tmp_path: Path) -> None:
    """读不出持仓时说读不出，交易所那一句照样在：那是来问的人要的那一半。"""
    lines = plain_leverage_lines(latest_risk_adaptation(_store(tmp_path, vols=None)))

    assert lines == [
        "真实杠杆（白话）：本轮没有读得出的持仓；交易所那一栏的 5x 只决定开仓占用多少保证金（名义的 1/5），"
        "不决定盈亏，也不决定强平"
    ]


def test_the_daily_report_prints_the_same_lines_above_the_m015_section(tmp_path: Path) -> None:
    """S1：日报与 `live status` 用同一个函数出这段文字。"""
    store = _store(tmp_path)
    payload = daily_payload(store, DAY, {})
    text = daily_markdown(payload)

    assert text.index("## 真实杠杆（白话）") < text.index("## Risk adaptation per symbol (M-015)")
    for line in plain_leverage_lines(payload["risk_adaptation"]):
        assert f"- {line}" in text
