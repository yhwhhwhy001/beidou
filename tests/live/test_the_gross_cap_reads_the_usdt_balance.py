"""risk-g11-denominator（操作者 2026-09-28 裁定）：约束侧改按可动用 USDT。

`max_gross` 2.0 与 `margin_cap` 0.40 是在没有抵押品的回测上定的，那里总权益与 USDT 余额是同一个数。demo 账户
约 43% 的权益是 BTC 抵押品，按总权益截 gross，尺子比它代表的政策宽约 1.75 倍。守卫现在先把 `max_gross` 乘上
USDT 占权益的份额，再交给共享的 `clamp_book`；`clamp_book` 与回测都不动。

读不到 USDT 余额时不猜：上限 0 会让 `clamp_book` 的 gross 上限整个失效，按总权益又回到了裁定改掉的那把尺子。
所以只减不加，并记一个原因码，边沿触发告警一次。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from beidou_live.guards import REASON_ZH, GuardParams, evaluate_guards

ROOT = Path(__file__).resolve().parents[2]

#: Five names at 0.5 is 2.5 x equity: over the cap on either ruler.
BOOK = {"A": 0.5, "B": 0.5, "C": -0.5, "D": 0.5, "E": -0.5}


def _decide(
    targets: dict[str, float],
    *,
    usdt_equity: float | None,
    denominator: str = "usdt_equity",
    held: dict[str, float] | None = None,
):
    return evaluate_guards(
        targets,
        current_weights=held or {},
        kill_switch=False,
        equity=10_000.0,
        day_start_equity=10_000.0,
        latest_bar_ms=100,
        expected_bar_ms=100,
        interval_ms=10,
        params=GuardParams(max_gross=2.0, max_weight=0.5, max_gross_denominator=denominator),
        usdt_equity=usdt_equity,
    )


def _gross(targets: dict[str, float]) -> float:
    return sum(abs(value) for value in targets.values())


def test_the_cap_is_two_times_the_usdt_balance() -> None:
    """1.5 x equity on an account whose USDT is half of it is 3 x USDT: cut to 2 x USDT, which is 1.0 x equity."""
    decision = _decide({"A": 0.5, "B": 0.5, "C": -0.5}, usdt_equity=5_000.0)

    assert decision.reasons == ["GROSS_CAPPED"]
    assert _gross(decision.targets) == pytest.approx(1.0)


def test_on_total_equity_the_same_book_passes_untouched() -> None:
    """The default is the historical reading, so a profile that does not name the key trades as before."""
    decision = _decide({"A": 0.5, "B": 0.5, "C": -0.5}, usdt_equity=5_000.0, denominator="equity")

    assert decision.reasons == []
    assert decision.targets == {"A": 0.5, "B": 0.5, "C": -0.5}


def test_an_all_usdt_account_is_capped_exactly_as_on_total_equity() -> None:
    """The backtest's case: no collateral, so the two rulers are one number and the rows are bit-identical."""
    on_usdt = _decide(BOOK, usdt_equity=10_000.0)
    on_equity = _decide(BOOK, usdt_equity=10_000.0, denominator="equity")

    assert on_usdt.targets == on_equity.targets
    assert on_usdt.reasons == on_equity.reasons == ["GROSS_CAPPED"]


def test_a_usdt_balance_above_equity_never_loosens_the_cap() -> None:
    decision = _decide(BOOK, usdt_equity=12_000.0)

    assert _gross(decision.targets) == pytest.approx(2.0)


@pytest.mark.parametrize("usdt_equity", [None, 0.0, -150.0])
def test_an_unread_or_empty_usdt_balance_holds_rather_than_guesses(usdt_equity: float | None) -> None:
    """A cap of 0 would switch `clamp_book`'s gross cap OFF; this holds what is there and adds nothing."""
    decision = _decide({"A": 0.4, "B": -0.3, "C": 0.2}, usdt_equity=usdt_equity, held={"A": 0.2, "B": -0.3})

    assert "NO_USDT_EQUITY" in decision.reasons and not decision.allow_increase
    assert decision.targets == {"A": 0.2, "B": -0.3, "C": 0.0}, "nothing grows past what is held"
    assert "NO_USDT_EQUITY" in REASON_ZH, "the alert names it in Chinese, as every other guard reason"


def test_the_shipped_profile_holds_the_ruling_and_a_typo_is_refused() -> None:
    from beidou_alpha.registry import parse_registry
    from beidou_live.config import live_config
    from tests.live.helpers_construction import live_config_for_profile

    assert live_config_for_profile().guards.max_gross_denominator == "usdt_equity"
    profile = yaml.safe_load((ROOT / "config" / "live.demo.yaml").read_text(encoding="utf-8"))
    registry = parse_registry(yaml.safe_load((ROOT / "config" / "alpha_registry.yaml").read_text(encoding="utf-8")))
    profile["portfolio"]["max_gross_denominator"] = "usdt"
    with pytest.raises(ValueError, match="max_gross_denominator"):
        live_config(profile, ["BTCUSDT"], registry, dry_run=True)


async def _one_cycle(panel, state_dir: Path, *, denominator: str, usdt_share: float) -> dict:
    """One real cycle through `LiveEngine` on a venue that reports `usdt_share` of its equity as USDT."""
    import dataclasses

    from beidou_alpha.model import AlphaModel
    from beidou_alpha.portfolio import PortfolioParams
    from beidou_alpha.registry import StrategyEntry
    from beidou_alpha.signals.tsmom import TsmomParams
    from beidou_live.engine import LiveConfig, LiveEngine
    from beidou_live.rebalancer import RebalanceParams
    from beidou_live.state import StateStore
    from tests.fakes.fake_venue import FakeVenue
    from tests.live.fakes import FakeClock, FakeMarketData

    class CollateralVenue(FakeVenue):
        async def account(self):  # type: ignore[no-untyped-def]
            state = await super().account()
            return dataclasses.replace(state, usdt_equity=state.equity * usdt_share)

    symbols = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
    cursor = 400
    market = FakeMarketData(panel, cursor)
    params = dict(TsmomParams(vol_window=100).__dict__) | {
        "horizons": [5, 20, 50],
        "horizon_weights": [0.2, 0.3, 0.5],
        "entry_threshold": 0.05,
    }
    model = AlphaModel(
        entries=(StrategyEntry("tsmom", params=params),),
        portfolio=PortfolioParams(covariance_halflife=48, vol_halflife=24, max_weight=0.15, max_gross=0.6),
        interval="1h",
        min_history_bars=0,
    )
    engine = LiveEngine(
        LiveConfig(
            interval="1h",
            history_bars=300,
            universe=symbols,
            leverage=2,
            rebalance=RebalanceParams(no_trade_band=0.002),
            guards=GuardParams(max_gross_denominator=denominator),
            kill_switch_path=state_dir / "KILL_SWITCH",
            strategy_weights={"tsmom": 1.0},
            poll_interval_seconds=0.0,
            grace_seconds=1.0,
        ),
        model=model,
        market=market,
        venue=CollateralVenue(),
        clock=FakeClock(market.bar_open_ms(cursor) + 5_000),
        store=StateStore(state_dir / "live"),
    )
    await engine.startup()
    await engine.run_cycle(market.bar_open_ms(cursor))
    return engine.store.read_jsonl(engine.store.cycles_path)[-1]


async def test_the_engine_hands_the_venues_usdt_balance_to_the_guard(august_panel, tmp_path: Path) -> None:
    """DL-X1: a correct guard nobody feeds measures nothing.  At 1% USDT the cap is 0.02 x equity, and it binds."""
    on_usdt = await _one_cycle(august_panel, tmp_path / "usdt", denominator="usdt_equity", usdt_share=0.01)
    on_equity = await _one_cycle(august_panel, tmp_path / "equity", denominator="equity", usdt_share=0.01)

    assert "GROSS_CAPPED" in on_usdt["guard_reasons"]
    assert _gross(on_usdt["targets"]) == pytest.approx(0.02)
    assert "GROSS_CAPPED" not in on_equity["guard_reasons"] and _gross(on_equity["targets"]) > 0.02


async def test_the_paper_account_reports_its_usdt_balance() -> None:
    """The L3 soak trades the shipped profile on a paper account; a None here would hold its book forever."""
    from beidou_live.paper import PaperVenue

    account = await PaperVenue(rules={}, balance=10_000.0).account()

    assert account.usdt_equity == account.equity == 10_000.0
