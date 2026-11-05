"""TARANG capture model. PASSIVE only: captures are data files; nothing here drives a bus."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DECODER_VERSION = "tarang-0.1"


@dataclass
class CaptureMeta:
    interface: str = "uart"
    config: dict[str, Any] = field(default_factory=dict)       # e.g. baud, parity, stop bits
    capture_device: str = "unknown"
    timestamp_basis: str = "none"                              # monotonic_s | wall_s | sample_index | none
    decoder_version: str = DECODER_VERSION
    dropped_samples: int = 0                                   # capture-side loss reported by the adapter
    notes: str = ""


@dataclass
class Chunk:
    data: bytes
    t: float | None = None
    flags: tuple[str, ...] = ()        # line-level errors reported by the capture adapter: framing_error, parity_error, overrun


@dataclass
class Capture:
    meta: CaptureMeta = field(default_factory=CaptureMeta)
    chunks: list[Chunk] = field(default_factory=list)

    def raw(self) -> bytes:
        return b"".join(c.data for c in self.chunks)

    @classmethod
    def from_bytes(cls, data: bytes, meta: CaptureMeta | None = None, chunk_size: int = 0,
                   t0: float | None = None, dt: float = 0.001) -> "Capture":
        size = chunk_size or max(1, len(data))
        chunks = [Chunk(data[i:i + size], None if t0 is None else t0 + (i // size) * dt)
                  for i in range(0, len(data), size)] if data else []
        m = meta or CaptureMeta()
        if t0 is not None and m.timestamp_basis == "none":
            m.timestamp_basis = "monotonic_s"
        return cls(m, chunks)


def save_capture(path: str | Path, cap: Capture) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "meta", "format": "tarang-1", **cap.meta.__dict__}) + "\n")
        for c in cap.chunks:
            f.write(json.dumps({"type": "chunk", "t": c.t, "hex": c.data.hex(), "flags": list(c.flags)}) + "\n")


def load_capture(path: str | Path) -> tuple[Capture, int]:
    """Returns (capture, bad_line_count). Malformed lines are skipped and counted, never fatal."""
    cap, bad = Capture(), 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            if d["type"] == "meta":
                d.pop("type"); d.pop("format", None)
                cap.meta = CaptureMeta(**d)
            elif d["type"] == "chunk":
                cap.chunks.append(Chunk(bytes.fromhex(d["hex"]), d.get("t"), tuple(d.get("flags", ()))))
            else:
                bad += 1
        except (ValueError, KeyError, TypeError):
            bad += 1
    return cap, bad
