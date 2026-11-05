# Interface checklist (Phase 0: verify the actual system)

Fill this in from the real robot's schematic, parts list and code. Nothing below is assumed. The right-hand column says what to
use when the answer is known.

## 1. Hardware and software inventory
| Item | Answer (fill in) | When known, use |
|---|---|---|
| Controller / computer, OS, CPU, RAM, storage | | `firebot-infra selftest`, `firebot-infra bench` on that machine |
| Language(s) of the existing robot code | | adapters only: thin `SensorDriver` / SETU handlers |
| Each sensor: model, bus (UART/I2C/SPI/CAN/Modbus/analog), rate, units | | `tantu.register_driver`, `UartSensorDriver`, `setu_edge.ModbusSensorDriver` |
| Coordinate frames in use and transforms between them | | `MeasurementSpec(frame_id=...)`, `StateEngine.add_transform` |
| Motor / actuator interface (read-only awareness only) | | stays OUTSIDE this software |
| Radios / network links | | `TcpTransport` (+TLS), `StreamTransport`, `jala` only if multi-hop |
| Power / battery telemetry source | | a `SensorDriver` |

## 2. Message schemas (one row per message type)
| message_type | version | fields + units | timestamp basis | validity / deadline | max payload | safe to retry? | needs which capability |
|---|---|---|---|---|---|---|---|
| (fill in) | 1 | | | | | | |
Register each with `SchemaRegistry.register`; map each to a capability for `EnvelopeVerifier`.

## 3. Failure behaviour (per service; see FAILURE_MODES.md for the software side)
| Situation | Required robot behaviour (fill in) | Software support |
|---|---|---|
| Link timeout / disconnect | | `SetuUnavailable`, `SetuTimeout`, bounded queues |
| Duplicate / replayed messages | | dedupe, idempotency keys, signed envelopes |
| Stale or frozen sensor | | `Status.STALE`, `Status.DEGRADED` (reason `flatline`) |
| Restart | | PRAVAH recovery, AKSHARA incomplete-run marking |
| Storage error / disk full | | counted drops, rollback to last whole record |
| Low power | | (application decision) |

## 4. Data policy
Complete the "what you still have to decide" list in DATA_POLICY.md: deny-list additions, salt, retention, disk access, encryption.

## 5. Fixtures and scenarios to create from the real robot
- A known-good capture and a malformed capture of every bus in use (`tarang.export_fixture`).
- Link-disconnect scenario, simulated task failures, replay expectations (`akshara.replay_run`).

## 6. Safety boundary
List every piece of code that can affect motion or suppression, and confirm none of it depends on, or can be blocked by, anything in
this repository. Do the hazard analysis before any hardware trial. Record the result in the release notes.
