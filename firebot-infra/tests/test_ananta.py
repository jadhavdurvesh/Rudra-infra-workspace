import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from firebot_infra import Event
from firebot_infra.ananta import ClickHouseHttpSink, Connector, DedupSink, JsonlFileSink, Spool, scrub


class FlakySink:
    def __init__(self):
        self.fail = True
        self.batches = []

    def send(self, batch_id, rows):
        if self.fail:
            raise ConnectionError("analytics down")
        self.batches.append((batch_id, rows))


def ev(i, **payload):
    return Event("r1", "nav", "step", payload={"i": i, **payload}, software_version="v1")


class AnantaTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.dir = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def test_ship_and_compact(self):
        out = self.dir / "out.jsonl"
        c = Connector(Spool(self.dir / "sp"), JsonlFileSink(out), batch_size=3, device_id="bot1", salt="s")
        for i in range(7):
            self.assertTrue(c.log(ev(i, success=True, duration_ms=12)))
        st = c.flush()
        self.assertEqual((st.sent_batches, st.sent_rows), (3, 7))
        rows = [json.loads(l) for l in out.read_text().splitlines()]
        self.assertEqual(len(rows), 7)
        self.assertTrue(rows[0]["success"] and rows[0]["duration_ms"] == 12.0)
        self.assertNotEqual(rows[0]["device_id"], "bot1")
        self.assertEqual(c.spool.pending_bytes(), 0)

    def test_outage_keeps_batch_and_retry_delivers_once(self):
        sink = FlakySink()
        c = Connector(Spool(self.dir / "sp"), sink, batch_size=10)
        for i in range(5):
            c.log(ev(i))
        st = c.flush()
        self.assertEqual(st.failed_batches, 1)
        self.assertGreater(c.spool.pending_bytes(), 0)
        self.assertGreater(c.backoff_s(), 0.5)
        sink.fail = False
        st = c.flush()
        self.assertEqual(st.sent_rows, 5)
        self.assertEqual(c.consecutive_failures, 0)

    def test_dedup_sink(self):
        inner = FlakySink()
        inner.fail = False
        d = DedupSink(inner)
        rows = [{"event_id": "a"}, {"event_id": "b"}]
        d.send("b1", rows)
        d.send("b1", rows)                      # duplicate batch
        d.send("b2", [{"event_id": "b"}, {"event_id": "c"}])  # overlapping event
        self.assertEqual(len(inner.batches), 2)
        self.assertEqual(inner.batches[1][1], [{"event_id": "c"}])
        self.assertEqual(d.duplicates, 3)

    def test_spool_bounded_drops_instead_of_blocking(self):
        c = Connector(Spool(self.dir / "sp", max_bytes=1500), FlakySink())
        results = [c.log(ev(i)) for i in range(50)]
        self.assertIn(False, results)
        self.assertGreater(c.spool.dropped, 0)

    def test_privacy_scrub(self):
        m = scrub({"transcript": "hello", "audio": b"x", "gps": [1, 2], "latency_ms": 5,
                   "note": "x" * 500, "nested": {"lat": 1, "ok": 2}})
        self.assertNotIn("transcript", m)
        self.assertNotIn("audio", m)
        self.assertNotIn("gps", m)
        self.assertEqual(m["latency_ms"], 5)          # 'latency' is not 'lat'
        self.assertNotIn("lat", m["nested"])
        self.assertTrue(m["note"].endswith("[truncated]"))

    def test_corrupt_spool_line_skipped(self):
        sp = Spool(self.dir / "sp")
        sp.append({"event_id": "a"})
        with open(sp.path, "ab") as f:
            f.write(b"{not json}\n")
        sp.append({"event_id": "b"})
        rows, off = sp.read_batch(10)
        self.assertEqual([r["event_id"] for r in rows], ["a", "b"])
        self.assertEqual(sp.corrupt, 1)

    def test_partial_trailing_line_not_consumed(self):
        sp = Spool(self.dir / "sp")
        sp.append({"event_id": "a"})
        with open(sp.path, "ab") as f:
            f.write(b'{"event_id":"b"')   # writer mid-line
        rows, _ = sp.read_batch(10)
        self.assertEqual(len(rows), 1)

    def test_clickhouse_http_sink_against_stub(self):
        got = {}

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got["path"] = self.path
                got["body"] = self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200); self.end_headers()
            def log_message(self, *a): pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            ClickHouseHttpSink(f"http://127.0.0.1:{srv.server_port}").send("tok123", [{"event_id": "a"}])
        finally:
            srv.shutdown()
        self.assertIn("insert_deduplication_token=tok123", got["path"])
        self.assertIn("JSONEachRow", got["path"])
        self.assertEqual(json.loads(got["body"]), {"event_id": "a"})

    def test_http_sink_failure_raises_so_batch_stays_queued(self):
        c = Connector(Spool(self.dir / "sp"), ClickHouseHttpSink("http://127.0.0.1:9", timeout=0.3))
        c.log(ev(1))
        st = c.flush()
        self.assertEqual(st.failed_batches, 1)
        self.assertGreater(c.spool.pending_bytes(), 0)

    def test_bad_table_name_rejected(self):
        with self.assertRaises(ValueError):
            ClickHouseHttpSink("http://x", table="t; DROP TABLE x")


if __name__ == "__main__":
    unittest.main()
