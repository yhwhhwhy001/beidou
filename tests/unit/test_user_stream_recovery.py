"""User-stream auto-recovery after terminal faults (BD-FIX).

Covers the bounded backoff restart wired into ``_user_stream_fault``:
a successful restart restores CONNECTED and resets the attempt counter,
repeated failures stop after the cap, and a stopping engine never restarts.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from beidou_core.engine import AutonomousEngine


def _engine(**overrides: object) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._user_stream_runtime = {
        "status": "FAILED",
        "last_event_mono": None,
        "last_state_mono": None,
        "listen_key_active": False,
        "last_error": "boom",
    }
    engine._user_stream_stopping = False
    engine._user_stream_restarting = False
    engine._user_stream_restart_attempts = 0
    engine._can_write = True
    engine._running = True
    for name, value in overrides.items():
        setattr(engine, name, value)
    return engine


@pytest.mark.asyncio
async def test_restart_success_restores_connected_and_resets_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(_user_stream_restart_attempts=2, _USER_STREAM_RESTART_BACKOFF_S=0.0)
    stop = AsyncMock()
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "_stop_user_stream", stop)
    monkeypatch.setattr(engine, "_start_user_stream", start)

    await engine._restart_user_stream_after_fault("LISTEN_KEY_KEEPALIVE_FAILED")

    stop.assert_awaited_once()
    start.assert_awaited_once()
    assert engine._user_stream_restart_attempts == 0
    assert engine._user_stream_runtime["status"] == "CONNECTED"
    assert engine._user_stream_runtime["last_error"] == ""
    assert engine._user_stream_runtime["listen_key_active"] is True
    assert engine._user_stream_restarting is False


@pytest.mark.asyncio
async def test_restart_failures_stop_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(_USER_STREAM_RESTART_BACKOFF_S=0.0)
    start = AsyncMock(return_value=False)
    stop = AsyncMock()
    fault = Mock()
    monkeypatch.setattr(engine, "_start_user_stream", start)
    monkeypatch.setattr(engine, "_stop_user_stream", stop)
    monkeypatch.setattr(engine, "_user_stream_fault", fault)

    for _ in range(engine._USER_STREAM_RESTART_MAX_ATTEMPTS + 2):
        await engine._restart_user_stream_after_fault("LISTEN_KEY_KEEPALIVE_FAILED")

    assert start.await_count == engine._USER_STREAM_RESTART_MAX_ATTEMPTS
    assert engine._user_stream_restart_attempts == engine._USER_STREAM_RESTART_MAX_ATTEMPTS
    assert fault.call_count == engine._USER_STREAM_RESTART_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_terminal_fault_schedules_bounded_restart_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through ``_user_stream_fault``: the created task keeps
    restarting (stop -> backoff -> start) until the attempt cap is reached,
    then the stream stays FAILED with no further restarts."""
    engine = _engine(_USER_STREAM_RESTART_BACKOFF_S=0.0)
    start = AsyncMock(return_value=False)
    monkeypatch.setattr(engine, "_start_user_stream", start)

    engine._user_stream_fault("LISTEN_KEY_KEEPALIVE_FAILED", terminal=True)

    for _ in range(100):
        if engine._user_stream_restart_attempts >= engine._USER_STREAM_RESTART_MAX_ATTEMPTS:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert engine._user_stream_restart_attempts == engine._USER_STREAM_RESTART_MAX_ATTEMPTS
    assert start.await_count == engine._USER_STREAM_RESTART_MAX_ATTEMPTS
    assert engine._user_stream_runtime["status"] == "FAILED"
    assert engine._user_stream_restarting is False


@pytest.mark.asyncio
async def test_stopping_engine_does_not_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(_user_stream_stopping=True)
    stop = AsyncMock()
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "_stop_user_stream", stop)
    monkeypatch.setattr(engine, "_start_user_stream", start)

    await engine._restart_user_stream_after_fault("LISTEN_KEY_KEEPALIVE_FAILED")

    stop.assert_not_awaited()
    start.assert_not_awaited()
    assert engine._user_stream_restart_attempts == 0


@pytest.mark.asyncio
async def test_engine_shutting_down_does_not_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(_running=False)
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "_start_user_stream", start)

    await engine._restart_user_stream_after_fault("LISTEN_KEY_KEEPALIVE_FAILED")

    start.assert_not_awaited()
    assert engine._user_stream_restart_attempts == 0


@pytest.mark.asyncio
async def test_terminal_fault_does_not_schedule_when_stopping(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(_user_stream_stopping=True)
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "_start_user_stream", start)

    engine._user_stream_fault("LISTEN_KEY_KEEPALIVE_FAILED", terminal=True)
    for _ in range(5):
        await asyncio.sleep(0)

    start.assert_not_awaited()
    assert engine._user_stream_restart_attempts == 0


@pytest.mark.asyncio
async def test_non_writable_engine_does_not_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(_can_write=False)
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "_start_user_stream", start)

    engine._user_stream_fault("LISTEN_KEY_KEEPALIVE_FAILED", terminal=True)
    for _ in range(5):
        await asyncio.sleep(0)

    start.assert_not_awaited()
    assert engine._user_stream_restart_attempts == 0


@pytest.mark.asyncio
async def test_g5_producer_restarts_user_stream_while_terminal_writes_are_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G5 may recover its session without authorizing terminal writes."""

    engine = _engine(
        _can_write=False,
        _producer_only=True,
        _USER_STREAM_RESTART_BACKOFF_S=0.0,
    )
    stop = AsyncMock()
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "_stop_user_stream", stop)
    monkeypatch.setattr(engine, "_start_user_stream", start)

    engine._user_stream_fault("LISTEN_KEY_EXPIRED", terminal=True)
    await engine._user_stream_restart_task

    stop.assert_awaited_once()
    start.assert_awaited_once()
    assert engine._user_stream_runtime["status"] == "CONNECTED"
    assert engine._user_stream_runtime["listen_key_active"] is True
