from __future__ import annotations

import time
from typing import Iterable

from .codecs import get_codec


def benchmark(samples: list[bytes], codecs: Iterable[str] = ("none", "zlib", "bz2", "lzma"), repeat: int = 3) -> list[dict]:
    """Measure ratio and speed on YOUR data before choosing (or building) a format."""
    data = b"".join(samples)
    out = []
    for name in codecs:
        c = get_codec(name)
        t0 = time.perf_counter()
        for _ in range(repeat):
            comp = c.compress(data)
        enc = (time.perf_counter() - t0) / repeat
        t0 = time.perf_counter()
        for _ in range(repeat):
            c.decompress(comp, len(data))
        dec = (time.perf_counter() - t0) / repeat
        mb = len(data) / 1e6
        out.append({"codec": name, "ratio": len(data) / max(1, len(comp)), "compressed_bytes": len(comp),
                    "enc_mb_s": mb / max(enc, 1e-9), "dec_mb_s": mb / max(dec, 1e-9)})
    return out
