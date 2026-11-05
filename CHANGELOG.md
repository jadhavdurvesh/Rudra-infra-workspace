# Changelog

## 1.2.0 (firebot-infra)
Written by the assistant in this session (details in `firebot-infra/RELEASE_NOTES.md`):
- ANANTA analytics backend, dashboards, version comparison, ClickHouse schema; SETU `trace_requests`; richer PRAVAH events.
- CLI `firebot-infra`; benchmark module; data-policy hardening (owner-only recordings, retention tools).
- Whole-system fault-injection tests (real SIGKILL, real disk-full, real network peer restart) and the three disk-full bug fixes they found.
- Documents: ARCHITECTURE (as built), DATA_POLICY, INTERFACE_CHECKLIST, TESTING, BENCHMARKS, RELEASE_NOTES; CI workflow and Makefile.
Unchanged from 1.1: the items listed below, including the files whose author is unknown.

## 1.1.0 (since v1.0)

## Written by Claude in this session
- DRISHTI: opt-in stuck-sensor ("flatline") detection per measurement type; `StateValue.reason` (stale | driver_quality | flatline);
  reasons in health summaries and status-change events. Tests in `tests/test_drishti.py` (FlatlineTests).
- KAVACH: `EnvelopeSigner` / `EnvelopeVerifier` (`kavach/envelope_auth.py`): every SETU message signed with Ed25519; receiver checks
  signer identity, tamper, freshness, replay, revocation and role capability. Tests in `tests/test_kavach_envelope.py`.
- SETU `Node`: optional `sign=` and `verify_response=` hooks; idempotent replay responses get a fresh message id and are re-signed.

## Found in the workspace, NOT written by Claude (reviewed and tested by Claude; origin unknown)
These appeared with modification times 04:49-04:56 on 2026-11-04 while Claude's session was idle. Reviewed by reading the code,
scanning the whole codebase for risky patterns (none found), and running all suites from a clean install.
- `setu/node.py`: signed deployments default a 30 s deadline on outgoing messages; replies inherit the request's deadline.
- `setu/tls.py`, `kavach/pki.py`, TLS options in `setu/transport.py`, `tests/test_tls.py`: mutual TLS 1.3 for the TCP transport
  with a small private CA; peer certificate name must equal the intended node id.
- `sangam/service.py`, `tests/test_sangam_service.py`: SANGAM sync over SETU (hello / push / pull, bounded, idempotent).
- `akshara/harness.py`, `tests/test_harness.py`: generic replay / diff / summary tools for recordings.
- `firebot-app`: flatline check enabled for the temperature probe, `replay.py` rebuilt on the infra harness, test updates.
- Documentation updates (READMEs, GETTING_STARTED, FAILURE_MODES).
