"""The third general guard, and the one that would have caught the biggest defect of 2026-09-09.

Two guards already exist for this repository's recurring shape - a thing that looks like a control
and controls nothing.  `test_every_threshold_has_a_consumer` holds every `Policy` field to having a
production reader; `test_the_digest_sees_every_live_knob` holds every registry leaf to moving a digest.
Both passed all day while `governance apply` wrote the registry without asking a single governance
rule, because both ask their question one level too low.

`test_every_threshold_has_a_consumer` searches for the field's NAME in the production packages, and
`beidou_governance` is one of them.  So `max_concurrent_probes` had a reader - `lifecycle.evaluate`.
And `lifecycle.evaluate` was called by `replay` and by tests.  And `scheduler`, which was to have
driven it, had no caller at all.  The field-level hole was closed; the module-level one was not, and
that is where R1, R3, R4, R5, R7 and K-EX14 were all sitting.

So: every production module must be reachable from a CLI entry point by following imports.  Not
"someone imports it" - `budget` was imported, by `scheduler`, which nothing could reach.  Reachable
from the thing a person or a launchd job actually runs.

Exemptions are NAMED, never counted, and each name carries what it is waiting for.  A count would let
the next unreachable module in silently, which is the failure mode the whole file exists to prevent.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (
    "beidou_alpha",
    "beidou_cli",
    "beidou_data",
    "beidou_exchange",
    "beidou_governance",
    "beidou_live",
    "beidou_shared",
)

#: What `beidou` actually runs.  `__main__` registers the groups; the four command modules are the
#: leaves launchd and the operator reach through it.
ENTRY_POINTS = (
    "beidou_cli.__main__",
    "beidou_cli.data_cmd",
    "beidou_cli.governance_cmd",
    "beidou_cli.live_cmd",
    "beidou_cli.research_cmd",
)

#: Each name says what would make it reachable, so an exemption is an argument rather than a shrug.
EXEMPT: dict[str, str] = {
    "beidou_governance.scheduler": (
        "DL-G8 is delivered as a pure decision function with no assembler and no command: `governance` "
        "has no `next`, and `deploy/` has neither run_research.sh nor com.beidou.research.plist.  §9 "
        "records this as '研究机 plist 未做', which understates it - there is no way to run the "
        "scheduler at all, so AC-G8 ('连跑 3 轮') cannot be attempted.  Reachable the moment a command "
        "builds `scheduler.Context` from the ledger and the shortlist reports."
    ),
    "beidou_governance.budget": (
        "R1 lives here and its only importer is `scheduler`, so it inherits that module's unreachability.  "
        "Measured 2026-09-09: `research mine` and `research validate` consult no budget at all - the "
        "window's 181/1700 rows and 3/4 mine rounds are counted by nothing at the moment they are spent."
    ),
    "beidou_data.liquidations": (
        "#19 判定不可用 (2026-09-09): Binance publishes no USDⓈ-M liquidation history, and the only "
        "coin-margined archive stopped 23 months before the live period, so the parity obligation is "
        "unmeetable and RISK-G3 keeps the column out of live.  The module stays as the record of what "
        "was checked; a CLI for it would be a command that can only fail."
    ),
    "beidou_data.liquidation_archive": "The archive half of #19; same ruling, same reason.",
    "beidou_data.onchain": (
        "#31 delivered its contract and its verification (Coin Metrics, 49/528 symbols) but no ingest "
        "command: `beidou data` has metrics/pool/spot/status/sync and nothing for on-chain.  Reachable "
        "the moment `data_cmd` grows one, which is the honest read of §6 块 1 for this column."
    ),
    "beidou_data.index_price": (
        "#29 the same shape as #31: contract and offset verification landed, store/sync/CLI did not."
    ),
    "beidou_data.macro": (
        "#32, merged 2026-09-09 into the same shape as #31 and #29 - contract, verification and the "
        "revision ledger landed, no store and no `beidou data macro`.  Its author names the omission "
        "as a scope call rather than an oversight, which is what an exemption is for.  This guard went "
        "red on it the first time it ran after the merge, which is the guard working: an 844-line "
        "module no command can reach is exactly what it exists to make visible."
    ),
}


def _modules() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
            out[name.removesuffix(".__init__")] = path
    return out


def _edges(modules: dict[str, Path]) -> dict[str, set[str]]:
    """Import edges, including `from X import Y` where Y is itself a module.

    That last case is not a detail: `signals/__init__` imports its nine signal modules exactly that
    way, and a walker that records only `X` reports all nine as dead code.  A guard whose first run
    produces nine false positives is a guard nobody will keep.
    """
    edges: dict[str, set[str]] = collections.defaultdict(set)
    for name, path in modules.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a file that cannot parse fails elsewhere first
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                edges[name].add(node.module)
                edges[name].update(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                edges[name].update(alias.name for alias in node.names)
    return edges


def _reachable() -> set[str]:
    modules = _modules()
    edges = _edges(modules)
    seen: set[str] = set()
    stack = list(ENTRY_POINTS)
    while stack:
        current = stack.pop()
        if current in seen or current not in modules:
            continue
        seen.add(current)
        stack.extend(target for target in edges[current] if target in modules and target not in seen)
    return seen


def test_no_production_module_is_unreachable_from_a_command() -> None:
    modules = _modules()
    seen = _reachable()
    # A package `__init__` is a namespace shim: production imports the submodule directly, so the
    # shim can be unreached while everything under it runs.  Its cost is caught by the line budget.
    dead = sorted(
        name for name, path in modules.items() if name not in seen and name not in EXEMPT and path.name != "__init__.py"
    )
    assert not dead, (
        f"these modules cannot be reached from any `beidou` command: {dead}.  Give one an entry point, "
        "or name it in EXEMPT with what it is waiting for.  A module nothing can run is not a control."
    )


def test_the_exemptions_are_named_rather_than_counted() -> None:
    """Every exemption has to be argued for on its own, and has to still be unreachable to earn one."""
    modules, seen = _modules(), _reachable()
    assert all(reason.strip() for reason in EXEMPT.values())
    unknown = sorted(name for name in EXEMPT if name not in modules)
    assert not unknown, f"EXEMPT names modules that no longer exist: {unknown}"
    stale = sorted(name for name in EXEMPT if name in seen)
    assert not stale, (
        f"these are reachable now and no longer need an exemption: {stale}.  Delete the entry - an "
        "exemption list nobody prunes stops being a list of decisions."
    )


def test_the_guard_can_fail() -> None:
    """Its own negative control: the walk has to actually walk, or every module is trivially dead."""
    seen = _reachable()
    assert "beidou_governance.lifecycle" in seen, "the state machine is reached through governance_cmd"
    assert "beidou_governance.canary" in seen, "wired 2026-09-09 by `governance canary`"
    assert "beidou_alpha.signals.tsmom" in seen, "reached only via `from beidou_alpha.signals import tsmom`"
    assert "beidou_live.engine" in seen and "beidou_data.spot" in seen
