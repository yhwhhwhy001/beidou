"""组合级总敞口硬门测试（M07-F01/R2）。

M07-R2（对抗审查）反例固化: inflight 保留、减仓/平仓永不封锁、
保守价格取 max(last, entry)。
"""

from __future__ import annotations

from types import SimpleNamespace

from beidou_core.engine import AutonomousEngine


class _FakeProtection:
    def __init__(self, positions: dict[str, tuple[float, float]]) -> None:
        # symbol -> (quantity, entry_price)
        self._positions = positions

    def all_positions(self) -> dict[str, SimpleNamespace]:
        out = {}
        for sym, (qty, entry) in self._positions.items():
            out[sym] = SimpleNamespace(instrument_id=sym, quantity=qty, entry_price=entry)
        return out


class _FakeOutbox:
    def __init__(self, inflight: dict[str, float]) -> None:
        self._inflight = inflight

    def inflight_signed_quantity(self, symbol: str):
        from decimal import Decimal

        return Decimal(str(self._inflight.get(symbol, 0.0)))


def _bare_engine(positions, inflight, last_prices, symbols) -> AutonomousEngine:
    engine = object.__new__(AutonomousEngine)
    engine._protection = _FakeProtection(positions)
    engine._outbox = _FakeOutbox(inflight)
    engine._last_prices = last_prices
    engine._symbols = symbols
    return engine


def test_projection_includes_inflight_and_current() -> None:
    """R2 反例 1/2: 已成交 + 在途必须计入投影（旧门只看已成交可被批量绕过）。"""
    engine = _bare_engine(
        positions={"BTCUSDT": (1.0, 100.0)},  # 已成交 100
        inflight={"ETHUSDT": 2.0},  # 在途 2 × 150 = 300
        last_prices={"BTCUSDT": 100.0, "ETHUSDT": 150.0},
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    )
    risk_increasing, projected = engine._portfolio_exposure_projection("SOLUSDT", 500.0)
    assert risk_increasing is True
    assert abs(projected - (100 + 300 + 500)) < 1e-9


def test_reduce_intent_is_never_blocked() -> None:
    """R2 反例 3: 减仓/平仓意图 risk_increasing=False,超限时不被 SKIP。"""
    engine = _bare_engine(
        positions={"BTCUSDT": (1.0, 100.0), "SOLUSDT": (20.0, 200.0)},  # 本 symbol 已 4000
        inflight={},
        last_prices={"BTCUSDT": 100.0, "SOLUSDT": 200.0},
        symbols=["BTCUSDT", "SOLUSDT"],
    )
    # 目标 2000 < 当前 4000 → 减仓
    risk_increasing, projected = engine._portfolio_exposure_projection("SOLUSDT", 2000.0)
    assert risk_increasing is False
    assert abs(projected - 100.0) < 1e-9  # 仅其他 symbol 敞口,本单增量不计入


def test_flatten_intent_is_never_blocked() -> None:
    engine = _bare_engine(
        positions={"BTCUSDT": (30.0, 100.0), "SOLUSDT": (20.0, 200.0)},
        inflight={},
        last_prices={"BTCUSDT": 100.0, "SOLUSDT": 200.0},
        symbols=["BTCUSDT", "SOLUSDT"],
    )
    risk_increasing, _ = engine._portfolio_exposure_projection("SOLUSDT", 0.0)
    assert risk_increasing is False


def test_conservative_price_uses_max_of_last_and_entry() -> None:
    """R2 反例 4: 陈旧行情价低估时取 max(last, entry) 保守计敞口。"""
    engine = _bare_engine(
        positions={"BTCUSDT": (1.0, 300.0)},  # entry 300
        inflight={},
        last_prices={"BTCUSDT": 100.0},  # 陈旧 last 100
        symbols=["BTCUSDT"],
    )
    _risk, projected = engine._portfolio_exposure_projection("SOLUSDT", 0.0)
    # 本 symbol 无持仓,但 BTC 敞口按 max(100,300)=300 计
    assert abs(projected - 300.0) < 1e-9
