# BD-AF-P1-T02 — Safe CLI facade and offline isolation

## Goal

Make the root CLI side-effect free by default and expose runtime construction only through an explicit execution subcommand, while preserving an offline Alpha workflow.

## Requirements and dependencies

- Requirements: `AF-REQ-003`, `AF-REQ-004`, `AF-REQ-018`.
- Dependency: independently accepted `BD-AF-P1-T01`.
- Required approval: `H1_TASK_START_PER_TASK`.

## Allowed paths

- `beidou_cli/`
- the minimum `pyproject.toml` entrypoint lines
- explicit launcher adapter/composition-root boundary only
- `tests/cli/test_safe_root_cli.py`
- `tests/integration/test_alpha_offline_isolation.py`
- architecture/write-registry tests needed to prove zero side effects

## Invariants

- Bare `beidou` and `beidou --help` never construct a launcher, connect a socket, read credentials, write files/databases, or start a trading loop.
- Runtime is reachable only by explicit `beidou execution start`; presence of the command does not grant authority to run it.
- Offline research commands operate on explicitly bound local data when network and forbidden packages are denied.
- Existing unsafe behavior is not retained as an implicit fallback.

## First failing proof and sequence

1. Capture current bare-entrypoint side effects with process/network/file/write spies.
2. Add a safe facade and explicit composition selection.
3. Add isolated-install and denied-import/network tests.
4. Prove help/exit semantics and document compatibility changes.
5. Run entrypoint, architecture, registry, and affected regression oracles.

Any side effect from bare invocation, credential read, unknown command fallback, or zero-inventory scan is FAIL.
