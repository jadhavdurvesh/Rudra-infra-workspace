import unittest

from firebot_infra.samaya import (CausalBuffer, ClockDriftError, HybridLogicalClock, LamportClock, OffsetEstimator,
                                  VectorClock, merge_logs)


class ClockTests(unittest.TestCase):
    def test_lamport(self):
        a, b = LamportClock(), LamportClock()
        t = a.tick(); a.tick()
        self.assertGreater(b.update(a.tick()), t)

    def test_hlc_monotonic_even_when_physical_clock_stalls_or_goes_back(self):
        now = [1000]
        c = HybridLogicalClock("n1", now_ms=lambda: now[0])
        s1 = c.now(); s2 = c.now()
        now[0] = 900                                   # clock stepped backwards
        s3 = c.now()
        self.assertLess(s1, s2); self.assertLess(s2, s3)
        self.assertEqual(s3[0], 1000)

    def test_hlc_receive_preserves_causality(self):
        na, nb = [1000], [500]                         # b's physical clock is far behind
        a = HybridLogicalClock("a", lambda: na[0]); b = HybridLogicalClock("b", lambda: nb[0])
        sent = a.now()
        got = b.update(sent)
        self.assertGreater(got, sent)                  # receive is ordered after send
        self.assertGreater(b.now(), got)

    def test_hlc_rejects_runaway_remote_clock(self):
        c = HybridLogicalClock("a", lambda: 1000, max_drift_ms=100)
        with self.assertRaises(ClockDriftError):
            c.update((5000, 0, "x"))
        self.assertEqual(c.l, 0)                       # our clock was not dragged forward

    def test_hlc_logical_counter_overflow_detected(self):
        c = HybridLogicalClock("a", lambda: 1000)
        c.now()
        c.c = HybridLogicalClock.MAX_LOGICAL
        with self.assertRaises(ClockDriftError):
            c.now()

    def test_vector_clock_relations(self):
        a = VectorClock().tick("a")
        b = a.copy().tick("b")
        c = a.copy().tick("c")
        self.assertEqual((a.compare(b), b.compare(a), b.compare(c), a.compare(a.copy())),
                         ("before", "after", "concurrent", "equal"))
        self.assertEqual(b.copy().merge(c).v, {"a": 1, "b": 1, "c": 1})
        self.assertEqual(VectorClock().compare(VectorClock()), "equal")

    def test_offset_estimation_and_uncertainty(self):
        # remote clock is 100 s ahead; symmetric 10 ms path each way
        est = OffsetEstimator()
        self.assertFalse(est.ready)
        t0 = 5.0
        est.add_sample(t0, t0 + 0.010 + 100, t0 + 0.011 + 100, t0 + 0.021)
        self.assertAlmostEqual(est.offset, 100.0, places=6)
        self.assertAlmostEqual(est.uncertainty, 0.010, places=6)
        local, unc = est.remote_to_local(205.0)
        self.assertAlmostEqual(local, 105.0, places=6)

    def test_best_sample_is_lowest_delay_and_bad_samples_rejected(self):
        est = OffsetEstimator()
        est.add_sample(0, 100.5, 100.5, 1.0)          # slow, asymmetric
        est.add_sample(10, 110.001, 110.001, 10.002)  # fast, precise
        self.assertAlmostEqual(est.offset, 100.0, places=3)
        self.assertFalse(est.add_sample(5, 10, 9, 6))   # t2 < t1
        self.assertFalse(est.add_sample(5, 1, 2, 4))    # t3 < t0
        self.assertEqual(est.rejected, 2)
        with self.assertRaises(ValueError):
            OffsetEstimator().offset


class OrderingTests(unittest.TestCase):
    def test_causal_delivery_holds_back_dependent_messages(self):
        buf = CausalBuffer()
        # p1 sends m1; p2 receives it then replies m2 (depends on m1). m2 arrives first at p3.
        self.assertEqual(buf.receive("p2", {"p1": 1, "p2": 1}, "m2"), [])
        self.assertEqual(buf.pending(), 1)
        self.assertEqual(buf.receive("p1", {"p1": 1}, "m1"), [("p1", "m1"), ("p2", "m2")])
        self.assertEqual(buf.pending(), 0)

    def test_fifo_per_sender_duplicates_and_overflow(self):
        buf = CausalBuffer(max_pending=2)
        self.assertEqual(buf.receive("a", {"a": 3}, "3"), [])
        self.assertEqual(buf.receive("a", {"a": 3}, "3"), [])          # duplicate of a pending message
        self.assertEqual(buf.receive("a", {"a": 2}, "2"), [])
        self.assertEqual(buf.receive("a", {"a": 4}, "4"), [])          # buffer full
        self.assertEqual(buf.rejected_overflow, 1)
        self.assertEqual([p for _, p in buf.receive("a", {"a": 1}, "1")], ["1", "2", "3"])
        self.assertEqual(buf.receive("a", {"a": 2}, "2"), [])          # already delivered
        self.assertEqual(buf.duplicates, 2)

    def test_concurrent_messages_both_deliverable(self):
        buf = CausalBuffer()
        self.assertEqual(len(buf.receive("a", {"a": 1}, "x")), 1)
        self.assertEqual(len(buf.receive("b", {"b": 1}, "y")), 1)

    def test_merge_logs_with_offsets_and_ambiguity(self):
        streams = {"robot": [(10.0, "r1"), (20.0, "r2")], "station": [(110.001, "s1"), (125.0, "s2")]}
        offsets = {"robot": (0.0, 0.0005), "station": (100.0, 0.0005)}
        m = merge_logs(streams, offsets)
        self.assertEqual([e.payload for e in m], ["r1", "s1", "r2", "s2"])
        self.assertTrue(m[0].ambiguous and m[1].ambiguous)             # 1 ms apart, uncertainty 0.5 ms each
        self.assertFalse(m[2].ambiguous)
        self.assertEqual(m[3].t, 25.0)

    def test_same_node_events_never_ambiguous_and_no_offsets_default(self):
        m = merge_logs({"a": [(1.0, "x"), (1.0, "y")]})
        self.assertFalse(any(e.ambiguous for e in m))
        self.assertEqual(merge_logs({}), [])


if __name__ == "__main__":
    unittest.main()
