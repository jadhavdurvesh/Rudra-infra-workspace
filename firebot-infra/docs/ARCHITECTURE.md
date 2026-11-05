# Architecture (as built)

The original dossier described a *proposed* design. This document describes what exists in this repository.
Scope is software only: nothing here drives motors, e-stop or suppression, and the packages that schedule work or
sync data refuse types that look like actuation.

## Two paths
    OPERATIONAL   data sources -> adapters/drivers (TANTU, SETU-EDGE, UART) -> SETU (messages) -> DRISHTI (state + quality)
                  -> your autonomy code
    OBSERVABILITY every system's on_event -> AKSHARA Recorder (black box) -> tee -> ANANTA connector -> analytics store

Rule: the observability path can fail, stall or fill the disk without the operational path noticing. Every `on_event`
call is wrapped, every queue is bounded, and drops are counted and visible, never silent.

## Packages
| Package | Responsibility | Depends on |
|---|---|---|
| `events`, `framing` | shared event record; CRC-checked, resynchronising byte framing | - |
| `akshara` | recorder (append-only, checksummed, owner-only files), replay, replay harness, retention | events |
| `ananta` | privacy filter, bounded spool, batched/deduplicated delivery, SQLite reference store, dashboards, ClickHouse schema | events |
| `setu` | versioned messages, schemas, deadlines, dedupe, idempotent retries; in-memory, TCP, TLS-TCP and byte-stream transports | framing |
| `drishti` | sensor observations with units, frames, freshness, flatline detection | - |
| `tantu` | read-only sensor runtime + explicit drivers | drishti |
| `pravah` | noncritical task runtime | - |
| `kavach` | device identity, challenge-response, signed envelopes, signed artifacts, private CA | setu (hooks) |
| `sangam` | offline sync (journals, version vectors, conflict policies) and sync-over-SETU service | setu |
| `tarang` | passive capture decoding, fixtures, offline fault injection | framing, setu |
| `lipi`, `dhara`, `samaya`, `jala`, `spanda`, `setu_edge`, `kshiti` | optional extensions (see README); core does not depend on them | - |
| `cli`, `benchmarks` | `firebot-infra` command, synthetic-data benchmarks | all |

## Interfaces and versions
| Interface | Definition | Version marker |
|---|---|---|
| Event record | `events.Event`: run_id, subsystem, event_type, severity, payload, event_id, event_time (wall, epoch s), mono_ns (monotonic), seq, software_version | `schema_version = 1` |
| SETU message | `setu.Envelope`: message_type, source_id, destination_id, payload, kind (event/request/response/error), payload_schema_version, message_id, correlation_id, created_at, deadline (absolute epoch s), idempotency_key, auth | `protocol_version = 1` |
| SETU payloads | registered per message type in a `SchemaRegistry` (field -> type, optional fields); unknown types/versions are rejected | per-type version |
| Byte framing | `0x7E` + LEN (u16 BE) + payload + CRC32 (over LEN+payload); default max payload 4096 | - |
| Recording | folder: `manifest.json` + `seg-NNNNNN.jsonl`; line = `crc32hex<TAB>json`; manifest lists segment SHA-256s, counts, drops, write errors, completeness | `format: akshara-1` |
| Capture file | JSONL: meta line + chunk lines (hex data, timestamp, line-error flags) | `format: tarang-1` |
| Sensor driver | `tantu.SensorDriver`: `measurement_type`, `unit`, `read() -> Reading | None`, `reset()`, `close()` | - |
| Measurement spec | `drishti.MeasurementSpec`: unit, max age, dims, range, frame, optional flatline window | - |
| Analytics row | the 12 columns in `ananta.COLUMNS`; `CLICKHOUSE_DDL` and the SQLite schema match them | - |
| Sync messages | `sangam.hello`, `sangam.push`, `sangam.pull` over SETU | protocol 1 |

## Event catalog (what the systems emit)
Every system takes `on_event(subsystem, event_type, severity, **fields)`; `Recorder.emit` has exactly that shape.
| subsystem | event_type | fields | notes |
|---|---|---|---|
| drishti | `rejected` | sensor_id, measurement_type, reason | rate-limited per sensor and reason |
| drishti | `status_change` | sensor_id, measurement_type, old, new, reason | reason: stale, driver_quality, flatline |
| tantu | `sensor_state` | sensor_id, old, new, error | ok / degraded / failed |
| pravah | `transition` | task_id, type, old, new, attempts, (duration_ms, success when finished) | |
| setu | `request_done` | node, message_type, dest, success, duration_ms, (error) | only with `Node(trace_requests=True)` |
| setu | `malformed`, `unsupported_version`, `expired`, `schema_error`, `forbidden`, `handler_error`, `bad_response_auth` | node, plus message_type / source where known | dropped or refused messages |
| kavach | `auth_ok`, `auth_rejected`, `authz_denied` | verifier, device_id, role / reason / capability | challenge-response |
| kavach | `message_rejected` | reason, source, message_type | signed-envelope checks |
| (yours) | anything | anything | the application chooses its own subsystem names |

## Security model
- Identity: each device has an Ed25519 key; the registry stores public keys only; keys are never overwritten on disk.
- Message authenticity: `EnvelopeSigner` / `EnvelopeVerifier` sign and check every SETU message (who, unaltered, fresh, not a replay,
  not revoked, role allowed). Works on any transport, including serial. It does not hide content.
- Confidentiality + peer authentication on TCP: mutual TLS 1.3 with a private CA (`kavach.pki`, `setu.tls`); the certificate name must equal the
  node id being contacted.
- Updates: signed artifact manifests with rollback protection.
- Everything fails closed: unknown peer, revoked peer, bad signature, expired deadline, unmapped capability -> refused.

## Safety boundary
Observability and noncritical services never gate safety behaviour. The robot must keep working with SETU disconnected, ANANTA
stopped, the disk full or the recorder dead. Emergency stop, motor drive and fire-suppression interlocks stay in validated
hardware / low-level control. Passing these tests is not evidence of field safety.

## Extension points
Add a sensor: subclass `SensorDriver`, `register_driver("name", Class)`. Add a message: `SchemaRegistry.register(...)`, then `Node.on(...)`.
Add a protocol decoder: implement `tarang.ProtocolDecoder`. Add a sync data type: `sangam.TypePolicy(name, policy)` (command-like names are refused).
Add a dashboard: write a query function returning `ananta.dashboards.Table` (it forces you to state units and time range).
