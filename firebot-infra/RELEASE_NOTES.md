# Release notes: firebot-infra 1.2.0 (2026-11-05)

## Tested environment (this is everything that was actually exercised)
- Python 3.12.3, Linux 6.18 x86_64 sandbox, 1 CPU, `cryptography` 50.0.2. No robot hardware, no real sensors, radios or batteries.
- Clean install into a fresh virtual environment, both modes:
  - core install: 276 tests pass (4 test files skipped because they need `cryptography`; the skip message says how to install it)
  - with `.[crypto]`: 307 tests pass, three consecutive full runs
  - `firebot` application (test workload): 40 tests pass
  - installed command `firebot-infra selftest`: 15 of 15 subsystem checks pass (KAVACH skipped without `cryptography`)
  - real-process fault-injection suite repeated 6 times: stable
- Not run: the CI workflow file (written, never executed), any Python other than 3.12, Windows/macOS.

## New in 1.2.0
- ANANTA analytics: SQLite reference store (idempotent batches, read-only query guard), dashboards for sensor dropouts, request latency,
  task durations, message problems, error rates, version-to-version regression checks; every table states units and UTC time range;
  ClickHouse table definition. SETU `trace_requests` and richer PRAVAH events feed it.
- DRISHTI opt-in flatline (stuck sensor) detection with a reason on every non-valid status.
- KAVACH signed SETU messages (identity, tamper, freshness, replay, revocation, role capability).
- Command line `firebot-infra` (selftest, run-info, diff-runs, decode, report, compare-versions, bench, prune-runs, schema).
- Benchmarks on synthetic data; data-policy enforcement (owner-only recordings, retention tools); as-built documents.
- Whole-system fault-injection tests using real process kills, a real disk-full condition and a real network peer restart.

## Bugs found by the new tests and fixed
- Disk full: a half-written record was left in the file. Effects were a failed DHARA write reappearing later, recorder events counted
  as written but unreadable, and a spool row lost behind a partial one. All three now cut the file back to the last whole record.
- AKSHARA now hands each record to the operating system immediately (previously up to one buffer could be lost on a process kill).
- `firebot-infra run-info` reported success for a mistyped folder; it now fails with a clear message.

## Known limitations and risks (read before relying on this)
1. No hardware testing and no hazard analysis. Passing these tests is not evidence of field safety. Emergency stop, motor control and
   suppression interlocks must stay in validated hardware / low-level control.
2. Power loss on real flash storage is not tested. Recordings are fsynced about once a second, so up to that window can be lost.
3. `ClickHouseHttpSink` and the ClickHouse table definition were only tested against a stub HTTP server, never a real ClickHouse server.
4. Signed messages prove who sent what and that it is fresh; they do not hide it. Confidentiality needs the TLS transport (TCP only).
5. The flatline check must be switched on per measurement and is wrong for legitimately constant or saturated signals.
6. SPANDA (transport), JALA (mesh) and KSHITI (kernel simulator) are research prototypes; their benchmarks are simulated or sandbox-only.
7. Only Python 3.12 was exercised although `pyproject.toml` allows 3.10 and newer.
8. `docs/FAILURE_MODES.md` has no owners and `docs/INTERFACE_CHECKLIST.md` is not filled in: both need the real robot.
9. Part of the workspace (TLS transport, KAVACH PKI, SANGAM sync service, replay harness, some documentation) was not written by the
   assistant that produced the rest; it was reviewed and tested but its origin is unknown. See `CHANGELOG.md` at the workspace root.
