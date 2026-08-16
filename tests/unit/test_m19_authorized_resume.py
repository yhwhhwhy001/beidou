"""M19-F01 (P0-12): authorize_resume 授权链挂接测试。

覆盖三条接线:
1. ControlPlane.execute_authorized_resume — 唯一受门禁的 RESUME 入口
2. ControlPlaneAPI.resume_trading — 手动 RESUME 过 TruthSnapshot 门禁
3. BeidouSupervisor._authorize_resume_via_truth_snapshot — 启动授权链门禁
   (零写模式走 EXEMPT-08 豁免)

快照构造器为独立实现(不 import 其他测试文件的 helper)。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

from beidou_control.plane import ControlAction
from beidou_control.truth import TruthSnapshot


def _fresh_snapshot() -> TruthSnapshot:
    """构建一个完整新鲜、三件套合格的快照。"""
    now = datetime.now(timezone.utc).timestamp()
    return TruthSnapshot(
        snapshot_id="m19-test",
        created_at=datetime.now(timezone.utc).isoformat(),
        market_hash="h" * 8,
        account_hash="h" * 8,
        order_hash="h" * 8,
        position_hash="h" * 8,
        ledger_hash="h" * 8,
        reconciliation_hash="h" * 8,
        protection_hash="h" * 8,
        risk_hash="h" * 8,
        config_hash="h" * 8,
        policy_hash="h" * 8,
        market_freshness=now,
        account_freshness=now,
        order_freshness=now,
        position_freshness=now,
        ledger_freshness=now,
        reconciliation_freshness=now,
        protection_freshness=now,
        risk_freshness=now,
        config_freshness=now,
        policy_freshness=now,
        reconciliation_status="MATCHED",
        protection_status="ACTIVE",
        risk_status="NORMAL",
    )


def _isolated_control_plane(tmp_path, monkeypatch):
    """ControlPlane 实例,状态文件重定向到 tmp_path(避免污染 CWD)。"""
    from beidou_control import plane as plane_mod

    monkeypatch.setattr(plane_mod, "_STATE_FILE", str(tmp_path / "control_state.json"))
    return plane_mod.ControlPlane()


class TestExecuteAuthorizedResume:
    """ControlPlane.execute_authorized_resume — 唯一受门禁 RESUME 入口。"""

    def test_rejects_without_snapshot(self, tmp_path, monkeypatch):
        cp = _isolated_control_plane(tmp_path, monkeypatch)
        allowed, reason = cp.execute_authorized_resume()
        assert not allowed
        assert "no truthsnapshot" in reason.lower()
        assert cp.get_status() == ControlAction.NO_NEW_RISK

    def test_passes_gate_and_switches_state(self, tmp_path, monkeypatch):
        cp = _isolated_control_plane(tmp_path, monkeypatch)
        allowed, reason = cp.execute_authorized_resume(_fresh_snapshot())
        assert allowed, reason
        assert cp.get_status() == ControlAction.RESUME

    def test_rejects_stale_snapshot_state_unchanged(self, tmp_path, monkeypatch):
        cp = _isolated_control_plane(tmp_path, monkeypatch)
        old = time.time() - 400
        stale = TruthSnapshot(
            **{
                **_fresh_snapshot().__dict__,
                "reconciliation_freshness": old,
                "protection_freshness": old,
                "risk_freshness": old,
            }
        )
        version_before = cp.version
        allowed, _ = cp.execute_authorized_resume(stale)
        assert not allowed
        assert cp.get_status() == ControlAction.NO_NEW_RISK
        assert cp.version == version_before

    def test_uses_latest_synced_snapshot(self, tmp_path, monkeypatch):
        cp = _isolated_control_plane(tmp_path, monkeypatch)
        cp.update_truth_snapshot(_fresh_snapshot())
        allowed, reason = cp.execute_authorized_resume()
        assert allowed, reason
        assert cp.get_status() == ControlAction.RESUME

    def test_rejects_mismatched_reconciliation(self, tmp_path, monkeypatch):
        cp = _isolated_control_plane(tmp_path, monkeypatch)
        snap = TruthSnapshot(**{**_fresh_snapshot().__dict__, "reconciliation_status": "MISMATCHED"})
        allowed, _ = cp.execute_authorized_resume(snap)
        assert not allowed
        assert cp.get_status() == ControlAction.NO_NEW_RISK


class TestResumeTradingApiGate:
    """ControlPlaneAPI.resume_trading — 手动 RESUME 过门禁。"""

    def test_without_snapshot_rejected(self, tmp_path, monkeypatch):
        from beidou_control.api import ControlPlaneAPI

        cp = _isolated_control_plane(tmp_path, monkeypatch)  # 已接线但无快照
        api = ControlPlaneAPI()
        api.wire_control_plane(cp)
        result = api.resume_trading()
        assert result["action"] == "RESUME"
        assert result["success"] is False
        assert "no truthsnapshot" in str(result.get("error", "")).lower()

    def test_with_valid_snapshot_allowed(self, tmp_path, monkeypatch):
        from beidou_control.api import ControlPlaneAPI

        cp = _isolated_control_plane(tmp_path, monkeypatch)
        cp.update_truth_snapshot(_fresh_snapshot())
        api = ControlPlaneAPI()
        api.wire_control_plane(cp)
        result = api.resume_trading()
        assert result["success"] is True, result
        assert result["new_status"] == "ControlAction.RESUME"

    def test_with_stale_snapshot_rejected(self, tmp_path, monkeypatch):
        from beidou_control.api import ControlPlaneAPI

        cp = _isolated_control_plane(tmp_path, monkeypatch)
        old = time.time() - 400
        stale = TruthSnapshot(**{**_fresh_snapshot().__dict__, "risk_freshness": old})
        cp.update_truth_snapshot(stale)
        api = ControlPlaneAPI()
        api.wire_control_plane(cp)
        result = api.resume_trading()
        assert result["success"] is False
        assert "rejected" in str(result.get("error", "")).lower()
        assert cp.get_status() == ControlAction.NO_NEW_RISK


def _make_supervisor(tmp_path, mode):
    from beidou_launcher.supervisor import BeidouSupervisor

    return BeidouSupervisor(
        project_root=tmp_path,
        mode=mode,
        symbols=["BTCUSDT"],
        port=19090,
        startup_timeout=0.02,
    )


class TestSupervisorStartupAuthorizationGate:
    """启动授权链 — _authorize_resume_via_truth_snapshot。"""

    def test_zero_write_mode_exempt_even_without_engine(self, tmp_path):
        sup = _make_supervisor(tmp_path, "paper")
        sup.engine = None
        allowed, reason = sup._authorize_resume_via_truth_snapshot()
        assert allowed
        assert "exempt-08" in reason.lower()

    def test_writable_mode_without_engine_rejected(self, tmp_path):
        sup = _make_supervisor(tmp_path, "testnet")
        sup.engine = None
        allowed, _ = sup._authorize_resume_via_truth_snapshot()
        assert not allowed

    def test_writable_mode_with_eligible_snapshot_allowed(self, tmp_path):
        sup = _make_supervisor(tmp_path, "testnet")
        snap = _fresh_snapshot()
        sup.engine = SimpleNamespace(
            build_truth_snapshot=lambda: snap,
            _control=SimpleNamespace(authorize_resume=lambda s: (True, "ok")),
        )
        allowed, reason = sup._authorize_resume_via_truth_snapshot()
        assert allowed, reason

    def test_writable_mode_with_rejected_snapshot_denied(self, tmp_path):
        sup = _make_supervisor(tmp_path, "testnet")
        snap = _fresh_snapshot()
        sup.engine = SimpleNamespace(
            build_truth_snapshot=lambda: snap,
            _control=SimpleNamespace(authorize_resume=lambda s: (False, "RESUME rejected: stale")),
        )
        allowed, reason = sup._authorize_resume_via_truth_snapshot()
        assert not allowed
        assert "stale" in reason.lower()
