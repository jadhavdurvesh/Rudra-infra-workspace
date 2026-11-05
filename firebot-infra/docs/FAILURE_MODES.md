# Failure modes (software only)

Owner column is intentionally blank: assign a person per service before any hardware trial.

| Service | If it crashes/stops | If its dependency is down | Bounds & timeouts | Restart behaviour | Owner |
|---|---|---|---|---|---|
| AKSHARA | Callers unaffected; `record()` returns False | Disk full/IO error: write_errors counted, recording degrades | Bounded queue (drops counted, visible as `seq` gaps); segment size cap | Manifest `complete=false` marks an interrupted run; reader recovers up to last good record, flags truncated tail | |
| ANANTA connector | Callers unaffected (`log()` never raises) | Sink down: batches stay in spool, exponential backoff hint | Spool `max_bytes` (drops counted); batch size cap; sink timeout | Cursor persisted; retried batches reuse a deterministic batch id; DedupSink / ClickHouse token dedupe | |
| SETU | Peers see timeouts/`SetuUnavailable`; robot stays in its own safe mode | Link loss: sends return False; requests raise | Bounded inbox (drops counted), payload cap, per-request deadline | Stateless; dedupe window rebuilt empty (retries rely on idempotency keys) | |
| DRISHTI | Consumers get UNAVAILABLE/STALE, never fabricated values | Missing sensor: UNAVAILABLE; late sensor: STALE; frozen-but-on-time sensor (opt-in `flatline_window_s`): DEGRADED with reason `flatline` | Per-type `max_age_s`; rejected buffer bounded | State starts empty (UNAVAILABLE) until fresh data arrives | |
| PRAVAH | Noncritical tasks stop; no authority over safety actuation (refused at registration) | Handler exceptions -> failed/retry per policy | Worker + resource limits; `timeout_s`, `deadline_s` (cooperative cancel) | Store reloaded; interrupted idempotent tasks re-run, others fail with reason | |
| KAVACH | Fail closed: unauthenticated peers stay unauthorised; unsigned, tampered, replayed, expired, spoofed or revoked-signer messages are rejected (`EnvelopeVerifier`) | Registry missing/unreadable: no peer verifies | Nonce TTL (10 s default), outstanding challenge cap, session TTL | Registry is JSON of public keys; revocation takes effect immediately | |
| SANGAM | Local operation unaffected; sync simply doesn't happen | Peer offline: entries accumulate, sync lag = `pending_for()` | Batch cap per round, max rounds, expiring entries | Journal persisted; version vectors make re-sync idempotent | |
| TARANG | Offline tool; robot unaffected | Missing/partial capture: reported, never guessed | Frame size cap; captures are files | Stateless | |
| TANTU | Sensors go STALE/UNAVAILABLE in DRISHTI; no actuator API exists | Driver error: counted, rate-limited events, reset + exponential backoff after `max_failures`; hung driver only stalls its own sensor | Per-sensor thread, period, slow-read counter, `stop()` joins with timeout | Drivers re-opened via `reset()`; health restarts as `starting` | |
| Serial framing / SETU stream | Frames dropped, never delivered corrupted | Noise/false SOF: resync (idle timeout discards a false header); CRC32 on every frame | Max frame size; reader thread contained | Parser starts empty | |
| LIPI | Callers get `LipiError` (only that) on any bad input; nothing else escapes | Truncated file: reader rebuilds index from complete blocks and flags `truncated_tail` | Decompression is size-bounded (zip-bomb safe); block size cap | Footer missing after crash: scan-recovery | |
| DHARA | Opens to a committed prefix after a crash at any byte (tested at every offset); torn tail truncated | Disk error: exception to caller, store stays consistent | Single-process lock; value/key size caps | `compact()` is atomic; leftover temp file removed at open | |
| SAMAYA | Library only; no runtime service | Missing/odd clock samples are rejected and counted; drift beyond limit raises | Bounded causal buffer (rejects new when full) | Stateless | |
| JALA | Node loses routes after timeouts; local operation unaffected | Link loss: re-route after hello timeout; no route: bounded queue then drop | TTL, bounded queue, bounded dedupe | Rebuilds routes from hellos/LSAs | |
| SPANDA | Experimental: reports `max_retries` error, stops accepting | Loss/dup/reorder/corruption handled by ARQ; dead link detected | Bounded queue, window, message size | Fresh state each connection | |
| SETU-EDGE | Driver errors become `DriverError` (sensor goes stale) | Timeouts/CRC errors: reads retried, writes never auto-retried | Per-request timeout, retry cap | Stateless client | |
| KSHITI | Simulator only: not part of the robot | n/a | Tick limits, mailbox/memory/CPU quotas inside the simulation | n/a | |
| ANANTA analytics store | Off-robot component: the robot is unaffected | Bad rows rejected and counted; duplicate batches/events ignored | Read-only query guard; retention via `prune` | Idempotent re-send from the spool | |

## Crash and disk-full semantics (tested with real process kills and a real file-size limit)
- AKSHARA: a killed process leaves a clean prefix marked "not cleanly closed". A failed write is cut back to the last whole record,
  counted in the manifest (`write_errors`) and never raised to the caller.
- ANANTA spool: a failed append is counted and removed; later rows are never garbled by a half-written one.
- DHARA: a failed write raises to the caller and leaves no trace; a write that returned successfully survives a crash or disk-full.
- PRAVAH: after a kill, idempotent tasks re-run and everything else is marked failed with the reason.
- SANGAM: the state file is replaced atomically; a kill never leaves it half-written.
- Not covered: power loss on real flash (data between fsyncs can be lost), failing hardware, electrical faults.
