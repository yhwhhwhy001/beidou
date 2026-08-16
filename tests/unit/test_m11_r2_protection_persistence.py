"""M11-R2: 对抗审查反例回归 —— S33 止损持久化代数的完整性与补写队列。

审查 CONFIRMED_BUG: M11-F02 把 position_generation=0 的 S33 重建止损
持久化 → 重启投影恢复硬阻断(engine.py:3990)+ 覆盖门恒跳过(2923)
→ NO_NEW_RISK 死锁。以及 persist 失败静默吞掉 → 内存/PG 分叉。
"""

from __future__ import annotations

from types import SimpleNamespace

from beidou_core.engine import AutonomousEngine


def _engine() -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._position_generation = {}
    return engine


class TestResolvePositionGeneration:
    """S33 重建代数的解析接缝。"""

    def test_inherits_projection_generation(self):
        engine = _engine()
        pp = SimpleNamespace(position_generation=5)
        assert engine._resolve_position_generation("BTCUSDT", pp) == 5

    def test_falls_back_to_engine_tracking(self):
        engine = _engine()
        engine._position_generation["BTCUSDT"] = 3
        pp = SimpleNamespace(position_generation=0)
        assert engine._resolve_position_generation("BTCUSDT", pp) == 3

    def test_allocates_new_generation_when_both_missing(self):
        engine = _engine()
        pp = SimpleNamespace(position_generation=0)
        gen = engine._resolve_position_generation("BTCUSDT", pp)
        assert gen >= 1
        assert engine._position_generation["BTCUSDT"] == gen

    def test_never_returns_zero(self):
        engine = _engine()
        pp = SimpleNamespace(position_generation=0)
        for _ in range(3):
            gen = engine._resolve_position_generation("BTCUSDT", pp)
            assert gen >= 1


class TestFlushPendingProtectionPersist:
    """persist 失败补写队列 —— 内存 ACTIVE 保留,每轮重试持久化。"""

    def test_flush_retries_and_clears_on_success(self):
        from beidou_safety.protection.engine import ProtectionStatus

        engine = _engine()
        calls: list[str] = []
        failures = {"remaining": 1}

        def _persist(p_order, status=None):
            if failures["remaining"] > 0:
                failures["remaining"] -= 1
                raise OSError("pg down")
            calls.append(p_order.protection_id)

        engine._persist_protection_order = _persist
        p_order = SimpleNamespace(
            protection_id="sl-pos-1",
            status=ProtectionStatus.ACTIVE,
        )
        engine._pending_protection_persist = {"pos-1": [p_order]}

        engine._flush_pending_protection_persist()  # 第一次失败,保留队列
        assert "pos-1" in engine._pending_protection_persist
        assert calls == []

        engine._flush_pending_protection_persist()  # 第二次成功,清空
        assert "pos-1" not in engine._pending_protection_persist
        assert calls == ["sl-pos-1"]

    def test_flush_skips_non_active_orders(self):
        from beidou_safety.protection.engine import ProtectionStatus

        engine = _engine()
        persisted: list[str] = []

        def _persist(p_order, status=None):
            persisted.append(p_order.protection_id)

        engine._persist_protection_order = _persist
        created = SimpleNamespace(protection_id="sl-created", status=ProtectionStatus.CREATED)
        engine._pending_protection_persist = {"pos-1": [created]}
        engine._flush_pending_protection_persist()
        assert persisted == []  # 非 ACTIVE 不补写
        assert "pos-1" not in engine._pending_protection_persist


class TestEnablePitrScript:
    """M16-R2: enable_pitr.sh 在 BSD sed 上的生效校验。"""

    @staticmethod
    def _run_script(tmp_path):
        import subprocess
        from pathlib import Path

        pgdata = Path(tmp_path) / "pgdata"
        pgdata.mkdir()
        conf = pgdata / "postgresql.conf"
        conf.write_text(
            "#wal_level = replica\n"
            "#archive_mode = off\n"
            "#archive_timeout = 0\n"
            "#max_wal_size = 1GB\n"
        )
        script = Path("/Users/maguannan/beidou/scripts/enable_pitr.sh")
        env = {"BEIDOU_PGDATA": str(pgdata), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}
        proc = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, env=env, timeout=60
        )
        return proc, conf

    def test_script_rewrites_commented_keys_and_verifies(self, tmp_path):
        proc, conf = self._run_script(tmp_path)
        assert proc.returncode == 0, proc.stderr
        content = conf.read_text()
        # 注释行被真实改写(BSD sed 下不再静默 no-op)
        assert "archive_mode = on" in content
        assert "wal_level = replica" in content
        assert "archive_timeout = 300" in content

    def test_script_fails_loudly_when_set_fails(self, tmp_path):
        import subprocess
        from pathlib import Path

        pgdata = Path(tmp_path) / "pgdata"
        pgdata.mkdir()
        conf = pgdata / "postgresql.conf"
        # 只读目录无法写 —— sed -i 失败但 grep 校验必须兜底非零退出
        conf.write_text("#archive_mode = off\n")
        conf.chmod(0o444)
        script = Path("/Users/maguannan/beidou/scripts/enable_pitr.sh")
        env = {"BEIDOU_PGDATA": str(pgdata), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}
        proc = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, env=env, timeout=60
        )
        assert proc.returncode != 0
