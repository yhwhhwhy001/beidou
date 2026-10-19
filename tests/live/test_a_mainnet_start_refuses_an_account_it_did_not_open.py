"""mainnet 启动前的三道拒绝（MAINNET_READINESS B3、B4、B8；切换方案 M3，2026-10-19）。

只在 venue 是 mainnet 主机的 profile 上生效（`AccountGate`），demo 与 testnet 的启动一个字节不变：

1. 状态目录必须是这个 venue 自己的。demo 的 state 拷过来带着 demo 的 `equity_hwm`，第一根周期就会读出深回撤、
   触发阶梯。
2. 第一次启动要空仓、没有挂单。拒绝而不是撤单或平仓：账户上的东西是有人放的，循环不知道是谁。
3. 权益不超过操作者的上限（环境变量 `BEIDOU_MAINNET_MAX_EQUITY_USDT`，不进公开仓库，没有默认值）。启动时超了就拒绝；
   运行中超了按上限定仓位，只有定仓位读这个上限，护栏、阶梯与归因照旧读真实权益。

拒绝发生在第一次写之前（`_ensure_leverage` 之前），所以被拒的启动什么都没发。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beidou_alpha.panel import Panel
from beidou_live.config import account_gate
from beidou_live.engine import MAINNET_EQUITY_CAP_ENV, AccountGate, LiveEngine
from beidou_live.state import LiveState, StateStore
from beidou_shared.types import OrderAck, Side
from tests.fakes.fake_venue import FakeVenue
from tests.live.fakes import FakeClock, FakeMarketData
from tests.live.test_live_loop import _config, _model, _prices

GATE = AccountGate("fapi.binance.com")


@pytest.fixture
def world(august_panel: Panel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv(MAINNET_EQUITY_CAP_ENV, "20000")
    cursor = 400
    market = FakeMarketData(august_panel, cursor)
    venue = FakeVenue(balance=10_000.0, prices=_prices(august_panel, cursor))
    clock = FakeClock(market.bar_open_ms(cursor) + 5_000)
    return {"market": market, "venue": venue, "clock": clock, "bar": market.bar_open_ms(cursor - 1), "tmp": tmp_path}


def _engine(world: dict, *, gate: AccountGate | None = GATE, state_dir: str = "live-mainnet") -> LiveEngine:
    return LiveEngine(
        _config(world["tmp"], account_gate=gate),
        model=_model(),
        market=world["market"],
        venue=world["venue"],
        clock=world["clock"],
        store=StateStore(world["tmp"] / state_dir),
    )


def _wrote_nothing(venue: FakeVenue) -> bool:
    return not [call for call in venue.calls if call.startswith(("set_leverage", "place_order", "cancel_order"))]


def test_only_a_mainnet_host_gets_a_gate() -> None:
    assert account_gate({"venue": {"rest_url": "https://fapi.binance.com"}}) == GATE
    assert account_gate({"venue": {"rest_url": "https://FAPI.binance.com/"}}) == GATE
    for url in ("https://demo-fapi.binance.com", "https://testnet.binancefuture.com", ""):
        assert account_gate({"venue": {"rest_url": url}}) is None
    assert account_gate({}) is None


async def test_a_fresh_flat_account_under_the_cap_starts_and_is_stamped(world: dict) -> None:
    engine = _engine(world)
    await engine.startup()
    assert engine.state.venue_host == "fapi.binance.com" and engine.equity_cap == 20_000.0
    record = await engine.run_cycle(world["bar"])
    assert record["equity_cap"] == {"cap": 20_000.0, "sizing_equity": pytest.approx(10_000.0)}
    # the stamp survives a restart, and a restart of its own state directory is not a first start
    again = _engine(world)
    await again.startup()
    assert again.state.cycles == 1 and again.state.restarts == 1


async def test_a_copied_demo_state_is_refused(world: dict) -> None:
    demo = _engine(world, gate=None, state_dir="live")
    await demo.startup()
    await demo.run_cycle(world["bar"])
    assert demo.state.venue_host is None  # demo writes no stamp: its state.json is unchanged in meaning
    world["venue"].calls.clear()
    with pytest.raises(RuntimeError, match="written against an unrecorded venue"):
        await _engine(world, state_dir="live").startup()
    assert _wrote_nothing(world["venue"])


async def test_an_equity_history_without_cycles_is_refused(world: dict) -> None:
    store = StateStore(world["tmp"] / "live-mainnet")
    store.save(LiveState(equity_hwm=7_810.0))
    with pytest.raises(RuntimeError, match="equity history"):
        await _engine(world).startup()


async def test_a_state_from_another_venue_is_refused(world: dict) -> None:
    store = StateStore(world["tmp"] / "live-mainnet")
    store.save(LiveState(cycles=3, venue_host="demo-fapi.binance.com"))
    with pytest.raises(RuntimeError, match=r"written against demo-fapi\.binance\.com"):
        await _engine(world).startup()


async def test_a_first_start_refuses_positions_instead_of_closing_them(world: dict) -> None:
    venue = world["venue"]
    venue._fill("BTCUSDT", 0.01, venue.prices["BTCUSDT"])
    with pytest.raises(RuntimeError, match="needs a flat account; it holds BTCUSDT"):
        await _engine(world).startup()
    assert _wrote_nothing(venue) and venue.qty["BTCUSDT"] == 0.01


async def test_a_first_start_refuses_open_orders_instead_of_cancelling_them(
    world: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    venue = world["venue"]
    resting = OrderAck("BTCUSDT", "bd-left-over", "1", Side.BUY, "NEW", executed_qty=0, avg_price=0.0)  # type: ignore[arg-type]

    async def open_orders(symbol: str | None = None) -> list[OrderAck]:
        return [resting]

    monkeypatch.setattr(venue, "open_orders", open_orders)
    # `bd-` is this loop's own prefix: without the gate the reconcile would have cancelled it
    with pytest.raises(RuntimeError, match="no open orders; the account has bd-left-over"):
        await _engine(world).startup()
    assert _wrote_nothing(venue)


async def test_equity_over_the_cap_is_refused_at_startup(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAINNET_EQUITY_CAP_ENV, "9999")
    with pytest.raises(RuntimeError, match=r"above the 9,999\.00 cap"):
        await _engine(world).startup()
    assert _wrote_nothing(world["venue"])


@pytest.mark.parametrize("raw", [None, "", "abc", "0", "-5", "nan", "inf"])
async def test_the_cap_has_no_default(world: dict, monkeypatch: pytest.MonkeyPatch, raw: str | None) -> None:
    if raw is None:
        monkeypatch.delenv(MAINNET_EQUITY_CAP_ENV)
    else:
        monkeypatch.setenv(MAINNET_EQUITY_CAP_ENV, raw)
    with pytest.raises(RuntimeError, match=MAINNET_EQUITY_CAP_ENV):
        await _engine(world).startup()
    assert _wrote_nothing(world["venue"])


async def test_over_the_cap_while_running_sizes_the_book_at_the_cap(world: dict) -> None:
    capped, free = _engine(world), _engine(world, gate=None, state_dir="live")
    await capped.startup()
    capped.equity_cap = 5_000.0  # equity grew past the cap after startup
    assert capped.sizing_equity(10_000.0) == 5_000.0 and capped.sizing_equity(4_000.0) == 4_000.0
    assert free.sizing_equity(10_000.0) == 10_000.0
    record = await capped.run_cycle(world["bar"])
    assert record["equity_cap"]["sizing_equity"] == 5_000.0
    # the targets are the model's; only the orders that carry them out are half the size
    assert record["equity"] == pytest.approx(10_000.0)
    gross = sum(abs(qty) * world["venue"].prices[symbol] for symbol, qty in world["venue"].qty.items())
    assert 0 < gross <= 0.6 * 5_000.0 * 1.01


def test_sizing_before_startup_is_an_error(world: dict) -> None:
    engine = _engine(world)
    with pytest.raises(RuntimeError, match="before startup"):
        engine.sizing_equity(1.0)
