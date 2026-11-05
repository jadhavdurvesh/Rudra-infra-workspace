"""Durable, bounded, append-only local spool with a persisted read cursor.

The robot side only ever appends. If the spool is full or the disk fails, rows are
dropped and counted; the caller is never blocked and never sees an exception.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class Spool:
    def __init__(self, directory: str | os.PathLike, max_bytes: int = 50_000_000) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "spool.jsonl"
        self.cursor_path = self.dir / "cursor.json"
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self.dropped = 0
        self.write_errors = 0
        self.corrupt = 0

    def append(self, row: dict[str, Any]) -> bool:
        line = (json.dumps(row, separators=(",", ":"), sort_keys=True, default=str) + "\n").encode()
        with self._lock:
            size = 0
            try:
                size = self.path.stat().st_size if self.path.exists() else 0
                if size + len(line) > self.max_bytes:
                    self.dropped += 1
                    return False
                with open(self.path, "ab") as f:
                    f.write(line)
                return True
            except OSError:
                self.write_errors += 1
                self.dropped += 1
                try:
                    os.truncate(self.path, size)      # remove any partial row the failed write left behind
                except OSError:
                    pass
                return False

    def _cursor(self) -> int:
        try:
            return int(json.loads(self.cursor_path.read_text())["offset"])
        except (OSError, ValueError, KeyError):
            return 0

    def pending_bytes(self) -> int:
        with self._lock:
            size = self.path.stat().st_size if self.path.exists() else 0
            return max(0, size - self._cursor())

    def read_batch(self, max_rows: int) -> tuple[list[dict[str, Any]], int]:
        """Return (rows, new_offset). Only complete lines are consumed; bad lines are skipped and counted."""
        with self._lock:
            if not self.path.exists():
                return [], 0
            offset = self._cursor()
            rows: list[dict[str, Any]] = []
            with open(self.path, "rb") as f:
                f.seek(offset)
                while len(rows) < max_rows:
                    line = f.readline()
                    if not line or not line.endswith(b"\n"):
                        break  # EOF or a partial trailing line still being written
                    offset += len(line)
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        self.corrupt += 1
            return rows, offset

    def commit(self, new_offset: int) -> None:
        with self._lock:
            size = self.path.stat().st_size if self.path.exists() else 0
            if new_offset >= size:  # fully drained: compact
                with open(self.path, "wb"):
                    pass
                new_offset = 0
            tmp = self.cursor_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"offset": new_offset}))
            os.replace(tmp, self.cursor_path)
