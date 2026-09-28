"""The suite never pages a real channel, however a test builds a child process's environment.

2026-09-28: `test_the_membership_page_is_once_a_day.py` copied `os.environ` into a subprocess and
overrode only the first webhook URL.  The child would have received the operator's real second URL
the day it was exported (O-2 of the production-refactor execution plan) and paged it on every run
started where `~/.zshrc`'s exports are in the environment: the operator's interactive shell, and the
nightly data job, which evals them before `pytest -m archive`.  CI never has the variables, so CI
could not have seen it.  `tests/conftest.py` now deletes every real-channel variable
before each test; these two hold that in place.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import REAL_CHANNEL_PREFIXES, strip_real_channels


def test_no_real_channel_is_visible_inside_a_test() -> None:
    """Trivially green in CI; on a machine that exports the URLs, red the day the autouse fixture goes."""
    assert not [name for name in os.environ if name.startswith(REAL_CHANNEL_PREFIXES)]


def test_the_strip_removes_every_channel_by_prefix_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "BEIDOU_ALERTS_WEBHOOK_URL": "https://example.invalid/one",
        "BEIDOU_ALERTS_WEBHOOK_URL_2": "https://example.invalid/two",
        "BEIDOU_DEADMAN_LOOP_URL": "https://example.invalid/loop",
        "BEIDOU_TRIALS_LEDGER_PROBE": "kept",
    }.items():
        monkeypatch.setenv(name, value)
    removed = strip_real_channels(monkeypatch)
    assert removed == ["BEIDOU_ALERTS_WEBHOOK_URL", "BEIDOU_ALERTS_WEBHOOK_URL_2", "BEIDOU_DEADMAN_LOOP_URL"]
    assert os.environ["BEIDOU_TRIALS_LEDGER_PROBE"] == "kept"
    assert not [name for name in os.environ if name.startswith(REAL_CHANNEL_PREFIXES)]
