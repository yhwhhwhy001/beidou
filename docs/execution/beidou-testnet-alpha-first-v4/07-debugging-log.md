# Incident remediation debugging log

| Symptom | Root cause | Evidence | Resolution |
|---|---|---|---|
| Verifier repeatedly reappeared after SIGTERM | launchd `KeepAlive=true` job at `db2debcf` | PID parent=launchd, launchctl label, repeated manifests | bootout + disable + repository launcher removal |
| Clean preflight coverage test failed | test read ignored local environment config | detached clean checkout failed; explicit DSN passed | set `DATABASE_URL` in test |
| Full suite appeared hung in engine runtime coverage | non-yielding `asyncio.sleep` doubles caused 60-second CPU busy wait | per-test durations: 60.22s before, 0.24s after | deterministic clock age and real zero-yield |
| No-write manifest claimed real Testnet write | manifest scanned all historical traces | regression with historical ACK returned true | current-runtime attempted/ACK/UNKNOWN flags |
| One FILLED trace remained unresolved while account was flat | later reduce-only close used a different trace identity | signed GET + all-orders + CLOSED close trace | strict startup linkage and append-only CLOSED snapshot |
| Legacy launcher no longer required G5 | V4 isolation requirement was applied to unrelated frozen path | diff of `4155567d` | restored fail-closed legacy preflight |

Unknowns remain fail-closed. No new campaign was used to diagnose or repair
these defects.
