"""ANANTA analytics: a reference backend (SQLite, standard library) that answers the engineering questions the
dossier lists, plus the ClickHouse schema for production use.

SqliteAnalytics is also a connector Sink: batches are applied idempotently (a repeated batch or a repeated
event_id changes nothing). Raw SQL goes through a read-only guard. Use ClickHouse when volume demands it;
this backend is the correctness baseline to compare against.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

COLUMNS = ("event_id", "event_time", "monotonic_ms", "subsystem", "event_type", "severity", "run_id",
           "software_version", "device_id", "success", "duration_ms", "metadata")

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, event_time REAL NOT NULL, monotonic_ms INTEGER, subsystem TEXT NOT NULL,
  event_type TEXT NOT NULL, severity TEXT, run_id TEXT, software_version TEXT, device_id TEXT,
  success INTEGER, duration_ms REAL, metadata TEXT);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events (subsystem, event_type);
CREATE INDEX IF NOT EXISTS ix_events_run ON events (run_id);
CREATE INDEX IF NOT EXISTS ix_events_time ON events (event_time);
CREATE TABLE IF NOT EXISTS batches (batch_id TEXT PRIMARY KEY);
"""

# Production schema. Columns match the connector's rows exactly. insert_deduplication_token (set by
# ClickHouseHttpSink) makes retried batches idempotent; keep non_replicated_deduplication_window > 0 on the table.
CLICKHOUSE_DDL = """
CREATE TABLE IF NOT EXISTS firebot_events
(
    event_id         String,
    event_time       Float64,
    monotonic_ms     Int64,
    subsystem        LowCardinality(String),
    event_type       LowCardinality(String),
    severity         LowCardinality(String),
    run_id           String,
    software_version LowCardinality(String),
    device_id        String,
    success          Nullable(Bool),
    duration_ms      Nullable(Float64),
    metadata         String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(toDateTime(toInt64(event_time)))
ORDER BY (subsystem, event_type, event_time)
SETTINGS non_replicated_deduplication_window = 1000
"""

_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


class SqliteAnalytics:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.RLock()
        self._db.executescript(_SQLITE_DDL)
        self.rejected_rows = 0
        self.duplicate_rows = 0
        self.duplicate_batches = 0

    # ---- Sink ----------------------------------------------------------------
    def send(self, batch_id: str, rows: list[dict[str, Any]]) -> None:
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,)).fetchone():
                self.duplicate_batches += 1
                return
            for r in rows:
                if not self._valid(r):
                    self.rejected_rows += 1
                    continue
                cur = self._db.execute(
                    f"INSERT OR IGNORE INTO events ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                    (r["event_id"], float(r["event_time"]), r.get("monotonic_ms"), r["subsystem"], r["event_type"],
                     r.get("severity"), r.get("run_id"), r.get("software_version"), r.get("device_id"),
                     None if r.get("success") is None else int(bool(r["success"])), r.get("duration_ms"),
                     json.dumps(r.get("metadata") or {}, sort_keys=True)))
                if cur.rowcount == 0:
                    self.duplicate_rows += 1
            self._db.execute("INSERT INTO batches VALUES (?)", (batch_id,))

    @staticmethod
    def _valid(r: Any) -> bool:
        return (isinstance(r, dict) and isinstance(r.get("event_id"), str) and isinstance(r.get("subsystem"), str)
                and isinstance(r.get("event_type"), str) and isinstance(r.get("event_time"), (int, float))
                and not isinstance(r.get("event_time"), bool))

    @classmethod
    def from_jsonl(cls, path: str | Path, path_db: str | Path = ":memory:") -> "SqliteAnalytics":
        db = cls(path_db)
        rows = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                db.rejected_rows += 1
        for i in range(0, len(rows), 500):
            db.send(f"jsonl-{i}", rows[i:i + 500])
        return db

    # ---- queries --------------------------------------------------------------
    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        """Read-only: a single SELECT/WITH statement. Anything that would write is refused by SQLite itself."""
        stripped = sql.strip().rstrip(";")
        if ";" in stripped or stripped.split(None, 1)[0].lower() not in ("select", "with"):
            raise ValueError("only a single SELECT statement is allowed")
        with self._lock:
            self._db.set_authorizer(lambda action, *a: sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY)
            try:
                cur = self._db.execute(stripped, tuple(params))
                names = [d[0] for d in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]
            except sqlite3.DatabaseError as e:
                raise ValueError(f"query refused: {e}") from e
            finally:
                self._db.set_authorizer(None)

    def search(self, *, subsystem: str | None = None, event_type: str | None = None, severity: str | None = None,
               run_id: str | None = None, since: float | None = None, until: float | None = None,
               limit: int = 100) -> list[dict[str, Any]]:
        where, args = [], []
        for col, val in (("subsystem", subsystem), ("event_type", event_type), ("severity", severity), ("run_id", run_id)):
            if val is not None:
                where.append(f"{col} = ?"); args.append(val)
        if since is not None:
            where.append("event_time >= ?"); args.append(since)
        if until is not None:
            where.append("event_time < ?"); args.append(until)
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY event_time LIMIT ?"
        rows = self.query(sql, [*args, int(limit)])
        for r in rows:
            r["metadata"] = json.loads(r["metadata"] or "{}")
        return rows

    def prune(self, before: float) -> int:
        """Retention: delete events older than `before` (epoch seconds). Returns how many were removed."""
        with self._lock, self._db:
            return self._db.execute("DELETE FROM events WHERE event_time < ?", (before,)).rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()
