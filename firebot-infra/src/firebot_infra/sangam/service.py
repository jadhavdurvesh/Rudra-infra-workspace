"""SANGAM sync over SETU: peers exchange version vectors, then push/pull only the journal entries the other lacks.

Messages (all idempotent, so SETU may retry them safely): sangam.hello, sangam.push, sangam.pull.
Bounded: every message carries at most max_entries entries and max_bytes of JSON; each sync has a round limit and
per-request timeouts, so an unreachable peer costs a bounded amount of time and never corrupts local state.
Run it from a background thread (BackgroundSync) so time-critical local work is never blocked by sync.

Authorization: map the three message types to capabilities in your KAVACH EnvelopeVerifier, e.g.
  {"sangam.hello": "sync_read", "sangam.pull": "sync_read", "sangam.push": "sync_write"}
and give Store(authorize=...) a per-type check for which origin peers may write which data types.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from ..setu import Node, SchemaError, SetuError
from .store import Store

PROTOCOL = 1


def register_schemas(node: Node) -> None:
    reg = node.registry
    for name, fields in (("sangam.hello", {"vv": dict, "v": int}), ("sangam.push", {"entries": list}),
                         ("sangam.pull", {"vv": dict, "max": int})):
        if not reg.has(name):
            reg.register(name, 1, fields)


def _fit(entries: list[dict], max_bytes: int) -> list[dict]:
    """Longest prefix of entries whose JSON fits max_bytes (at least one, so progress is always possible)."""
    out, size = [], 0
    for e in entries:
        n = len(json.dumps(e, separators=(",", ":")))
        if out and size + n > max_bytes:
            break
        out.append(e)
        size += n
    return out


class SyncService:
    """Answers a peer's sync requests. Create one per Store on the node that should be syncable."""

    def __init__(self, store: Store, node: Node, *, max_entries: int = 200, max_bytes: int = 32_000) -> None:
        self.store, self.node, self.max_entries, self.max_bytes = store, node, max_entries, max_bytes
        register_schemas(node)
        node.on("sangam.hello", self._hello)
        node.on("sangam.push", self._push)
        node.on("sangam.pull", self._pull)

    def _hello(self, env: Any) -> dict:
        return {"vv": self.store.version_vector(), "v": PROTOCOL}

    def _push(self, env: Any) -> dict:
        entries = env.payload["entries"]
        if len(entries) > self.max_entries * 2:
            return {"error": "too_many_entries", "vv": self.store.version_vector()}
        r = self.store.apply(entries, source_peer=env.source_id)
        return {"applied": r.applied, "duplicates": r.duplicates, "superseded": r.superseded, "expired": r.expired,
                "conflicts": r.conflicts, "rejected": [list(x) for x in r.rejected[:50]], "vv": self.store.version_vector()}

    def _pull(self, env: Any) -> dict:
        n = max(1, min(int(env.payload["max"]), self.max_entries))
        return {"entries": _fit(self.store.delta_for(env.payload["vv"], n), self.max_bytes), "vv": self.store.version_vector()}


@dataclass
class SyncReport:
    peer: str
    pushed: int = 0                 # entries we sent that the peer accepted as new
    pulled: int = 0                 # entries we received that were new to us
    conflicts_local: int = 0
    conflicts_remote: int = 0
    rejected_local: list = field(default_factory=list)
    rejected_remote: list = field(default_factory=list)
    rounds: int = 0
    converged: bool = False
    error: str = ""


class SyncClient:
    def __init__(self, store: Store, node: Node, *, timeout: float = 2.0, retries: int = 2, max_entries: int = 200,
                 max_bytes: int = 32_000, max_rounds: int = 50) -> None:
        self.store, self.node = store, node
        self.timeout, self.retries, self.max_entries, self.max_bytes, self.max_rounds = timeout, retries, max_entries, max_bytes, max_rounds
        register_schemas(node)

    def _req(self, peer: str, mtype: str, payload: dict) -> dict:
        return self.node.request(peer, mtype, payload, timeout=self.timeout, retries=self.retries, safe_to_retry=True).payload

    def sync_with(self, peer: str) -> SyncReport:
        rep = SyncReport(peer)
        try:
            peer_vv = self._req(peer, "sangam.hello", {"vv": self.store.version_vector(), "v": PROTOCOL})["vv"]
            for _ in range(self.max_rounds):
                rep.rounds += 1
                progressed = False
                out = _fit(self.store.delta_for(peer_vv, self.max_entries), self.max_bytes)
                if out:
                    r = self._req(peer, "sangam.push", {"entries": out})
                    if "error" in r:
                        rep.error = str(r["error"])
                        return rep
                    rep.pushed += r["applied"] + r["superseded"] + r["conflicts"] + r["expired"]
                    rep.conflicts_remote += r["conflicts"]
                    rep.rejected_remote += r["rejected"]
                    progressed |= r["vv"] != peer_vv
                    peer_vv = r["vv"]
                got = self._req(peer, "sangam.pull", {"vv": self.store.version_vector(), "max": self.max_entries})
                if got["entries"]:
                    before = self.store.version_vector()
                    res = self.store.apply(got["entries"], source_peer=peer)
                    rep.pulled += res.applied + res.superseded + res.conflicts + res.expired
                    rep.conflicts_local += res.conflicts
                    rep.rejected_local += res.rejected
                    progressed |= self.store.version_vector() != before
                    peer_vv = got["vv"]
                if not progressed:
                    rep.converged = not out and not got["entries"]
                    return rep
            rep.error = "max_rounds"
        except (SetuError, SchemaError) as e:
            rep.error = f"{type(e).__name__}: {e}"
        return rep


class BackgroundSync:
    """Periodically syncs with a list of peers on its own thread; the application is never blocked."""

    def __init__(self, client: SyncClient, peers: list[str], interval_s: float = 5.0) -> None:
        self.client, self.peers, self.interval = client, list(peers), interval_s
        self.last: dict[str, SyncReport] = {}
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, name="sangam-sync", daemon=True)

    def start(self) -> "BackgroundSync":
        self._t.start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            for p in self.peers:
                try:
                    self.last[p] = self.client.sync_with(p)
                except Exception as e:  # never let a bug kill the loop
                    self.last[p] = SyncReport(p, error=f"{type(e).__name__}: {e}")
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        self._t.join(timeout=2)
