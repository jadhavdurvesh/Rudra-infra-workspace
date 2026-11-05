import tempfile
import time
import unittest
from pathlib import Path

from firebot_infra.akshara import Recorder, read_run, replay


def make_run(root, n=20, **kw):
    with Recorder(root, "run1", software_version="t1", **kw) as r:
        for i in range(n):
            r.emit("sensor", "reading", i=i)
    return Path(root) / "run1"


class AksharaTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = self._t.name

    def tearDown(self):
        self._t.cleanup()

    def test_roundtrip_clean(self):
        d = make_run(self.root)
        events, rep = read_run(d)
        self.assertTrue(rep.clean, rep)
        self.assertEqual([e.payload["i"] for e in events], list(range(20)))
        self.assertTrue(all(e.software_version == "t1" for e in events))

    def test_replay_is_deterministic(self):
        d = make_run(self.root)
        a, _ = read_run(d)
        b, _ = read_run(d)
        self.assertEqual(a, b)
        seen = []
        replay(d, {"sensor": seen.append})
        self.assertEqual(len(seen), 20)

    def test_segment_rotation(self):
        d = make_run(self.root, n=200, segment_bytes=2000)
        self.assertGreater(len(list(d.glob("seg-*.jsonl"))), 1)
        events, rep = read_run(d)
        self.assertTrue(rep.clean, rep)
        self.assertEqual(len(events), 200)

    def test_truncated_tail_is_detected_and_recovered(self):
        d = make_run(self.root)
        seg = sorted(d.glob("seg-*.jsonl"))[-1]
        raw = seg.read_bytes()
        seg.write_bytes(raw[:-7])
        events, rep = read_run(d)
        self.assertTrue(rep.truncated_tail)
        self.assertEqual(len(events), 19)
        self.assertIn(seg.name, rep.segment_hash_mismatches)
        self.assertFalse(rep.clean)

    def test_corrupt_middle_record_skipped_and_visible(self):
        d = make_run(self.root)
        seg = sorted(d.glob("seg-*.jsonl"))[0]
        lines = seg.read_bytes().split(b"\n")
        lines[4] = lines[4][:-3] + b"XXX"
        seg.write_bytes(b"\n".join(lines))
        events, rep = read_run(d)
        self.assertEqual(rep.corrupt_records, 1)
        self.assertEqual(len(events), 19)
        self.assertEqual(rep.seq_gaps, [(5, 5)])

    def test_missing_manifest_still_readable(self):
        d = make_run(self.root)
        (d / "manifest.json").unlink()
        events, rep = read_run(d)
        self.assertEqual(len(events), 20)
        self.assertFalse(rep.manifest_found)
        self.assertFalse(rep.clean)

    def test_slow_consumer_drops_not_blocks(self):
        class Slow(Recorder):
            def _write(self, seq, ev):
                time.sleep(0.02)
                super()._write(seq, ev)

        r = Slow(self.root, "slow", queue_size=5)
        t0 = time.monotonic()
        for i in range(100):
            r.emit("sensor", "reading", i=i)
        self.assertLess(time.monotonic() - t0, 0.5)  # caller never blocked
        r.close()
        events, rep = read_run(Path(self.root) / "slow")
        self.assertGreater(rep.declared_dropped, 0)
        self.assertTrue(rep.seq_gaps)
        self.assertEqual(rep.events_read + rep.declared_dropped, 100)

    def test_record_after_close_is_safe(self):
        r = Recorder(self.root, "x")
        r.close()
        self.assertFalse(r.emit("a", "b"))

    def test_run_id_cannot_be_reused(self):
        make_run(self.root)
        with self.assertRaises(FileExistsError):
            Recorder(self.root, "run1")


if __name__ == "__main__":
    unittest.main()
