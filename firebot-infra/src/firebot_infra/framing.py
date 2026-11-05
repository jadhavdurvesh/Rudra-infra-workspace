"""Byte-stream framing for serial-style links.

Frame:  0x7E | LEN (2 bytes, big-endian) | PAYLOAD | CRC32 (4 bytes, over LEN+PAYLOAD)
The parser resynchronises after garbage/corruption, reports every anomaly with its byte
offset, and never raises on any input.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass

SOF = 0x7E
MAX_PAYLOAD = 4096
OVERHEAD = 7


def encode_frame(payload: bytes) -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large for 16-bit length")
    body = len(payload).to_bytes(2, "big") + payload
    return bytes([SOF]) + body + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")


@dataclass(frozen=True)
class ParseEvent:
    kind: str                 # frame | garbage | bad_crc | oversize | truncated
    offset: int               # absolute byte offset in the stream
    length: int
    payload: bytes | None = None
    detail: str = ""


class FrameParser:
    def __init__(self, max_payload: int = MAX_PAYLOAD) -> None:
        self.max_payload = max_payload
        self._buf = bytearray()
        self._pos = 0          # absolute offset of _buf[0]
        self._covered = 0      # bytes already attributed to an earlier event (avoid double reporting)

    def feed(self, data: bytes) -> list[ParseEvent]:
        self._buf += data
        return self._drain(final=False)

    def finish(self) -> list[ParseEvent]:
        """End of stream/capture: report any partial frame as truncated."""
        return self._drain(final=True)

    @property
    def has_partial(self) -> bool:
        """True while bytes are buffered waiting for the rest of a possible frame."""
        return bool(self._buf)

    def timeout(self) -> list[ParseEvent]:
        """Call when the line has gone idle mid-frame. A false SOF with a plausible length would otherwise
        hide real frames behind it; treat the pending SOF as noise and rescan what follows."""
        out: list[ParseEvent] = []
        if self._buf:
            out.append(ParseEvent("truncated", self._pos, 1, detail="idle timeout: SOF treated as noise"))
            self._covered = max(self._covered, self._pos + 1)
            del self._buf[:1]
            self._pos += 1
        return out + self._drain(final=False)

    def _garbage(self, out: list[ParseEvent], start: int, end: int) -> None:
        start = max(start, self._covered)
        if end > start:
            out.append(ParseEvent("garbage", start, end - start))
        self._covered = max(self._covered, end)

    def _drain(self, final: bool) -> list[ParseEvent]:
        out: list[ParseEvent] = []
        buf = self._buf
        while buf:
            i = buf.find(SOF)
            if i == -1:
                self._garbage(out, self._pos, self._pos + len(buf))
                self._pos += len(buf)
                buf.clear()
                break
            if i > 0:
                self._garbage(out, self._pos, self._pos + i)
                del buf[:i]
                self._pos += i
            if len(buf) < 3:
                if final:
                    out.append(ParseEvent("truncated", self._pos, len(buf)))
                    self._pos += len(buf)
                    buf.clear()
                break
            n = (buf[1] << 8) | buf[2]
            if n > self.max_payload:
                out.append(ParseEvent("oversize", self._pos, 1, detail=f"declared length {n}"))
                self._covered = max(self._covered, self._pos + 1)
                del buf[:1]
                self._pos += 1
                continue
            total = OVERHEAD + n
            if len(buf) < total:
                if final:
                    out.append(ParseEvent("truncated", self._pos, len(buf), detail=f"need {total} bytes"))
                    self._pos += len(buf)
                    buf.clear()
                break
            body = bytes(buf[1:3 + n])
            if (zlib.crc32(body) & 0xFFFFFFFF) == int.from_bytes(buf[3 + n:total], "big"):
                out.append(ParseEvent("frame", self._pos, total, payload=bytes(buf[3:3 + n])))
                del buf[:total]
                self._pos += total
                self._covered = max(self._covered, self._pos)
            else:
                out.append(ParseEvent("bad_crc", self._pos, total))
                self._covered = max(self._covered, self._pos + total)
                del buf[:1]    # the SOF may have been noise: rescan from the next byte
                self._pos += 1
        return out
