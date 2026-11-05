"""AKSHARA replay: read a run, verify it, and feed it to a simulator/test harness.

Replay only calls the handlers you give it. It never talks to hardware.
"""
from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..events import Event


@dataclass
class ReplayReport:
    manifest_found: bool = False
    complete: bool = False
    events_read: int = 0
    corrupt_records: int = 0
    unsupported_records: int = 0
    out_of_order: int = 0
    truncated_tail: bool = False
    segment_hash_mismatches: list[str] = field(default_factory=list)
    seq_gaps: list[tuple[int, int]] = field(default_factory=list)  # inclusive (first, last) missing seq
    declared_dropped: int = 0
    declared_write_errors: int = 0

    @property
    def clean(self) -> bool:
        return (
            self.manifest_found and self.complete and not self.corrupt_records
            and not self.unsupported_records and not self.out_of_order
            and not self.truncated_tail and not self.segment_hash_mismatches
            and not self.seq_gaps and not self.declared_dropped and not self.declared_write_errors
        )


def read_run(run_dir: str | Path) -> tuple[list[Event], ReplayReport]:
    d = Path(run_dir)
    rep = ReplayReport()
    manifest = None
    mp = d / "manifest.json"
    if mp.exists():
        try:
            manifest = json.loads(mp.read_text())
        except ValueError:
            manifest = None
    hashes: dict[str, str] = {}
    last_declared = 0
    if manifest:
        rep.manifest_found = True
        rep.complete = bool(manifest.get("complete"))
        rep.declared_dropped = int(manifest.get("events_dropped", 0))
        rep.declared_write_errors = int(manifest.get("write_errors", 0))
        last_declared = int(manifest.get("last_seq_assigned", 0))
        hashes = {s["name"]: s["sha256"] for s in manifest.get("segments", [])}

    events: list[Event] = []
    prev = 0
    segs = sorted(d.glob("seg-*.jsonl"))
    for i, seg in enumerate(segs):
        raw = seg.read_bytes()
        if seg.name in hashes and hashlib.sha256(raw).hexdigest() != hashes[seg.name]:
            rep.segment_hash_mismatches.append(seg.name)
        lines = raw.split(b"\n")
        tail = lines.pop()  # "" if the file ends with a newline, else a partial record
        if tail:
            if i == len(segs) - 1:
                rep.truncated_tail = True
            else:
                rep.corrupt_records += 1
        for line in lines:
            crc, sep, body = line.partition(b"\t")
            if not sep or f"{zlib.crc32(body):08x}".encode() != crc:
                rep.corrupt_records += 1
                continue
            try:
                ev = Event.from_dict(json.loads(body))
            except json.JSONDecodeError:
                rep.corrupt_records += 1
                continue
            except (ValueError, TypeError):
                rep.unsupported_records += 1
                continue
            if ev.seq <= prev:
                rep.out_of_order += 1
            elif ev.seq != prev + 1:
                rep.seq_gaps.append((prev + 1, ev.seq - 1))
            if ev.seq > prev:
                prev = ev.seq
            events.append(ev)
    if last_declared > prev:
        rep.seq_gaps.append((prev + 1, last_declared))  # tail loss
    rep.events_read = len(events)
    return events, rep


def replay(
    run_dir: str | Path,
    handlers: dict[str, Callable[[Event], None]],
    default: Callable[[Event], None] | None = None,
) -> ReplayReport:
    """Feed events, in recorded order, to per-subsystem handlers (simulator inputs)."""
    events, report = read_run(run_dir)
    for ev in events:
        h = handlers.get(ev.subsystem, default)
        if h:
            h(ev)
    return report
