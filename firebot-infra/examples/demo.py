"""End-to-end demo: DRISHTI + SETU + PRAVAH -> AKSHARA -> ANANTA connector. Run: PYTHONPATH=src python examples/demo.py"""
import tempfile, time
from pathlib import Path
from firebot_infra.akshara import Recorder, read_run
from firebot_infra.ananta import Connector, JsonlFileSink, Spool
from firebot_infra.drishti import MeasurementSpec, Observation, StateEngine
from firebot_infra.pravah import Scheduler, Task
from firebot_infra.setu import MemoryHub, Node, SchemaRegistry

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    conn = Connector(Spool(d / "spool"), JsonlFileSink(d / "analytics.jsonl"), device_id="firebot-1", salt="demo")
    rec = Recorder(d / "runs", "demo-run", software_version="0.1.0", tee=conn.log)

    eng = StateEngine(on_event=rec.emit)
    eng.register(MeasurementSpec("temperature", "degC", max_age_s=2.0, min_value=-40, max_value=1200))
    eng.ingest(Observation("t1", "temperature", 451.0, "degF", time.monotonic()))     # converted to degC
    eng.ingest(Observation("t1", "temperature", float("nan"), "degC", time.monotonic()))  # rejected, logged
    print("temperature:", eng.get("t1", "temperature"))

    schema = SchemaRegistry(); schema.register("ping", 1, {"n": int})
    hub = MemoryHub()
    a, b = Node("a", hub.transport("a"), schema, on_event=rec.emit), Node("b", hub.transport("b"), schema, on_event=rec.emit)
    b.on("ping", lambda e: {"n": e.payload["n"] + 1})
    print("SETU reply:", a.request("b", "ping", {"n": 1}).payload)

    sched = Scheduler({"report": lambda args, tok: "report built"}, on_event=rec.emit)
    sched.submit(Task("t1", "report")); sched.wait(2)
    print("PRAVAH:", sched.status("t1"))
    time.sleep(0.2); a.close(); b.close(); sched.stop(); rec.close()

    events, report = read_run(d / "runs" / "demo-run")
    print(f"AKSHARA: {len(events)} events, clean={report.clean}")
    print("ANANTA:", conn.flush())
    print("rows in analytics sink:", len((d / "analytics.jsonl").read_text().splitlines()))
