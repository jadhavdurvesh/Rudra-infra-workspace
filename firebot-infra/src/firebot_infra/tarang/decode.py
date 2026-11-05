from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..framing import FrameParser, MAX_PAYLOAD
from ..setu.envelope import MalformedMessage, PayloadTooLarge, UnsupportedVersion, decode as setu_decode
from .capture import Capture, CaptureMeta, Chunk, DECODER_VERSION


class ProtocolDecoder(Protocol):
    """Modular application-protocol layer. Add I2C/SPI/CAN/Modbus byte-level decoders beside this later."""
    name: str

    def decode(self, payload: bytes) -> dict[str, Any] | None:
        """Return decoded fields, or None if the payload is not recognised."""


class RawDecoder:
    name = "raw"

    def decode(self, payload: bytes) -> dict[str, Any] | None:
        return {"hex": payload.hex(), "length": len(payload)}


class SetuDecoder:
    name = "setu"

    def decode(self, payload: bytes) -> dict[str, Any] | None:
        try:
            e = setu_decode(payload)
        except (MalformedMessage, UnsupportedVersion, PayloadTooLarge):
            return None
        return {"message_type": e.message_type, "kind": e.kind, "source": e.source_id,
                "destination": e.destination_id, "message_id": e.message_id, "correlation_id": e.correlation_id}


@dataclass(frozen=True)
class DecodedFrame:
    offset: int
    length: int
    t: float | None
    payload: bytes
    decoded: dict[str, Any] | None


@dataclass(frozen=True)
class Annotation:
    kind: str     # garbage | bad_crc | oversize | truncated | line_error | unrecognised_payload
    offset: int
    length: int
    t: float | None = None
    detail: str = ""


@dataclass
class DecodeReport:
    meta: CaptureMeta
    frames: list[DecodedFrame] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    total_bytes: int = 0
    capture_loss: bool = False            # adapter reported dropped samples: results are incomplete
    timestamps: str = "ok"                # ok | none (declared) | missing | non_monotonic
    frame_gap_s: dict[str, float] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.annotations and not self.capture_loss and self.timestamps in ("ok", "none")

    def count(self, kind: str) -> int:
        return sum(1 for a in self.annotations if a.kind == kind)

    def unknown_bytes(self) -> int:
        return sum(a.length for a in self.annotations if a.kind in ("garbage", "oversize", "truncated"))


def decode_capture(cap: Capture, decoder: ProtocolDecoder | None = None, max_payload: int = MAX_PAYLOAD) -> DecodeReport:
    rep = DecodeReport(meta=cap.meta, total_bytes=len(cap.raw()), capture_loss=cap.meta.dropped_samples > 0)
    starts, pos = [], 0
    for c in cap.chunks:
        starts.append(pos)
        pos += len(c.data)

    def t_at(offset: int) -> float | None:
        if not starts:
            return None
        return cap.chunks[max(0, bisect.bisect_right(starts, offset) - 1)].t

    ts = [c.t for c in cap.chunks]
    if cap.chunks and all(t is None for t in ts) and cap.meta.timestamp_basis == "none":
        rep.timestamps = "none"            # declared: this capture carries no timing (still visible in the report)
    elif cap.chunks and any(t is None for t in ts):
        rep.timestamps = "missing"
    elif any(b < a for a, b in zip(ts, ts[1:])):  # type: ignore[operator]
        rep.timestamps = "non_monotonic"
    for c, start in zip(cap.chunks, starts):
        if c.flags:
            rep.annotations.append(Annotation("line_error", start, len(c.data), c.t, ",".join(c.flags)))

    parser = FrameParser(max_payload)
    events = []
    for c in cap.chunks:
        events += parser.feed(c.data)
    events += parser.finish()
    for ev in events:
        if ev.kind == "frame":
            d = decoder.decode(ev.payload) if decoder else None  # type: ignore[arg-type]
            rep.frames.append(DecodedFrame(ev.offset, ev.length, t_at(ev.offset), ev.payload, d))  # type: ignore[arg-type]
            if decoder and d is None:
                rep.annotations.append(Annotation("unrecognised_payload", ev.offset, ev.length, t_at(ev.offset),
                                                  f"not a valid {decoder.name} message"))
        else:
            rep.annotations.append(Annotation(ev.kind, ev.offset, ev.length, t_at(ev.offset), ev.detail))
    rep.annotations.sort(key=lambda a: (a.offset, a.kind))
    if rep.timestamps == "ok":
        times = [f.t for f in rep.frames if f.t is not None]
        gaps = [b - a for a, b in zip(times, times[1:])]
        if gaps:
            rep.frame_gap_s = {"min": min(gaps), "max": max(gaps), "mean": sum(gaps) / len(gaps)}
    return rep


# ---- regression fixtures -------------------------------------------------
def export_fixture(cap: Capture, path: str | Path, decoder: ProtocolDecoder | None = None) -> None:
    rep = decode_capture(cap, decoder)
    doc = {"format": "tarang-fixture-1", "decoder": decoder.name if decoder else None,
           "capture": {"meta": cap.meta.__dict__,
                       "chunks": [{"t": c.t, "hex": c.data.hex(), "flags": list(c.flags)} for c in cap.chunks]},
           "expected": {"frames": [f.payload.hex() for f in rep.frames],
                        "annotations": [[a.kind, a.offset, a.length] for a in rep.annotations]}}
    Path(path).write_text(json.dumps(doc, indent=1))


def run_fixture(path: str | Path, decoder: ProtocolDecoder | None = None) -> list[str]:
    """Re-decode a stored fixture; returns human-readable mismatches (empty list = pass)."""
    doc = json.loads(Path(path).read_text())
    cap = Capture(CaptureMeta(**doc["capture"]["meta"]),
                  [Chunk(bytes.fromhex(c["hex"]), c["t"], tuple(c["flags"])) for c in doc["capture"]["chunks"]])
    rep = decode_capture(cap, decoder)
    problems = []
    got_frames = [f.payload.hex() for f in rep.frames]
    if got_frames != doc["expected"]["frames"]:
        problems.append(f"frames differ: expected {len(doc['expected']['frames'])}, got {len(got_frames)}")
    got_ann = [[a.kind, a.offset, a.length] for a in rep.annotations]
    if got_ann != doc["expected"]["annotations"]:
        problems.append(f"annotations differ: expected {doc['expected']['annotations']}, got {got_ann}")
    return problems
