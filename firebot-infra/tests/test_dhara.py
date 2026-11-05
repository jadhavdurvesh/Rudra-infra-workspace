import os
import shutil
import tempfile
import unittest
from pathlib import Path

from firebot_infra.dhara import DharaCorruption, DharaStore, SqliteKV, StoreLocked, compare


class SimulatedCrash(Exception): ...


class DharaTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def test_basic_ops_and_persistence(self):
        with DharaStore(self.d / "s") as s:
            s.put("a", b"1"); s.put(b"b", b"22"); s.put("a", b"3")
            self.assertEqual((s.get("a"), s.get("b"), s.get("zz")), (b"3", b"22", None))
            self.assertTrue(s.delete("b")); self.assertFalse(s.delete("b"))
            s.put("p:1", b"x"); s.put("p:2", b"y")
            self.assertEqual([k for k, _ in s.items("p:")], [b"p:1", b"p:2"])
            self.assertEqual(len(s), 3)
        with DharaStore(self.d / "s") as s:
            self.assertEqual(dict(s.items()), {b"a": b"3", b"p:1": b"x", b"p:2": b"y"})
            self.assertEqual(s.recovered_dropped_bytes, 0)

    def test_validation(self):
        with DharaStore(self.d / "s") as s:
            for bad in ("", b"x" * 70000):
                with self.assertRaises(ValueError): s.put(bad, b"v")
            with self.assertRaises(ValueError): s.write_batch([("explode", "k")])
            s.put("empty", b"")
            self.assertEqual(s.get("empty"), b"")

    def test_batch_is_atomic_and_visible_together(self):
        with DharaStore(self.d / "s") as s:
            s.put("a", b"old"); s.put("b", b"old")
            s.write_batch([("put", "a", b"new"), ("delete", "b"), ("put", "c", b"new")])
            self.assertEqual(dict(s.items()), {b"a": b"new", b"c": b"new"})
        with DharaStore(self.d / "s") as s:
            self.assertEqual(dict(s.items()), {b"a": b"new", b"c": b"new"})

    def test_crash_at_every_byte_offset_recovers_to_a_committed_prefix(self):
        ops = [("put", b"a", b"1"), ("put", b"b", b"22"), ("delete", b"a"),
               ("batch", [("put", b"c", b"3"), ("put", b"a", b"4"), ("delete", b"b")]),
               ("put", b"c", b"5" * 50), ("put", b"d", b"")]
        src = self.d / "src"
        states, ends = [{}], [0]
        with DharaStore(src, sync="none") as s:
            for op in ops:
                if op[0] == "put": s.put(op[1], op[2])
                elif op[0] == "delete": s.delete(op[1])
                else: s.write_batch(op[1])
                states.append(dict(s.items()))
                ends.append(os.path.getsize(src / "dhara.log"))
        blob = (src / "dhara.log").read_bytes()
        for n in range(len(blob) + 1):
            d = self.d / f"c{n}"
            d.mkdir()
            (d / "dhara.log").write_bytes(blob[:n])
            k = max(i for i, e in enumerate(ends) if e <= n)
            with DharaStore(d) as s:
                self.assertEqual(dict(s.items()), states[k], f"truncated at byte {n}")
                self.assertEqual(s.recovered_dropped_bytes, n - ends[k])
            shutil.rmtree(d)

    def test_every_single_byte_corruption_is_survived_without_exceptions(self):
        src = self.d / "src"
        with DharaStore(src, sync="none") as s:
            for i in range(6):
                s.put(f"k{i}", f"value-{i}".encode())
            s.write_batch([("put", "k9", b"z"), ("delete", "k0")])
        blob = (src / "dhara.log").read_bytes()
        valid = {frozenset(), }
        for n in range(len(blob)):
            d = self.d / "c"
            d.mkdir(exist_ok=True)
            bad = bytearray(blob); bad[n] ^= 0xFF
            (d / "dhara.log").write_bytes(bytes(bad))
            with DharaStore(d) as s:
                for k, v in s.items():                      # whatever survives must be readable and correct
                    self.assertEqual(s.get(k), v)
            shutil.rmtree(d)

    def test_torn_write_during_operation(self):
        class Crashy(DharaStore):
            crash = False
            def _append(self, buf):
                if self.crash:
                    self._f.write(buf[:len(buf) // 2]); self._f.flush()
                    raise SimulatedCrash
                super()._append(buf)
        s = Crashy(self.d / "s")
        s.put("keep", b"1")
        s.write_batch([("put", "a", b"1"), ("put", "b", b"2")])
        s.crash = True
        with self.assertRaises(SimulatedCrash):
            s.write_batch([("put", "a", b"X"), ("put", "b", b"Y"), ("put", "c", b"Z")])
        s._f.close(); os.close(s._lockfd)                  # process "dies"
        with DharaStore(self.d / "s") as s2:
            self.assertEqual(dict(s2.items()), {b"keep": b"1", b"a": b"1", b"b": b"2"})   # all-or-nothing
            self.assertGreater(s2.recovered_dropped_bytes, 0)
            s2.put("after", b"ok")
            self.assertEqual(s2.get("after"), b"ok")

    def test_read_detects_bit_rot_after_open(self):
        with DharaStore(self.d / "s") as s:
            s.put("k", b"hello world")
            with open(s.path, "r+b") as f:
                f.seek(-3, 2); f.write(b"\xff")
            with self.assertRaises(DharaCorruption):
                s.get("k")

    def test_compaction_shrinks_and_preserves(self):
        with DharaStore(self.d / "s", sync="none") as s:
            for i in range(200):
                s.put("hot", os.urandom(100))
            s.put("cold", b"stay"); s.put("gone", b"x"); s.delete("gone")
            before, hot = s.stats(), s.get("hot")
            self.assertGreater(before["garbage_ratio"], 0.8)
            s.compact()
            after = s.stats()
            self.assertLess(after["file_bytes"], before["file_bytes"] / 20)
            self.assertEqual((s.get("hot"), s.get("cold"), s.get("gone")), (hot, b"stay", None))
            s.put("new", b"1")
        with DharaStore(self.d / "s") as s:
            self.assertEqual(sorted(k for k, _ in s.items()), [b"cold", b"hot", b"new"])

    def test_leftover_compaction_file_ignored(self):
        with DharaStore(self.d / "s") as s:
            s.put("a", b"1")
        (self.d / "s" / "dhara.log.compact").write_bytes(b"half-written garbage")
        with DharaStore(self.d / "s") as s:
            self.assertEqual(s.get("a"), b"1")
        self.assertFalse((self.d / "s" / "dhara.log.compact").exists())

    def test_second_open_is_refused(self):
        with DharaStore(self.d / "s"):
            with self.assertRaises(StoreLocked):
                DharaStore(self.d / "s")
        DharaStore(self.d / "s").close()                     # lock released on close

    def test_sync_always_mode(self):
        with DharaStore(self.d / "s", sync="always") as s:
            s.put("a", b"1")
            self.assertEqual(s.get("a"), b"1")
        with self.assertRaises(ValueError):
            DharaStore(self.d / "x", sync="maybe")

    def test_sqlite_baseline_has_same_interface(self):
        for make in (lambda p: DharaStore(p), SqliteKV):
            s = make(self.d / f"cmp{make.__class__.__name__}{id(make)}")
            s.put("a", b"1"); s.put("b", b"2")
            s.write_batch([("put", "c", b"3"), ("delete", "a")])
            self.assertEqual(dict(s.items()), {b"b": b"2", b"c": b"3"})
            self.assertEqual((s.get("a"), len(s), s.delete("zzz")), (None, 2, False))
            s.close()

    def test_compare_runs(self):
        r = compare(self.d / "bench", n=200)
        self.assertEqual(set(r), {"dhara", "sqlite"})
        self.assertGreater(r["dhara"]["get_ops_s"], 0)


if __name__ == "__main__":
    unittest.main()
