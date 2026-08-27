# Rollback — neutral boundaries

The legacy composition path must remain available behind its existing explicit entrypoint during this slice; no runtime cutover is authorized.

1. Disable only the new Alpha composition route through its explicit composition selection.
2. Re-run the pre-task characterization suite and prove existing behavior is restored without removing neutral evidence artifacts.
3. Verify no schema/data migration or external state was introduced.
4. Preserve the forbidden-edge report and rollback transcript.

Rollback succeeds only when the old explicit path behaves as before, offline Alpha changes are no longer selected, and the original accepted baseline can be reconstructed without reset/clean. A hidden fallback or changed runtime default is failure.
