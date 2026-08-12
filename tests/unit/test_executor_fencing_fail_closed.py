"""Single-owner lease and global fencing-generation invariants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from beidou_safety.executor_impl import FencingProtection, LeaseManager
from beidou_shared.types import ResultStatus


def test_lease_rejects_invalid_configuration_and_empty_owner() -> None:
    with pytest.raises(ValueError, match="positive"):
        LeaseManager(0)
    assert not LeaseManager().acquire(" ")


def test_lease_is_global_and_only_current_owner_can_renew_or_release() -> None:
    lease = LeaseManager(30)
    assert lease.acquire("executor-1")
    assert lease.is_active("executor-1")
    assert not lease.acquire("executor-2")
    assert not lease.renew("executor-2")
    lease.release("executor-2")
    assert lease.is_active("executor-1")
    assert lease.renew("executor-1")
    lease.release("executor-1")
    assert not lease.is_active("executor-1")
    assert lease.acquire("executor-2")


def test_expired_owner_cannot_renew_and_new_owner_can_acquire() -> None:
    lease = LeaseManager(1)
    assert lease.acquire("executor-1")
    lease._expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not lease.is_active("executor-1")
    assert not lease.renew("executor-1")
    assert lease.acquire("executor-2")
    assert lease.is_active("executor-2")


def test_fencing_rejects_unknown_stale_and_reused_generations() -> None:
    fencing = FencingProtection()
    assert fencing.check("unknown", 1) is ResultStatus.ERROR
    with pytest.raises(ValueError, match="strictly newer"):
        fencing.promote("", 1)
    fencing.promote("executor-1", 1)
    assert fencing.check("executor-1", 1) is ResultStatus.SUCCESS
    assert fencing.check("executor-1", 0) is ResultStatus.ERROR
    with pytest.raises(ValueError, match="strictly newer"):
        fencing.promote("executor-1", 1)


def test_new_executor_promotion_implicitly_fences_old_owner() -> None:
    fencing = FencingProtection()
    fencing.promote("executor-1", 1)
    fencing.promote("executor-2", 2)
    assert fencing.check("executor-1", 1) is ResultStatus.ERROR
    assert fencing.check("executor-2", 2) is ResultStatus.SUCCESS
    fencing.fence("executor-2")
    assert fencing.check("executor-2", 2) is ResultStatus.ERROR
