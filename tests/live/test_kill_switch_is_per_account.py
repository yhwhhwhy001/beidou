"""L1-07, still open: the emergency stop resolves against the working directory.

DL-L5 fixed the half that lived inside one process - `live_cmd` and `live_config` each built the path
from the raw profile string, so they could disagree without anyone moving.  One helper, resolved once,
and that part is real.  The half the helper's own docstring describes is untouched: `Path(...).resolve()`
anchors a relative path to `os.getcwd()`, so

    beidou live kill-switch          # from the repo    -> /Users/.../beidou/.beidou/live/KILL_SWITCH
    beidou live kill-switch          # from a worktree  -> /Users/.../worktrees/x/.beidou/live/KILL_SWITCH

are different files, and the loop reads the first.  Measured 2026-09-07 against the running loop: the
second prints "kill switch engaged", exits 0, and stops nothing.

This is DL-L1's sentence again.  What has to be unique per ACCOUNT cannot be addressed by a path
relative to a working directory - which is why the instance lock is keyed to the API key and lives in
Application Support, and why the kill switch belongs beside it.

The engine reads the union of both, because a kill switch must fail toward stopping: an operator who
engages the old path still stops the book, and release has to clear both or they would be stuck.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_the_account_switch_sits_beside_the_account_lock() -> None:
    from beidou_live.lock import account_kill_switch_path, account_lock_path

    key = "some-api-key"
    switch = account_kill_switch_path(key)
    lock = account_lock_path(key)

    assert switch.parent == lock.parent
    assert switch.stem == lock.stem  # same account fingerprint, different suffix
    assert switch != lock


def test_two_working_directories_reach_the_same_account_switch(tmp_path: Path) -> None:
    """The whole point.  The path may not depend on where the command was run from."""
    from beidou_live.lock import account_kill_switch_path

    root = tmp_path / "support"
    assert account_kill_switch_path("k", root=root) == account_kill_switch_path("k", root=root)
    assert account_kill_switch_path("k", root=root) != account_kill_switch_path("other", root=root)


def test_either_file_stops_the_book(tmp_path: Path) -> None:
    """Fail toward stopping: the legacy path an operator may already rely on still works."""
    from beidou_live.engine import kill_switch_engaged

    legacy = tmp_path / "legacy" / "KILL_SWITCH"
    account = tmp_path / "account" / "k.KILL_SWITCH"
    legacy.parent.mkdir()
    account.parent.mkdir()

    assert kill_switch_engaged((legacy, account)) is False
    legacy.write_text("engaged", encoding="utf-8")
    assert kill_switch_engaged((legacy, account)) is True
    legacy.unlink()
    account.write_text("engaged", encoding="utf-8")
    assert kill_switch_engaged((legacy, account)) is True


def test_release_clears_both_or_the_operator_is_stuck(tmp_path: Path) -> None:
    from beidou_live.engine import release_kill_switches

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_text("x", encoding="utf-8")
    b.write_text("x", encoding="utf-8")

    released = release_kill_switches((a, b, tmp_path / "never-existed"))

    assert sorted(p.name for p in released) == ["a", "b"]
    assert not a.exists() and not b.exists()


def test_a_missing_directory_is_not_an_engaged_switch(tmp_path: Path) -> None:
    """An unreadable path must not read as "stop" - that would halt the book on a typo."""
    from beidou_live.engine import kill_switch_engaged

    assert kill_switch_engaged((tmp_path / "no" / "such" / "dir" / "KILL_SWITCH",)) is False


def test_the_engine_config_carries_both_paths() -> None:
    from beidou_live.engine import LiveConfig

    assert "kill_switch_paths" in LiveConfig.__dataclass_fields__


@pytest.mark.parametrize("key", ["Zq7XpLm3VtRw9KdN", "Ae4BsCf6GhJk8MnP"])
def test_the_fingerprint_never_contains_the_key(key: str) -> None:
    """Same rule as the lock: the file name is a digest, never the credential.

    The key here has to be credential-shaped rather than a letter: a one-character "key" appears
    inside `Application Support` by accident, and a test that passes for that reason checks nothing.
    """
    from beidou_live.lock import account_kill_switch_path

    assert key not in str(account_kill_switch_path(key))


def test_engaging_writes_both_so_there_is_no_transition_gap(tmp_path: Path) -> None:
    """A loop started before this change reads only the configured path.

    Writing the account path alone would make the switch LESS effective until the next restart, which
    is the wrong direction for an emergency stop to move even briefly.
    """
    from beidou_live.engine import engage_kill_switches

    a = tmp_path / "repo" / "KILL_SWITCH"
    b = tmp_path / "support" / "abc.KILL_SWITCH"

    written = engage_kill_switches((a, b), "engaged at T\n")

    assert sorted(p.name for p in written) == ["KILL_SWITCH", "abc.KILL_SWITCH"]
    assert a.read_text(encoding="utf-8") == "engaged at T\n"
    assert b.read_text(encoding="utf-8") == "engaged at T\n"
