"""TEMPLATE: how a Fire Bot application wires the infrastructure together.

Copy this file as the start of your robot program and replace the marked parts.
Run as-is (simulated sensors):  python examples/robot_app_skeleton.py --seconds 3

Shutdown order matters: stop producers first, close the recorder last so the run is marked complete.
Nothing here controls motors, e-stop or suppression: that stays in validated low-level control.
"""
import argparse
import random
import tempfile
import time
from pathlib import Path

from firebot_infra.akshara import Recorder, read_run
from firebot_infra.ananta import Connector, JsonlFileSink, Spool
from firebot_infra.drishti import StateEngine, Status
from firebot_infra.pravah import Scheduler, Task
from firebot_infra.setu import MemoryHub, Node, SchemaRegistry
from firebot_infra.tantu import PeripheralRuntime, build_bindings

VERSION = "0.1.0"

# 1) SENSORS ------------------------------------------------------------------------------------------
# Replace "sim" with real drivers later ("uart_frames", "modbus", or your own registered with register_driver()).
SENSOR_CONFIG = [
    {"sensor_id": "temp1", "driver": "sim", "period_s": 0.1,
     "params": {"measurement_type": "temperature", "unit": "degC", "source": lambda: 25 + random.random() * 5},
     "spec": {"measurement_type": "temperature", "unit": "degC", "max_age_s": 1.0, "min_value": -40, "max_value": 1200}},
    {"sensor_id": "dist1", "driver": "sim", "period_s": 0.1,
     "params": {"measurement_type": "distance", "unit": "m", "source": lambda: 1 + random.random()},
     "spec": {"measurement_type": "distance", "unit": "m", "max_age_s": 1.0, "min_value": 0, "max_value": 50}},
]


def robot_logic(engine, rec):
    """2) YOUR FIRE BOT AUTONOMY GOES HERE. It consumes qualified state and must treat anything not VALID as untrusted."""
    t = engine.get("temp1", "temperature")
    d = engine.get("dist1", "distance")
    if t.status is not Status.VALID or d.status is not Status.VALID:
        rec.emit("autonomy", "sensor_untrusted", "warning", temp=t.status.value, dist=d.status.value)
        return
    rec.emit("autonomy", "step", duration_ms=1, success=True, temp_c=round(t.value, 1), dist_m=round(d.value, 2))


def main(seconds: float, data_dir: Path) -> None:
    run_id = time.strftime("run-%Y%m%d-%H%M%S")

    # Observability path: recorder -> (tee) -> ANANTA connector -> sink (swap JsonlFileSink for ClickHouseHttpSink later)
    conn = Connector(Spool(data_dir / "spool"), JsonlFileSink(data_dir / "analytics.jsonl"), device_id="firebot-1", salt="change-me")
    rec = Recorder(data_dir / "runs", run_id, software_version=VERSION, tee=conn.log)

    # Operational path: sensors -> DRISHTI -> your logic
    engine = StateEngine(on_event=rec.emit)
    runtime = PeripheralRuntime(engine, on_event=rec.emit)
    for b in build_bindings(SENSOR_CONFIG):
        runtime.bind(b)

    # Noncritical background jobs
    sched = Scheduler({"flush_analytics": lambda args, tok: conn.flush().sent_rows}, on_event=rec.emit)

    # Component messaging (here in-process; swap MemoryHub for TcpTransport / StreamTransport across devices)
    schema = SchemaRegistry()
    schema.register("status", 1, {"ok": bool})
    hub = MemoryHub()
    autonomy = Node("autonomy", hub.transport("autonomy"), schema, on_event=rec.emit)
    monitor = Node("monitor", hub.transport("monitor"), schema, on_event=rec.emit)
    monitor.on("status", lambda env: {"ok": True})

    runtime.start()
    try:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            robot_logic(engine, rec)
            autonomy.request("monitor", "status", {"ok": True}, timeout=0.5)
            time.sleep(0.2)
        sched.submit(Task("flush", "flush_analytics"))
        sched.wait(5)
    finally:
        runtime.stop(); autonomy.close(); monitor.close(); sched.stop()
        rec.close()                                  # last: marks the run complete
    conn.flush()

    events, report = read_run(data_dir / "runs" / run_id)
    print(f"run {run_id}: {len(events)} events recorded, clean={report.clean}")
    print("sensor health:", {k: v.state for k, v in runtime.health().items()})
    print("analytics rows:", len((data_dir / "analytics.jsonl").read_text().splitlines()))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=3)
    ap.add_argument("--data-dir", type=Path, default=None)
    a = ap.parse_args()
    if a.data_dir:
        a.data_dir.mkdir(parents=True, exist_ok=True)
        main(a.seconds, a.data_dir)
    else:
        with tempfile.TemporaryDirectory() as d:
            main(a.seconds, Path(d))
