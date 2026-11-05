"""AKSHARA recorder: non-blocking, bounded, append-only, checksummed.

Record format (one per line):  <crc32 hex>\t<compact json>\n
Segments:  seg-000001.jsonl, seg-000002.jsonl, ...
Manifest:  manifest.json (complete=false while running, true after clean close)

Guarantees:
- record() never blocks and never raises; if the queue is full the event is
  dropped and counted. The robot is never slowed by the recorder.
- Dropped events leave a visible gap in `seq` and are counted in the manifest.
- Disk errors are counted; recording degrades, the caller is unaffected.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from ..events import Event


class Recorder:
    def __init__(
        self,
        root: str | os.PathLike,
        run_id: str,
        software_version: str = "",
        config: dict[str, Any] | None = None,
        queue_size: int = 10_000,
        segment_bytes: int = 4_000_000,
        fsync_interval_s: float = 1.0,
        tee: Callable[[Event], Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True)  # refuses to reuse an existing run
        self._private(self.dir, 0o700)   # recordings can hold sensitive payloads: owner-only access
        self.software_version = software_version
        self._tee = tee            # e.g. ANANTA Connector.log; runs on the writer thread, errors are contained
        self.tee_errors = 0
        self.config = dict(config or {})
        self._segment_bytes = segment_bytes
        self._fsync_interval = fsync_interval_s
        self._q: queue.Queue = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._seq = 0
        self._dropped = 0
        self._written = 0
        self._write_errors = 0
        self._closed = False
        self._segments: list[dict[str, Any]] = []
        self._seg_index = 0
        self._fh = None
        self._cur_name = ""
        self._seg_size = 0
        self._seg_records = 0
        self._started_wall = time.time()
        self._started_mono_ns = time.monotonic_ns()
        self._last_sync = time.monotonic()
        self._write_manifest(complete=False)
        self._thread = threading.Thread(target=self._run, name="akshara-writer", daemon=True)
        self._thread.start()

    # ---- public API -------------------------------------------------
    def record(self, event: Event) -> bool:
        """Queue an event. Returns False if it was dropped. Never blocks or raises."""
        with self._lock:
            self._seq += 1
            if self._closed:
                self._dropped += 1
                return False
            try:
                self._q.put_nowait((self._seq, event))
                return True
            except queue.Full:
                self._dropped += 1
                return False

    def emit(self, subsystem: str, event_type: str, severity: str = "info", **payload: Any) -> bool:
        return self.record(Event(self.run_id, subsystem, event_type, severity, payload))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._q.put(None)  # sentinel; writer is draining so this cannot deadlock
        self._thread.join()
        while True:  # anything that raced in behind the sentinel
            try:
                self._q.get_nowait()
                self._dropped += 1
            except queue.Empty:
                break
        try:
            self._close_segment()
        except OSError:
            self._write_errors += 1
        self._write_manifest(complete=True)

    def __enter__(self) -> "Recorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- writer thread ----------------------------------------------
    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                self._maybe_sync()
                continue
            if item is None:
                return
            self._write(*item)
            self._maybe_sync()

    def _write(self, seq: int, ev: Event) -> None:
        ev = replace(
            ev,
            seq=seq,
            run_id=self.run_id,
            software_version=ev.software_version or self.software_version,
        )
        body = json.dumps(ev.to_dict(), separators=(",", ":"), sort_keys=True, default=str)
        line = f"{zlib.crc32(body.encode()):08x}\t{body}\n".encode()
        size_before = self._seg_size
        try:
            if self._fh is None or (self._seg_size and self._seg_size + len(line) > self._segment_bytes):
                self._close_segment()
                self._open_segment()
                size_before = self._seg_size
            self._fh.write(line)
            self._fh.flush()          # hand the record to the OS now: a process crash can no longer lose it
            self._seg_size += len(line)
            self._seg_records += 1
            self._written += 1
        except OSError:
            self._write_errors += 1  # e.g. disk full: degrade, never crash the caller
            self._rollback(size_before)
        if self._tee is not None:
            try:
                self._tee(ev)
            except Exception:
                self.tee_errors += 1

    @staticmethod
    def _private(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass                      # e.g. a filesystem without permissions: the data policy doc says to secure the folder

    def _rollback(self, size: int) -> None:
        """A failed write may have left part of a record behind: cut the segment back to its last whole record."""
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.close()
        except OSError:
            pass
        path = self.dir / self._cur_name
        try:
            os.truncate(path, size)
            self._fh = open(path, "ab")
            self._seg_size = size
        except OSError:
            self._fh = None           # the next record starts a fresh segment

    def _open_segment(self) -> None:
        self._seg_index += 1
        self._cur_name = f"seg-{self._seg_index:06d}.jsonl"
        self._fh = open(self.dir / self._cur_name, "ab")
        self._private(self.dir / self._cur_name, 0o600)
        self._seg_size = 0
        self._seg_records = 0

    def _close_segment(self) -> None:
        if self._fh is None:
            return
        fh, self._fh = self._fh, None
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()
        sha = hashlib.sha256((self.dir / self._cur_name).read_bytes()).hexdigest()
        self._segments.append({"name": self._cur_name, "sha256": sha, "records": self._seg_records})

    def _maybe_sync(self) -> None:
        if self._fh is not None and time.monotonic() - self._last_sync >= self._fsync_interval:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError:
                self._write_errors += 1
            self._last_sync = time.monotonic()

    def _write_manifest(self, complete: bool) -> None:
        m = {
            "format": "akshara-1",
            "run_id": self.run_id,
            "software_version": self.software_version,
            "config": self.config,
            "started_wall": self._started_wall,
            "started_mono_ns": self._started_mono_ns,
            "complete": complete,
            "segments": list(self._segments),
            "events_written": self._written,
            "events_dropped": self._dropped,
            "write_errors": self._write_errors,
            "last_seq_assigned": self._seq,
            "clock_note": "wall time from one host's time.time(); ordering uses seq and mono_ns; no cross-device sync",
        }
        tmp = self.dir / "manifest.json.tmp"
        tmp.write_text(json.dumps(m, indent=2))
        self._private(tmp, 0o600)
        os.replace(tmp, self.dir / "manifest.json")
