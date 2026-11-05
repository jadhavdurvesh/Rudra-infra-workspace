"""Established codecs only (zlib/bz2/lzma from the standard library). LIPI does not invent a compression
algorithm: use bench.benchmark() on real data before deciding one is worth replacing.
Decompression is size-bounded so a corrupt or hostile block can't exhaust memory."""
from __future__ import annotations

import bz2
import lzma
import zlib
from typing import Protocol

from .varint import LipiError


class Codec(Protocol):
    name: str
    def compress(self, data: bytes) -> bytes: ...
    def decompress(self, data: bytes, max_size: int) -> bytes: ...


class NoneCodec:
    name = "none"
    def compress(self, data: bytes) -> bytes: return bytes(data)
    def decompress(self, data: bytes, max_size: int) -> bytes:
        if len(data) > max_size:
            raise LipiError("too large")
        return bytes(data)


class ZlibCodec:
    name = "zlib"
    def __init__(self, level: int = 6) -> None: self.level = level
    def compress(self, data: bytes) -> bytes: return zlib.compress(data, self.level)
    def decompress(self, data: bytes, max_size: int) -> bytes:
        try:
            d = zlib.decompressobj()
            out = d.decompress(data, max_size + 1)
        except zlib.error as e:
            raise LipiError(f"zlib: {e}") from e
        if len(out) > max_size or d.unconsumed_tail:
            raise LipiError("decompressed size exceeds limit")
        if not d.eof:
            raise LipiError("zlib: truncated stream")
        return out


class Bz2Codec:
    name = "bz2"
    def compress(self, data: bytes) -> bytes: return bz2.compress(data)
    def decompress(self, data: bytes, max_size: int) -> bytes:
        try:
            d = bz2.BZ2Decompressor()
            out = d.decompress(data, max_size + 1)
        except (OSError, ValueError) as e:
            raise LipiError(f"bz2: {e}") from e
        if len(out) > max_size or not d.eof:
            raise LipiError("bz2: size limit or truncated")
        return out


class LzmaCodec:
    name = "lzma"
    def compress(self, data: bytes) -> bytes: return lzma.compress(data)
    def decompress(self, data: bytes, max_size: int) -> bytes:
        try:
            d = lzma.LZMADecompressor()
            out = d.decompress(data, max_size + 1)
        except lzma.LZMAError as e:
            raise LipiError(f"lzma: {e}") from e
        if len(out) > max_size or not d.eof:
            raise LipiError("lzma: size limit or truncated")
        return out


CODECS: dict[str, Codec] = {c.name: c for c in (NoneCodec(), ZlibCodec(), Bz2Codec(), LzmaCodec())}


def get_codec(name: str) -> Codec:
    try:
        return CODECS[name]
    except KeyError:
        raise LipiError(f"unknown codec {name!r}") from None
