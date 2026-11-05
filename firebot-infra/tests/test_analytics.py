import json
import tempfile
import time
import unittest
from pathlib import Path

from firebot_infra.akshara import Recorder
from firebot_infra.ananta import CLICKHOUSE_DDL, COLUMNS, Connector, Spool, SqliteAnalytics, dashboards, to_row
from firebot_infra.drishti import MeasurementSpec, Observation, StateEngine
from firebot_infra.events import Event
from firebot_infra.pravah import Scheduler, Task
from firebot_infra.setu import MemoryHub, Node, SchemaRegistry, SetuRemoteError

T0 = 1_800_000_000.0     # 2027-01-15 08:00:00 UTC


def row(i, subsystem="nav", event_type="step", severity="info", version="v1", t=None, run="r1", success=None,
        duration=None, **meta):
    return {"event_id": f"e{i}", "event_time": T0 + i if t is None else t, "monotonic_ms": i * 1000, "subsystem": subsystem,
            "event_type": event_type, "severity": severity, "run_id": run, "software_version": version, "device_id": "d1",
            "success": success, "duration_ms": duration, "metadata": meta}


class BackendTests(unittest.TestCase):
    def test_idempotent_batches_and_events(self):
        db = SqliteAnalytics()
        db.send("b1", [row(1), row(2)])
        db.send("b1", [row(1), row(2)])                 # same batch again
        db.send("b2", [row(2), row(3)])                 # overlapping event id
        self.assertEqual(db.count(), 3)
        self.assertEqual((db.duplicate_batches, db.duplicate_rows), (1, 1))

    def test_bad_rows_rejected_and_good_rows_kept(self):
        db = SqliteAnalytics()
        db.send("b", [row(1), {"event_id": 5}, "junk", {**row(2), "event_time": "yesterday"}, {**row(3), "event_time": True}])
        self.assertEqual((db.count(), db.rejected_rows), (1, 4))

    def test_query_is_read_only(self):
        db = SqliteAnalytics()
        db.send("b", [row(1)])
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM events")[0]["n"], 1)
        for bad in ("DELETE FROM events", "DROP TABLE events", "SELECT 1; DROP TABLE events", "INSERT INTO batches VALUES ('x')",
                    "WITH x AS (SELECT 1) DELETE FROM events", "PRAGMA writable_schema=1", "UPDATE events SET severity='x'"):
            with self.assertRaises(ValueError, msg=bad):
                db.query(bad)
        self.assertEqual(db.count(), 1)                   # nothing was damaged, and the guard was released afterwards
        db.send("b2", [row(2)])
        self.assertEqual(db.count(), 2)

    def test_search_filters(self):
        db = SqliteAnalytics()
        db.send("b", [row(1), row(2, "setu", "malformed", "warning"), row(3, "setu", "expired", "warning", run="r2"),
                      row(4, "nav", "step", "error", node="x")])
        self.assertEqual([r["event_id"] for r in db.search(subsystem="setu")], ["e2", "e3"])
        self.assertEqual([r["event_id"] for r in db.search(severity="warning", run_id="r2")], ["e3"])
        self.assertEqual([r["event_id"] for r in db.search(since=T0 + 2, until=T0 + 4)], ["e2", "e3"])
        self.assertEqual(db.search(event_type="step", severity="error")[0]["metadata"], {"node": "x"})
        self.assertEqual(len(db.search(limit=2)), 2)

    def test_jsonl_roundtrip_and_clickhouse_schema_matches_rows(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.jsonl"
            p.write_text("\n".join(json.dumps(r) for r in (row(1), row(2))) + "\nnot json\n")
            db = SqliteAnalytics.from_jsonl(p)
            self.assertEqual((db.count(), db.rejected_rows), (2, 1))
        ddl_cols = [l.split()[0] for l in CLICKHOUSE_DDL.splitlines() if l.startswith("    ") and not l.strip().startswith(("PARTITION", "ORDER"))]
        self.assertEqual(ddl_cols, list(COLUMNS))
        self.assertEqual(set(to_row(Event("r", "s", "t")).keys()), set(COLUMNS))      # the connector emits exactly these columns


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteAnalytics()
        rows, i = [], 0

        def add(**kw):
            nonlocal i
            i += 1
            rows.append(row(i, **kw))
        for _ in range(3):
            add(subsystem="drishti", event_type="status_change", severity="warning", sensor_id="thermal", new="stale", old="valid", reason="stale")
        add(subsystem="drishti", event_type="status_change", severity="warning", sensor_id="thermal", new="degraded", old="valid", reason="flatline")
        add(subsystem="drishti", event_type="status_change", severity="warning", sensor_id="gps", new="unavailable", old="valid", reason="")
        add(subsystem="drishti", event_type="status_change", severity="info", sensor_id="gps", new="valid", old="stale", reason="")
        for _ in range(2):
            add(subsystem="tantu", event_type="sensor_state", severity="warning", sensor_id="gps", new="failed", old="degraded")
        for d in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100):
            add(subsystem="setu", event_type="request_done", severity="info", success=True, duration=float(d), message_type="status")
        add(subsystem="setu", event_type="request_done", severity="warning", success=False, duration=1000.0, message_type="status")
        add(subsystem="setu", event_type="malformed", severity="warning")
        add(subsystem="setu", event_type="malformed", severity="warning")
        add(subsystem="setu", event_type="expired", severity="warning")
        for d, ok in ((100, True), (300, True), (200, False)):
            add(subsystem="pravah", event_type="transition", success=ok, duration=float(d), type="upload", old="running", new="x")
        self.n = len(rows)
        self.db.send("b", rows)

    def table(self, t):
        return {r[0]: r[1:] for r in t.rows}

    def test_overview_and_error_rates(self):
        r = dashboards.overview(self.db).rows[0]
        self.assertEqual((r[0], r[1], r[2], r[3]), (self.n, 1, 1, 1))
        by = self.table(dashboards.errors_by_subsystem(self.db))
        self.assertEqual(by["drishti"][:3], [6, 5, 0])                 # 6 events, 5 warnings
        self.assertAlmostEqual(by["drishti"][3], 1000 * 5 / 6)
        self.assertEqual(by["setu"][1], 4)                             # 3 + 1 failed request

    def test_sensor_dropouts_and_failures(self):
        s = self.table(dashboards.sensor_dropouts(self.db))
        self.assertEqual(s["thermal"][:4], [3, 0, 1, 0])               # stale, unavailable, flatline, other
        self.assertEqual(s["gps"][:4], [0, 1, 0, 0])
        self.assertEqual(list(s)[0], "thermal")                        # worst first
        self.assertEqual(self.table(dashboards.sensor_failures(self.db)), {"gps": [2]})

    def test_request_latency_percentiles_and_success_rate(self):
        r = dashboards.request_latency(self.db).rows[0]
        self.assertEqual(r[:2], ["status", 11])
        self.assertAlmostEqual(r[2], 100 * 10 / 11)
        self.assertEqual((r[3], r[4], r[5]), (60.0, 1000.0, 1000.0))   # nearest-rank p50 of 11 values, p95, max

    def test_message_problems_and_task_durations(self):
        self.assertEqual(self.table(dashboards.message_problems(self.db)), {"malformed": [2], "expired": [1]})
        r = dashboards.task_durations(self.db).rows[0]
        self.assertEqual((r[0], r[1], r[2], r[3], r[4], r[5]), ("upload", 3, 1, 200.0, 300.0, 300.0))

    def test_timeseries_buckets(self):
        t = dashboards.timeseries(self.db, "setu", "request_done", bucket_s=10)
        self.assertEqual(sum(r[1] for r in t.rows), 11)
        self.assertTrue(all("UTC" in r[0] for r in t.rows))

    def test_tables_state_units_and_time_range(self):
        for t in dashboards.full_report(self.db):
            self.assertTrue(t.units, t.title)
            self.assertIn("UTC", t.caption())
            self.assertNotIn("n/a", t.caption().split("data from")[1].split(" to ")[0]) if t.rows else None
        md = dashboards.to_markdown(dashboards.full_report(self.db))
        self.assertIn("2027-01-15", md)
        self.assertIn("milliseconds", md)
        self.assertEqual(dashboards.timeseries(SqliteAnalytics(), "x").rows, [])
        self.assertIn("(no data)", dashboards.request_latency(SqliteAnalytics()).to_markdown())

    def test_html_report_escapes_content(self):
        db = SqliteAnalytics()
        db.send("b", [row(1, "drishti", "status_change", "warning", sensor_id="<script>alert(1)</script>", new="stale", reason="")])
        page = dashboards.to_html(dashboards.full_report(db))
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)


class VersionComparisonTests(unittest.TestCase):
    def build(self, cand_bad_rate, n=100, cand_dur=100.0):
        db, i, rows = SqliteAnalytics(), 0, []
        for version, bad, dur in (("v1", 2, 100.0), ("v2", cand_bad_rate, cand_dur)):
            for k in range(n):
                i += 1
                rows.append(row(i, "setu", "request_done", "warning" if k < bad else "info", version=version,
                                duration=dur, success=True))
        db.send("b", rows)
        return db

    def verdicts(self, db, **kw):
        return {r[0]: r[4] for r in dashboards.compare_versions(db, "v1", "v2", **kw).rows}

    def test_flags_worse_error_rate_and_slower_durations(self):
        v = self.verdicts(self.build(cand_bad_rate=30, cand_dur=300.0))
        self.assertEqual(v["setu: warn+error rate"], "REGRESSION")
        self.assertEqual(v["setu/request_done: mean duration"], "REGRESSION")

    def test_no_false_alarm_for_equal_or_tiny_changes(self):
        v = self.verdicts(self.build(cand_bad_rate=2))
        self.assertEqual(set(v.values()), {"ok"})
        v = self.verdicts(self.build(cand_bad_rate=4))                  # 2x but only +20/1000... still above abs floor
        self.assertEqual(v["setu: warn+error rate"], "REGRESSION")
        v = self.verdicts(self.build(cand_bad_rate=4), min_abs_per_1000=50)
        self.assertEqual(v["setu: warn+error rate"], "ok")

    def test_too_little_data_is_not_judged(self):
        v = self.verdicts(self.build(cand_bad_rate=10, n=10))
        self.assertEqual(set(v.values()), {"not enough data"})


class EndToEndTests(unittest.TestCase):
    def test_real_components_feed_the_dashboards(self):
        with tempfile.TemporaryDirectory() as d:
            db = SqliteAnalytics()
            conn = Connector(Spool(Path(d) / "spool"), db, device_id="bot", salt="s")
            rec = Recorder(Path(d) / "runs", "run-1", software_version="e2e-1", tee=conn.log)
            now = [100.0]
            eng = StateEngine(clock=lambda: now[0], on_event=rec.emit)
            eng.register(MeasurementSpec("temperature", "degC", max_age_s=1.0, flatline_window_s=1.0))
            for i in range(30):                                          # a probe that freezes
                now[0] = 100 + i * 0.1
                eng.ingest(Observation("probe", "temperature", 21.0, "degC", now[0]))
            now[0] = 110.0
            eng.check_freshness()                                        # ... and then goes silent
            hub = MemoryHub()
            schema = SchemaRegistry(); schema.register("ping", 1, {"n": int})
            a = Node("a", hub.transport("a"), schema, on_event=rec.emit, trace_requests=True)
            b = Node("b", hub.transport("b"), schema)
            b.on("ping", lambda e: {})
            for _ in range(5):
                a.request("b", "ping", {"n": 1}, timeout=1)
            hub.disconnect("b")
            with self.assertRaises(Exception):
                a.request("b", "ping", {"n": 1}, timeout=0.1)
            hub._deliver("a", "a", b"garbage")                           # junk arriving on a (b is disconnected)
            time.sleep(0.2)
            sched = Scheduler({"job": lambda args, tok: 1, "bad": lambda args, tok: 1 / 0}, on_event=rec.emit)
            sched.submit(Task("t1", "job"), Task("t2", "job"), Task("t3", "bad")); sched.wait(3); time.sleep(0.1)
            a.close(); b.close(); sched.stop(); rec.close()
            conn.flush()

            self.assertGreater(db.count(), 10)
            s = {r[0]: r for r in dashboards.sensor_dropouts(db).rows}
            self.assertEqual((s["probe"][3], s["probe"][1]), (1, 1))     # one flatline, one stale
            lat = dashboards.request_latency(db).rows[0]
            self.assertEqual((lat[0], lat[1]), ("ping", 6))
            self.assertAlmostEqual(lat[2], 100 * 5 / 6)
            self.assertEqual(dict((r[0], r[1]) for r in dashboards.message_problems(db).rows), {"malformed": 1})
            td = dashboards.task_durations(db).rows[0]
            self.assertEqual((td[0], td[1], td[2]), ("job", 3, 1)) if td[0] == "job" else None
            types = {r[0]: r for r in dashboards.task_durations(db).rows}
            self.assertEqual((types["job"][1], types["job"][2]), (2, 0))
            self.assertEqual((types["bad"][1], types["bad"][2]), (1, 1))
            self.assertTrue(all(r["device_id"] != "bot" for r in db.search(limit=1000)))     # pseudonymised on the way in


if __name__ == "__main__":
    unittest.main()
