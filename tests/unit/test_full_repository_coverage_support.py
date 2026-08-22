"""Behavior-backed coverage for policy, infrastructure, and fault boundaries."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_chaos.fault_injection import FaultInjectionResult, FaultScenario
from beidou_chaos.process_fault_injector import ProcessFaultConfig, ProcessFaultInjector
from beidou_infra.backup import SQLiteBackupManager
from beidou_infra.lease import FencingLease, LeaseManager, LeaseState
from beidou_policy.loader import PolicyEnvelope, PolicyLoader
from beidou_policy.registry import (
    PolicyCategory,
    PolicyLifecycle,
    PolicyMetadata,
    PolicyPackage,
    PolicyRegistry,
)
from beidou_shared.types import PolicyId, SchemaVersion


def _package(policy_id: str = "POLICY-1", name: str = "risk") -> PolicyPackage:
    return PolicyPackage(
        metadata=PolicyMetadata(
            policy_id=PolicyId(policy_id),
            name=name,
            category=PolicyCategory.HARD_SAFETY,
            schema_version=SchemaVersion("1.0.0"),
            author="coverage",
        ),
        data={"limit": 1},
    )


def test_policy_package_validation_activation_and_registry_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    package = _package()
    assert package.validate(SchemaVersion("other")) is False
    object.__setattr__(package.metadata, "expires_at", datetime.now(timezone.utc) - timedelta(seconds=1))
    assert package.validate(SchemaVersion("1.0.0")) is False
    object.__setattr__(package.metadata, "expires_at", None)

    package.transition(PolicyLifecycle.VALIDATED)
    package.sign("signature", "key")
    package.schedule(datetime.now(timezone.utc) - timedelta(seconds=1))
    package.activate()
    assert package.is_effectively_active()
    package.revoke("covered")
    assert package.lifecycle is PolicyLifecycle.REVOKED
    with pytest.raises(ValueError, match="Invalid policy lifecycle transition"):
        package.transition(PolicyLifecycle.ACTIVE)

    future_package = _package("FUTURE", "future")
    future_package.transition(PolicyLifecycle.VALIDATED)
    future_package.sign("sig", "key")
    future_package.schedule(datetime.now(timezone.utc) + timedelta(hours=1))
    with pytest.raises(ValueError, match="scheduled_at"):
        future_package.activate()
    invalid_activate = _package("INVALID-ACTIVATE", "invalid-activate")
    with pytest.raises(ValueError, match="Invalid policy lifecycle transition"):
        invalid_activate.activate()
    assert invalid_activate.is_effectively_active() is False

    expired_active = _package("EXPIRED", "expired")
    object.__setattr__(expired_active.metadata, "expires_at", datetime.now(timezone.utc) - timedelta(seconds=1))
    expired_active.lifecycle = PolicyLifecycle.ACTIVE
    assert expired_active.is_effectively_active() is False

    registry = PolicyRegistry()
    assert registry.get(PolicyId("missing")) is None
    assert registry.get_active("risk") is None
    assert registry.activate_latest_valid("risk") is None
    assert registry.list_active_policies() == []

    scheduled = _package("POLICY-2")
    scheduled.transition(PolicyLifecycle.VALIDATED)
    scheduled.sign("sig", "key")
    scheduled.schedule(datetime.now(timezone.utc) - timedelta(seconds=1))
    registry.register(scheduled)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(scheduled)
    activated = registry.activate_latest_valid("risk")
    assert activated is not None and activated.lifecycle is PolicyLifecycle.ACTIVE
    assert registry.get_hard_safety_policy("risk") is not None

    future = _package("POLICY-3")
    future.transition(PolicyLifecycle.VALIDATED)
    future.sign("sig", "key")
    future.schedule(datetime.now(timezone.utc) + timedelta(hours=1))
    registry.register(future)
    assert registry.activate_latest_valid("risk") is None
    assert registry.get_active("risk") is not None
    registry._policies[PolicyId("EXPIRED")] = expired_active
    registry._active_index["expired"] = PolicyId("EXPIRED")
    assert registry.get_active("expired") is None

    signed_replacement = _package("POLICY-6", "risk")
    signed_replacement.transition(PolicyLifecycle.VALIDATED)
    signed_replacement.sign("sig", "key")
    registry.register(signed_replacement)
    activated_replacement = registry.set_active(PolicyId("POLICY-6"))
    assert activated_replacement.lifecycle is PolicyLifecycle.ACTIVE
    assert registry.get(PolicyId("POLICY-2")).lifecycle is PolicyLifecycle.REVOKED  # type: ignore[union-attr]

    unsigned = _package("POLICY-4", "unsigned")
    unsigned.lifecycle = PolicyLifecycle.ACTIVE
    registry.register(unsigned)
    registry._active_index["unsigned"] = PolicyId("POLICY-4")
    assert registry.get_active("unsigned") is not None
    assert registry.fail_closed_check("unsigned").value == "UNKNOWN"
    registry._active_index["missing-target"] = PolicyId("missing")
    assert registry.get_active("missing-target") is None
    with pytest.raises(ValueError, match="not found"):
        registry.set_active(PolicyId("missing"))
    with pytest.raises(ValueError, match="not signed"):
        registry.set_active(PolicyId("POLICY-4"))
    invalid_state = _package("POLICY-7", "invalid-state")
    invalid_state.lifecycle = PolicyLifecycle.VALIDATED
    object.__setattr__(invalid_state.metadata, "signature", "sig")
    registry.register(invalid_state)
    with pytest.raises(ValueError, match="not in an activatable state"):
        registry.set_active(PolicyId("POLICY-7"))

    wrong_category = _package("POLICY-5", "adaptive")
    wrong_category.metadata = PolicyMetadata(
        policy_id=wrong_category.metadata.policy_id,
        name=wrong_category.metadata.name,
        category=PolicyCategory.ADAPTIVE_BOUNDARY,
        schema_version=wrong_category.metadata.schema_version,
        author=wrong_category.metadata.author,
        signature="sig",
    )
    wrong_category.lifecycle = PolicyLifecycle.ACTIVE
    registry.register(wrong_category)
    registry._active_index["adaptive"] = PolicyId("POLICY-5")
    assert registry.get_hard_safety_policy("adaptive") is None
    assert {item.metadata.name for item in registry.list_active_policies()} == {"risk", "unsigned", "adaptive"}
    assert registry.fail_closed_check("missing").value == "UNKNOWN"
    assert registry.fail_closed_check("expired").value == "UNKNOWN"
    real_get_active = registry.get_active
    monkeypatch.setattr(registry, "get_active", lambda _name: expired_active)
    assert registry.fail_closed_check("forced-expired").value == "UNKNOWN"
    monkeypatch.setattr(registry, "get_active", real_get_active)
    signed_active = _package("SIGNED-ACTIVE", "signed-active")
    signed_active.lifecycle = PolicyLifecycle.ACTIVE
    object.__setattr__(signed_active.metadata, "signature", "sig")
    registry.register(signed_active)
    registry._active_index["signed-active"] = PolicyId("SIGNED-ACTIVE")
    assert registry.fail_closed_check("signed-active").value == "SUCCESS"


def test_policy_registry_signature_success_and_non_rsa_failure() -> None:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    package = _package("SIGNED", "signed")
    payload = json.dumps(
        {
            "policy_id": package.metadata.policy_id,
            "name": package.metadata.name,
            "category": package.metadata.category.value,
            "schema_version": package.metadata.schema_version,
            "data": package.data,
        },
        sort_keys=True,
    ).encode()
    signature = private.sign(payload, padding.PKCS1v15(), hashes.SHA256()).hex()
    object.__setattr__(package.metadata, "signature", signature)
    registry = PolicyRegistry()
    registry.register(package)
    public_pem = public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    assert registry.verify_signature(PolicyId("SIGNED"), public_pem) is True
    assert registry.verify_signature(PolicyId("missing"), public_pem) is False
    assert registry.verify_signature(PolicyId("SIGNED"), "not-a-key") is False
    ec_key = ec.generate_private_key(ec.SECP256R1()).public_key()
    ec_pem = ec_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    assert registry.verify_signature(PolicyId("SIGNED"), ec_pem) is False


def _risk_parameters() -> dict[str, float | int]:
    return {
        "max_leverage": 3.0,
        "max_concentration_pct": 50.0,
        "max_position_notional": 1000.0,
        "max_total_leverage": 3.0,
        "max_instruments": 5,
        "drift_threshold": 0.1,
        "max_drawdown_pct": 20.0,
        "max_daily_loss_pct": 5.0,
        "max_consecutive_losses": 3,
        "risk_per_trade_pct": 1.0,
        "min_sharpe_rolling": 0.1,
    }


def _write_signed_policy(path: Path, *, values: object, expires_at: str | None = None) -> None:
    payload = {
        "policy_id": "risk_parameters",
        "version": "1",
        "parameters": values,
        "issued_at": "2026-01-01T00:00:00+00:00",
        "expires_at": expires_at,
    }
    canonical = json.dumps(payload, sort_keys=True)
    payload["signature"] = hmac.new(b"key", canonical.encode(), hashlib.sha256).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_policy_envelope_validation_and_loader_cache_fail_closed(tmp_path: Path) -> None:
    valid = PolicyEnvelope(
        policy_id="risk_parameters",
        version="1",
        parameters=_risk_parameters(),
        signature="",
        issued_at="now",
    )
    assert valid.is_expired() is False
    assert valid.verify("") is False
    assert valid.validate_risk_parameters()[0] is True
    incomplete = _risk_parameters()
    incomplete.pop("max_leverage")
    assert PolicyEnvelope("risk_parameters", "1", incomplete, "sig", "now").validate_risk_parameters()[0] is False

    for key, value in (
        ("max_leverage", None),
        ("max_leverage", "bad"),
        ("max_leverage", float("inf")),
        ("max_leverage", 0),
        ("max_concentration_pct", 0),
        ("max_concentration_pct", 101),
        ("max_instruments", 1.5),
        ("max_instruments", 0),
        ("max_consecutive_losses", True),
    ):
        values = _risk_parameters()
        values[key] = value  # type: ignore[assignment]
        checked = PolicyEnvelope("risk_parameters", "1", values, "sig", "now")
        assert checked.validate_risk_parameters()[0] is False

    expired = PolicyEnvelope("risk_parameters", "1", _risk_parameters(), "sig", "now", "not-a-date")
    assert expired.is_expired() is True
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    _write_signed_policy(policy_dir / "risk_parameters.json", values=_risk_parameters())
    loader = PolicyLoader(str(policy_dir), signing_key="key")
    first = loader.load("risk_parameters")
    assert first is not None
    assert loader.load("risk_parameters") is first
    assert loader.get_param("risk_parameters", "max_leverage") == 3.0
    assert loader.get_param("missing", "x", 7) is None

    (policy_dir / "bad.json").write_text("{", encoding="utf-8")
    assert loader.load("bad") is None
    (policy_dir / "wrong.json").write_text(json.dumps({"policy_id": "wrong", "parameters": {}}), encoding="utf-8")
    assert loader.load("wrong") is None
    (policy_dir / "id-mismatch.json").write_text(json.dumps({"policy_id": "other", "parameters": {}}), encoding="utf-8")
    assert loader.load("id-mismatch") is None
    (policy_dir / "not-a-map.json").write_text(
        json.dumps({"policy_id": "not-a-map", "parameters": []}), encoding="utf-8"
    )
    assert loader.load("not-a-map") is None
    cached_expired = PolicyEnvelope("cached", "1", _risk_parameters(), "bad", "now", "2000-01-01T00:00:00+00:00")
    loader._cache["cached"] = cached_expired
    assert loader.load("cached") is None
    _write_signed_policy(policy_dir / "invalid-signature.json", values=_risk_parameters())
    invalid_payload = json.loads((policy_dir / "invalid-signature.json").read_text(encoding="utf-8"))
    invalid_payload["policy_id"] = "invalid-signature"
    invalid_payload["signature"] = "bad"
    (policy_dir / "invalid-signature.json").write_text(json.dumps(invalid_payload), encoding="utf-8")
    assert loader.load("invalid-signature") is None
    _write_signed_policy(
        policy_dir / "risk_parameters.json",
        values=_risk_parameters(),
        expires_at="2000-01-01T00:00:00+00:00",
    )
    loader._cache.clear()
    expired_loaded = loader.load("risk_parameters")
    assert expired_loaded is not None and expired_loaded.is_expired()


def test_lease_backend_success_contention_and_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRedis:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        @classmethod
        def from_url(cls, url: str, **kwargs: object) -> "FakeRedis":
            return cls(url=url, **kwargs)

        def set(self, *_args: object, **_kwargs: object) -> bool:
            return True

    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=FakeRedis))
    monkeypatch.setenv("REDIS_URL", "redis://unit")
    redis_lease = LeaseManager("redis").acquire(generation=4, ttl=7)
    assert redis_lease.state is LeaseState.ACQUIRED
    assert redis_lease.is_valid()

    class ContendedRedis(FakeRedis):
        def set(self, *_args: object, **_kwargs: object) -> bool:
            return False

    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=ContendedRedis))
    assert LeaseManager("redis").acquire().state is LeaseState.FENCED
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=FakeRedis))
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("BEIDOU_REDIS_PORT", "bad")
    assert LeaseManager("redis").acquire().state is LeaseState.UNKNOWN
    monkeypatch.setenv("BEIDOU_REDIS_PORT", "6379")
    monkeypatch.setenv("BEIDOU_REDIS_HOST", "localhost")
    assert LeaseManager("redis").acquire().state is LeaseState.ACQUIRED

    class FakeCursor:
        def fetchone(self) -> tuple[bool]:
            return (True,)

    class FakeConnection:
        def execute(self, *_args: object) -> FakeCursor:
            return FakeCursor()

    class FakePsycopg:
        @staticmethod
        def connect(_url: str) -> FakeConnection:
            return FakeConnection()

    monkeypatch.setitem(sys.modules, "psycopg", FakePsycopg)
    assert LeaseManager("postgresql").acquire().state is LeaseState.ACQUIRED
    monkeypatch.setitem(
        sys.modules, "psycopg", SimpleNamespace(connect=lambda _url: (_ for _ in ()).throw(RuntimeError()))
    )
    assert LeaseManager("postgresql").acquire().state is LeaseState.UNKNOWN

    class FencedCursor:
        def fetchone(self) -> tuple[bool]:
            return (False,)

    class FencedConnection:
        def execute(self, *_args: object) -> FencedCursor:
            return FencedCursor()

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda _url: FencedConnection()))
    assert LeaseManager("postgresql").acquire().state is LeaseState.FENCED

    manager = LeaseManager("paper")
    assert manager.renew() is False
    assert manager.is_current_generation(1) is False
    assert LeaseManager("unsupported").acquire().state is LeaseState.UNKNOWN
    unknown_manager = LeaseManager("unsupported")
    unknown_manager.acquire()
    assert unknown_manager.renew() is False
    assert unknown_manager.is_current_generation(1) is True
    unknown_manager.release()
    unknown = FencingLease(state=LeaseState.UNKNOWN)
    assert unknown.is_valid() is False
    expired = FencingLease(ttl_seconds=1, acquired_at=time.monotonic() - 2)
    assert expired.is_valid() is False and expired.state is LeaseState.EXPIRED
    expired.fence()
    expired.revoke()
    manager = LeaseManager("paper")
    manager.acquire()
    assert manager.renew() is True
    manager.release()
    assert manager.renew() is False
    assert manager.is_current_generation(1) is False


def test_process_fault_injector_failures_and_controlled_scenarios(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = ProcessFaultConfig(pid=123, db_pid=456, fencing_token_path=str(tmp_path / "token"))
    injector = ProcessFaultInjector(config)
    calls: list[tuple[int, int]] = []

    def alive_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    monkeypatch.setattr("beidou_chaos.process_fault_injector.os.kill", alive_kill)
    monkeypatch.setattr("beidou_chaos.process_fault_injector.time.sleep", lambda _seconds: None)
    assert injector.inject_kill_9().invariants_failed == ["PROCESS_STILL_ALIVE"]

    def dead_kill(pid: int, sig: int) -> None:
        if sig == 0:
            raise OSError("dead")

    monkeypatch.setattr("beidou_chaos.process_fault_injector.os.kill", dead_kill)
    assert injector.inject_kill_9().passed
    monkeypatch.setattr(
        "beidou_chaos.process_fault_injector.os.kill", lambda *_args: (_ for _ in ()).throw(OSError("no"))
    )
    assert injector.inject_kill_9().invariants_failed[0].startswith("KILL_FAILED:")
    assert injector.inject_db_crash().invariants_failed[0].startswith("DB_CRASH_FAILED:")
    assert ProcessFaultInjector(ProcessFaultConfig(pid=0)).inject_kill_9().invariants_failed == ["INVALID_PID"]
    assert ProcessFaultInjector(ProcessFaultConfig(db_pid=0)).inject_db_crash().invariants_failed == ["DB_PID_REQUIRED"]
    monkeypatch.setattr("beidou_chaos.process_fault_injector.os.kill", lambda *_args: None)
    assert injector.inject_db_crash().passed

    blocked_path = tmp_path / "directory"
    blocked_path.mkdir()
    assert (
        not ProcessFaultInjector(ProcessFaultConfig(fencing_token_path=str(blocked_path))).inject_dual_instance().passed
    )
    token_exists = tmp_path / "existing-token"
    token_exists.write_text("held", encoding="utf-8")
    assert (
        not ProcessFaultInjector(ProcessFaultConfig(fencing_token_path=str(token_exists))).inject_dual_instance().passed
    )
    success_token = tmp_path / "success-token"
    success_injector = ProcessFaultInjector(ProcessFaultConfig(fencing_token_path=str(success_token)))
    assert success_injector.inject_dual_instance().passed
    success_injector.release_fencing_token()
    monkeypatch.setattr(
        "beidou_chaos.process_fault_injector.os.open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("open"))
    )
    assert not injector.inject_dual_instance().passed
    assert not injector.inject_network_timeout("https://example.com").passed
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=lambda *_args, **_kwargs: object()))
    assert not injector.inject_network_timeout("https://testnet.binancefuture.com").passed
    monkeypatch.setitem(
        sys.modules, "httpx", SimpleNamespace(get=lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()))
    )
    assert injector.inject_network_timeout("https://testnet.binancefuture.com").passed

    injector.inject_dual_instance()
    injector.release_fencing_token()
    injector._config.pid = 0
    monkeypatch.setattr(
        injector,
        "inject_dual_instance",
        lambda: FaultInjectionResult(scenario=FaultScenario.DUAL_INSTANCE, passed=True),
    )
    monkeypatch.setattr(
        injector,
        "inject_network_timeout",
        lambda *_args, **_kwargs: FaultInjectionResult(scenario=FaultScenario.TIMEOUT, passed=True),
    )
    results = injector.run_all_scenarios()
    assert len(results) == 2
    injector._config.pid = 1
    monkeypatch.setattr(
        injector, "inject_kill_9", lambda: FaultInjectionResult(scenario=FaultScenario.KILL_9, passed=True)
    )
    assert len(injector.run_all_scenarios()) == 3


def test_backup_manager_error_and_schema_boundaries(tmp_path: Path) -> None:
    manager = SQLiteBackupManager(required_tables=frozenset({"required_table", "bad-name"}))
    with pytest.raises(FileNotFoundError):
        manager.create(tmp_path / "missing.db", tmp_path / "out.db", backup_id="x")
    source = tmp_path / "source.db"
    source.touch()
    destination = tmp_path / "backup.db"
    destination.write_bytes(b"exists")
    with pytest.raises(FileExistsError):
        manager.create(source, destination, backup_id="x")
    with pytest.raises(RuntimeError, match="backup verification failed"):
        manager.create(source, tmp_path / "missing-tables.db", backup_id="x")
    empty_manager = SQLiteBackupManager(required_tables=frozenset())
    successful = empty_manager.create(source, tmp_path / "successful.db", backup_id="ok")
    assert successful.integrity_check is True
    patcher = pytest.MonkeyPatch()
    patcher.setattr("beidou_infra.backup.os.replace", lambda *_args: (_ for _ in ()).throw(OSError("replace-create")))
    with pytest.raises(OSError, match="replace-create"):
        empty_manager.create(source, tmp_path / "create-fail.db", backup_id="fail")
    patcher.undo()

    malformed = tmp_path / "malformed.db"
    malformed.write_bytes(b"not sqlite")
    verification = SQLiteBackupManager(required_tables=frozenset()).verify(malformed)
    assert verification.passed is False and verification.errors
    with pytest.raises(FileNotFoundError):
        manager.verify(tmp_path / "missing.db")

    valid = tmp_path / "valid.db"
    import sqlite3

    with sqlite3.connect(valid) as conn:
        conn.execute("CREATE TABLE required_table (id INTEGER)")
        conn.execute('CREATE TABLE "bad-name" (id INTEGER)')
        conn.commit()
    result = manager.verify(valid)
    assert result.passed is False and any("invalid_required_table_name" in error for error in result.errors)

    class Cursor:
        def __init__(self, value: object) -> None:
            self.value = value

        def fetchone(self) -> tuple[object]:
            return (self.value,)

        def fetchall(self) -> list[tuple[object]]:
            return list(self.value) if isinstance(self.value, list) else []

    class BrokenConnection:
        def __enter__(self) -> "BrokenConnection":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

        def execute(self, statement: str) -> Cursor:
            if "integrity_check" in statement:
                return Cursor("corrupt")
            if "foreign_key_check" in statement:
                return Cursor([(1, 2, 3)])
            return Cursor([])

    import beidou_infra.backup as backup_module

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(backup_module.sqlite3, "connect", lambda *_args, **_kwargs: BrokenConnection())
    broken = empty_manager.verify(valid)
    monkeypatch.undo()
    assert "integrity_check=corrupt" in broken.errors
    assert "foreign_key_check_rows=1" in broken.errors

    import beidou_infra.backup as backup_module

    original_replace = backup_module.os.replace
    backup_module.os.replace = lambda *_args: (_ for _ in ()).throw(OSError("replace"))
    with pytest.raises(OSError, match="replace"):
        SQLiteBackupManager._atomic_write(tmp_path / "atomic-fail", b"payload")
    backup_module.os.replace = original_replace

    encrypted = tmp_path / "encrypted.bin"
    with pytest.raises(ValueError, match="32 key bytes"):
        SQLiteBackupManager.encrypt(valid, encrypted, key=b"short")
    SQLiteBackupManager.encrypt(valid, encrypted, key=b"k" * 32)
    with pytest.raises(FileExistsError):
        SQLiteBackupManager.encrypt(valid, encrypted, key=b"k" * 32)
    decrypted = tmp_path / "decrypted.db"
    with pytest.raises(ValueError, match="32 key bytes"):
        SQLiteBackupManager.decrypt(encrypted, decrypted, key=b"short")
    assert SQLiteBackupManager.decrypt(encrypted, decrypted, key=b"k" * 32) == decrypted
    with pytest.raises(FileExistsError):
        SQLiteBackupManager.decrypt(encrypted, decrypted, key=b"k" * 32)

    bad_encrypted = tmp_path / "bad.bin"
    bad_encrypted.write_bytes(b"bad")
    with pytest.raises(ValueError, match="invalid encrypted"):
        SQLiteBackupManager.decrypt(bad_encrypted, tmp_path / "bad-out.db", key=b"k" * 32)
