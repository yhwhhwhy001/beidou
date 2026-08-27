"""Rare but safety-critical engine bootstrap branches."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

from beidou_core.engine import AutonomousEngine
from beidou_shared.config import DatabaseConfig, Environment, ExchangeConfig, InfrastructureConfig, TypedSettings


class _Store:
    def restore_user_stream_projection(self, *_args):
        return {"last_sequence": 4, "positions": {}, "open_orders": []}

    def restore_user_stream_events(self, **_kwargs):
        return []

    def __getattr__(self, name: str):
        if name.startswith("restore_"):
            return lambda *_args, **_kwargs: []
        return lambda *_args, **_kwargs: None


class _Outbox:
    _db_path = "diagnostic"
    _outbox: ClassVar[list[object]] = []

    def restore_pending_approvals(self):
        return [
            {
                "approval_id": "approval-1",
                "signature": "signed-1",
                "expires_at": "4102444800",
                "nonce": "nonce-1",
                "risk_snapshot_hash": "risk-hash",
                "policy_version": "policy-v1",
            }
        ]

    def inflight_signed_quantity(self, _symbol):
        return 0.0

    def __getattr__(self, name: str):
        return lambda *_args, **_kwargs: []


class _Signer:
    def __init__(self, **_kwargs):
        pass

    def register(self, *_args, **_kwargs):
        return None

    def restore_replay_state(self):
        return 0

    def restore_signature(self, _signature, _expires_at):
        return True

    def nonce_consumed(self, _nonce):
        return True


class _ApprovalState:
    def approve(self, *_args, **_kwargs):
        return None

    def consume(self, *_args, **_kwargs):
        return None


def _settings(tmp_path: Path, *, environment: Environment, database_url: str) -> TypedSettings:
    return TypedSettings(
        environment=environment,
        database=DatabaseConfig(url=database_url),
        exchange=ExchangeConfig(rest_base_url="https://paper.invalid", ws_base_url="wss://paper.invalid"),
        infrastructure=InfrastructureConfig(
            health_host="127.0.0.1",
            health_port=0,
        ),
    )


def test_engine_bootstrap_explicit_hold_unsupported_backend_and_restore_edges(tmp_path, monkeypatch) -> None:
    import beidou_core.engine as module

    settings = _settings(tmp_path, environment=Environment.TESTNET, database_url="mysql://diagnostic.invalid/state")
    envelope = SimpleNamespace(
        policy_id="risk_parameters",
        version="policy-v1",
        signature="signature",
        parameters={
            "universe_score_weights": {
                "capacity": 0.2,
                "depth": 0.2,
                "spread": 0.2,
                "stability": 0.2,
                "volume": 0.2,
            },
            "max_leverage": 3.0,
            "max_concentration_pct": 50.0,
            "max_position_notional": 500000.0,
            "max_total_leverage": 3.0,
            "drift_threshold": 0.1,
            "max_instruments": 10,
            "max_drawdown_pct": 20.0,
            "max_daily_loss_pct": 5.0,
            "max_consecutive_losses": 5,
            "risk_per_trade_pct": 1.0,
            "min_sharpe_rolling": 0.0,
        },
        validate_risk_parameters=lambda: (True, "complete"),
    )
    monkeypatch.setattr(module.ConfigProvider, "load", lambda _self, environment="": settings)
    monkeypatch.setattr(module.PolicyLoader, "load", lambda _self, _policy_id: envelope)
    store = _Store()
    monkeypatch.setattr(module.PersistentStore, "get_instance", staticmethod(lambda _path: store))
    monkeypatch.setattr(module, "IntentOutbox", lambda **_kwargs: _Outbox())
    monkeypatch.setattr(module, "RiskApprovalSignerImpl", _Signer)
    monkeypatch.setattr(module, "RiskApprovalStateMachine", _ApprovalState)
    monkeypatch.setenv("BEIDOU_TERMINAL_WRITE_HOLD", "hard")
    monkeypatch.setenv("BEIDOU_CHAOS_ENABLED", "true")

    class BrokenChaos:
        def __init__(self):
            raise RuntimeError("chaos bootstrap")

    monkeypatch.setattr("beidou_chaos.engine.ChaosEngine", BrokenChaos)
    monkeypatch.setattr(
        "beidou_core.evidence_bridge.EvidenceBridge.load_and_apply",
        staticmethod(lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("evidence bridge"))),
    )

    engine = AutonomousEngine(["btcusdt"], mode="testnet")
    assert engine._can_write is True
    assert engine._state_backend_supported is False
    assert engine._state_backend_error == "UNSUPPORTED_DATABASE_URL"
    assert engine._user_stream_projector.sequencer.last_sequence == 4
    assert engine._event_stream_facts is not None
    assert engine._trading_pool._score_weights == {
        "capacity": 0.2,
        "depth": 0.2,
        "spread": 0.2,
        "stability": 0.2,
        "volume": 0.2,
    }
    assert engine._chaos_engine is None


def test_engine_bootstrap_policy_loader_exception_is_retained_as_blocker(tmp_path, monkeypatch) -> None:
    import beidou_core.engine as module

    settings = _settings(tmp_path, environment=Environment.PAPER, database_url=f"sqlite:///{tmp_path / 'state.db'}")
    monkeypatch.setattr(module.ConfigProvider, "load", lambda _self, environment="": settings)

    def broken_load(_self, _policy_id):
        raise RuntimeError("policy store unavailable")

    monkeypatch.setattr(module.PolicyLoader, "load", broken_load)
    monkeypatch.setattr(module.PersistentStore, "_instance", None)
    engine = AutonomousEngine(["BTCUSDT"], mode="paper")
    assert engine._policy_error == "SIGNED_POLICY_LOAD_ERROR:RuntimeError"
