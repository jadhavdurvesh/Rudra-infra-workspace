"""Tiny schema-driven binary record codec (varints + fixed floats). Strict: any malformed input -> LipiError."""
from __future__ import annotations

import struct
from typing import Any

from .varint import LipiError, decode_uvarint, encode_uvarint, unzigzag, zigzag

TYPES = ("u", "i", "f32", "f64", "str", "bytes", "bool")


class RecordSchema:
    def __init__(self, fields: list[tuple[str, str]], version: int = 1, optional: tuple[str, ...] = (),
                 max_len: int = 1 << 20) -> None:
        names = [n for n, _ in fields]
        if len(set(names)) != len(names):
            raise ValueError("duplicate field names")
        for _, t in fields:
            if t not in TYPES:
                raise ValueError(f"unknown type {t!r}")
        if not set(optional) <= set(names):
            raise ValueError("optional names must be fields")
        if not 0 <= version < 256:
            raise ValueError("version must fit one byte")
        self.fields, self.version, self.max_len = fields, version, max_len
        self.optional = [n for n in names if n in optional]

    def encode(self, rec: dict[str, Any]) -> bytes:
        extra = set(rec) - {n for n, _ in self.fields}
        if extra:
            raise LipiError(f"unexpected fields {sorted(extra)}")
        out = bytearray([self.version])
        if self.optional:
            mask = sum(1 << i for i, n in enumerate(self.optional) if rec.get(n) is not None)
            out += encode_uvarint(mask)
        for name, typ in self.fields:
            v = rec.get(name)
            if v is None:
                if name in self.optional:
                    continue
                raise LipiError(f"missing field {name!r}")
            out += self._enc(typ, v, name)
        return bytes(out)

    def _enc(self, typ: str, v: Any, name: str) -> bytes:
        try:
            if typ == "u":
                if isinstance(v, bool): raise TypeError
                return encode_uvarint(v)
            if typ == "i":
                if isinstance(v, bool): raise TypeError
                return encode_uvarint(zigzag(v))
            if typ == "f32": return struct.pack(">f", v)
            if typ == "f64": return struct.pack(">d", v)
            if typ == "bool":
                if not isinstance(v, bool): raise TypeError
                return b"\x01" if v else b"\x00"
            if typ == "str":
                if not isinstance(v, str): raise TypeError
                raw = v.encode("utf-8")
            else:
                if not isinstance(v, (bytes, bytearray, memoryview)): raise TypeError   # bytes(5) would silently make zeros
                raw = bytes(v)
            if len(raw) > self.max_len:
                raise LipiError(f"field {name!r} too long")
            return encode_uvarint(len(raw)) + raw
        except (TypeError, struct.error, OverflowError):
            raise LipiError(f"bad value for field {name!r}") from None

    def decode(self, data: bytes) -> dict[str, Any]:
        if not data or data[0] != self.version:
            raise LipiError("schema version mismatch")
        pos, mask = 1, 0
        if self.optional:
            mask, pos = decode_uvarint(data, pos)
            if mask >> len(self.optional):
                raise LipiError("bad presence mask")
        out: dict[str, Any] = {}
        for name, typ in self.fields:
            if name in self.optional and not mask >> self.optional.index(name) & 1:
                out[name] = None
                continue
            out[name], pos = self._dec(typ, data, pos)
        if pos != len(data):
            raise LipiError("trailing bytes")
        return out

    def _dec(self, typ: str, d: bytes, pos: int) -> tuple[Any, int]:
        if typ == "u":
            return decode_uvarint(d, pos)
        if typ == "i":
            u, pos = decode_uvarint(d, pos)
            return unzigzag(u), pos
        if typ in ("f32", "f64"):
            n = 4 if typ == "f32" else 8
            if pos + n > len(d): raise LipiError("truncated float")
            return struct.unpack(">f" if typ == "f32" else ">d", d[pos:pos + n])[0], pos + n
        if typ == "bool":
            if pos >= len(d) or d[pos] > 1: raise LipiError("bad bool")
            return bool(d[pos]), pos + 1
        n, pos = decode_uvarint(d, pos)
        if n > self.max_len or pos + n > len(d):
            raise LipiError("bad length")
        raw = bytes(d[pos:pos + n])
        if typ == "bytes":
            return raw, pos + n
        try:
            return raw.decode("utf-8"), pos + n
        except UnicodeDecodeError:
            raise LipiError("bad utf-8") from None
