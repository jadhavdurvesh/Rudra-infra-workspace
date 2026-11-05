"""OFFLINE fault injection on capture files, for building malformed-input test fixtures.
Nothing here transmits on a bus; active injection belongs on a controlled bench setup."""
from __future__ import annotations

import random
from dataclasses import replace

from .capture import Capture, Chunk


def _rebuild(cap: Capture, data: bytes) -> Capture:
    """Re-chunk `data` keeping each original chunk's timestamp where possible."""
    chunks, i = [], 0
    for c in cap.chunks:
        piece = data[i:i + len(c.data)]
        if piece:
            chunks.append(Chunk(piece, c.t, c.flags))
        i += len(c.data)
    if i < len(data):
        chunks.append(Chunk(data[i:], cap.chunks[-1].t if cap.chunks else None))
    return Capture(replace(cap.meta), chunks)


def truncate_tail(cap: Capture, n_bytes: int) -> Capture:
    raw = cap.raw()
    return _rebuild(cap, raw[:max(0, len(raw) - n_bytes)])


def flip_byte(cap: Capture, offset: int) -> Capture:
    raw = bytearray(cap.raw())
    raw[offset] ^= 0xFF
    return _rebuild(cap, bytes(raw))


def inject_noise(cap: Capture, offset: int, n: int = 3, seed: int = 0) -> Capture:
    rng = random.Random(seed)
    raw = cap.raw()
    return _rebuild(cap, raw[:offset] + bytes(rng.randrange(256) for _ in range(n)) + raw[offset:])


def drop_chunk(cap: Capture, index: int) -> Capture:
    """Simulates capture-side loss: the chunk disappears and the loss is declared in the metadata."""
    meta = replace(cap.meta, dropped_samples=cap.meta.dropped_samples + len(cap.chunks[index].data))
    return Capture(meta, [c for i, c in enumerate(cap.chunks) if i != index])
