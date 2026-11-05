import os
import tempfile
import unittest
from pathlib import Path

from firebot_infra.lipi import (BlockReader, BlockWriter, LipiError, RecordSchema, benchmark, decode_uvarint,
                                encode_uvarint, get_codec, unzigzag, zigzag)


class VarintTests(unittest.TestCase):
    def test_roundtrip_edges(self):
        for n in (0, 1, 127, 128, 300, 2 ** 32, 2 ** 64 - 1):
            self.assertEqual(decode_uvarint(encode_uvarint(n)), (n, len(encode_uvarint(n))))
        for n in (0, -1, 1, -(2 ** 63), 2 ** 63 - 1):
            self.assertEqual(unzigzag(zigzag(n)), n)

    def test_malformed(self):
        for bad in (b"", b"\x80", b"\xff" * 11):
            with self.assertRaises(LipiError):
                decode_uvarint(bad)
        with self.assertRaises(LipiError):
            decode_uvarint(b"\xff" * 9 + b"\x7f")        # overflows 64 bits
        with self.assertRaises(LipiError):
            encode_uvarint(-1)
        with self.assertRaises(LipiError):
            zigzag(2 ** 63)


class SchemaTests(unittest.TestCase):
    S = RecordSchema([("t", "f64"), ("n", "u"), ("d", "i"), ("name", "str"), ("raw", "bytes"), ("ok", "bool"),
                      ("x", "f32"), ("opt", "str")], optional=("opt",))

    def rec(self, **kw):
        r = {"t": 1.5, "n": 7, "d": -3, "name": "héllo", "raw": b"\x00\x01", "ok": True, "x": 0.5, "opt": None}
        r.update(kw)
        return r

    def test_roundtrip_with_and_without_optional(self):
        for opt in (None, "here"):
            r = self.rec(opt=opt)
            self.assertEqual(self.S.decode(self.S.encode(r)), r)

    def test_compact(self):
        self.assertLess(len(self.S.encode(self.rec())), 40)

    def test_rejects_bad_input_and_garbage(self):
        good = self.S.encode(self.rec())
        for bad in (good + b"\x00", good[:-1], b"", b"\x02" + good[1:], good[:5]):
            with self.assertRaises(LipiError):
                self.S.decode(bad)
        for r in (self.rec(n=-1), self.rec(n=True), self.rec(ok=1), self.rec(name=5), {"t": 1.0}, dict(self.rec(), z=1)):
            with self.assertRaises(LipiError):
                self.S.encode(r)

    def test_decoder_never_raises_other_exceptions_on_random_bytes(self):
        import random
        rng = random.Random(3)
        for _ in range(500):
            data = bytes([1]) + bytes(rng.randrange(256) for _ in range(rng.randrange(40)))
            try:
                self.S.decode(data)
            except LipiError:
                pass

    def test_length_limit(self):
        s = RecordSchema([("s", "str")], max_len=4)
        with self.assertRaises(LipiError):
            s.encode({"s": "toolong"})
        with self.assertRaises(LipiError):
            s.decode(bytes([1]) + encode_uvarint(10) + b"x" * 10)


class CodecTests(unittest.TestCase):
    def test_roundtrip_and_bounds(self):
        data = b"abc" * 1000
        for name in ("none", "zlib", "bz2", "lzma"):
            c = get_codec(name)
            self.assertEqual(c.decompress(c.compress(data), len(data)), data)
            with self.assertRaises(LipiError):
                c.decompress(c.compress(data), len(data) - 1)      # size bound
        with self.assertRaises(LipiError):
            get_codec("zlib").decompress(b"garbage", 100)
        with self.assertRaises(LipiError):
            get_codec("zlib").decompress(get_codec("zlib").compress(data)[:-5], 10 ** 6)   # truncated
        with self.assertRaises(LipiError):
            get_codec("nope")

    def test_zip_bomb_is_bounded(self):
        bomb = get_codec("zlib").compress(b"\x00" * 50_000_000)
        with self.assertRaises(LipiError):
            get_codec("zlib").decompress(bomb, 1000)

    def test_benchmark(self):
        rows = benchmark([b"sensor,reading,12.5\n" * 500], repeat=1)
        by = {r["codec"]: r for r in rows}
        self.assertEqual(by["none"]["ratio"], 1.0)
        self.assertGreater(by["zlib"]["ratio"], 5)


class BlockFileTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.p = Path(self._t.name) / "f.lipi"
        self.recs = [f"record-{i}".encode() * (1 + i % 7) for i in range(1000)]

    def tearDown(self):
        self._t.cleanup()

    def write(self, codec="zlib", block_bytes=2000):
        with BlockWriter(self.p, codec, block_bytes) as w:
            for i, r in enumerate(self.recs):
                self.assertEqual(w.append(r), i)

    def test_random_access_and_iteration(self):
        for codec in ("zlib", "lzma", "none"):
            if self.p.exists(): self.p.unlink()
            self.write(codec)
            with BlockReader(self.p) as r:
                self.assertFalse(r.recovered)
                self.assertEqual(len(r), 1000)
                for i in (0, 1, 499, 500, 999):
                    self.assertEqual(r.get(i), self.recs[i])
                self.assertEqual(list(r), self.recs)
                self.assertEqual(r.verify()[1], [])
                with self.assertRaises(IndexError):
                    r.get(1000)

    def test_smaller_than_raw(self):
        self.write()
        self.assertLess(self.p.stat().st_size, sum(map(len, self.recs)) / 3)

    def test_missing_footer_recovers_complete_blocks(self):
        self.write()
        size = self.p.stat().st_size
        with open(self.p, "r+b") as f:
            f.truncate(size - 40)                    # chop the footer and part of the index
        with BlockReader(self.p) as r:
            self.assertTrue(r.recovered)
            self.assertEqual(list(r), self.recs[:len(r)])
            self.assertGreater(len(r), 900)

    def test_torn_last_block_detected(self):
        with BlockWriter(self.p, "zlib", 2000) as w:
            for rec in self.recs:
                w.append(rec)
        data = self.p.read_bytes()
        # find where the last block starts via a reader, then cut into it
        with BlockReader(self.p) as r:
            last_off = r._index[-1][0]
        self.p.write_bytes(data[:last_off + 20])
        with BlockReader(self.p) as r:
            self.assertTrue(r.recovered and r.truncated_tail)
            self.assertEqual(list(r), self.recs[:len(r)])

    def test_corrupt_block_found_by_verify_and_get_raises(self):
        self.write()
        data = bytearray(self.p.read_bytes())
        data[len(data) // 3] ^= 0xFF
        self.p.write_bytes(bytes(data))
        with BlockReader(self.p) as r:
            good, bad = r.verify()
            self.assertEqual(len(bad), 1)
            hit = [i for i in range(0, 1000, 50) if self._raises(r, i)]
            self.assertTrue(hit)

    @staticmethod
    def _raises(r, i):
        try:
            r.get(i)
            return False
        except LipiError:
            return True

    def test_empty_and_foreign_files(self):
        with BlockWriter(self.p) as w:
            pass
        with BlockReader(self.p) as r:
            self.assertEqual((len(r), list(r)), (0, []))
        self.p.write_bytes(b"not a lipi file")
        with self.assertRaises(LipiError):
            BlockReader(self.p)


if __name__ == "__main__":
    unittest.main()
