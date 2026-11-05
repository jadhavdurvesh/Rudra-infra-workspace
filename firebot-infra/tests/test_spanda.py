import time
import unittest

from firebot_infra.spanda import UdpTransport, compare_windows, decode_packet, encode_packet, make_pair
from firebot_infra.spanda.sim import run_until


class PacketTests(unittest.TestCase):
    def test_roundtrip_and_rejection(self):
        p = encode_packet(1, 7, 3, 2, b"payload")
        self.assertEqual(decode_packet(p), (1, 7, 3, 2, b"payload"))
        for bad in (b"", p[:-1], p + b"x", b"\x00" * 20):
            self.assertIsNone(decode_packet(bad))
        for i in range(len(p)):
            flipped = p[:i] + bytes([p[i] ^ 1]) + p[i + 1:]
            self.assertIsNone(decode_packet(flipped), i)        # every single-bit error is caught
        with self.assertRaises(ValueError):
            encode_packet(1, 0, 0, 0, b"x" * 70000)


class TransferTests(unittest.TestCase):
    def test_lossless_in_order_and_fragmented(self):
        a, b, link, clock, _, got_b, errs = make_pair({"latency": 0.01})
        msgs = [bytes([i]) * (100 + 400 * (i % 5)) for i in range(40)] + [b"", b"x" * 100_000]
        for m in msgs:
            self.assertTrue(a.send_message(m))
        run_until(a, b, link, lambda: len(got_b) == len(msgs))
        self.assertEqual(got_b, msgs)
        self.assertEqual(errs, [])
        self.assertEqual(a.stats["retransmits"], 0)

    def test_hostile_link_loss_dup_reorder_corruption(self):
        a, b, link, clock, _, got_b, errs = make_pair(
            {"latency": 0.02, "jitter": 0.04, "loss": 0.2, "dup": 0.1, "corrupt": 0.05, "seed": 4})
        msgs = [bytes([i]) * 1500 for i in range(50)]
        for m in msgs:
            a.send_message(m)
        run_until(a, b, link, lambda: len(got_b) == len(msgs) and a.idle())
        self.assertEqual(got_b, msgs)                      # exactly once, in order, uncorrupted
        self.assertEqual(errs, [])
        self.assertGreater(a.stats["retransmits"], 0)
        self.assertGreater(b.stats["duplicates"] + a.stats["corrupt_dropped"] + b.stats["corrupt_dropped"], 0)

    def test_bidirectional(self):
        a, b, link, clock, got_a, got_b, _ = make_pair({"latency": 0.01, "loss": 0.1, "seed": 2})
        for i in range(20):
            a.send_message(b"A%d" % i); b.send_message(b"B%d" % i)
        run_until(a, b, link, lambda: len(got_a) == 20 and len(got_b) == 20)
        self.assertEqual(got_b, [b"A%d" % i for i in range(20)])
        self.assertEqual(got_a, [b"B%d" % i for i in range(20)])

    def test_backpressure_is_bounded(self):
        a, *_ = make_pair(queue_limit=10)
        self.assertTrue(a.send_message(b"x" * 5000))        # 10 segments at mss 512 (some already in flight)
        self.assertTrue(a.send_message(b"y" * 500))
        results = [a.send_message(b"z" * 5000) for _ in range(3)]
        self.assertIn(False, results)
        self.assertGreater(a.stats["queue_full"], 0)
        with self.assertRaises(ValueError):
            a.send_message(b"x" * (a.max_message + 1))

    def test_dead_link_reports_failure_and_stops_accepting(self):
        a, b, link, clock, _, _, errs = make_pair({"loss": 1.0}, rto_initial=0.05, rto_max=0.2, max_retries=4)
        a.send_message(b"hello")
        run_until(a, b, link, lambda: bool(errs), max_time=30)
        self.assertEqual(errs, ["max_retries"])
        self.assertFalse(a.send_message(b"more"))

    def test_window_beats_stop_and_wait(self):
        t = compare_windows(windows=(1, 32), n_messages=60, link_kw={"latency": 0.05})
        self.assertGreater(t[1] / t[32], 5)

    def test_rtt_estimator_adapts_and_cwnd_grows(self):
        a, b, link, clock, _, got_b, _ = make_pair({"latency": 0.05})
        for i in range(30):
            a.send_message(b"m%d" % i)
        run_until(a, b, link, lambda: len(got_b) == 30)
        self.assertAlmostEqual(a.srtt, 0.1, delta=0.03)
        self.assertGreater(a.cwnd, 2)

    def test_garbage_and_bogus_acks_ignored(self):
        a, b, link, clock, _, got_b, _ = make_pair()
        a.on_packet(b"junk"); a.on_packet(encode_packet(2, 0, 999, 0, b""))   # ack for data never sent
        a.on_packet(encode_packet(2, 0, 0, 0, b"abc"))                         # malformed SACK list
        self.assertEqual(a.stats["corrupt_dropped"], 1)
        self.assertEqual(a.stats["bad_ack"], 2)
        a.send_message(b"still works")
        run_until(a, b, link, lambda: len(got_b) == 1)
        self.assertEqual(got_b, [b"still works"])

    def test_out_of_window_data_dropped(self):
        a, b, *_ = make_pair(window=4)
        b.on_packet(encode_packet(1, 1000, 0, 3, b"far future"))
        self.assertEqual(b.stats["out_of_window"], 1)

    def test_new_message_start_resets_stale_partial(self):
        a, b, link, clock, _, got_b, _ = make_pair()
        b.on_packet(encode_packet(1, 0, 0, 1, b"first half"))        # FIRST, never LAST
        b.on_packet(encode_packet(1, 1, 0, 3, b"fresh"))              # FIRST|LAST
        self.assertEqual(got_b, [b"fresh"])
        self.assertEqual(b.stats["protocol_resets"], 1)


class UdpTests(unittest.TestCase):
    def test_real_udp_with_injected_loss(self):
        got = []
        a = UdpTransport(on_message=lambda m: None, rto_initial=0.05, rto_max=0.3)
        b = UdpTransport(on_message=got.append, rto_initial=0.05, rto_max=0.3)
        try:
            a.peer, b.peer = b.addr, a.addr
            n = [0]
            def lossy(d):
                n[0] += 1
                return n[0] % 4 != 0                       # drop every 4th datagram
            a.tx_filter = lossy
            msgs = [b"udp-%d" % i * 50 for i in range(15)]
            for m in msgs:
                a.send_message(m)
            deadline = time.monotonic() + 8
            while (len(got) < len(msgs) or not a.idle()) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(got, msgs)
        finally:
            a.close(); b.close()


if __name__ == "__main__":
    unittest.main()
