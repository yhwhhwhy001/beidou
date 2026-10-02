"""The hourly check pages a daily-report ALERT once, not twice (operator ruling 2026-10-02).

`deploy/run_check.sh` runs `report daily --check` every hour.  The report pages its own ALERT and then exited 1,
and the check paged the same content again under `check-report`: two pages an hour from 2026-09-17, about 610
by 09-30 (the 09-30 system audit, S2).  Its D2 option a: the report says when its page is out - exit 3,
`REPORT_PAGED`, sent now or inside the window - and the check logs that and does not page.  A page that did
not go out still exits 1, as does a crash, and the check pages those as before: it is the fallback.

Two layers: the command's exit code with the channel replaced, and the script's own `check_report` and
`notify`, cut out verbatim and run in bash against a stub `beidou` and a local webhook.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner, Result

from beidou_cli import main
from beidou_cli.live_cmd import REPORT_PAGED
from beidou_shared.config import load_yaml
from tests.cli.test_the_daily_report_closes_the_day_its_newest_bar_belongs_to import BAR_23, HOUR, _cycle, _store
from tests.live.test_the_membership_page_is_once_a_day import _LocalServer, _Webhook

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "run_check.sh"
PAGE = "风险预算告警：滑点（主书）9.0 bps 高于假设的 4 bps"


class _Channel:
    """`WebhookAlerts` as `report daily` uses it, without a network.  Class attributes set per test."""

    sent: list[str]
    delivers: bool
    recent: bool

    def __init__(self, url: str, *, secondary_url: str = "", state_path: Any = None) -> None:
        self.enabled = bool(url or secondary_url)

    def recently_sent(self, text: str, *, key: str | None = None) -> bool:
        return type(self).recent

    async def send(self, text: str, *, key: str | None = None, force: bool = False) -> bool:
        type(self).sent.append(text)
        return type(self).delivers


@pytest.fixture
def channel(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Channel]]:
    fake = type("Channel", (_Channel,), {"sent": [], "delivers": True, "recent": False})
    monkeypatch.setattr("beidou_cli.live_cmd.WebhookAlerts", fake)
    monkeypatch.setattr("beidou_live.reports.daily_alerts", lambda data: ([PAGE], []))
    yield fake


def _report(tmp_path: Path) -> Result:
    _store(tmp_path, [_cycle(BAR_23 - HOUR), _cycle(BAR_23)])
    profile = load_yaml(ROOT / "config/live.demo.yaml")
    profile["paths"] = {"state_dir": str(tmp_path / "live"), "reports_dir": str(tmp_path / "reports")}
    profile["alerts"] = {"webhook_url": "https://hooks.example/beidou"}  # the channel above, never the network
    profile["costs"] = str(ROOT / "config/costs.yaml")
    profile["registry"] = str(ROOT / "config/alpha_registry.yaml")
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    (tmp_path / "archive" / "klines").mkdir(parents=True, exist_ok=True)
    command = ["report", "daily", "--profile", str(tmp_path / "profile.yaml"), "--out", str(tmp_path / "out")]
    return CliRunner().invoke(main, [*command, "--data-root", str(tmp_path / "archive"), "--check"])


def test_a_page_the_report_sent_exits_3(tmp_path: Path, channel: type[_Channel]) -> None:
    result = _report(tmp_path)
    assert result.exit_code == REPORT_PAGED == 3, result.output
    assert len(channel.sent) == 1 and PAGE in channel.sent[0]


def test_a_page_already_out_inside_the_window_exits_3_and_is_not_sent_again(
    tmp_path: Path, channel: type[_Channel]
) -> None:
    channel.recent = True
    result = _report(tmp_path)
    assert result.exit_code == REPORT_PAGED, result.output
    assert channel.sent == []
    assert "没有送达" not in result.output, "a duplicate inside the window is delivered, not lost"


def test_a_page_that_did_not_go_out_exits_1_so_the_check_pages_it(tmp_path: Path, channel: type[_Channel]) -> None:
    channel.delivers = False
    result = _report(tmp_path)
    assert result.exit_code == 1, result.output
    assert "上面这条告警没有送达任何通道" in result.output


def test_no_alert_exits_0(tmp_path: Path, channel: type[_Channel], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("beidou_live.reports.daily_alerts", lambda data: ([], ["只是提示"]))
    result = _report(tmp_path)
    assert result.exit_code == 0, result.output
    assert channel.sent == []


@pytest.fixture
def webhook() -> Iterator[tuple[str, list[str]]]:
    """A local webhook that records every page, as `test_the_membership_page_is_once_a_day` runs one."""
    handler = type("Webhook", (_Webhook,), {"received": []})
    server = _LocalServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/hook", handler.received
    finally:
        server.shutdown()
        server.server_close()


def _function(name: str) -> str:
    found = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", SCRIPT.read_text(encoding="utf-8"), flags=re.S | re.M)
    assert found, f"run_check.sh no longer defines {name}() the way this test reads it"
    return found.group(0)


def _run_check_report(tmp_path: Path, url: str, code: int) -> subprocess.CompletedProcess[str]:
    """The script's `check_report` and `notify`, with `report daily` replaced by a stub that exits `code`."""
    repo, support = tmp_path / "repo", tmp_path / "support"
    (repo / ".venv" / "bin").mkdir(parents=True)
    support.mkdir()
    python = repo / ".venv" / "bin" / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    beidou = repo / ".venv" / "bin" / "beidou"
    beidou.write_text(f'#!/bin/sh\necho "北斗日报 2026-10-01：{PAGE}"\nexit {code}\n', encoding="utf-8")
    for stub in (python, beidou):
        stub.chmod(0o755)
    env = {
        key: value for key, value in os.environ.items() if key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
    }
    env = {key: value for key, value in env.items() if not key.startswith("BEIDOU_ALERTS_WEBHOOK_URL")}
    env.update(
        REPO=str(repo),
        SUPPORT=str(support),
        BEIDOU_ALERTS_WEBHOOK_URL=url,
        PYTHONPATH=str(ROOT),
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
    )
    program = "\n".join(
        [
            "stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }",
            "failed=0",
            _function("notify"),
            _function("check_report"),
            "check_report",
            'echo "failed=$failed"',
        ]
    )
    return subprocess.run(["bash", "-c", program], env=env, capture_output=True, text=True, timeout=120, check=False)


def test_the_check_logs_a_page_the_report_sent_and_does_not_send_it_again(
    tmp_path: Path, webhook: tuple[str, list[str]]
) -> None:
    url, received = webhook
    result = _run_check_report(tmp_path, url, REPORT_PAGED)
    assert result.returncode == 0, result.stderr
    assert received == []
    assert "FAIL report (the report paged this itself)" in result.stdout and "failed=1" in result.stdout


def test_the_check_pages_what_the_report_could_not(tmp_path: Path, webhook: tuple[str, list[str]]) -> None:
    url, received = webhook
    result = _run_check_report(tmp_path, url, 1)
    assert result.returncode == 0, result.stderr
    assert received == [f"北斗巡检失败（report）：北斗日报 2026-10-01：{PAGE} "], "the fallback, as before"
    assert "failed=1" in result.stdout


def test_a_clean_report_is_ok_and_pages_nothing(tmp_path: Path, webhook: tuple[str, list[str]]) -> None:
    url, received = webhook
    result = _run_check_report(tmp_path, url, 0)
    assert result.returncode == 0, result.stderr
    assert received == [] and "ok   report" in result.stdout and "failed=0" in result.stdout


def test_the_script_runs_the_function_it_defines() -> None:
    lines = [line.rstrip() for line in SCRIPT.read_text(encoding="utf-8").splitlines()]
    assert "check_report" in lines, "defining check_report is not running it"
    assert sum("report daily --check" in line for line in lines if not line.lstrip().startswith("#")) == 1
