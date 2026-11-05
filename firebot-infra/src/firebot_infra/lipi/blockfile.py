"""Block-compressed record file with random access.

Trade-off made explicit: records are compressed in blocks (better ratio than per-record) and an index lets get(i)
decompress only one block (cheaper than whole-file compression). If the footer is missing (crash), the reader
scans forward and recovers every complete, checksum-valid block.

Layout: MAGIC | namelen | codec name | blocks... | index | n_blocks u32 | crc32(index) u32 | END
Block : payload_len u32 | crc32(payload) u32 | n_records u32 | raw_len u32 | payload
        raw = (uvarint(len) + record)*
"""
from __future__ import annotations

import bisect
import os
import struct
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Iterator

from .codecs import Codec, get_codec
from .varint import LipiError, decode_uvarint, encode_uvarint

MAGIC, END = b"LIPIBLK1", b"LIPIEND1"
HDR = struct.Struct(">IIII")
IDX = struct.Struct(">QII")
FOOTER = struct.Struct(">II")
MAX_BLOCK_RAW = 64 << 20


def _crc(plen: int, n: int, raw_len: int, payload: bytes) -> int:
    """CRC over the header fields AND the payload, so a flipped length/count is detected too."""
    return zlib.crc32(struct.pack(">III", plen, n, raw_len) + payload) & 0xFFFFFFFF


def _parse_records(raw: bytes, n: int) -> list[bytes]:
    out, pos = [], 0
    for _ in range(n):
        ln, pos = decode_uvarint(raw, pos)
        if pos + ln > len(raw):
            raise LipiError("record overruns block")
        out.append(raw[pos:pos + ln])
        pos += ln
    if pos != len(raw):
        raise LipiError("trailing bytes in block")
    return out


class BlockWriter:
    def __init__(self, path: str | os.PathLike, codec: str = "zlib", block_bytes: int = 64 * 1024) -> None:
        self.codec: Codec = get_codec(codec)
        self.block_bytes = block_bytes
        self._f = open(path, "xb")
        name = codec.encode()
        self._f.write(MAGIC + bytes([len(name)]) + name)
        self._index: list[tuple[int, int, int]] = []
        self._buf = bytearray()
        self._n = 0
        self._count = 0
        self._closed = False

    def append(self, record: bytes) -> int:
        self._buf += encode_uvarint(len(record)) + record
        self._n += 1
        self._count += 1
        idx = self._count - 1
        if len(self._buf) >= self.block_bytes:
            self._flush()
        return idx

    def _flush(self) -> None:
        if not self._n:
            return
        raw = bytes(self._buf)
        comp = self.codec.compress(raw)
        off = self._f.tell()
        self._f.write(HDR.pack(len(comp), _crc(len(comp), self._n, len(raw), comp), self._n, len(raw)) + comp)
        self._index.append((off, self._count - self._n, self._n))
        self._buf.clear()
        self._n = 0

    def close(self) -> None:
        if self._closed:
            return
        self._flush()
        idx = b"".join(IDX.pack(*e) for e in self._index)
        self._f.write(idx + FOOTER.pack(len(self._index), zlib.crc32(idx) & 0xFFFFFFFF) + END)
        self._f.flush()
        os.fsync(self._f.fileno())
        self._f.close()
        self._closed = True

    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class BlockReader:
    def __init__(self, path: str | os.PathLike, cache_blocks: int = 4) -> None:
        self._f = open(path, "rb")
        if self._f.read(len(MAGIC)) != MAGIC:
            raise LipiError("not a LIPI block file")
        nl = self._f.read(1)
        if not nl:
            raise LipiError("truncated header")
        self.codec = get_codec(self._f.read(nl[0]).decode())
        self._data_start = self._f.tell()
        self._cache: OrderedDict[int, list[bytes]] = OrderedDict()
        self._cache_n = cache_blocks
        self.recovered = False          # True if the footer was missing/invalid and the index was rebuilt by scanning
        self.truncated_tail = False     # True if scanning stopped at a partial/corrupt block
        self._index = self._read_footer() or self._scan()
        self._firsts = [e[1] for e in self._index]
        self._total = sum(e[2] for e in self._index)

    def _read_footer(self) -> list[tuple[int, int, int]] | None:
        size = os.fstat(self._f.fileno()).st_size
        tail = FOOTER.size + len(END)
        if size < self._data_start + tail:
            return None
        self._f.seek(size - tail)
        raw = self._f.read(tail)
        if raw[FOOTER.size:] != END:
            return None
        n, crc = FOOTER.unpack(raw[:FOOTER.size])
        start = size - tail - n * IDX.size
        if start < self._data_start:
            return None
        self._f.seek(start)
        idx = self._f.read(n * IDX.size)
        if zlib.crc32(idx) & 0xFFFFFFFF != crc:
            return None
        return [IDX.unpack_from(idx, i * IDX.size) for i in range(n)]

    def _scan(self) -> list[tuple[int, int, int]]:
        self.recovered = True
        out, first = [], 0
        self._f.seek(self._data_start)
        while True:
            off = self._f.tell()
            h = self._f.read(HDR.size)
            if not h:
                break
            if len(h) < HDR.size:
                self.truncated_tail = True
                break
            plen, crc, n, raw_len = HDR.unpack(h)
            payload = self._f.read(plen) if plen <= MAX_BLOCK_RAW else b""
            if len(payload) != plen or _crc(plen, n, raw_len, payload) != crc or n == 0 or raw_len > MAX_BLOCK_RAW:
                self.truncated_tail = True
                break
            try:
                _parse_records(self.codec.decompress(payload, raw_len), n)
            except LipiError:
                self.truncated_tail = True
                break
            out.append((off, first, n))
            first += n
        return out

    def _block(self, b: int) -> list[bytes]:
        if b in self._cache:
            self._cache.move_to_end(b)
            return self._cache[b]
        off, _, n = self._index[b]
        self._f.seek(off)
        h = self._f.read(HDR.size)
        if len(h) < HDR.size:
            raise LipiError("truncated block header")
        plen, crc, hn, raw_len = HDR.unpack(h)
        payload = self._f.read(plen) if plen <= MAX_BLOCK_RAW else b""
        if len(payload) != plen or _crc(plen, hn, raw_len, payload) != crc:
            raise LipiError(f"block {b}: checksum mismatch")
        recs = _parse_records(self.codec.decompress(payload, min(raw_len, MAX_BLOCK_RAW)), hn)
        if hn != n:
            raise LipiError("block/index mismatch")
        self._cache[b] = recs
        if len(self._cache) > self._cache_n:
            self._cache.popitem(last=False)
        return recs

    def __len__(self) -> int:
        return self._total

    def get(self, i: int) -> bytes:
        if not 0 <= i < self._total:
            raise IndexError(i)
        b = bisect.bisect_right(self._firsts, i) - 1
        return self._block(b)[i - self._index[b][1]]

    def __iter__(self) -> Iterator[bytes]:
        for b in range(len(self._index)):
            yield from self._block(b)

    def verify(self) -> tuple[int, list[int]]:
        """(good_block_count, [bad block numbers])."""
        bad = []
        for b in range(len(self._index)):
            self._cache.pop(b, None)
            try:
                self._block(b)
            except LipiError:
                bad.append(b)
        return len(self._index) - len(bad), bad

    def close(self) -> None:
        self._f.close()

    def __enter__(self): return self
    def __exit__(self, *a): self.close()
