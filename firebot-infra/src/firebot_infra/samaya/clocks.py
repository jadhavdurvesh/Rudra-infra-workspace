"""SAMAYA: time and ordering across devices.

Rule of thumb from the dossier: use monotonic clocks locally; reach for these only when multi-device logs
need a common order. Nothing here claims perfect synchronisation: offsets always carry an uncertainty.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable


class ClockDriftError(Exception): ...


class LamportClock:
    def __init__(self) -> None:
        self.t = 0

    def tick(self) -> int:
        self.t += 1
        return self.t

    def update(self, remote: int) -> int:
        self.t = max(self.t, remote) + 1
        return self.t


class HybridLogicalClock:
    """HLC: timestamps (physical_ms, logical, node) that stay close to wall time AND respect causality.
    Compare timestamps as tuples."""

    MAX_LOGICAL = 0xFFFF

    def __init__(self, node_id: str, now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 max_drift_ms: int = 60_000) -> None:
        self.node, self._now, self.max_drift = node_id, now_ms, max_drift_ms
        self.l = 0
        self.c = 0

    def now(self) -> tuple[int, int, str]:
        """Local or send event."""
        pt = self._now()
        if pt > self.l:
            self.l, self.c = pt, 0
        else:
            self.c += 1
        return self._stamp()

    def update(self, remote: tuple[int, int, str]) -> tuple[int, int, str]:
        """Receive event. Rejects remote clocks too far ahead rather than letting them drag ours forward."""
        pt = self._now()
        rl, rc = remote[0], remote[1]
        if rl - pt > self.max_drift:
            raise ClockDriftError(f"remote clock {rl - pt} ms ahead (limit {self.max_drift})")
        new_l = max(self.l, rl, pt)
        if new_l == self.l == rl:
            c = max(self.c, rc) + 1
        elif new_l == self.l:
            c = self.c + 1
        elif new_l == rl:
            c = rc + 1
        else:
            c = 0
        self.l, self.c = new_l, c
        return self._stamp()

    def _stamp(self) -> tuple[int, int, str]:
        if self.c > self.MAX_LOGICAL:
            raise ClockDriftError("logical counter overflow (physical clock stuck?)")
        return (self.l, self.c, self.node)


class VectorClock:
    def __init__(self, counts: dict[str, int] | None = None) -> None:
        self.v = dict(counts or {})

    def tick(self, node: str) -> "VectorClock":
        self.v[node] = self.v.get(node, 0) + 1
        return self

    def merge(self, other: "VectorClock") -> "VectorClock":
        for k, n in other.v.items():
            self.v[k] = max(self.v.get(k, 0), n)
        return self

    def copy(self) -> "VectorClock":
        return VectorClock(self.v)

    def compare(self, other: "VectorClock") -> str:
        """equal | before | after | concurrent"""
        keys = set(self.v) | set(other.v)
        le = all(self.v.get(k, 0) <= other.v.get(k, 0) for k in keys)
        ge = all(self.v.get(k, 0) >= other.v.get(k, 0) for k in keys)
        if le and ge:
            return "equal"
        return "before" if le else "after" if ge else "concurrent"

    def __eq__(self, o: object) -> bool:
        return isinstance(o, VectorClock) and self.compare(o) == "equal"

    def __repr__(self) -> str:
        return f"VectorClock({self.v})"


class OffsetEstimator:
    """NTP-style offset estimate from request/response timestamps.

    Sample = (t0, t1, t2, t3): t0 local send, t1 remote receive, t2 remote send, t3 local receive.
    The sample with the smallest round-trip delay is the most trustworthy; uncertainty = delay / 2.
    """

    def __init__(self, window: int = 8) -> None:
        self._s: deque[tuple[float, float]] = deque(maxlen=window)   # (delay, offset)
        self.rejected = 0

    def add_sample(self, t0: float, t1: float, t2: float, t3: float) -> bool:
        delay = (t3 - t0) - (t2 - t1)
        if delay < 0 or t3 < t0 or t2 < t1:
            self.rejected += 1          # impossible timing: a clock stepped or timestamps are wrong
            return False
        self._s.append((delay, ((t1 - t0) + (t2 - t3)) / 2))
        return True

    @property
    def ready(self) -> bool:
        return bool(self._s)

    def _best(self) -> tuple[float, float]:
        if not self._s:
            raise ValueError("no samples")
        return min(self._s)

    @property
    def offset(self) -> float:
        """remote_time - local_time"""
        return self._best()[1]

    @property
    def uncertainty(self) -> float:
        return self._best()[0] / 2

    def remote_to_local(self, t_remote: float) -> tuple[float, float]:
        return t_remote - self.offset, self.uncertainty
