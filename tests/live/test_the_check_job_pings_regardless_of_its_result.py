"""The hourly check pings the off-host dead-man whatever its checks found (WP-R1, D-P4 reopened 2026-09-28).

The ping says THE CHECK JOB IS ALIVE, not that everything passed: results still leave through `notify`
and the exit code, and on 2026-09-28 this job carried a report FAIL every hour.  A ping gated on
`$failed` would have the service report the check as dead for as long as any check fails, and teach the
operator to ignore the one page that says the host itself is gone.

Two halves, because each misses what the other sees.  The text half pins the shape: the ping is the last
statement before `exit "$failed"`, at top level, and nothing before it can leave the script early.  The
stub half runs the real script under `/bin/bash` - 3.2 on the Mac, where launchd runs it - once with
every `beidou` call passing and once with every one failing, and counts what a local server receives.
"""

from __future__ import annotations

import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "deploy" / "run_check.sh"
PING_OPENS = 'if [ -n "${BEIDOU_DEADMAN_CHECK_URL:-}" ]; then'
TOKEN = "check-token-that-must-stay-off-argv"


def _code(lines: list[str]) -> list[str]:
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def test_the_ping_is_the_last_statement_before_the_exit_and_behind_no_result() -> None:
    lines = CHECK.read_text(encoding="utf-8").splitlines()
    assert _code(lines)[-1] == 'exit "$failed"', "run_check.sh no longer ends by exiting with its checks' result"
    # Exact and at column 0: not indented under a branch, and not behind a `[ "$failed" … ] &&` guard.
    assert PING_OPENS in lines, "the dead-man ping is gone, or no longer opens the way this test reads it"
    opens = lines.index(PING_OPENS)
    closes = lines.index("fi", opens)
    assert not [line for line in lines[opens : closes + 1] if re.search(r"\$\{?failed\b", line)], "it reads the result"
    # Only `exit "$failed"` follows, and it is the script's last statement, so no `if`, loop or function
    # can enclose the ping: whatever opened it would have to close after the exit.
    assert _code(lines[closes + 1 :]) == ['exit "$failed"']
    # And nothing before it leaves early.  `cd "$REPO" || exit 78` is the one exit allowed: a job that
    # cannot reach its repo cannot check anything, and its silence is what should page.  `set -e` would
    # turn any failing command into another way out - the failure mode FM-PR1 named.
    before = _code(lines[:opens])
    assert [line for line in before if re.search(r"(^|[\s;&|])exit(\s|$)", line)] == ['cd "$REPO" || exit 78']
    assert not [line for line in before if re.match(r"\s*set\s+-[a-z]*e", line)], "errexit would skip the ping"


class _Service(BaseHTTPRequestHandler):
    hits: list[str]

    def do_GET(self) -> None:
        type(self).hits.append(self.path)
        self.send_response(404 if "unknown" in self.path else 200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class _LocalServer(HTTPServer):
    def server_bind(self) -> None:
        """`HTTPServer.server_bind` calls `socket.getfqdn`, a reverse lookup measured at 35 s on the Mac."""
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", int(self.server_address[1])


@pytest.fixture
def service() -> Iterator[tuple[str, list[str]]]:
    handler = type("Service", (_Service,), {"hits": []})
    server = _LocalServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler.hits
    finally:
        server.shutdown()
        server.server_close()


def _run(tmp_path: Path, *, beidou_exit: int, url: str | None) -> tuple[subprocess.CompletedProcess[str], str]:
    """The real run_check.sh in a fake repo: every `beidou` call exits `beidou_exit`, curl logs its argv.

    HOME is a scratch directory, so the script finds no `~/.zshrc` to eval and no env.sh to source: the
    only channel it can reach is the one handed to it here.
    """
    run = tmp_path / f"run-{beidou_exit}-{'url' if url else 'none'}-{len(list(tmp_path.iterdir()))}"
    repo, home, fake_bin = run / "repo", run / "home", run / "bin"
    for directory in (repo / "deploy", repo / ".venv" / "bin", home, fake_bin):
        directory.mkdir(parents=True)
    shutil.copy(CHECK, repo / "deploy" / "run_check.sh")
    stubs = {
        repo / ".venv" / "bin" / "beidou": f'#!/bin/sh\necho "stub beidou $*"\nexit {beidou_exit}\n',
        repo / ".venv" / "bin" / "python": f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
    }
    real_curl = shutil.which("curl")
    assert real_curl, "curl is what the job pings with"
    argv_log = run / "curl-argv.log"
    stubs[fake_bin / "curl"] = f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{argv_log}"\nexec "{real_curl}" "$@"\n'
    for path, text in stubs.items():
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    proxies = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    env = {key: value for key, value in os.environ.items() if key.lower() not in proxies}
    env.update(HOME=str(home), PATH=f"{fake_bin}{os.pathsep}{env.get('PATH', '')}", NO_PROXY="127.0.0.1")
    if url:
        env["BEIDOU_DEADMAN_CHECK_URL"] = url
    result = subprocess.run(
        ["/bin/bash", str(repo / "deploy" / "run_check.sh")],
        env=env,
        cwd=run,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result, argv_log.read_text(encoding="utf-8") if argv_log.exists() else ""


@pytest.mark.parametrize("beidou_exit", [0, 1], ids=["checks-pass", "checks-fail"])
def test_the_check_job_pings_regardless_of_its_result(
    tmp_path: Path, service: tuple[str, list[str]], beidou_exit: int
) -> None:
    base, hits = service
    url = f"{base}/ping/{TOKEN}"

    result, argv = _run(tmp_path, beidou_exit=beidou_exit, url=url)

    assert result.returncode == beidou_exit, (
        f"the ping must not change the job's result:\n{result.stdout}{result.stderr}"
    )
    assert hits == [f"/ping/{TOKEN}"], f"one ping per run, whatever the checks said; got {hits}"
    if beidou_exit:
        assert "FAIL status" in result.stdout, "precondition: this run really took the failure branch"
    assert argv, "precondition: the logging curl stub was the curl that ran"
    assert TOKEN not in argv, "the URL was on curl's argv, where every user on the machine reads it through `ps`"
    assert TOKEN not in result.stdout + result.stderr


def test_no_url_means_no_ping_and_no_curl(tmp_path: Path, service: tuple[str, list[str]]) -> None:
    _, hits = service

    result, argv = _run(tmp_path, beidou_exit=0, url=None)

    assert result.returncode == 0, result.stdout + result.stderr
    assert hits == [] and argv == ""


def test_a_refused_ping_is_logged_without_its_url_and_changes_nothing(
    tmp_path: Path, service: tuple[str, list[str]]
) -> None:
    """healthchecks.io answers 404 for a check it does not know - a mistyped export, say."""
    base, hits = service
    url = f"{base}/unknown/{TOKEN}"

    result, _ = _run(tmp_path, beidou_exit=0, url=url)

    assert result.returncode == 0, result.stdout + result.stderr
    assert hits == [f"/unknown/{TOKEN}"]
    assert "dead-man ping failed" in result.stdout
    assert TOKEN not in result.stdout + result.stderr
