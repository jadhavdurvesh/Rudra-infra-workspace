"""ANANTA connector: event -> privacy filter -> local spool -> batched, retried delivery.

Fire Bot (or any other workload) only calls log(). Delivery failures never reach the caller.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

from ..events import Event
from .sinks import Sink
from .spool import Spool

DEFAULT_DENY = frozenset({
    "audio", "voice", "speech", "transcript", "text", "message", "image", "images", "camera_frame",
    "frame_data", "lat", "lon", "lng", "latitude", "longitude", "gps", "location", "address",
})
MAX_META_KEYS = 20
MAX_STR = 200


def scrub(meta: Any, deny: frozenset[str] = DEFAULT_DENY, _depth: int = 0) -> Any:
    """Drop sensitive keys, bound string/key sizes, keep metadata small and flat-ish."""
    if isinstance(meta, dict):
        out = {}
        for k, v in meta.items():
            if str(k).lower() in deny:
                continue
            if len(out) >= MAX_META_KEYS:
                break
            out[str(k)] = scrub(v, deny, _depth + 1) if _depth < 2 else None
        return out
    if isinstance(meta, str):
        return meta if len(meta) <= MAX_STR else meta[:MAX_STR] + "...[truncated]"
    if isinstance(meta, (int, float, bool)) or meta is None:
        return meta
    if isinstance(meta, (list, tuple)):
        return [scrub(x, deny, _depth + 1) for x in list(meta)[:20]] if _depth < 2 else None
    return str(meta)[:MAX_STR]


def pseudonymize(device_id: str, salt: str) -> str:
    return hmac.new(salt.encode(), device_id.encode(), hashlib.sha256).hexdigest()[:16]


def to_row(ev: Event, device_pseudonym: str = "", deny: frozenset[str] = DEFAULT_DENY) -> dict[str, Any]:
    meta = scrub(ev.payload, deny)
    success = meta.pop("success", None)
    duration = meta.pop("duration_ms", None)
    return {
        "event_id": ev.event_id,
        "event_time": ev.event_time,
        "monotonic_ms": ev.mono_ns // 1_000_000,
        "subsystem": ev.subsystem,
        "event_type": ev.event_type,
        "severity": ev.severity,
        "run_id": ev.run_id,
        "software_version": ev.software_version,
        "device_id": device_pseudonym,
        "success": bool(success) if success is not None else None,
        "duration_ms": float(duration) if isinstance(duration, (int, float)) else None,
        "metadata": meta,
    }


@dataclass
class FlushStats:
    sent_batches: int = 0
    sent_rows: int = 0
    failed_batches: int = 0
    last_error: str = ""


class Connector:
    def __init__(self, spool: Spool, sink: Sink, batch_size: int = 500, device_id: str = "",
                 salt: str = "", deny_keys: frozenset[str] = DEFAULT_DENY) -> None:
        self.spool, self.sink, self.batch_size = spool, sink, batch_size
        self.device_pseudonym = pseudonymize(device_id, salt) if device_id else ""
        self.deny = deny_keys
        self.consecutive_failures = 0

    def log(self, ev: Event) -> bool:
        """Non-blocking from the caller's view: never raises. False means the row was dropped."""
        try:
            return self.spool.append(to_row(ev, self.device_pseudonym, self.deny))
        except Exception:
            self.spool.dropped += 1
            return False

    def flush(self, max_batches: int = 10) -> FlushStats:
        st = FlushStats()
        for _ in range(max_batches):
            rows, new_offset = self.spool.read_batch(self.batch_size)
            if not rows:
                if new_offset:
                    self.spool.commit(new_offset)  # only corrupt lines were consumed
                break
            batch_id = hashlib.sha256("\n".join(str(r.get("event_id")) for r in rows).encode()).hexdigest()[:32]
            try:
                self.sink.send(batch_id, rows)
            except Exception as e:  # sink down, network error, etc: keep the batch queued
                st.failed_batches += 1
                st.last_error = f"{type(e).__name__}: {e}"
                self.consecutive_failures += 1
                break
            self.spool.commit(new_offset)
            self.consecutive_failures = 0
            st.sent_batches += 1
            st.sent_rows += len(rows)
        return st

    def backoff_s(self, base: float = 0.5, cap: float = 60.0) -> float:
        return min(cap, base * (2 ** min(self.consecutive_failures, 10)))
