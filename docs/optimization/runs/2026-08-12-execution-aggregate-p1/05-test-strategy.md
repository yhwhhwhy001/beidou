# Test strategy

| Requirement | Layer | Controlled evidence |
|---|---|---|
| P1-EXEC-001/002 | unit + engine contract | SQLite restart and multi-slice ACK/UNKNOWN tests |
| P1-EXEC-003/005 | state-machine/property | duplicate, terminal regression, partial-fill and replay permutations |
| P1-EXEC-004 | unit + integration | long/short/flat/reversal/current/pending delta matrix |
| P1-EXEC-006 | migration + PostgreSQL adapter contract | fake transactional cursor plus forward-only SQL assertions |
| P1-EXEC-007 | user-event contract | duplicate event, partial fill, terminal and gap fixtures |
| P1-EXEC-008 | repository gates | focused pytest, full pytest, mypy, critical Ruff, format and diff checks |

Real PostgreSQL, WebSocket and venue behavior remain NOT_VERIFIABLE without separately authorized environment access and side effects.
