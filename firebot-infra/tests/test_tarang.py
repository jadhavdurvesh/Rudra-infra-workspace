import tempfile
import unittest
from pathlib import Path

from firebot_infra.framing import encode_frame
from firebot_infra.setu import Node, SchemaRegistry, StreamTransport, make_pipe
from firebot_infra.tarang import (Capture, CaptureMeta, Chunk, RawDecoder, SetuDecoder, decode_capture, drop_chunk,
                                  export_fixture, flip_byte, inject_noise, load_capture, run_fixture, save_capture,
                                  truncate_tail)

F1, F2, F3 = encode_frame(b"one"), encode_frame(b"two"), encode_frame(b"three")


def cap(data, **kw):
    return Capture.from_bytes(data, CaptureMeta(config={"baud": 115200}, capture_device="test"), **kw)


class TarangTests(unittest.TestCase):
    def test_known_capture_decodes_to_expected_frames_with_times(self):
        rep = decode_capture(cap(F1 + F2 + F3, chunk_size=len(F1), t0=10.0, dt=0.5), RawDecoder())
        self.assertTrue(rep.clean)
        self.assertEqual([f.payload for f in rep.frames], [b"one", b"two", b"three"][:3])
        self.assertEqual([f.t for f in rep.frames], [10.0, 10.5, 11.0][:3] if len(F1) == len(F2) else [f.t for f in rep.frames])
        self.assertEqual(rep.frames[0].decoded["length"], 3)
        self.assertEqual(rep.frame_gap_s["min"], 0.5)

    def test_malformed_and_truncated_reported_with_offsets(self):
        rep = decode_capture(truncate_tail(cap(F1 + F2), 3))
        self.assertEqual(len(rep.frames), 1)
        self.assertEqual(rep.count("truncated"), 1)
        t = [a for a in rep.annotations if a.kind == "truncated"][0]
        self.assertEqual(t.offset, len(F1))
        rep2 = decode_capture(flip_byte(cap(F1 + F2), 4))
        self.assertEqual((rep2.count("bad_crc"), len(rep2.frames)), (1, 1))
        rep3 = decode_capture(inject_noise(cap(F1 + F2), len(F1), n=4, seed=3))
        self.assertEqual(len(rep3.frames), 2)
        self.assertGreaterEqual(rep3.unknown_bytes(), 1)
        self.assertFalse(rep3.clean)

    def test_capture_loss_and_timestamp_limits_are_visible(self):
        c = cap(F1 + F2 + F3, chunk_size=len(F1), t0=0.0)
        lossy = decode_capture(drop_chunk(c, 1))
        self.assertTrue(lossy.capture_loss)
        self.assertEqual(lossy.meta.dropped_samples, len(F2))
        self.assertFalse(lossy.clean)
        self.assertEqual(decode_capture(cap(F1)).timestamps, "none")            # declared: no timing in this capture
        half = Capture(CaptureMeta(timestamp_basis="monotonic_s"), [Chunk(F1, 1.0), Chunk(F2, None)])
        self.assertEqual(decode_capture(half).timestamps, "missing")
        c.chunks[2].t = -5.0
        self.assertEqual(decode_capture(c).timestamps, "non_monotonic")

    def test_line_errors_from_adapter_are_annotated(self):
        c = Capture(CaptureMeta(), [Chunk(F1, 1.0, ("framing_error",)), Chunk(F2, 2.0)])
        rep = decode_capture(c)
        a = [x for x in rep.annotations if x.kind == "line_error"][0]
        self.assertEqual((a.offset, a.length, a.detail), (0, len(F1), "framing_error"))
        self.assertEqual(len(rep.frames), 2)

    def test_save_load_roundtrip_and_bad_lines_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.jsonl"
            c = cap(F1 + F2, chunk_size=5, t0=1.0)
            save_capture(p, c)
            loaded, bad = load_capture(p)
            self.assertEqual((bad, loaded.raw(), loaded.meta.config), (0, c.raw(), {"baud": 115200}))
            with open(p, "a") as f:
                f.write("not json\n{\"type\":\"chunk\",\"hex\":\"zz\"}\n")
            self.assertEqual(load_capture(p)[1], 2)

    def test_fixture_export_and_regression_check(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "fx.json"
            export_fixture(flip_byte(cap(F1 + F2 + F3, chunk_size=4, t0=0.0), 20), p, RawDecoder())
            self.assertEqual(run_fixture(p, RawDecoder()), [])
            import json
            doc = json.loads(p.read_text())
            doc["expected"]["frames"][0] = "00"            # regression: stored expectation no longer matches
            p.write_text(json.dumps(doc))
            self.assertTrue(run_fixture(p, RawDecoder()))

    def test_decodes_real_setu_traffic_captured_from_a_serial_link(self):
        sa, sb = make_pipe()
        sa.record = []
        r = SchemaRegistry(); r.register("ping", 1, {"n": int})
        a, b = Node("a", StreamTransport(sa, "b"), r), Node("b", StreamTransport(sb, "a"), r)
        try:
            b.on("ping", lambda e: {"n": 1})
            a.request("b", "ping", {"n": 1}, timeout=2)
        finally:
            a.close(); b.close()
        rep = decode_capture(Capture.from_bytes(b"".join(sa.record)), SetuDecoder())
        self.assertTrue(rep.clean)
        self.assertEqual(rep.frames[0].decoded["message_type"], "ping")
        self.assertEqual(rep.frames[0].decoded["kind"], "request")
        # a non-SETU payload is flagged, not hidden
        bad = decode_capture(cap(F1), SetuDecoder())
        self.assertEqual(bad.count("unrecognised_payload"), 1)

    def test_empty_capture(self):
        rep = decode_capture(Capture())
        self.assertEqual((len(rep.frames), rep.total_bytes), (0, 0))


if __name__ == "__main__":
    unittest.main()
