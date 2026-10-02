"""The live record gets a dated copy every night (operator ruling 2026-10-02, the 09-30 system audit's D8 B).

`.beidou/live` is the only out-of-sample evidence there is and nothing copied it.  `beidou live backup` archives
it into Application Support, keeps the newest 14, refuses the repository - which is public - and pages when it
fails.  `deploy/run_data.sh` runs it first each night, between bars.
"""

from __future__ import annotations

import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner, Result

from beidou_cli import main
from beidou_live.backup import REPOSITORY, BackupRefused, write_backup
from beidou_shared.config import load_yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "run_data.sh"
BASH = Path("/bin/bash")  # what the plist runs: 3.2 on macOS
FILES = {"cycles.jsonl": b'{"bar_open_ms": 1}\n', "state.json": b'{"restarts": 67}\n', "trades.jsonl": b""}


def _record(tmp_path: Path) -> Path:
    live = tmp_path / "live"
    live.mkdir()
    for name, body in FILES.items():
        (live / name).write_bytes(body)
    return live


def _restored(archive: Path) -> dict[str, bytes]:
    with tarfile.open(archive, "r:gz") as tree:
        return {
            Path(member.name).name: tree.extractfile(member).read()  # type: ignore[union-attr]
            for member in tree.getmembers()
            if member.isfile()
        }


def test_a_night_writes_a_copy_that_restores_byte_for_byte(tmp_path: Path) -> None:
    written = write_backup(_record(tmp_path), tmp_path / "backup", now=datetime(2026, 10, 2, 17, 20, 5, tzinfo=UTC))
    assert written.name == "live-20261002T172005Z.tar.gz"
    assert _restored(written) == FILES
    assert written.stat().st_mode & 0o777 == 0o600 and written.parent.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in written.parent.iterdir()] == [written.name], "no partial file is left behind"


def test_only_the_newest_copies_are_kept(tmp_path: Path) -> None:
    live, start = _record(tmp_path), datetime(2026, 10, 1, 17, 20, tzinfo=UTC)
    for night in range(16):
        write_backup(live, tmp_path / "backup", keep=14, now=start + timedelta(days=night))
    kept = sorted(p.name for p in (tmp_path / "backup").glob("live-*.tar.gz"))
    assert len(kept) == 14 and kept[0] == "live-20261003T172000Z.tar.gz" and kept[-1] == "live-20261016T172000Z.tar.gz"


def test_the_repository_is_refused_before_anything_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Against a stand-in checkout: if the refusal ever broke, this test must not be what writes into the real one."""
    checkout = tmp_path / "checkout"
    monkeypatch.setattr("beidou_live.backup.REPOSITORY", checkout)
    with pytest.raises(BackupRefused, match="public"):
        write_backup(_record(tmp_path), checkout / "backup")
    assert not checkout.exists()


def test_the_repository_it_refuses_is_this_checkout() -> None:
    assert REPOSITORY == ROOT


def test_a_directory_without_the_record_is_refused(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(BackupRefused, match=r"cycles\.jsonl"):
        write_backup(tmp_path / "empty", tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


class _Channel:
    sent: list[tuple[str, str | None]]

    def __init__(self, url: str, *, secondary_url: str = "", state_path: Any = None) -> None:
        self.enabled = bool(url or secondary_url)

    async def send(self, text: str, *, key: str | None = None, force: bool = False) -> bool:
        type(self).sent.append((text, key))
        return True


def _backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_dir: Path, *args: str
) -> tuple[Result, type[_Channel]]:
    channel = type("Channel", (_Channel,), {"sent": []})
    monkeypatch.setattr("beidou_cli.live_cmd.WebhookAlerts", channel)
    profile = load_yaml(ROOT / "config/live.demo.yaml")
    profile["paths"] = {"state_dir": str(state_dir)}
    profile["alerts"] = {"webhook_url": "https://hooks.example/beidou"}  # the channel above, never the network
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    result = CliRunner().invoke(main, ["live", "backup", "--profile", str(tmp_path / "profile.yaml"), *args])
    return result, channel


def test_the_command_writes_into_app_support_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_app_support: Path
) -> None:
    result, channel = _backup(tmp_path, monkeypatch, _record(tmp_path))
    assert result.exit_code == 0, result.output
    written = list((isolated_app_support / "backup").glob("live-*.tar.gz"))
    assert len(written) == 1 and _restored(written[0]) == FILES and "ok   backup:" in result.output
    assert channel.sent == []


def test_a_failed_night_pages_and_exits_non_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, channel = _backup(tmp_path, monkeypatch, tmp_path / "gone", "--to", str(tmp_path / "backup"))
    assert result.exit_code == 1 and "backup failed" in result.output
    assert len(channel.sent) == 1 and channel.sent[0][0].startswith("北斗夜间备份失败：BackupRefused")
    assert channel.sent[0][1] == "live-backup"


def test_the_nightly_job_backs_up_first_and_a_failure_sets_its_exit_code(tmp_path: Path) -> None:
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    step = '"$BEIDOU" live backup || { echo "[$(stamp)] FAIL live backup"; fail=1; }'
    assert lines.index(step) < next(i for i, line in enumerate(lines) if line.startswith('"$BEIDOU" data sync'))

    stub = tmp_path / "beidou"
    stub.write_text('#!/bin/sh\necho "$*" >&2\nexit 1\n', encoding="utf-8")
    stub.chmod(0o755)
    program = "\n".join(
        ["stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }", "fail=0", f'BEIDOU="{stub}"', step, 'echo "fail=$fail"']
    )
    ran = subprocess.run([str(BASH), "-c", program], capture_output=True, text=True, timeout=30, check=False)
    assert "FAIL live backup" in ran.stdout and "fail=1" in ran.stdout and "live backup" in ran.stderr
