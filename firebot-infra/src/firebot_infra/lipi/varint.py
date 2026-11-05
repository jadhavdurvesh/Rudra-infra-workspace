from __future__ import annotations


class LipiError(ValueError):
    """Any malformed/oversized/corrupt input. Decoders raise only this."""


MAX_VARINT_BYTES = 10  # 64-bit


def encode_uvarint(n: int) -> bytes:
    if n < 0 or n >= 1 << 64:
        raise LipiError("uvarint out of range")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def decode_uvarint(buf: bytes | memoryview, pos: int = 0) -> tuple[int, int]:
    """Returns (value, new_pos)."""
    shift = value = 0
    for i in range(MAX_VARINT_BYTES):
        if pos + i >= len(buf):
            raise LipiError("truncated varint")
        b = buf[pos + i]
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            if value >= 1 << 64:
                raise LipiError("varint overflow")
            return value, pos + i + 1
        shift += 7
    raise LipiError("varint too long")


def zigzag(n: int) -> int:
    if not -(1 << 63) <= n < (1 << 63):
        raise LipiError("int out of 64-bit range")
    return (n << 1) if n >= 0 else ((-n) << 1) - 1


def unzigzag(u: int) -> int:
    return (u >> 1) if not u & 1 else -((u + 1) >> 1)
