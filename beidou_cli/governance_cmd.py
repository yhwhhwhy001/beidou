"""``beidou governance`` - Phase 0 is read-only: replay the rules, write nothing.

The git reading lives here rather than in `beidou_governance` deliberately.  What the registry has
cited, and when, is a fact about this checkout's history; keeping it out of the package is what lets
the replay be tested against fixtures instead of against whatever `git log` says today, and it keeps
the package free of `subprocess`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import click

from beidou_alpha.registry import parse_registry
from beidou_cli import main
from beidou_governance.policy import Policy
from beidou_governance.promote import apply as apply_transaction
from beidou_governance.promote import closed, read_log
from beidou_governance.promote import plan as plan_transaction
from beidou_governance.replay import load_jsonl, render, replay_adoptions, replay_live
from beidou_governance.state import read as read_state
from beidou_live.config import registry_evidence_problems
from beidou_live.health import CONSTRUCTION_ALIASES
from beidou_shared.config import load_yaml

REGISTRY = "config/alpha_registry.yaml"
#: The autonomy switch.  One of the three human confirmation points the plan keeps (§0): single
#: promotions are the machine's from the first transaction (Q9), but whether the machine may write at
#: all is a person's decision, taken once, and it is OFF until somebody takes it.
SWITCH = "governance/ENABLED"
TRANSACTIONS = "governance/transactions.jsonl"


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


def _switch(root: Path) -> Path:
    return root / SWITCH


@governance.command("status")
@click.option("--root", default=".", help="Checkout to read state and transactions from.")
def status_cmd(root: str) -> None:
    """What rules this process would run under, and whether it is allowed to write."""
    checkout = Path(root).resolve()
    policy = Policy()
    book = read_state(checkout / "governance" / "governance_state.json")
    enabled = _switch(checkout).exists()
    click.echo(f"policy   {policy.version} digest={policy.digest()} window={policy.window_days}d")
    click.echo(f"autonomy {'ENABLED' if enabled else 'DISABLED'}  ({_switch(checkout)})")
    click.echo(
        f"window   {book.window}  promotions {book.promotions_this_window}/{policy.max_queued_to_probe_per_window}"
    )
    click.echo(
        f"probes   {len(book.probes)}/{policy.max_concurrent_probes}  "
        f"fraction {book.probe_fraction:.3f}/{policy.probe_budget_share:.3f}"
        + (f"  FROZEN until window {book.frozen_until_window}" if book.frozen() else "")
    )
    for name, candidate in sorted(book.candidates.items()):
        click.echo(
            f"  {name:40s} {candidate.state.value:10s} entries={candidate.probe_entries} "
            f"windows={candidate.windows_survived}/{policy.windows_to_main}"
        )
    log = read_log(checkout / TRANSACTIONS)
    click.echo(f"transactions {len(log)} rows, chain {'closed' if closed(log) else 'BROKEN'}")


@governance.command("transactions")
@click.option("--root", default=".", help="Checkout to read the transaction log from.")
def transactions_cmd(root: str) -> None:
    """Every attempt to change the registry, whatever happened to it."""
    log = read_log(Path(root).resolve() / TRANSACTIONS)
    if not log:
        click.echo("no transactions")
        return
    for transaction in log:
        click.echo(
            f"{transaction.at}  {transaction.action:8s} actor={transaction.actor:8s} "
            f"{transaction.before_digest} -> {transaction.after_digest}  {transaction.candidate}"
            + (f"  {'; '.join(transaction.reasons)}" if transaction.reasons else "")
        )
    if not closed(log):
        raise SystemExit("the transaction chain is broken: something changed the registry outside a transaction")


def _gate(profile_path: str) -> Callable[[Path], list[str]]:
    profile = load_yaml(profile_path)

    def gate(path: Path) -> list[str]:
        registry = parse_registry(load_yaml(str(path)))
        return registry_evidence_problems(registry, profile)

    return gate


@governance.command("plan")
@click.option("--proposed", required=True, help="Path to the candidate registry YAML.")
@click.option("--registry", "registry_path", default=REGISTRY, show_default=True)
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--candidate", default="", help="Which candidate this change is for; recorded in the log.")
def plan_cmd(proposed: str, registry_path: str, profile: str, candidate: str) -> None:
    """What `apply` would do, asked of the same gate, without touching the file."""
    transaction = plan_transaction(
        Path(registry_path),
        Path(proposed).read_text(encoding="utf-8"),
        gate=_gate(profile),
        candidate=candidate or Path(proposed).name,
    )
    click.echo(json.dumps(json.loads(transaction.to_json()), indent=2, ensure_ascii=False))
    if transaction.reasons:
        raise SystemExit(1)


@governance.command("enable")
@click.option("--root", default=".", help="Checkout to write the switch into.")
@click.confirmation_option(prompt="Allow the machine to write config/alpha_registry.yaml on its own?")
def enable_cmd(root: str) -> None:
    """Turn on autonomous registry writes.  A human confirmation point, taken once (§0)."""
    path = _switch(Path(root).resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"enabled {datetime.now(UTC).isoformat()}\n", encoding="utf-8")
    click.echo(f"autonomy ENABLED ({path})")


@governance.command("disable")
@click.option("--root", default=".", help="Checkout to remove the switch from.")
def disable_cmd(root: str) -> None:
    """Turn off autonomous registry writes.  Never confirmed: stopping is always allowed."""
    path = _switch(Path(root).resolve())
    path.unlink(missing_ok=True)
    click.echo(f"autonomy DISABLED ({path})")


@governance.command("apply")
@click.option("--proposed", required=True, help="Path to the candidate registry YAML.")
@click.option("--registry", "registry_path", default=REGISTRY, show_default=True)
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--candidate", default="", help="Which candidate this change is for; recorded in the log.")
@click.option("--root", default=".", help="Checkout holding the switch and the transaction log.")
def apply_cmd(proposed: str, registry_path: str, profile: str, candidate: str, root: str) -> None:
    """Write the registry as a transaction, rolling back if the startup gate refuses.

    Refused while the autonomy switch is off.  Q9 removed the confirmation point from a SINGLE
    promotion, not from the decision to let the machine write at all - that one is taken once, by a
    person, and `governance disable` takes it back without confirmation because stopping always may.
    """
    checkout = Path(root).resolve()
    if not _switch(checkout).exists():
        raise click.ClickException(
            f"autonomy is disabled ({_switch(checkout)} does not exist).  `beidou governance enable` "
            "turns it on; `governance plan` shows what this would do without it."
        )
    transaction = apply_transaction(
        Path(registry_path),
        Path(proposed).read_text(encoding="utf-8"),
        gate=_gate(profile),
        log_path=checkout / TRANSACTIONS,
        candidate=candidate or Path(proposed).name,
    )
    click.echo(json.dumps(json.loads(transaction.to_json()), indent=2, ensure_ascii=False))
    if transaction.action == "ROLLBACK":
        raise SystemExit(1)
    if transaction.restart_required:
        click.echo("restart required: the engine builds its model at startup (KILL-Q15)", err=True)
