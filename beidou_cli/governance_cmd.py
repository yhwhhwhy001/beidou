"""``beidou governance`` - Phase 0 is read-only: replay the rules, write nothing.

The git reading lives here rather than in `beidou_governance` deliberately.  What the registry has
cited, and when, is a fact about this checkout's history; keeping it out of the package is what lets
the replay be tested against fixtures instead of against whatever `git log` says today, and it keeps
the package free of `subprocess`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import click

from beidou_cli import main
from beidou_governance.replay import load_jsonl, render, replay_adoptions, replay_live
from beidou_live.health import CONSTRUCTION_ALIASES

REGISTRY = "config/alpha_registry.yaml"


@main.group()
def governance() -> None:
    """Autonomous promotion/demotion rules.  Phase 0: replay only."""


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    return result.stdout


def _adoptions(root: Path) -> tuple[dict[str, str], set[str]]:
    """Every report the registry has ever cited, the date it first cited it, and which were acknowledged.

    A REJECT pointer only counts as acknowledged when the registry version that INTRODUCED it carried
    `accepted_despite` (D-029).  Checking today's registry instead would backdate an acknowledgement
    onto pointers that never had one, which is the stale-pointer failure this project keeps finding.
    """
    log = _git(root, "log", "--format=%H|%ad", "--date=short", "-p", "--", REGISTRY)
    adoptions: dict[str, str] = {}
    introduced: list[tuple[str, str]] = []
    commit = date = ""
    for line in log.splitlines():
        head, sep, rest = line.partition("|")
        if sep and len(head) == 40:
            commit, date = head, rest
        elif line.startswith("+") and "report:" in line:
            path = line.split("report:", 1)[1].strip()
            if path not in adoptions:
                adoptions[path] = date
                introduced.append((path, commit))
    acknowledged = set()
    for path, commit in introduced:
        name = path.rsplit("/", 1)[-1]
        if not name.startswith("book-"):
            continue
        if "accepted_despite" in _git(root, "show", f"{commit}:{REGISTRY}"):
            acknowledged.add(name)
    return adoptions, acknowledged


def _reports(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted((root / "reports" / "research").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(payload, dict):
            out[path.relative_to(root).as_posix()] = payload
    return out


@governance.command("replay")
@click.option("--since", default="", help="Only replay adoptions and cycles on or after this date (YYYY-MM-DD).")
@click.option("--state-dir", default=".beidou/live", help="Live state directory to read cycles/attribution from.")
@click.option("--out", default="", help="Write the artefact here instead of stdout.")
@click.option("--root", default=".", help="Checkout to read reports and registry history from.")
def replay_cmd(since: str, state_dir: str, out: str, root: str) -> None:
    """Replay the governance rules against the record and attribute every difference (AC-G0)."""
    checkout = Path(root).resolve()
    adoptions, acknowledged = _adoptions(checkout)
    if since:
        adoptions = {path: when for path, when in adoptions.items() if when >= since}
    live = Path(state_dir)
    cycles = load_jsonl((live / "cycles.jsonl").read_text(encoding="utf-8")) if (live / "cycles.jsonl").exists() else []
    attribution = (
        load_jsonl((live / "attribution.jsonl").read_text(encoding="utf-8"))
        if (live / "attribution.jsonl").exists()
        else []
    )
    if since:
        cycles = [row for row in cycles if str(row.get("at", "")) >= since]
        attribution = [row for row in attribution if str(row.get("at", "")) >= since]
    # DL-G9: the evidence-side construction digests the loop has actually recorded.  A report can now
    # be checked against them by string equality, which is what makes KILL-AR-07 readable after the fact.
    seen = sorted({str(row["evidence_construction"]) for row in cycles if row.get("evidence_construction")})
    adopted = replay_adoptions(
        _reports(checkout), adoptions, acknowledged_rejects=sorted(acknowledged), live_constructions=seen
    )
    lived = replay_live(cycles, attribution, construction_aliases=CONSTRUCTION_ALIASES)
    text = render(adopted, lived)
    unattributed = len(adopted.unattributed) + len(lived.unattributed)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        click.echo(f"wrote {out}")
    else:
        click.echo(text)
    click.echo(
        f"AC-G0: {len(adopted.reproduced) + len(lived.reproduced)} reproduced, "
        f"{len(adopted.differences) + len(lived.differences)} differences, {unattributed} unattributed",
        err=True,
    )
    if unattributed:
        raise SystemExit(1)
