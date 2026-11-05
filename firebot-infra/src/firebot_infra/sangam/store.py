"""SANGAM: offline state sync for NONCRITICAL records (logs, run summaries, maps, config).

Peers keep a per-peer change journal. Sync exchanges version vectors and ships only missing
entries (bounded batches). Each data type declares its conflict policy up front.
Operational/actuator command types are refused: stale commands must expire, not queue.
Entries may carry expires_at; expired entries are journaled (so sync converges) but never applied.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

FORBIDDEN_PREFIXES = ("command", "cmd", "actuat", "motor", "estop", "e_stop", "suppress", "emergency")


class Policy(str, Enum):
    APPEND_ONLY = "append_only"            # logs/summaries: unique keys, never conflict
    SINGLE_WRITER = "single_writer"        # one owner may write; others rejected
    LAST_WRITER_WINS = "last_writer_wins"  # deterministic (wall, peer) ordering; only where loss is acceptable
    REJECT_AND_REVIEW = "reject_and_review"  # concurrent edits surfaced; resolved explicitly


@dataclass(frozen=True)
class TypePolicy:
    name: str
    policy: Policy
    owner: str | None = None

    def __post_init__(self) -> None:
        if self.name.lower().startswith(FORBIDDEN_PREFIXES):
            raise ValueError(f"{self.name!r}: operational commands are not synchronisable")
        if self.policy == Policy.SINGLE_WRITER and not self.owner:
            raise ValueError("single_writer needs an owner")


class ConflictPending(Exception): ...


@dataclass
class SyncResult:
    applied: int = 0
    duplicates: int = 0
    superseded: int = 0
    expired: int = 0
    conflicts: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)   # (entry id, reason)

    def merge(self, o: "SyncResult") -> None:
        self.applied += o.applied; self.duplicates += o.duplicates; self.superseded += o.superseded
        self.expired += o.expired; self.conflicts += o.conflicts; self.rejected += o.rejected


def eid(e: dict[str, Any]) -> str:
    return f"{e['peer']}:{e['seq']}"


_REQUIRED = {"peer": str, "seq": int, "type": str, "key": str, "wall": (int, float)}


class Store:
    def __init__(self, peer_id: str, types: list[TypePolicy], path: str | os.PathLike | None = None,
                 authorize: Callable[[str, str], bool] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.peer_id = peer_id
        self._types = {t.name: t for t in types}
        self._authorize = authorize          # (origin_peer, type) -> bool; plug KAVACH policy in here
        self._clock = clock
        self._journal: dict[str, list[dict]] = {}
        self._heads: dict[tuple[str, str], dict] = {}
        self._lineage: dict[tuple[str, str], set[str]] = {}
        self.conflicts: list[dict] = []
        self.rejected: deque[tuple[str, str]] = deque(maxlen=500)
        self.last_sync: dict[str, float] = {}
        self._lock = threading.RLock()
        self.path = Path(path) if path else None
        if self.path and self.path.exists():
            self._load()

    # ---- local writes ---------------------------------------------------
    def put(self, type_: str, key: str | None, value: Any, ttl_s: float | None = None) -> dict:
        with self._lock:
            tp = self._type(type_)
            if tp.policy == Policy.SINGLE_WRITER and tp.owner != self.peer_id:
                raise PermissionError(f"{self.peer_id} is not the owner of {type_}")
            if self._authorize and not self._authorize(self.peer_id, type_):
                raise PermissionError("not authorized")
            seq = len(self._journal.get(self.peer_id, [])) + 1
            key = key if key is not None else f"{self.peer_id}:{seq}"
            hk = (type_, key)
            head = self._heads.get(hk)
            if tp.policy == Policy.APPEND_ONLY and head is not None:
                raise KeyError(f"append-only key {key!r} already exists")
            if tp.policy == Policy.REJECT_AND_REVIEW and self._conflicts_for(type_, key):
                raise ConflictPending(f"{type_}/{key} has unresolved conflicts; call resolve()")
            wall = self._clock()
            if tp.policy == Policy.LAST_WRITER_WINS and head is not None:
                wall = max(wall, head["wall"] + 1e-6)   # an intentional local write wins locally
            return self._commit_local(tp, hk, value, head, wall, ttl_s, [])

    def resolve(self, type_: str, key: str, value: Any) -> dict:
        with self._lock:
            tp = self._type(type_)
            if tp.policy != Policy.REJECT_AND_REVIEW:
                raise ValueError("only reject_and_review types have conflicts to resolve")
            hk = (type_, key)
            confs = self._conflicts_for(type_, key)
            if not confs:
                raise ValueError("nothing to resolve")
            head = self._heads.get(hk)
            ids = ([eid(head)] if head else []) + [eid(c["entry"]) for c in confs]
            return self._commit_local(tp, hk, value, head, self._clock(), None, ids)

    def _commit_local(self, tp: TypePolicy, hk: tuple[str, str], value: Any, head: dict | None,
                      wall: float, ttl_s: float | None, resolves: list[str]) -> dict:
        json.dumps(value)  # must be JSON-serialisable
        peer_journal = self._journal.setdefault(self.peer_id, [])
        e = {"peer": self.peer_id, "seq": len(peer_journal) + 1, "type": hk[0], "key": hk[1], "value": value,
             "base": eid(head) if head else None, "resolves": resolves, "wall": wall,
             "expires_at": None if ttl_s is None else wall + ttl_s}
        peer_journal.append(e)
        self._heads[hk] = e
        lin = self._lineage.setdefault(hk, set())
        lin.add(eid(e))
        if e["base"]:
            lin.add(e["base"])
        lin.update(resolves)
        self.conflicts = [c for c in self.conflicts
                          if not (c["type"] == hk[0] and c["key"] == hk[1] and eid(c["entry"]) in resolves)]
        self.save()
        return dict(e)

    # ---- reads ----------------------------------------------------------
    def get(self, type_: str, key: str) -> Any | None:
        with self._lock:
            e = self._heads.get((type_, key))
        if e is None or self._expired(e):
            return None
        return e["value"]

    def items(self, type_: str) -> dict[str, Any]:
        with self._lock:
            return {k: e["value"] for (t, k), e in self._heads.items() if t == type_ and not self._expired(e)}

    def version_vector(self) -> dict[str, int]:
        with self._lock:
            return {p: len(j) for p, j in self._journal.items()}

    def pending_for(self, remote_vv: dict[str, int]) -> int:
        """Sync lag: how many journal entries the remote peer lacks."""
        with self._lock:
            return sum(max(0, len(j) - remote_vv.get(p, 0)) for p, j in self._journal.items())

    def delta_for(self, remote_vv: dict[str, int], max_entries: int = 500) -> list[dict]:
        out: list[dict] = []
        with self._lock:
            for p in sorted(self._journal):
                for e in self._journal[p][remote_vv.get(p, 0):]:
                    if len(out) >= max_entries:
                        return out
                    out.append(dict(e))
        return out

    # ---- remote apply -----------------------------------------------------
    def apply(self, entries: list[dict], source_peer: str | None = None) -> SyncResult:
        res = SyncResult()
        with self._lock:
            for e in entries:
                self._apply_one(e, res)
            if source_peer:
                self.last_sync[source_peer] = self._clock()
            self.save()
        return res

    def _reject(self, res: SyncResult, e: Any, reason: str) -> None:
        ident = eid(e) if isinstance(e, dict) and "peer" in e and "seq" in e else "?"
        res.rejected.append((ident, reason))
        self.rejected.append((ident, reason))

    def _apply_one(self, e: Any, res: SyncResult) -> None:
        if not isinstance(e, dict) or any(not isinstance(e.get(k), t) or isinstance(e.get(k), bool)
                                           for k, t in _REQUIRED.items()):
            return self._reject(res, e, "malformed")
        tp = self._types.get(e["type"])
        if tp is None:
            return self._reject(res, e, "unknown_type")
        peer = e["peer"]
        have = len(self._journal.get(peer, []))
        if e["seq"] <= have:
            res.duplicates += 1
            return
        if peer == self.peer_id:
            return self._reject(res, e, "forged_origin")   # nobody else may extend our journal
        if e["seq"] != have + 1:
            return self._reject(res, e, "gap")
        if tp.policy == Policy.SINGLE_WRITER and peer != tp.owner:
            return self._reject(res, e, "not_owner")
        if self._authorize and not self._authorize(peer, e["type"]):
            return self._reject(res, e, "unauthorized")
        e = {**e, "resolves": list(e.get("resolves") or []), "base": e.get("base"), "expires_at": e.get("expires_at")}
        self._journal.setdefault(peer, []).append(e)    # journal first so sync converges
        if e["expires_at"] is not None and self._clock() > e["expires_at"]:
            res.expired += 1                             # never applied as current
            return
        outcome = self._apply_state(e, tp)
        if outcome == "applied":
            res.applied += 1
        elif outcome == "conflict":
            res.conflicts += 1
        else:
            res.superseded += 1

    def _apply_state(self, e: dict, tp: TypePolicy) -> str:
        hk = (e["type"], e["key"])
        head = self._heads.get(hk)
        lin = self._lineage.setdefault(hk, set())
        if tp.policy == Policy.SINGLE_WRITER:
            self._heads[hk] = e
            return "applied"
        if tp.policy == Policy.APPEND_ONLY:
            if head is None:
                self._heads[hk] = e
                return "applied"
            self._conflict(e)                             # same key, different entry: surface, never overwrite
            return "conflict"
        if tp.policy == Policy.LAST_WRITER_WINS:
            if head is None or (e["wall"], e["peer"]) > (head["wall"], head["peer"]):
                self._heads[hk] = e
                return "applied"
            return "superseded"
        # REJECT_AND_REVIEW
        if eid(e) in lin:                                 # an ancestor of what we already hold
            if e["base"]:
                lin.add(e["base"])
            return "superseded"
        if head is None or e["base"] == eid(head) or eid(head) in e["resolves"]:
            self._heads[hk] = e
            lin.add(eid(e))
            if e["base"]:
                lin.add(e["base"])
            lin.update(e["resolves"])
            self.conflicts = [c for c in self.conflicts
                              if not (c["type"] == hk[0] and c["key"] == hk[1] and eid(c["entry"]) in e["resolves"])]
            return "applied"
        self._conflict(e)
        return "conflict"

    def _conflict(self, e: dict) -> None:
        if not any(eid(c["entry"]) == eid(e) for c in self.conflicts):
            self.conflicts.append({"type": e["type"], "key": e["key"], "entry": e})

    def _conflicts_for(self, type_: str, key: str) -> list[dict]:
        return [c for c in self.conflicts if c["type"] == type_ and c["key"] == key]

    def _type(self, type_: str) -> TypePolicy:
        tp = self._types.get(type_)
        if tp is None:
            raise ValueError(f"unknown data type {type_!r}")
        return tp

    def _expired(self, e: dict) -> bool:
        return e.get("expires_at") is not None and self._clock() > e["expires_at"]

    # ---- persistence ------------------------------------------------------
    def save(self) -> None:
        if not self.path:
            return
        data = {"journal": self._journal,
                "heads": [[t, k, e] for (t, k), e in self._heads.items()],
                "lineage": [[t, k, sorted(v)] for (t, k), v in self._lineage.items()],
                "conflicts": self.conflicts, "last_sync": self.last_sync}
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _load(self) -> None:
        d = json.loads(self.path.read_text())  # type: ignore[union-attr]
        self._journal = d["journal"]
        self._heads = {(t, k): e for t, k, e in d["heads"]}
        self._lineage = {(t, k): set(v) for t, k, v in d["lineage"]}
        self.conflicts, self.last_sync = d["conflicts"], d.get("last_sync", {})


def sync(a: Store, b: Store, max_rounds: int = 20, batch: int = 500) -> SyncResult:
    """Bidirectional, bounded. Converges when neither side has anything new to send."""
    total = SyncResult()
    for _ in range(max_rounds):
        to_b = a.delta_for(b.version_vector(), batch)
        to_a = b.delta_for(a.version_vector(), batch)
        if not to_b and not to_a:
            break
        rb, ra = b.apply(to_b, a.peer_id), a.apply(to_a, b.peer_id)
        total.merge(rb); total.merge(ra)
        if not (rb.applied or rb.conflicts or rb.superseded or rb.expired or ra.applied or ra.conflicts
                or ra.superseded or ra.expired):
            break  # only rejections/duplicates remain: surfaced in the result, no point looping
    return total
