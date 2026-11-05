import tempfile
import time
import unittest
from pathlib import Path

from firebot_infra.akshara import Recorder, read_run
from firebot_infra.ananta import Connector, Spool
from firebot_infra.drishti import MeasurementSpec, Observation, StateEngine, Status
from firebot_infra.pravah import Scheduler, Task
from firebot_infra.setu import MemoryHub, Node, SchemaRegistry


class Sink:
    def __init__(self): self.down, self.rows = True, []
    def send(self, batch_id, rows):
        if self.down:
            raise ConnectionError("analytics down")
        self.rows += rows


class IntegrationTests(unittest.TestCase):
    def test_observability_path_survives_analytics_outage(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            sink = Sink()
            conn = Connector(Spool(d / "spool"), sink, device_id="bot")
            rec = Recorder(d / "runs", "run1", software_version="0.1", tee=conn.log)

            now = [100.0]
            eng = StateEngine(clock=lambda: now[0], on_event=rec.emit)
            eng.register(MeasurementSpec("distance", "m", max_age_s=1.0, min_value=0, max_value=50))
            hub = MemoryHub()
            schema = SchemaRegistry(); schema.register("ping", 1, {"n": int})
            a = Node("a", hub.transport("a"), schema, on_event=rec.emit)
            b = Node("b", hub.transport("b"), schema, on_event=rec.emit)
            sched = Scheduler({"job": lambda args, tok: 42}, on_event=rec.emit)
            try:
                b.on("ping", lambda e: {"n": e.payload["n"]})
                # operational path keeps working while analytics is down
                self.assertTrue(eng.ingest(Observation("s1", "distance", 2.0, "m", 100.0)).accepted)
                self.assertFalse(eng.ingest(Observation("s1", "distance", float("nan"), "m", 100.0)).accepted)
                self.assertEqual(a.request("b", "ping", {"n": 7}).payload, {"n": 7})
                hub._deliver("a", "b", b"garbage")
                sched.submit(Task("t1", "job")); self.assertTrue(sched.wait(2))
                now[0] = 103.0
                eng.check_freshness()
                self.assertEqual(eng.get("s1", "distance").status, Status.STALE)
                rec.emit("nav", "step", duration_ms=5, success=True, transcript="never shipped")
                time.sleep(0.3)
            finally:
                a.close(); b.close(); sched.stop(); rec.close()

            events, rep = read_run(d / "runs" / "run1")
            self.assertTrue(rep.clean, rep)
            kinds = {(e.subsystem, e.event_type) for e in events}
            self.assertTrue({("drishti", "rejected"), ("drishti", "status_change"), ("setu", "malformed"),
                             ("pravah", "transition"), ("nav", "step")} <= kinds, kinds)

            st = conn.flush()                       # analytics still down
            self.assertEqual(st.failed_batches, 1)
            self.assertGreater(conn.spool.pending_bytes(), 0)
            sink.down = False
            conn.flush()
            self.assertEqual(len(sink.rows), len(events))        # nothing lost, nothing duplicated
            self.assertNotIn("transcript", str(sink.rows))       # privacy filter applied
            self.assertTrue(all(r["device_id"] and r["device_id"] != "bot" for r in sink.rows))


if __name__ == "__main__":
    unittest.main()
