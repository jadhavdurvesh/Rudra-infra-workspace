# firebot-infra

Software-only supporting infrastructure for Fire Bot. Python 3.10+, standard library only
(KAVACH additionally needs `cryptography`; install with `pip install .[crypto]`).
Nothing here touches motors, e-stop or suppression, and PRAVAH/SANGAM refuse types that look like actuation.

## Systems (all nine dossier systems plus all optional extensions built)
| Package | What it does |
|---|---|
| `akshara` | Black-box recorder: bounded non-blocking queue, append-only checksummed segments, manifest, deterministic replay with gap/corruption reporting |
| `ananta` | Telemetry connector: privacy filter -> durable bounded spool -> batched, retried, deduplicated delivery (JSONL / ClickHouse HTTP sinks) |
| `setu` | Message fabric: versioned envelope, schema registry, deadlines, dedupe, idempotent retries, bounded queues; in-memory (with fault injection) and TCP transports |
| `drishti` | Sensor state with data quality: units, frames, freshness, ordering, range, degraded/stale/unavailable, opt-in frozen-sensor (flatline) detection with a reason code |
| `pravah` | Noncritical task runtime: explicit states, dependencies, timeouts, bounded concurrency, restart recovery |
| `kavach` | Ed25519 device identity, challenge-response, role policy, key rotation/revocation, signed artifact manifests, and per-message signing/verification for SETU (authenticity, integrity, freshness, replay protection; not encryption) |
| `sangam` | Offline sync: per-peer journals, version vectors, per-type conflict policies, expiring entries |
| `tarang` | Passive serial diagnostics: capture files, frame decoder with byte offsets, line/timing/capture-loss annotations, regression fixtures, offline fault injection |
| `tantu` | Read-only peripheral runtime: explicit sensor drivers (sim + framed UART), one thread per sensor, health/backoff/reset, config-built bindings feeding DRISHTI |
| `framing` + `setu.stream` | CRC-checked, resynchronising frame format; SETU can run over any byte stream (serial/pipe) |
| `lipi` | Codec layer: varints, schema-based compact records, zlib/bz2/lzma, block-compressed random-access files, benchmark helper |
| `dhara` | Crash-tested log-structured key-value store + SQLite baseline with the same interface + comparison benchmark |
| `samaya` | Hybrid logical, Lamport and vector clocks, clock-offset estimator with uncertainty, causal delivery, multi-device log merge |
| `jala` | Link-state mesh routing with deterministic network simulator (route recovery, partitions, TTL) |
| `spanda` | Experimental reliable transport (selective-repeat ARQ, SACK, AIMD) with lossy-link simulator and real UDP adapter |
| `setu_edge` | Modbus RTU/TCP codec, read-only-by-default client, simulated server, TANTU driver |
| `kshiti` | Deterministic microkernel SIMULATOR (scheduler, IPC, quotas, watchdog, deadlock detection). Not an OS |

The last seven rows are the dossier's optional extensions, built ahead of need. Using them is optional: the core
systems do not depend on them (setu_edge only plugs a driver into TANTU).

## Data flow
Operational: data sources -> adapters -> SETU -> DRISHTI -> existing Fire Bot autonomy.
Observability: any system's `on_event` -> AKSHARA `Recorder.emit` -> (`tee`) -> ANANTA connector.
Every `on_event` call is wrapped so an observability failure can never affect the operational path.

## Install and use
See `docs/GETTING_STARTED.md` (install, where each part runs, how to build Fire Bot on top, hardware adapters).

    pip install .            # or  pip install .[crypto]  for KAVACH

## Run
    PYTHONPATH=src python -m unittest discover -s tests      # whole suite
    PYTHONPATH=src python examples/demo.py                    # recorder + analytics pipeline
    PYTHONPATH=src python examples/demo_serial.py             # TANTU + serial SETU + TARANG
    PYTHONPATH=src python examples/robot_app_skeleton.py      # template for the real Fire Bot program

See `docs/FAILURE_MODES.md` for the per-service failure table (fill in owners).

## Not yet verified (needs the real robot code)
Adapters to the actual sensors/controllers, the real device list, clock sync across devices, and a hazard analysis.
Passing simulation tests is not evidence of field safety.

## Command line
    firebot-infra selftest                              # 15-check smoke test of every subsystem on this machine
    firebot-infra run-info runs/<run>                   # summarise a recording and say whether it can be trusted
    firebot-infra diff-runs runs/a runs/b               # did a change alter behaviour?
    firebot-infra decode capture.jsonl --setu           # decode a captured serial/bus trace
    firebot-infra report events.jsonl --html r.html     # analytics dashboards (units and UTC time ranges stated)
    firebot-infra compare-versions events.jsonl v1 v2   # exit code 1 if v2 regressed
    firebot-infra bench [--quick]                       # synthetic-data benchmarks
    firebot-infra prune-runs runs --older-than-days 30  # retention (dry run unless --yes)
    firebot-infra schema                                # ClickHouse table definition

## Documents
`docs/GETTING_STARTED.md` (install and use), `docs/ARCHITECTURE.md` (as built: interfaces, event catalog, security model),
`docs/DATA_POLICY.md`, `docs/INTERFACE_CHECKLIST.md` (fill in when hardware is chosen), `docs/FAILURE_MODES.md`,
`docs/TESTING.md`, `docs/BENCHMARKS.md`, `RELEASE_NOTES.md`.
