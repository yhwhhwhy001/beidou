"""Coverage-gap tests for beidou_launcher.preflight G5 journal probe."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from beidou_launcher import preflight
from beidou_launcher.models import CheckStatus


class _FakeJournalCursor:
    def fetchone(self):
        return ("table",)

    def fetchall(self):
        return []


class _FakeJournalConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _sql, _params=()):
        return _FakeJournalCursor()


def test_preflight_g5_journal_probe_success_path(monkeypatch) -> None:
    from beidou_policy.loader import PolicyLoader

    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(preflight, "current_commit", lambda _root: "a" * 40)
    monkeypatch.setattr(preflight, "_git_worktree_state", lambda _root: (True, [" M source.py"], ""))
    monkeypatch.setattr(preflight, "_port_available", lambda _port: (True, "available"))
    monkeypatch.setattr(preflight, "check_package_imports", lambda: [])
    monkeypatch.setenv("BEIDOU_BINANCE_API_KEY", "testnet-api-key")
    monkeypatch.setenv("BEIDOU_BINANCE_API_SECRET", "testnet-api-secret")
    monkeypatch.setenv("BEIDOU_SIGNING_KEY", "testnet-signing-key")
    monkeypatch.setattr(
        PolicyLoader,
        "load",
        lambda self, policy_id: SimpleNamespace(
            validate_risk_parameters=lambda: (policy_id == "risk_parameters", "complete")
        ),
    )
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda *a, **k: _FakeJournalConnection()))

    checks, _settings = preflight._run_preflight(root, "testnet", 19107, require_g5_certificate=False)

    journal_check = next(item for item in checks if item.check_id == "startup.safety.g5_baseline_journal")
    assert journal_check.status is CheckStatus.PASS
    assert journal_check.evidence.get("journal_count") == 0
    assert "probe_error" not in journal_check.evidence
