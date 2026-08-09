"""Closed-bar live factor pairing contracts.

These tests exercise the same small boundary used by ``AutonomousEngine``
without starting the service or touching an exchange.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from beidou_core.engine import AutonomousEngine


def _engine_for_pairing() -> AutonomousEngine:
    engine = object.__new__(AutonomousEngine)
    engine._factor_pairs_by_scope = {}
    engine._factor_pending_by_scope = {}
    engine._factor_last_bar_by_scope = {}
    return engine


def test_prediction_pairs_only_with_next_closed_bar_forward_return() -> None:
    engine = _engine_for_pairing()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)

    assert engine._advance_factor_bar("BTCUSDT", "1h", t0, 100.0)
    engine._store_factor_predictions("BTCUSDT", "1h", t0, 100.0, {"factor": 0.5})
    assert engine._advance_factor_bar("BTCUSDT", "1h", t1, 110.0)

    scope = ("BINANCE", "BTCUSDT", "1h", 1)
    assert engine._factor_pairs_by_scope[scope]["factor"] == [(0.5, 0.1)]


def test_same_bar_polling_and_gaps_do_not_duplicate_or_change_horizon() -> None:
    engine = _engine_for_pairing()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t3 = t0 + timedelta(hours=3)

    assert engine._advance_factor_bar("BTCUSDT", "1h", t0, 100.0)
    engine._store_factor_predictions("BTCUSDT", "1h", t0, 100.0, {"factor": 1.0})
    assert not engine._advance_factor_bar("BTCUSDT", "1h", t0, 101.0)
    assert engine._advance_factor_bar("BTCUSDT", "1h", t3, 120.0)

    scope = ("BINANCE", "BTCUSDT", "1h", 1)
    assert engine._factor_pairs_by_scope.get(scope, {}).get("factor", []) == []


def test_symbol_scopes_never_mix_factor_samples() -> None:
    engine = _engine_for_pairing()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)

    for symbol, entry, prediction, exit_price in (
        ("BTCUSDT", 100.0, 1.0, 110.0),
        ("ETHUSDT", 200.0, -1.0, 180.0),
    ):
        assert engine._advance_factor_bar(symbol, "1h", t0, entry)
        engine._store_factor_predictions(symbol, "1h", t0, entry, {"factor": prediction})
        assert engine._advance_factor_bar(symbol, "1h", t1, exit_price)

    assert engine._factor_pairs_by_scope[("BINANCE", "BTCUSDT", "1h", 1)]["factor"] == [(1.0, 0.1)]
    assert engine._factor_pairs_by_scope[("BINANCE", "ETHUSDT", "1h", 1)]["factor"] == [(-1.0, -0.1)]
