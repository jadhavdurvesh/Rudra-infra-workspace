from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class CausalBuffer:
    """Causal delivery (Birman-Schiper-Stephenson). A message is delivered only after every message it
    causally depends on. msg stamp: the sender's vector with its own entry incremented for this message."""

    def __init__(self, max_pending: int = 1000) -> None:
        self.delivered: dict[str, int] = {}
        self._pending: list[tuple[str, dict[str, int], Any]] = []
        self.max_pending = max_pending
        self.duplicates = 0
        self.rejected_overflow = 0

    def pending(self) -> int:
        return len(self._pending)

    def _ready(self, sender: str, vc: dict[str, int]) -> bool:
        if vc.get(sender, 0) != self.delivered.get(sender, 0) + 1:
            return False
        return all(n <= self.delivered.get(k, 0) for k, n in vc.items() if k != sender)

    def receive(self, sender: str, vc: dict[str, int], payload: Any) -> list[tuple[str, Any]]:
        """Returns messages (sender, payload) that became deliverable, in causal order."""
        if vc.get(sender, 0) <= self.delivered.get(sender, 0):
            self.duplicates += 1
            return []
        if any(s == sender and v.get(sender) == vc.get(sender) for s, v, _ in self._pending):
            self.duplicates += 1
            return []
        if len(self._pending) >= self.max_pending and not self._ready(sender, vc):
            self.rejected_overflow += 1      # full: only a message that can be delivered right now is still accepted
            return []                        # (otherwise the very message everything waits for would be refused)
        self._pending.append((sender, dict(vc), payload))
        out: list[tuple[str, Any]] = []
        progressed = True
        while progressed:
            progressed = False
            for item in list(self._pending):
                s, v, p = item
                if self._ready(s, v):
                    self._pending.remove(item)
                    self.delivered[s] = v[s]
                    out.append((s, p))
                    progressed = True
        return out


@dataclass(frozen=True)
class MergedEvent:
    t: float                 # best estimate on the reference timeline
    uncertainty: float
    node: str
    payload: Any
    ambiguous: bool          # order vs. an event from another node cannot be trusted (uncertainty intervals overlap)


def merge_logs(streams: dict[str, list[tuple[float, Any]]],
               offsets: dict[str, tuple[float, float]] | None = None) -> list[MergedEvent]:
    """Merge per-device logs onto one timeline.

    streams: node -> [(local_time, payload)];  offsets: node -> (offset_to_reference, uncertainty) where
    reference_time = local_time - offset. Events whose uncertainty intervals overlap with another node's
    event are flagged ambiguous instead of pretending to a precise global order.
    """
    offsets = offsets or {}
    ev = []
    for node, rows in streams.items():
        off, unc = offsets.get(node, (0.0, 0.0))
        for t, p in rows:
            ev.append([t - off, unc, node, p, False])
    ev.sort(key=lambda e: (e[0], e[2]))
    for i, a in enumerate(ev):
        for b in ev[i + 1:]:
            if b[0] - a[0] > a[1] + b[1] + 1e-9:    # touching intervals count as overlapping (conservative)
                break
            if a[2] != b[2]:
                a[4] = b[4] = True
    return [MergedEvent(*e) for e in ev]
