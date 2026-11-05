import random
import time
import unittest

from firebot_infra.framing import FrameParser, encode_frame
from firebot_infra.setu import Node, SchemaRegistry, SetuTimeout, StreamTransport, make_pipe


def kinds(events):
    return [e.kind for e in events]


class FramingTests(unittest.TestCase):
    def test_roundtrip_and_empty_payload(self):
        p = FrameParser()
        ev = p.feed(encode_frame(b"hello") + encode_frame(b""))
        self.assertEqual([e.payload for e in ev], [b"hello", b""])
        self.assertEqual([e.offset for e in ev], [0, 12])

    def test_byte_at_a_time(self):
        data = encode_frame(b"abc") + encode_frame(b"defg")
        p, out = FrameParser(), []
        for b in data:
            out += p.feed(bytes([b]))
        self.assertEqual([e.payload for e in out], [b"abc", b"defg"])

    def test_garbage_between_frames_resyncs_with_offsets(self):
        f = encode_frame(b"x")
        ev = FrameParser().feed(f + b"\x01\x02\x03" + f)
        self.assertEqual(kinds(ev), ["frame", "garbage", "frame"])
        self.assertEqual((ev[1].offset, ev[1].length, ev[2].offset), (len(f), 3, len(f) + 3))

    def test_bad_crc_reported_and_next_frame_recovered(self):
        f = bytearray(encode_frame(b"payload"))
        f[5] ^= 0xFF
        ev = FrameParser().feed(bytes(f) + encode_frame(b"ok"))
        self.assertEqual(kinds(ev)[0], "bad_crc")
        self.assertEqual(ev[-1].payload, b"ok")

    def test_oversize_length_does_not_stall(self):
        ev = FrameParser(max_payload=16).feed(b"\x7e\xff\xff" + encode_frame(b"fine"))
        self.assertIn("oversize", kinds(ev))
        self.assertEqual(ev[-1].payload, b"fine")

    def test_truncated_reported_only_on_finish(self):
        p = FrameParser()
        self.assertEqual(p.feed(encode_frame(b"abcdef")[:-3]), [])
        fin = p.finish()
        self.assertEqual(kinds(fin), ["truncated"])
        self.assertEqual(fin[0].length, len(encode_frame(b"abcdef")) - 3)

    def test_never_raises_and_never_yields_invalid_frames_on_noise(self):
        rng = random.Random(1)
        for _ in range(200):
            p = FrameParser(max_payload=64)
            data = bytes(rng.choice([0x7E, 0x00, 0x01, 0xFF, rng.randrange(256)]) for _ in range(rng.randrange(300)))
            for i in range(0, len(data), 7):
                p.feed(data[i:i + 7])
            p.finish()
        # a false SOF with a plausible length hides the real frame until the line goes idle
        f = encode_frame(b"needle")
        p = FrameParser()
        self.assertEqual(p.feed(bytes([0x7E, 0x00, 0x7E]) + f), [])
        self.assertTrue(p.has_partial)
        ev = p.timeout()
        self.assertEqual(ev[-1].payload, b"needle")
        self.assertFalse(p.has_partial)

    def test_oversize_encode_rejected(self):
        with self.assertRaises(ValueError):
            encode_frame(b"x" * 70000)


def reg():
    r = SchemaRegistry(); r.register("ping", 1, {"n": int}); return r


class StreamTransportTests(unittest.TestCase):
    def setUp(self):
        self.sa, self.sb = make_pipe()
        self.ta, self.tb = StreamTransport(self.sa, "b"), StreamTransport(self.sb, "a")
        self.a, self.b = Node("a", self.ta, reg()), Node("b", self.tb, reg())
        self.b.on("ping", lambda e: {"n": e.payload["n"] + 1})

    def tearDown(self):
        self.a.close(); self.b.close()

    def test_request_response_over_serial_framing(self):
        self.assertEqual(self.a.request("b", "ping", {"n": 1}, timeout=2).payload, {"n": 2})

    def test_wrong_destination_not_sent(self):
        self.assertFalse(self.ta.send("somebody-else", b"x"))

    def test_corrupted_frame_dropped_then_retry_succeeds(self):
        hits = []
        def corrupt_first(data):
            if not hits:
                hits.append(1)
                return data[:6] + bytes([data[6] ^ 0xFF]) + data[7:]
            return data
        self.sa.tx_fault = corrupt_first
        with self.assertRaises(SetuTimeout):
            self.a.request("b", "ping", {"n": 1}, timeout=0.3)
        self.assertEqual(self.tb.stats["bad_crc"], 1)
        self.assertEqual(self.a.request("b", "ping", {"n": 1}, timeout=2).payload, {"n": 2})

    def test_noise_on_line_does_not_break_following_messages(self):
        self.sa.write(b"\x00\x7e\x7e\x13garbage")
        self.assertEqual(self.a.request("b", "ping", {"n": 5}, timeout=2).payload, {"n": 6})

    def test_false_sof_with_plausible_length_does_not_block_real_frames(self):
        self.sa.write(b"\x7e\x00\x7e")          # looks like a 126-byte frame header, then the line goes idle
        time.sleep(0.4)                          # idle resync discards the false header
        self.assertGreaterEqual(self.tb.stats["truncated"], 1)
        self.assertEqual(self.a.request("b", "ping", {"n": 9}, timeout=2).payload, {"n": 10})

    def test_closed_stream_reports_send_failure(self):
        self.sa.close()
        self.assertFalse(self.ta.send("b", b"x"))
        self.assertEqual(self.ta.stats["write_errors"], 1)


if __name__ == "__main__":
    unittest.main()
