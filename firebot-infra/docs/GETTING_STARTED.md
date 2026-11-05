# Getting started: install and use

## 1. Install (on the computer that runs Fire Bot's software)
Requirements: Python 3.10 or newer (Windows, Linux, macOS, Raspberry Pi OS all fine). Nothing else is needed.

    unzip firebot-infra-v1.0.zip && cd firebot-infra
    python -m venv .venv
    source .venv/bin/activate            # Windows: .venv\Scripts\activate
    pip install .                        # add  .[crypto]  if you will use KAVACH (device identity / signing)
    python -m unittest discover -s tests # optional: the full test suite
    python examples/robot_app_skeleton.py --seconds 3

For development use `pip install -e .` so your edits take effect immediately.
Your own project then does `from firebot_infra.drishti import StateEngine` etc. Fire Bot's code lives in YOUR
repository and imports this package; do not copy files into `site-packages`.

## 2. Where each system runs
| Machine | Runs |
|---|---|
| Robot computer (PC / Raspberry Pi) | akshara, ananta (connector side), setu, drishti, tantu, pravah, kavach (prover side), sangam (robot peer), framing, lipi, dhara |
| Monitoring station / laptop | ananta sink (e.g. ClickHouse HTTP), sangam (station peer), kavach (verifier + registry), tarang for analysis |
| Developer machine | tarang (decode captures), jala/spanda/kshiti simulators, all tests |
| Only if a real need appears | jala (multi-hop radios), spanda (lossy links), setu_edge (Modbus gear), samaya (multi-device logs) |
kshiti is a simulator only. It never runs on the robot.

## 3. How to implement Fire Bot on top (the pattern)
1. Sensors: write or configure a driver per sensor (`tantu`). Real hardware = a small `SensorDriver` subclass whose
   `read()` returns a `Reading`; register it with `register_driver("name", Class)`. Start with `"sim"` drivers.
2. Declare each measurement once (`MeasurementSpec`: unit, max age, range, frame) so DRISHTI can judge every value.
3. Autonomy code reads `engine.get(sensor, type)` and trusts a value only when `status is Status.VALID`.
4. Split Fire Bot into components that talk through SETU messages (register a schema per message type).
   In one process use `MemoryHub`; across devices use `TcpTransport` (network) or `StreamTransport` (serial).
5. Pass `on_event=rec.emit` to every system so everything lands in AKSHARA; the recorder `tee` feeds ANANTA.
6. Background chores (upload logs, build reports) go through PRAVAH. Never put motor, e-stop or suppression there.
7. Replay a failed run later: `read_run(dir)` then `replay(dir, {"subsystem": simulator_handler})`.
See `examples/robot_app_skeleton.py` for all of this in about 80 lines.

## 4. Connecting to real hardware later (adapters only)
| Hardware | Use |
|---|---|
| Serial sensor with your own packets | `UartSensorDriver(serial_port, type, unit, parse)` (a pyserial `Serial` works as the stream) |
| Modbus RTU/TCP devices | `ModbusClient` + `ModbusSensorDriver`; client is read-only unless `allow_write=True` |
| Microcontroller link | `StreamTransport(serial_port, peer_id)` so SETU messages flow over the wire |
| Analytics database | `ClickHouseHttpSink(url)` instead of `JsonlFileSink` |

## 5. Two protections worth turning on
**Catch frozen sensors (DRISHTI).** A sensor that keeps answering on time with the same number looks perfectly fresh.
Opt in per measurement type, only for noisy analog signals:

    MeasurementSpec("temperature", "degC", max_age_s=0.5, flatline_window_s=1.0, flatline_min_samples=5)
    # a value that never changes for 1 s -> status DEGRADED, reason "flatline" (and a status_change event)

Do NOT enable it for things that are legitimately constant (counters, a beam reading its max range).

**Sign operator commands (KAVACH + SETU).** Give each device an `Identity`, enrol public keys in a `Registry`, then:

    robot   = Node("robot", transport, schema, sign=EnvelopeSigner(robot_id),
                   authorize=EnvelopeVerifier(registry, policy, {"abort": "operate"}))
    station = Node("station", transport, schema, sign=EnvelopeSigner(station_id),
                   verify_response=EnvelopeVerifier(registry, policy, {}).verify_response)

The robot then only accepts messages that are signed by a registered, non-revoked device, unaltered, recent (a short
deadline is mandatory), not replayed, and whose role holds the capability mapped to that message type (default deny).
Revoking a device in the registry takes effect on its very next message. This authenticates; it does not encrypt.

## 6. Rules that keep it safe
- Emergency stop, motor drive and fire-suppression interlocks stay in validated hardware/low-level control.
- Test on a bench and in simulation first; run the fault-injection tests after every change.
- Fill the owner column in `docs/FAILURE_MODES.md` and do a hazard review before any hardware trial.

## 6. Ask questions of your recordings (ANANTA analytics)
    firebot-infra report events.jsonl --html report.html      # sensor dropouts, message latency, task durations, error rates
    firebot-infra compare-versions events.jsonl v1.4 v1.5      # flags a version whose error rate or durations got worse
In code: `Connector(spool, SqliteAnalytics("analytics.db"))` ships rows straight into the reference store; use
`ClickHouseHttpSink` and the table from `firebot-infra schema` when volume needs it. To get request latency, create nodes with
`Node(..., trace_requests=True, on_event=recorder.emit)`.

## 7. Check the machine
Run `firebot-infra selftest` after installing on the robot computer and `firebot-infra bench` to see what it can sustain.
