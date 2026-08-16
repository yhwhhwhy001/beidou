"""PKG-29: MAPE-K 自愈测试。指纹匹配、安全降级、检查点、恢复验证。"""

from beidou_autonomy import (
    FaultFingerprint,
    MAPEKController,
    RecoveryAction,
    RecoveryResult,
)


class TestFaultFingerprint:
    def test_exact_match_similarity(self):
        a = FaultFingerprint(symptom_vector={"cpu": 0.95, "memory": 0.90, "latency": 0.85})
        b = FaultFingerprint(symptom_vector={"cpu": 0.95, "memory": 0.90, "latency": 0.85})
        assert a.similarity(b) == 1.0

    def test_no_overlap_zero_similarity(self):
        a = FaultFingerprint(symptom_vector={"cpu": 0.90})
        b = FaultFingerprint(symptom_vector={"disk": 0.90})
        assert a.similarity(b) == 0.0

    def test_partial_overlap(self):
        a = FaultFingerprint(symptom_vector={"cpu": 0.90, "mem": 0.80})
        b = FaultFingerprint(symptom_vector={"cpu": 0.90, "net": 0.70})
        sim = a.similarity(b)
        assert 0.0 < sim < 1.0


class TestMAPEKController:
    def test_no_match_triggers_lock(self):
        ctrl = MAPEKController()
        action, _reason = ctrl.decide_action({"unknown_symptom": 1.0}, "test_module")
        assert action == RecoveryAction.LOCK

    def test_low_similarity_triggers_lock(self):
        ctrl = MAPEKController()
        fp = FaultFingerprint(
            symptom_vector={"cpu": 0.95, "mem": 0.90},
            effective_actions=[RecoveryAction.RESTART_MODULE],
            approved_runbook="runbook-001",
        )
        ctrl.register_fingerprint(fp)
        action, _reason = ctrl.decide_action({"cpu": 0.20, "disk": 0.10}, "test_module")
        assert action == RecoveryAction.LOCK

    def test_approved_runbook_match(self):
        ctrl = MAPEKController()
        fp = FaultFingerprint(
            symptom_vector={"cpu": 0.95, "latency": 0.90},
            effective_actions=[RecoveryAction.RESTART_MODULE],
            approved_runbook="runbook-001",
        )
        ctrl.register_fingerprint(fp)
        action, _reason = ctrl.decide_action({"cpu": 0.95, "latency": 0.90}, "test_module")
        assert action == RecoveryAction.RESTART_MODULE

    def test_unapproved_runbook_degrades(self):
        ctrl = MAPEKController()
        fp = FaultFingerprint(
            symptom_vector={"cpu": 0.95, "latency": 0.90},
            effective_actions=[RecoveryAction.RESTART_MODULE],
            approved_runbook=None,
        )
        ctrl.register_fingerprint(fp)
        action, _reason = ctrl.decide_action({"cpu": 0.95, "latency": 0.90}, "test_module")
        assert action == RecoveryAction.DEGRADE_TO_NO_NEW_RISK

    def test_max_restarts_prevents_infinite_loop(self):
        ctrl = MAPEKController()
        fp = FaultFingerprint(
            symptom_vector={"crash": 1.0},
            effective_actions=[RecoveryAction.RESTART_MODULE],
            approved_runbook="runbook-001",
        )
        ctrl.register_fingerprint(fp)
        checkpoint = ctrl.save_checkpoint("test_module", {}, True)
        for _ in range(3):
            action, _ = ctrl.decide_action({"crash": 1.0}, "test_module")
            ctrl.execute_recovery(action, "test_module", checkpoint)
        action, _ = ctrl.decide_action({"crash": 1.0}, "test_module")
        assert action == RecoveryAction.LOCK

    def test_checkpoint_save_and_retrieve(self):
        ctrl = MAPEKController()
        cp = ctrl.save_checkpoint("test_module", {"pos": 100}, True)
        assert cp.sequence_number == 0
        assert cp.invariants_valid
        retrieved = ctrl.get_latest_valid_checkpoint("test_module")
        assert retrieved is not None
        assert retrieved.checkpoint_id == cp.checkpoint_id

    def test_invalid_checkpoint_not_retrieved(self):
        ctrl = MAPEKController()
        ctrl.save_checkpoint("test_module", {"pos": 100}, False)
        ctrl.save_checkpoint("test_module", {"pos": 200}, False)
        retrieved = ctrl.get_latest_valid_checkpoint("test_module")
        assert retrieved is None

    def test_verify_recovery_all_pass(self):
        ctrl = MAPEKController()
        assert ctrl.verify_recovery("test", {"data_integrity": True, "account_facts": True})

    def test_verify_recovery_any_fail(self):
        ctrl = MAPEKController()
        assert not ctrl.verify_recovery("test", {"data_integrity": True, "account_facts": False})


class TestGovernedRecoveryExecution:
    """M15-F01 (P0-13): execute_recovery_governed — 真实控制面动作 + LOCK 安全过滤。"""

    @staticmethod
    def _control_plane(tmp_path, monkeypatch):
        from beidou_control import plane as plane_mod

        monkeypatch.setattr(plane_mod, "_STATE_FILE", str(tmp_path / "control_state.json"))
        return plane_mod.ControlPlane()

    def test_degrade_action_really_changes_control_state(self, tmp_path, monkeypatch):
        from beidou_control.plane import ControlAction

        ctrl = MAPEKController()
        cp = self._control_plane(tmp_path, monkeypatch)
        result = ctrl.execute_recovery_governed(
            RecoveryAction.DEGRADE_TO_NO_NEW_RISK,
            "test_module",
            control_plane=cp,
        )
        assert result == RecoveryResult.DEGRADED
        assert cp.get_status() == ControlAction.NO_NEW_RISK

    def test_unknown_fault_lock_downgraded_to_no_new_risk(self, tmp_path, monkeypatch):
        """无指纹/低相似 LOCK 改写为 NO_NEW_RISK —— fail-closed 但不锁死。"""
        from beidou_control.plane import ControlAction

        ctrl = MAPEKController()
        cp = self._control_plane(tmp_path, monkeypatch)
        result = ctrl.execute_recovery_governed(
            RecoveryAction.LOCK,
            "test_module",
            reason="No similar fingerprint found; safe default is LOCK",
            control_plane=cp,
        )
        assert result == RecoveryResult.DEGRADED
        assert cp.get_status() == ControlAction.NO_NEW_RISK  # 非 LOCK 终态

    def test_restart_loop_lock_stays_lock(self, tmp_path, monkeypatch):
        """超重启保护的 LOCK 真实执行 —— 循环崩溃必须终态阻断。"""
        from beidou_control.plane import ControlAction

        ctrl = MAPEKController()
        cp = self._control_plane(tmp_path, monkeypatch)
        result = ctrl.execute_recovery_governed(
            RecoveryAction.LOCK,
            "test_module",
            reason="Module test_module restarted 3 times; refusing further auto-restart",
            control_plane=cp,
        )
        assert result == RecoveryResult.DEGRADED
        assert cp.get_status() == ControlAction.LOCK

    def test_governed_never_issues_resume(self, tmp_path, monkeypatch):
        """恢复执行只降级不升级 —— RESUME 永远走 execute_authorized_resume 门禁。"""
        from beidou_control.plane import ControlAction

        ctrl = MAPEKController()
        cp = self._control_plane(tmp_path, monkeypatch)
        for action in RecoveryAction:
            ctrl.execute_recovery_governed(action, "test_module", control_plane=cp)
            assert cp.get_status() != ControlAction.RESUME

    def test_governed_without_control_plane_still_logs(self):
        """无控制面注入时降级动作不抛异常(证据仍记录)。"""
        ctrl = MAPEKController()
        result = ctrl.execute_recovery_governed(
            RecoveryAction.DEGRADE_TO_EXIT_ONLY,
            "test_module",
            control_plane=None,
        )
        assert result == RecoveryResult.DEGRADED
