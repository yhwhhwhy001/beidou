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
from typing import Any

import click
import yaml

from beidou_alpha.registry import parse_registry
from beidou_alpha.validation.ledger import resolve_ledger_path
from beidou_cli import main
from beidou_governance.admission import Admission, admit
from beidou_governance.canary import evaluate as evaluate_canary
from beidou_governance.family_gate import failures as gate_failures
from beidou_governance.family_gate import recheck as recheck_gate
from beidou_governance.lifecycle import State
from beidou_governance.policy import Policy
from beidou_governance.promote import apply as apply_transaction
from beidou_governance.promote import closed, read_log
from beidou_governance.promote import plan as plan_transaction
from beidou_governance.replay import load_jsonl, render, replay_adoptions, replay_live
from beidou_governance.state import read as read_state
from beidou_governance.tenure import books_in, tenure
from beidou_live.config import registry_evidence_problems
from beidou_live.health import CONSTRUCTION_ALIASES
from beidou_shared.config import load_yaml

REGISTRY = "config/alpha_registry.yaml"
#: The autonomy switch.  One of the three human confirmation points the plan keeps (§0): single
#: promotions are the machine's from the first transaction (Q9), but whether the machine may write at
#: all is a person's decision, taken once, and it is OFF until somebody takes it.
SWITCH = "governance/ENABLED"
TRANSACTIONS = "governance/transactions.jsonl"
#: The lifecycle state R3/R4/R5/R7 are counted in.  Read by `status`, `tenure` and, since 2026-09-09,
#: by the admission gate `plan`/`apply` run before a write.
STATE = "governance/governance_state.json"


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
    book = read_state(checkout / STATE)
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


@governance.command("tenure")
@click.option("--root", default=".", help="Checkout to read governance state from.")
@click.option("--cycles", default=".beidou/live/cycles.jsonl", show_default=True, help="The append-only live record.")
@click.option(
    "--anchor",
    default="2026-09-03T00:00:00+00:00",
    show_default=True,
    help="When the batch calendar starts.  Global: every sleeve's windows are counted off this one clock.",
)
@click.option("--started", help="ISO start for one sleeve, as book=ISO; repeatable.", multiple=True)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable, for the report rather than the terminal.")
def tenure_cmd(root: str, cycles: str, anchor: str, started: tuple[str, ...], as_json: bool) -> None:
    """DL-G6': what the live record implies about the time rule.  Reports; promotes nothing.

    Split from `apply` for the same reason `plan` is: a probe reaching main is a registry transaction
    and a restart, and the operator should be able to read what the record says before either.
    """
    path = Path(cycles)
    if not path.exists():
        raise click.ClickException(f"no live record at {path}")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    policy = Policy()
    book = read_state(Path(root).resolve() / STATE)
    starts = dict(pair.split("=", 1) for pair in started if "=" in pair)

    out = []
    for name in books_in(rows) or tuple(book.candidates):
        # The sleeve's own start, falling back to the calendar anchor: KILL-AR-06 grandfathered both
        # running sleeves to the registry's effective date, which is that same day.
        result = tenure(rows, book=name, started_at=starts.get(name, anchor), window_anchor=anchor, policy=policy)
        out.append(result)

    if as_json:
        click.echo(json.dumps([r.as_dict() for r in out], indent=2, sort_keys=True, ensure_ascii=False))
        return
    if not out:
        click.echo("the record names no probe; nothing to derive")
        return
    for result in out:
        # by STRATEGY, not by book: the record's probe rows are keyed on the book ("flow_short")
        # and `governance_state.json` on the registry entry id ("flow").  Keying on the book meant
        # this annotation silently never printed.
        state = book.candidates.get(result.strategy) or book.candidates.get(result.book)
        held = f" (state holds {state.windows_survived})" if state is not None else ""
        click.echo(
            f"{result.book:20s} windows {result.windows_survived}/{policy.windows_to_main}{held}  "
            f"cycles {result.cycles_read}" + (f"  STOPPED {result.stopped_at}" if result.stopped_at else "")
        )
        for event in result.events:
            click.echo(f"    {event.at}  {event.event.value:15s} {event.why}")
        for skip in result.skipped:
            click.echo(f"    {skip.at}  {'(not counted)':15s} {skip.why}")
        if result.windows_survived >= policy.windows_to_main and not result.stopped_at:
            click.echo("    -> the record supports probe -> main; that is a transaction and a restart")
    # By BOOK and by STRATEGY, because the two namespaces meet here: `books_in` reads the record's probe
    # rows (keyed on the book, "main") and `governance_state.json` keys on the registry entry id
    # ("tsmom").  Comparing one against the other is the same mismatch this file already fixed once a
    # few lines above, and it left the note below claiming main -> probe was unreachable for hours
    # AFTER the main book got a stop and started appearing in the record.
    named = set(books_in(rows)) | {result.strategy for result in out if result.strategy}
    silent = sorted(n for n, c in book.candidates.items() if c.state is State.MAIN and n not in named)
    if silent:
        # Only true while the main sleeve carries no stop: `probes_from_registry` includes MAIN_BOOK
        # only when its entry declares one (2026-09-09), so a main book without a stop reports nothing
        # and the main -> probe edge stays unreachable from the record - the same way probe -> main was
        # before this module existed.
        click.echo(
            f"note: {', '.join(silent)} {'sits' if len(silent) == 1 else 'sit'} in main with no stop in "
            "the registry, so the record reports nothing for it and main -> probe cannot fire from it"
        )


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


def _rows(path: Path) -> list[dict[str, Any]]:
    return load_jsonl(path.read_text(encoding="utf-8")) if path.exists() else []


def _admission(registry_path: str, proposed: str, root: Path, state_dir: str, shadow_dir: str) -> Admission:
    """R3/R4/R5/R7 and K-EX14, asked of the change before the bytes move.

    Added 2026-09-09.  Until then `apply` asked the startup gate and nothing else, so every constraint
    §3 lists as a precondition of `queued -> probe` was decorative on the only path that promotes:
    a proposal moving 0.9 of the book to an unproven sleeve was accepted against a cap of 1/3.
    """
    return admit(
        parse_registry(load_yaml(registry_path)),
        parse_registry(yaml.safe_load(proposed)),
        book=read_state(root / STATE),
        policy=Policy(),
        cycles=_rows(Path(state_dir) / "cycles.jsonl"),
        shadow=_rows(Path(shadow_dir) / "cycles.jsonl"),
        # K-EX14's clock reads the canonical construction, not the raw digest: three of the raw
        # changes since 09-04 altered no behaviour and were declared equivalent here on the read side.
        aliases=CONSTRUCTION_ALIASES,
    )


def _report_admission(admission: Admission) -> None:
    for key, value in sorted(admission.measured.items()):
        click.echo(f"  {key}: {value}", err=True)
    for reason in admission.reasons:
        click.echo(f"  REFUSED {reason}", err=True)


@governance.command("plan")
@click.option("--proposed", required=True, help="Path to the candidate registry YAML.")
@click.option("--registry", "registry_path", default=REGISTRY, show_default=True)
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--candidate", default="", help="Which candidate this change is for; recorded in the log.")
@click.option("--root", default=".", help="Checkout holding the governance state and transaction log.")
@click.option("--state-dir", default=".beidou/live", show_default=True, help="The armed loop's record.")
@click.option("--shadow-dir", default=".beidou/live-shadow", show_default=True, help="The canary's record.")
def plan_cmd(
    proposed: str, registry_path: str, profile: str, candidate: str, root: str, state_dir: str, shadow_dir: str
) -> None:
    """What `apply` would do, asked of the same gate, without touching the file."""
    admission = _admission(registry_path, Path(proposed).read_text(encoding="utf-8"), Path(root).resolve(), state_dir, shadow_dir)
    click.echo(f"admission: {'ALLOWED' if admission.allowed else 'REFUSED'} "
               f"(promoting: {', '.join(admission.promoting) or 'nothing'})", err=True)
    _report_admission(admission)
    transaction = plan_transaction(
        Path(registry_path),
        Path(proposed).read_text(encoding="utf-8"),
        gate=_gate(profile),
        candidate=candidate or Path(proposed).name,
    )
    click.echo(json.dumps(json.loads(transaction.to_json()), indent=2, ensure_ascii=False))
    if transaction.reasons or not admission.allowed:
        raise SystemExit(1)


@governance.command("gate")
@click.option("--registry", "registry_path", default=REGISTRY, show_default=True)
@click.option("--root", default=".", help="Checkout to read the trials ledger and reports from.")
@click.option("--check", is_flag=True, help="Exit non-zero if any running strategy fails at today's N.")
def gate_cmd(registry_path: str, root: str, check: bool) -> None:
    """R0 recomputed at today's bucket size - §3's third condition on `probe -> main`.

    The gate is `max_sharpe_quantile(N, variance, alpha)` and N is the strategy's ledger bucket, which
    is append-only.  So the threshold a strategy was adopted against is not the one it faces today, and
    §3 says a probe may only reach main if it still clears the recomputed one.  That condition had no
    branch in the state machine and no producer anywhere until 2026-09-09.

    Everything except N is held at the values the evidence report recorded, including the annualisation
    scale, which is backed out of the report's own threshold/quantile pair.  Any movement here is
    therefore attributable to the denominator and to nothing else - which is the honest form of
    "searching more retires your own incumbents".

    Read-only.  A FAIL is an event for the state machine (`FAMILY_GATE_FAILED` -> retired), and nothing
    on this path retires anything: `lifecycle.apply` still has no production caller, so the operator
    sees the reading and decides.
    """
    checkout = Path(root).resolve()
    registry = parse_registry(load_yaml(registry_path))
    ledger = resolve_ledger_path(root=checkout)
    lines = ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else []

    def read_report(path: str) -> dict[str, Any]:
        return json.loads((checkout / path).read_text(encoding="utf-8"))

    readings = recheck_gate(registry, read_report, lines)
    for reading in readings:
        click.echo(f"{reading.status:10s} {reading.strategy:22s} {reading.why}")
    failed = gate_failures(readings)
    unreadable = [r for r in readings if r.status == "UNREADABLE"]
    click.echo(
        f"{len(readings)} strategies: {len(readings) - len(failed) - len(unreadable)} pass, "
        f"{len(failed)} fail, {len(unreadable)} unreadable (ledger {ledger})"
    )
    if check and failed:
        raise SystemExit(1)


@governance.command("canary")
@click.option("--shadow-dir", default=".beidou/live-shadow", show_default=True, help="The soak's state directory.")
@click.option("--state-dir", default=".beidou/live", show_default=True, help="The armed loop, as the baseline.")
@click.option("--gate-refusals", default=0, show_default=True, help="Startup-gate refusals the soak itself saw.")
def canary_cmd(shadow_dir: str, state_dir: str, gate_refusals: int) -> None:
    """L4 / DL-G5: score a finished shadow soak against the armed loop over the same window.

    The soak is `deploy/run_shadow.sh`; this is the half that reads it.  Until 2026-09-09 there was no
    such half: the module computing the checks had no caller outside its own tests, so §3's
    `Canary 健康检查过` was a precondition nothing could ever satisfy or refuse.

    Deployment health only (KILL-AR-04).  A candidate that passes has been shown to deploy, not to
    have edge; a candidate that fails has hit a wiring or venue problem, and reading that as evidence
    against the sleeve is the mistake this command's own docstring exists to prevent.
    """
    shadow = _rows(Path(shadow_dir) / "cycles.jsonl")
    baseline = _rows(Path(state_dir) / "cycles.jsonl")
    if not shadow:
        raise click.ClickException(f"no shadow record at {shadow_dir}/cycles.jsonl; run deploy/run_shadow.sh first")
    result = evaluate_canary(shadow, baseline, gate_refusals=gate_refusals, aliases=CONSTRUCTION_ALIASES)
    for check in result.checks:
        click.echo(f"{'PASS' if check.passed else 'FAIL'}  {check.name:22s} {check.detail}")
    click.echo(f"{'HEALTHY' if result.healthy else 'UNHEALTHY'}  {result.soaked} cycles soaked")
    if not result.healthy:
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
@click.option("--state-dir", default=".beidou/live", show_default=True, help="The armed loop's record.")
@click.option("--shadow-dir", default=".beidou/live-shadow", show_default=True, help="The canary's record.")
@click.option("--actor", default="machine", show_default=True, help="Who initiated this; recorded in the log.")
def apply_cmd(
    proposed: str,
    registry_path: str,
    profile: str,
    candidate: str,
    root: str,
    state_dir: str,
    shadow_dir: str,
    actor: str,
) -> None:
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
    text = Path(proposed).read_text(encoding="utf-8")
    admission = _admission(registry_path, text, checkout, state_dir, shadow_dir)
    if not admission.allowed:
        _report_admission(admission)
        raise click.ClickException(
            "the governance rules refuse this change; `governance plan` shows the same reasons without "
            "writing.  Loosening a rule to admit a change is R10's whole subject - the thresholds live "
            "in policy.py and move with a version, a test and a pre-registration."
        )
    transaction = apply_transaction(
        Path(registry_path),
        text,
        gate=_gate(profile),
        log_path=checkout / TRANSACTIONS,
        candidate=candidate or Path(proposed).name,
        actor=actor,
    )
    click.echo(json.dumps(json.loads(transaction.to_json()), indent=2, ensure_ascii=False))
    if transaction.action == "ROLLBACK":
        raise SystemExit(1)
    if transaction.restart_required:
        click.echo("restart required: the engine builds its model at startup (KILL-Q15)", err=True)
