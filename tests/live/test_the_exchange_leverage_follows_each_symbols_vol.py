"""交易所杠杆按各交易对自己的波动率分档（`leverage: by_vol`）——分交易对杠杆报告的方案 R1，§5.2 的规则，§9.4 的 T-4、T-5、T-8、T-9、T-10。

操作者 2026-09-26 在卡片上选了「交易所也分档」。分档只改交易所那一栏的杠杆，也就只改开仓占用多少保证金；
权重、订单、退出都在读它之前就定了。这里钉住的是四条规则各自成立、合起来不碰订单：

- T-4：档位 = 5 × σ_ref / σ 在档位表上按对数距离取最近的一档，与报告附录 A 逐个相同；
- T-5：一本全是高 σ 名字、gross 顶到 max_gross 的书，档位自动上调，保证金预检不缩单；
- T-8：σ 在两档边界来回抖，一次也不下发；持续变了，满 24 个周期才下发；
- T-9：按当前名义读完整档位表，名义超过某档上限就降到交易所在这个名义上肯收的那一档；
- T-10：下发被拒或网络出错，保留原设置、写日志、告警，循环照常，没有订单依赖这次下发。

T-6（Mac 上 cycles.jsonl 的逐周期回放）与 T-11（14 天 σ 的下发频率）要 Mac 上的数据，T-12 是操作者上线首日在
交易所界面上核对，都不在这里。

引擎测试用 `august_panel`：四个币的年化 σ 在 0.15 到 0.29 之间，出厂的 σ_ref 0.90 下全落在顶档 15x。要看到分档
真的分开，测试把 σ_ref 调到 0.25（BTC 6x、ETH 5x、BNB 8x、SOL 4x），要看到不变量上调，调到 0.05（全落在 1x–2x）。
"""

from __future__ import annotations

import logging
import math
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from beidou_alpha.panel import Panel
from beidou_alpha.registry import parse_registry
from beidou_exchange.binance_usdm.rest_client import BinanceRestClient
from beidou_exchange.binance_usdm.venue import BinanceUsdmVenue
from beidou_live.config import live_config
from beidou_live.cycle_record import undeclared
from beidou_live.engine import LiveEngine
from beidou_live.guards import GuardParams
from beidou_live.leverage import (
    VOL_TIERS,
    LeveragePlan,
    bracket_leverage,
    by_vol_problems,
    hysteresis_step,
    plan_leverage,
    scale_orders_to_margin,
    tier_leverage,
)
from beidou_live.rebalancer import PlannedOrder
from beidou_live.report_risk import latest_risk_adaptation, plain_leverage_lines
from beidou_live.state import StateStore
from beidou_shared.types import Side, VenueError
from tests.fakes.fake_venue import FakeVenue
from tests.live.fakes import FakeClock, FakeMarketData
from tests.live.helpers_construction import ROOT, live_config_for_profile
from tests.live.test_live_loop import SYMBOLS, _config, _model, _prices

# 报告附录 A：2026-09-26 Mac 上的年化 σ 与它在 σ_ref 0.925 下的档位；出厂的 σ_ref 0.90 下逐个相同。
# 后四个在 k=0.175 下目标不到权益的 1%（D3），不持有：档位照样算，只是不占保证金。
APPENDIX_A: dict[str, tuple[float, int]] = {
    "BTCUSDT": (0.32, 15),
    "BNBUSDT": (0.33, 15),
    "ETHUSDT": (0.36, 12),
    "SOLUSDT": (0.52, 8),
    "HYPEUSDT": (0.63, 8),
    "XRPUSDT": (0.71, 6),
    "DOGEUSDT": (0.75, 6),
    "ADAUSDT": (0.81, 6),
    "TRUMPUSDT": (0.93, 5),
    "ZECUSDT": (0.94, 5),
    "1000PEPEUSDT": (1.17, 4),
    "SUIUSDT": (1.29, 4),
    "UNIUSDT": (1.42, 3),
    "NEARUSDT": (1.59, 3),
    "ENAUSDT": (1.64, 3),
    "LSKUSDT": (2.70, 2),
    "AKEUSDT": (4.30, 1),
}
NOT_HELD_AT_K0175 = {"NEARUSDT", "ENAUSDT", "LSKUSDT", "AKEUSDT"}

# 一个交易对的档位表，故意乱序：(名义上限, 这一档允许的最大杠杆)。
BRACKETS = [(50_000.0, 10), (10_000.0, 20), (200_000.0, 5), (1_000_000.0, 2)]


def _plan(sigma: dict[str, float], **overrides: Any) -> LeveragePlan:
    kwargs: dict[str, Any] = {
        "managed": list(sigma),
        "sigma": sigma,
        "standing": {},
        "tiered": set(),
        "streaks": {},
        "target_notional": {},
        "position_notional": {},
        "auto": dict.fromkeys(sigma, 5),
        "tables": {},
        "base": 5,
        "sigma_ref": 0.90,
        "tiers": VOL_TIERS,
        "cycles": 24,
    }
    kwargs.update(overrides)
    return plan_leverage(**kwargs)


# --- T-4：档位 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("sigma_ref", [0.925, 0.90])
def test_each_symbol_gets_the_tier_appendix_a_gives_it(sigma_ref: float) -> None:
    """附录 A 是在 σ_ref 0.925 下算的；出厂的 0.90 逐个落在同一档，换参考值没有悄悄挪动任何一个。"""
    got = {
        symbol: tier_leverage(sigma, base=5, sigma_ref=sigma_ref, tiers=VOL_TIERS)
        for symbol, (sigma, _) in APPENDIX_A.items()
    }

    assert got == {symbol: tier for symbol, (_, tier) in APPENDIX_A.items()}
    held = [tier for symbol, tier in got.items() if symbol not in NOT_HELD_AT_K0175]
    assert len(held) == 13 and (min(held), max(held)) == (3, 15), "k=0.175 下持有的 13 个落在卡片上的 3x 到 15x"


@pytest.mark.parametrize("sigma", [math.nan, 0.0, -0.4, math.inf, None])
def test_a_sigma_it_cannot_read_has_no_tier(sigma: Any) -> None:
    assert tier_leverage(sigma, base=5, sigma_ref=0.90, tiers=VOL_TIERS) is None


def test_a_tie_goes_to_the_lower_tier_the_side_that_asks_for_more_margin() -> None:
    """理想值 2 到 1 与到 4 的对数距离都是 ln 2。档位表乱序给，也照样排好再取。"""
    assert tier_leverage(1.0, base=2, sigma_ref=1.0, tiers=(4, 1)) == 1


def test_a_symbol_without_a_sigma_this_cycle_keeps_what_stands() -> None:
    plan = _plan(
        {"BTCUSDT": math.nan, "ETHUSDT": 0.36, "NEWUSDT": math.nan},
        standing={"BTCUSDT": 15, "ETHUSDT": 5},
        tiered={"BTCUSDT", "ETHUSDT"},
    )

    assert plan.ideal == {"ETHUSDT": 12}
    assert plan.wanted == {"BTCUSDT": 15, "ETHUSDT": 5}, "BTC 读不到 σ 就留原样；NEW 两样都没有，不设"
    assert plan.streaks == {"ETHUSDT": 1}, "ETH 已分过档，换档要等迟滞"


# --- T-5：不变量 ------------------------------------------------------------------------------------


def test_a_book_of_only_high_vol_names_at_max_gross_raises_the_tiers_and_is_not_scaled() -> None:
    """四个高 σ 名字各占权益的 0.5 倍，gross 2.0 = max_gross。

    只按波动率它们落在 1x 到 3x，初始保证金要 1.08 倍权益，预检（可用余额 × 0.9）必然缩单。不变量把最低的档
    一格一格往上抬，直到整本书的保证金不超过 `auto`（5x）要的 0.40 倍权益：档位变了，订单一张没动。
    """
    equity = 10_000.0
    sigma = {symbol: APPENDIX_A[symbol][0] for symbol in sorted(NOT_HELD_AT_K0175)}
    target = dict.fromkeys(sigma, 0.5 * equity)

    plan = _plan(sigma, target_notional=target)

    assert plan.ideal == {"AKEUSDT": 1, "ENAUSDT": 3, "LSKUSDT": 2, "NEARUSDT": 3}
    assert plan.wanted == dict.fromkeys(sigma, 5) and plan.raised == sorted(sigma)
    assert plan.margin["tiered"] <= plan.margin["auto"] == pytest.approx(0.40 * equity)
    orders = [
        PlannedOrder(
            symbol=symbol,
            side=Side.BUY,
            quantity=Decimal("5000"),
            reduce_only=False,
            client_order_id=f"bd-t5-{symbol}",
            target_weight=0.5,
            current_notional=0.0,
            target_notional=5_000.0,
            price=1.0,
        )
        for symbol in sigma
    ]
    kept, margin = scale_orders_to_margin(orders, equity, plan.wanted, {}, buffer=0.10)
    assert margin["scaled"] is False and kept == orders
    _, unguarded = scale_orders_to_margin(orders, equity, plan.ideal, {}, buffer=0.10)
    assert unguarded["scaled"] is True, "没有不变量，同一本书在分档下会被缩单——它挡的就是这个"


def test_the_tiered_book_never_needs_more_margin_than_auto_and_never_passes_a_cap() -> None:
    """M7 的一般情形：300 本随机的书（σ 0.1 到 5，多空都有，gross 不超过 max_gross，一半的币带档位表）。"""
    rng = random.Random(20260927)
    for _ in range(300):
        symbols = [f"S{index}USDT" for index in range(rng.randint(1, 12))]
        sigma = {symbol: rng.uniform(0.1, 5.0) for symbol in symbols}
        raw = {symbol: rng.uniform(-1.0, 1.0) for symbol in symbols}
        scale = 2.0 * rng.uniform(0.1, 1.0) / sum(abs(value) for value in raw.values())
        target = {symbol: value * scale * 10_000.0 for symbol, value in raw.items()}
        tables = {
            symbol: [(rng.choice([500.0, 2_000.0, 8_000.0]), 20), (1e9, rng.choice([2, 4, 5, 10]))]
            for symbol in symbols
            if rng.random() < 0.5
        }

        plan = _plan(sigma, target_notional=target, tables=tables)

        assert plan.margin["tiered"] <= plan.margin["auto"] * (1 + 1e-9)
        for symbol in symbols:
            cap = bracket_leverage(tables.get(symbol, ()), target[symbol])
            assert 1 <= plan.wanted[symbol] <= min(15, cap or 15), (symbol, plan)


# --- T-8：迟滞 ------------------------------------------------------------------------------------


def test_the_hysteresis_step_counts_cycles_that_disagree_and_resets_when_they_agree() -> None:
    assert hysteresis_step(8, None, 0, 24) == (8, 0), "还没设过：马上设"
    assert hysteresis_step(8, 8, 5, 24) == (None, 0), "一致：清零"
    assert hysteresis_step(6, 8, 22, 24) == (None, 23)
    assert hysteresis_step(6, 8, 23, 24) == (6, 0), "第 24 个不一致的周期：下发"
    assert hysteresis_step(6, 8, 0, 1) == (6, 0), "迟滞 1 就是不迟滞"


def _walk(sigmas: list[float], *, standing: int) -> list[int]:
    """把 SOL 按给定的 σ 逐周期过一遍 `plan_leverage`，像循环那样把这周期发出的当作下一周期的原设置。"""
    now, streaks, path = {"SOLUSDT": standing}, {}, []
    for sigma in sigmas:
        plan = _plan({"SOLUSDT": sigma}, standing=now, tiered={"SOLUSDT"}, streaks=streaks)
        now, streaks = dict(plan.wanted), plan.streaks
        path.append(now["SOLUSDT"])
    return path


def test_sigma_dithering_across_a_rung_sends_nothing() -> None:
    """σ 0.52 落 8x，0.66 落 6x。两种抖法 48 个周期都一次不发：交替抖，以及连着 23 个周期不同、第 24 个回来。"""
    assert tier_leverage(0.52, base=5, sigma_ref=0.90, tiers=VOL_TIERS) == 8
    assert tier_leverage(0.66, base=5, sigma_ref=0.90, tiers=VOL_TIERS) == 6

    assert _walk([0.66, 0.52] * 24, standing=8) == [8] * 48
    assert _walk(([0.66] * 23 + [0.52]) * 2, standing=8) == [8] * 48


def test_a_move_that_holds_is_sent_on_the_24th_cycle_to_that_cycles_tier() -> None:
    assert _walk([0.66] * 30, standing=8) == [8] * 23 + [6] * 7
    # 不一致的周期里要的是哪一档不要紧，按第 24 个周期那一档发：前 12 个要 6x，后 12 个要 5x（σ 0.93）。
    assert _walk([0.66] * 12 + [0.93] * 12, standing=8) == [8] * 23 + [5]


# --- T-9：按名义读档位表 ----------------------------------------------------------------------------


def test_the_bracket_is_read_at_the_positions_notional_not_at_bracket_one() -> None:
    assert bracket_leverage(BRACKETS, 0.0) == 20
    assert bracket_leverage(BRACKETS, 9_999.0) == 20
    assert bracket_leverage(BRACKETS, 10_000.0) == 10, "名义等于上限归下一档"
    assert bracket_leverage(BRACKETS, -60_000.0) == 5, "空头按绝对值"
    assert bracket_leverage(BRACKETS, 5_000_000.0) == 2, "超过所有上限，给表里最后一档"
    assert bracket_leverage([], 1.0) is None


def test_a_tier_the_venue_would_refuse_at_this_size_is_capped_at_once() -> None:
    """BTC 按波动率要 15x；名义 6 万时交易所最多给 5x（-2027 在上面等着），当周期就降，不等迟滞。"""
    tables = {"BTCUSDT": BRACKETS}
    btc = {"BTCUSDT": 0.32}

    small = _plan(btc, target_notional={"BTCUSDT": 5_000.0}, tables=tables)
    large = _plan(btc, target_notional={"BTCUSDT": 60_000.0}, tables=tables)
    closing = _plan(btc, target_notional={"BTCUSDT": 5_000.0}, position_notional={"BTCUSDT": 60_000.0}, tables=tables)
    held = _plan(
        btc, standing={"BTCUSDT": 15}, tiered={"BTCUSDT"}, target_notional={"BTCUSDT": 60_000.0}, tables=tables
    )

    assert small.wanted == {"BTCUSDT": 15} and small.clamped == {}
    assert large.wanted == large.clamped == {"BTCUSDT": 5}
    assert closing.wanted == {"BTCUSDT": 5}, "还拿着 6 万的仓位时，按两者中大的那个读"
    assert held.wanted == {"BTCUSDT": 5} and held.streaks == {}, "迟滞管不到上限"


def test_a_cap_that_binds_every_cycle_is_not_a_move_and_builds_no_streak() -> None:
    """迟滞比的是压过上限之后的值：BTC 按波动率一直要 15x、一直只许 5x，这不算「要换档」，不攒周期数。"""
    plan = _plan(
        {"BTCUSDT": 0.32},
        standing={"BTCUSDT": 5},
        tiered={"BTCUSDT"},
        target_notional={"BTCUSDT": 60_000.0},
        tables={"BTCUSDT": BRACKETS},
    )

    assert plan.wanted == plan.clamped == {"BTCUSDT": 5} and plan.streaks == {}


def test_a_by_vol_profile_that_cannot_work_has_its_problems_named() -> None:
    assert by_vol_problems(VOL_TIERS, 0.90, 24) == []
    for tiers in [(), (5, 3, 8), (0, 5), (5, 5, 8)]:
        assert by_vol_problems(tiers, 0.90, 24), tiers
    for sigma_ref in [0.0, -1.0, math.nan]:
        assert by_vol_problems(VOL_TIERS, sigma_ref, 24), sigma_ref
    assert by_vol_problems(VOL_TIERS, 0.90, 0)


# --- 引擎：接进实盘循环之后 --------------------------------------------------------------------------


class _Alerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None]] = []

    @property
    def enabled(self) -> bool:
        return True

    def clear(self, key: str) -> None:
        pass

    async def send(self, text: str, *, key: str | None = None, force: bool = False) -> bool:
        self.sent.append((text, key))
        return True


def _world(august_panel: Panel, tmp_path: Path, *, venue: FakeVenue | None = None, **overrides: Any) -> dict[str, Any]:
    cursor = 400
    market = FakeMarketData(august_panel, cursor)
    venue = venue or FakeVenue(balance=10_000.0, prices=_prices(august_panel, cursor))
    clock = FakeClock(market.bar_open_ms(cursor) + 5_000)
    store = StateStore(tmp_path / "live")
    alerts = _Alerts()
    engine = LiveEngine(
        _config(tmp_path, **{"leverage_mode": "by_vol", **overrides}),
        model=_model(),
        market=market,
        venue=venue,
        clock=clock,
        store=store,
        alerts=alerts,  # type: ignore[arg-type]
    )
    return {
        "engine": engine,
        "venue": venue,
        "market": market,
        "clock": clock,
        "store": store,
        "alerts": alerts,
        "bar": market.bar_open_ms(cursor - 1),
    }


async def _cycles(world: dict[str, Any], count: int, *, start: int = 0) -> list[dict[str, Any]]:
    """连着跑 ``count`` 个小时周期，行情、bar 与时钟一起往前走。``start`` 是从第几个小时接着跑。"""
    records = []
    for hour in range(start, start + count):
        if hour:
            world["market"].cursor += 1
            world["clock"].advance(3600)
        records.append(await world["engine"].run_cycle(world["bar"] + hour * 3_600_000))
    return records


def _orders(record: dict[str, Any]) -> list[tuple[str, str, str, bool, str]]:
    return [
        (order["symbol"], order["side"], order["quantity"], order["reduce_only"], order["client_order_id"])
        for order in record["orders"]
    ]


async def test_the_first_by_vol_cycle_tiers_every_symbol_at_once(august_panel: Panel, tmp_path: Path) -> None:
    world = _world(august_panel, tmp_path, leverage_sigma_ref=0.25)
    engine, venue = world["engine"], world["venue"]
    await engine.startup()
    assert venue.leverage == dict.fromkeys(SYMBOLS, 5), "启动时还没有 σ：先发 `auto` 的 5x，跟今天一样"

    [record] = await _cycles(world, 1)
    block = record["leverage_tiers"]

    expected = {s: tier_leverage(record["asset_vol"][s], base=5, sigma_ref=0.25, tiers=VOL_TIERS) for s in SYMBOLS}
    assert block["ideal"] == expected == {"BTCUSDT": 6, "ETHUSDT": 5, "BNBUSDT": 8, "SOLUSDT": 4}
    assert block["raised"] == [] and block["clamped"] == {} and block["refused"] == {}
    assert venue.leverage == block["set"] == engine.state.leverage_set == expected
    assert block["sent"] == {s: v for s, v in expected.items() if v != 5}, "ETH 本来就是 5x，不重发"
    assert not block["reasserted"], "启动刚全量发过，同一天的第一个周期不再重发"
    assert engine.state.leverage_tiered == sorted(SYMBOLS)
    assert block["margin"]["tiered"] <= block["margin"]["auto"]


@pytest.mark.parametrize("sigma_ref", [0.90, 0.25, 0.05])
async def test_by_vol_places_exactly_the_orders_auto_places(
    august_panel: Panel, tmp_path: Path, sigma_ref: float
) -> None:
    """T-5 在循环里：三个 σ_ref 分别是全 15x、分开的 4x–8x、全落在 1x–2x 而被不变量抬回 5x。订单逐张相同，预检从不缩单。"""
    auto = _world(august_panel, tmp_path / "auto", leverage_mode="auto")
    tiered = _world(august_panel, tmp_path / "by_vol", leverage_sigma_ref=sigma_ref)
    await auto["engine"].startup()
    await tiered["engine"].startup()

    expected = await _cycles(auto, 4)
    got = await _cycles(tiered, 4)

    assert [_orders(record) for record in got] == [_orders(record) for record in expected]
    assert any(_orders(record) for record in got), "空的订单列表相同，什么也证明不了"
    assert not any(record.get("margin", {}).get("scaled") for record in got)
    if sigma_ref == 0.05:
        assert got[0]["leverage_tiers"]["raised"] == sorted(SYMBOLS)
        assert set(got[0]["leverage_tiers"]["ideal"].values()) <= {1, 2}
        assert tiered["venue"].leverage == dict.fromkeys(SYMBOLS, 5)


async def test_a_refused_or_failed_send_keeps_the_old_setting_alerts_and_the_loop_runs_on(
    august_panel: Panel, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """T-10。BTC 被交易所拒（-4028），SOL 走代理超时；ETH、BNB 照常。

    被拒的两个保留启动时的 5x（交易所那边与 `leverage_set` 都是），告警一条列出两个，日志各一行；周期照常下单，
    订单与一个没被拒的世界逐张相同。之后不按小时重试、不按小时告警：迟滞满了再发一次（这里迟滞设成 2）。
    """
    control = _world(august_panel, tmp_path / "control", leverage_sigma_ref=0.25, leverage_hysteresis=2)
    world = _world(august_panel, tmp_path / "refused", leverage_sigma_ref=0.25, leverage_hysteresis=2)
    engine, venue, alerts = world["engine"], world["venue"], world["alerts"]
    await control["engine"].startup()
    await engine.startup()
    accept = venue.set_leverage
    failing = {"BTCUSDT": VenueError("Leverage 6 is not valid", code=-4028), "SOLUSDT": TimeoutError("proxy timed out")}

    async def set_leverage(symbol: str, leverage: int) -> int:
        if symbol in failing:
            raise failing[symbol]
        return await accept(symbol, leverage)

    venue.set_leverage = set_leverage  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="beidou_live.engine"):
        [first] = await _cycles(world, 1)
    [reference] = await _cycles(control, 1)

    block = first["leverage_tiers"]
    assert set(block["refused"]) == {"BTCUSDT", "SOLUSDT"} and block["sent"] == {"BNBUSDT": 8}
    assert engine.state.leverage_set == venue.leverage == {"BTCUSDT": 5, "ETHUSDT": 5, "BNBUSDT": 8, "SOLUSDT": 5}
    assert [key for _, key in alerts.sent] == ["leverage-refused"]
    assert "BTCUSDT 6x" in alerts.sent[0][0] and "SOLUSDT 4x" in alerts.sent[0][0]
    assert sum("refused for" in message for message in caplog.messages) == 2
    assert not first["skip"] and _orders(first) == _orders(reference) and _orders(first)

    calls = len(venue.calls)
    [second] = await _cycles(world, 1, start=1)
    assert not [call for call in venue.calls[calls:] if call.startswith("set_leverage")], "不按小时重试"
    assert second["leverage_tiers"]["pending"] == {"BTCUSDT": 1, "SOLUSDT": 1}
    assert len(alerts.sent) == 1

    failing.clear()
    [third] = await _cycles(world, 1, start=2)
    assert third["leverage_tiers"]["sent"] == {"BTCUSDT": 6, "SOLUSDT": 4}, "迟滞满了再发一次，这回成了"
    assert venue.leverage == {"BTCUSDT": 6, "ETHUSDT": 5, "BNBUSDT": 8, "SOLUSDT": 4}


async def test_the_first_cycle_of_a_new_utc_day_resends_every_setting(august_panel: Panel, tmp_path: Path) -> None:
    """S4。demo 上读不回交易所的设置，账户重置又会一声不响地把它改回默认（2026-09-04）：每天全量重发一次是唯一的修复。"""
    world = _world(august_panel, tmp_path, leverage_sigma_ref=0.25)
    engine, venue = world["engine"], world["venue"]
    await engine.startup()
    await _cycles(world, 1)
    venue.leverage.clear()  # 夜里账户被重置了，没人告诉循环

    world["market"].cursor += 23
    world["clock"].advance(23 * 3600)
    [next_day] = await _cycles(world, 1, start=24)
    [same_day] = await _cycles(world, 1, start=25)

    assert next_day["leverage_tiers"]["reasserted"] is True
    assert next_day["leverage_tiers"]["sent"] == {"BTCUSDT": 6, "ETHUSDT": 5, "BNBUSDT": 8, "SOLUSDT": 4}
    assert venue.leverage == engine.state.leverage_set
    assert same_day["leverage_tiers"]["reasserted"] is False and same_day["leverage_tiers"]["sent"] == {}


async def test_a_restart_resends_the_tiers_it_had_set_and_a_rollback_to_auto_forgets_them(
    august_panel: Panel, tmp_path: Path
) -> None:
    """重启按记下的档位全量重发（不是先退回 5x 再等一个周期）；回滚成 `auto` 则全发 5x，并忘掉分档的记忆。"""
    world = _world(august_panel, tmp_path, leverage_sigma_ref=0.25)
    await world["engine"].startup()
    await _cycles(world, 1)
    venue = world["venue"]
    tiers = {"BTCUSDT": 6, "ETHUSDT": 5, "BNBUSDT": 8, "SOLUSDT": 4}
    assert venue.leverage == tiers

    venue.leverage.clear()
    restarted = _world(august_panel, tmp_path, venue=venue, leverage_sigma_ref=0.25)
    await restarted["engine"].startup()
    assert venue.leverage == tiers and restarted["engine"].state.leverage_tiered == sorted(SYMBOLS)

    rolled_back = _world(august_panel, tmp_path, venue=venue, leverage_mode="auto")
    await rolled_back["engine"].startup()
    state = rolled_back["engine"].state
    assert venue.leverage == dict.fromkeys(SYMBOLS, 5)
    assert state.leverage_tiered == [] and state.leverage_streaks == {}
    [record] = await _cycles(rolled_back, 1)
    assert "leverage_tiers" not in record


async def test_the_bracket_table_is_read_once_a_day_and_kept_when_a_read_fails(
    august_panel: Panel, tmp_path: Path
) -> None:
    """T-9 在循环里。BTC 的目标名义约 1,500，档位表说 1,000 以上最多 2x：出厂 σ_ref 下它要 15x，发出去的是 2x。"""
    world = _world(august_panel, tmp_path)
    reads: list[str] = []

    async def table() -> dict[str, list[tuple[float, int]]]:
        reads.append("read")
        if len(reads) > 1:
            raise RuntimeError("leverageBracket 503")
        return {"BTCUSDT": [(1_000.0, 20), (1e9, 2)]}

    world["venue"].leverage_bracket_table = table  # type: ignore[attr-defined]
    await world["engine"].startup()
    first, second = await _cycles(world, 2)
    world["market"].cursor += 22
    world["clock"].advance(22 * 3600)
    [next_day] = await _cycles(world, 1, start=24)

    assert len(reads) == 2, "同一天读一次；第二天读失败"
    for record in (first, second, next_day):
        assert record["leverage_tiers"]["clamped"] == {"BTCUSDT": 2}, "读失败就用上次读到的那张表"
        assert record["leverage_tiers"]["set"]["BTCUSDT"] == 2 and record["leverage_tiers"]["pending"] == {}
    assert first["leverage_tiers"]["ideal"]["BTCUSDT"] == 15


async def test_a_dry_run_decides_and_records_and_sends_nothing(august_panel: Panel, tmp_path: Path) -> None:
    world = _world(august_panel, tmp_path, leverage_sigma_ref=0.25, dry_run=True)
    await world["engine"].startup()
    [record] = await _cycles(world, 1)

    assert record["leverage_tiers"]["wanted"] == {"BTCUSDT": 6, "ETHUSDT": 5, "BNBUSDT": 8, "SOLUSDT": 4}
    assert not [call for call in world["venue"].calls if call.startswith("set_leverage")]


async def test_a_by_vol_cycle_writes_nothing_undeclared(august_panel: Panel, tmp_path: Path) -> None:
    """`test_the_cycle_record_is_declared` 跑的是固定杠杆，写不出这个键；这里补上 by_vol 那一侧的核对。"""
    world = _world(august_panel, tmp_path, leverage_sigma_ref=0.25)
    await world["engine"].startup()
    await world["engine"].guarded_cycle(world["bar"])

    row = world["store"].read_jsonl(world["store"].cycles_path)[-1]
    assert "leverage_tiers" in row and not undeclared(row)


async def test_the_plain_leverage_line_names_the_tiers_by_vol_set(august_panel: Panel, tmp_path: Path) -> None:
    """O-1（#175）那句白话读的是 `leverage_set`，所以分档之后它说的是「4x–8x」，不再是「一律 5x」。

    这是报告里 O-1 与 R1 接上的那一处：操作者问的就是交易所那一栏的数，分档之后那一栏各不相同，白话要跟着说。
    钉在这里，免得哪天那句改成读配置里的 `leverage`，分档了却还写 5x。
    """
    world = _world(august_panel, tmp_path, leverage_sigma_ref=0.25)
    await world["engine"].startup()
    await world["engine"].guarded_cycle(world["bar"])

    assert world["engine"].state.leverage_set == {"BTCUSDT": 6, "ETHUSDT": 5, "BNBUSDT": 8, "SOLUSDT": 4}
    line = plain_leverage_lines(latest_risk_adaptation(world["store"]))[0]
    assert "交易所那一栏的 4x–8x 只决定开仓占用多少保证金" in line, line


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"leverage_tiers": (5, 3, 8)}, "leverage_tiers"),
        ({"leverage_sigma_ref": 0.0}, "leverage_sigma_ref"),
        ({"leverage_hysteresis": 0}, "leverage_hysteresis"),
        ({"guards": GuardParams(max_gross=3.0)}, "margin_cap"),
    ],
)
async def test_a_by_vol_profile_that_cannot_work_is_refused_at_startup(
    august_panel: Panel, tmp_path: Path, overrides: dict[str, Any], match: str
) -> None:
    """S5：像 D-016 核 `auto` 的保证金那样，在循环开始之前拒掉；max_gross 3.0 / 5x 要 60% 保证金，超过 40%。"""
    world = _world(august_panel, tmp_path, **overrides)
    with pytest.raises(RuntimeError, match=match):
        await world["engine"].startup()
    assert not [call for call in world["venue"].calls if call.startswith("set_leverage")]


# --- 配置与交易所接口 ---------------------------------------------------------------------------------


def test_the_shipped_profile_runs_by_vol_on_the_reports_ladder() -> None:
    config = live_config_for_profile()

    assert config.leverage_mode == "by_vol"
    assert (config.leverage_sigma_ref, config.leverage_tiers, config.leverage_hysteresis) == (0.90, VOL_TIERS, 168)
    assert (config.max_leverage, config.margin_cap) == (5, 0.40), "5x 仍是 σ_ref 上的档位，也是回滚成 auto 的值"


@pytest.mark.parametrize(
    ("raw", "mode", "fixed"), [("auto", "auto", 2), ("AUTO", "auto", 2), ("by_vol", "by_vol", 2), (3, "fixed", 3)]
)
def test_the_leverage_line_parses_each_mode(raw: Any, mode: str, fixed: int) -> None:
    profile = yaml.safe_load((ROOT / "config" / "live.demo.yaml").read_text(encoding="utf-8"))
    registry = parse_registry(yaml.safe_load((ROOT / "config" / "alpha_registry.yaml").read_text(encoding="utf-8")))
    profile["portfolio"]["leverage"] = raw

    config = live_config(profile, ["BTCUSDT"], registry, dry_run=True)

    assert (config.leverage_mode, config.leverage) == (mode, fixed)


async def test_the_venue_reads_the_bracket_table_past_bracket_one() -> None:
    """`/fapi/v1/leverageBracket` 原样的形状：每个币一组档位，数字不是字符串，顺序不保证。空的那个币不出现。"""
    payload = [
        {
            "symbol": "BTCUSDT",
            "brackets": [
                {"bracket": 2, "initialLeverage": 100, "notionalCap": 250000, "notionalFloor": 50000, "cum": 50.0},
                {"bracket": 1, "initialLeverage": 125, "notionalCap": 50000, "notionalFloor": 0, "cum": 0.0},
            ],
        },
        {"symbol": "EMPTYUSDT", "brackets": []},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/leverageBracket"
        return httpx.Response(200, json=payload)

    venue = BinanceUsdmVenue(
        BinanceRestClient("https://demo-fapi.binance.com", "k", "s", transport=httpx.MockTransport(handler))
    )

    assert await venue.leverage_bracket_table() == {"BTCUSDT": [(50000.0, 125), (250000.0, 100)]}
