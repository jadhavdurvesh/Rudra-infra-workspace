# firebot (application layer, simulation first)

The first Fire Bot behavior, running in a simulated room on top of `firebot-infra`:
**search the room -> find the heat source -> stop at a safe standoff -> report where it is.**
See `docs/sample_run.svg` for a picture of one run.

## Run it
    pip install ./firebot-infra ./firebot-app          # from the workspace folder
    python -m firebot --scenario obstacles --seed 7 --randomize --svg run.svg
    python -m firebot --scenario empty_room --seed 2 --data-dir runs --replay   # record + replay-verify
    python -m firebot --scenario no_fire                # must report "not found"
    python -m unittest discover -s firebot-app/tests    # 40 tests

Scenarios: `empty_room`, `obstacles`, `no_fire`. `--randomize` puts the fire (and start heading) somewhere new per seed.

## What is in it
| Part | File | Role |
|---|---|---|
| Simulated world | `sim/world.py` | 2-D room, obstacles, heat model (hotter near the fire, weaker behind walls), robot motion, collisions |
| Simulated sensors | `sim/sensors.py` | 3 thermal probes, 7 range beams over 180 degrees, odometry with drift, battery; fault injection (dropout / garbage / stuck). They are TANTU drivers, the same interface real ones will use |
| Perception helper | `perception.py` | "How far can my body travel straight ahead?" (exact for point obstacles); used by autonomy and guard |
| Autonomy | `autonomy/mission.py` | State machine SEARCH -> APPROACH -> FOUND, plus SAFE_HOLD, NOT_FOUND, ABORTED. Trusts a sensor only when DRISHTI says VALID. Outputs velocity *requests* only |
| Motion guard | `autonomy/guard.py` | Can only make a command safer (stop / slow / clamp); never reverses; latched e-stop flag. **Simulation stand-in for the real low-level safety layer** |
| Runner | `runner.py` | Wires TANTU -> DRISHTI -> autonomy -> guard -> world; every event to AKSHARA; recorder tee to ANANTA rows; robot and operator station talk over SETU (heartbeats, mission report with retry, operator abort) |
| Replay | `replay.py` | Feeds a recording's raw sensor inputs into a fresh autonomy and checks the decisions are identical (regression check for any code change) |
| Report | `report.py` | ASCII map and SVG picture |

## How the search works (v0.1)
Reactive exploration with a coverage memory: drive forward, steer away from nearby things, and choose new headings toward
cells not yet visited. When any thermal probe rises 8 C above ambient it follows the heat gradient (left vs right probe),
stops when the centre probe is 200 C hotter (about 1.7 m from the fire), and inverts the thermal model to estimate the
fire's position. Heat lost for 3 s returns to searching. A jam detector (told to move but not moving) triggers a recovery turn.

## Measured behavior (simulation)
In randomized layouts, 16/16 missions found the fire, 0 collisions, position error under 0.5 m (typically 2-10 cm).
With no fire it searches for the full 300 s and reports "not found" (about 70% floor coverage). A 1 s sensor dropout pauses
the robot and the mission carries on; a 10 s+ outage aborts safely.

## Known limitations (honest list)
- Simulation only. The thermal model, sensor noise and calibration constants are made up; real sensors need real calibration.
- Reactive exploration, no global map: it can be slow in large or multi-room layouts (frontier exploration is the next step).
- Position comes from odometry only and drifts over time.
- Frozen sensors: thermal probes use DRISHTI's flatline check; range beams do not (an open-space beam legitimately reads its max range, so that check is off by design; a different detector is needed there).
- The guard is a stand-in. Emergency stop, motor drive and suppression interlocks must live in validated hardware/low-level control.
- No fire suppression of any kind: this software finds and reports only.
- Operator commands over SETU are not authenticated in this test workload yet (the infra supports signed messages: `EnvelopeSigner` / `EnvelopeVerifier`).

## Next steps
1. Frontier-based exploration with a proper occupancy map (multi-room, doors).
2. Wire KAVACH signing/verification into the robot-station link (the infra now supports it); SANGAM: sync mission logs to the station.
4. Real drivers (UART/Modbus) behind the same TANTU interface; then bench tests with fault injection.
5. Hazard analysis before any hardware trial.
