from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class Sink(Protocol):
    def send(self, batch_id: str, rows: list[dict[str, Any]]) -> None:
        """Deliver rows or raise. Must be safe to call again with the same batch."""


class JsonlFileSink:
    """Local fallback / dev sink: appends rows to a JSONL file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def send(self, batch_id: str, rows: list[dict[str, Any]]) -> None:
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":"), sort_keys=True) + "\n")


class DedupSink:
    """Wraps a sink; drops duplicate batches and duplicate event_ids (bounded memory)."""

    def __init__(self, inner: Sink, window: int = 100_000) -> None:
        self.inner = inner
        self.window = window
        self._batches: OrderedDict[str, None] = OrderedDict()
        self._events: OrderedDict[str, None] = OrderedDict()
        self.duplicates = 0

    def send(self, batch_id: str, rows: list[dict[str, Any]]) -> None:
        if batch_id in self._batches:
            self.duplicates += len(rows)
            return
        fresh = []
        for r in rows:
            eid = r.get("event_id")
            if eid is not None and eid in self._events:
                self.duplicates += 1
            else:
                fresh.append(r)
        if fresh:
            self.inner.send(batch_id, fresh)
        self._batches[batch_id] = None
        for r in fresh:
            if r.get("event_id") is not None:
                self._events[r["event_id"]] = None
        while len(self._batches) > self.window:
            self._batches.popitem(last=False)
        while len(self._events) > self.window:
            self._events.popitem(last=False)


class ClickHouseHttpSink:
    """Inserts JSONEachRow over ClickHouse's HTTP interface.

    batch_id is passed as insert_deduplication_token so retried batches are
    de-duplicated by ClickHouse (for tables where block deduplication is enabled).
    Raises on any failure so the connector keeps the batch queued.
    """

    def __init__(self, url: str, table: str = "firebot_events", database: str | None = None,
                 user: str | None = None, password: str | None = None, timeout: float = 5.0) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise ValueError("invalid table name")
        self.url, self.table, self.database = url.rstrip("/"), table, database
        self.user, self.password, self.timeout = user, password, timeout

    def send(self, batch_id: str, rows: list[dict[str, Any]]) -> None:
        params = {"query": f"INSERT INTO {self.table} FORMAT JSONEachRow",
                  "insert_deduplication_token": batch_id}
        if self.database:
            params["database"] = self.database
        headers = {}
        if self.user:
            headers["X-ClickHouse-User"] = self.user
            headers["X-ClickHouse-Key"] = self.password or ""
        body = "\n".join(json.dumps(r, separators=(",", ":")) for r in rows).encode()
        req = Request(f"{self.url}/?{urlencode(params)}", data=body, method="POST", headers=headers)
        with urlopen(req, timeout=self.timeout) as resp:
            resp.read()
