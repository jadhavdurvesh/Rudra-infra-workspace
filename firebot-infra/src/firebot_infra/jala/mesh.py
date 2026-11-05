"""JALA: link-state mesh routing for noncritical traffic over EXISTING links/radios.

Neighbour discovery with symmetric-link check, flooded link-state advertisements (sequence numbers, ageing),
hop-count shortest paths over bidirectionally confirmed links, TTL, duplicate suppression, bounded queues.
Pure logic: the caller supplies send(neighbor_id, bytes) -> bool and a clock; MeshSim provides a deterministic
simulated network for testing route recovery. Not for safety-critical commands.
"""
from __future__ import annotations

import json
import random
from collections import Counter, OrderedDict, deque
from typing import Any, Callable

MAX_PACKET = 4096


class MeshNode:
    def __init__(self, node_id: str, send: Callable[[str, bytes], bool], clock: Callable[[], float], *,
                 hello_interval: float = 1.0, hello_timeout: float = 3.5, lsa_interval: float = 4.0,
                 lsa_max_age: float = 12.0, ttl: int = 16, queue_limit: int = 32,
                 on_deliver: Callable[[str, bytes], None] | None = None) -> None:
        self.id, self._send, self._clock = node_id, send, clock
        self.hello_interval, self.hello_timeout = hello_interval, hello_timeout
        self.lsa_interval, self.lsa_max_age, self.ttl = lsa_interval, lsa_max_age, ttl
        self.queue_limit, self.on_deliver = queue_limit, on_deliver
        self._heard: dict[str, tuple[float, bool]] = {}           # neighbour -> (last heard, link symmetric)
        self._lsdb: dict[str, tuple[int, frozenset[str], float]] = {}   # origin -> (seq, symmetric neighbours, received_at)
        self._seq = 0
        self._last_hello = -1e18
        self._last_lsa = -1e18
        self._dirty = True
        self._advertised: frozenset[str] = frozenset()
        self.routes: dict[str, str] = {}
        self._pending: deque[tuple[str, bytes]] = deque()
        self._seen: OrderedDict[tuple[str, int], None] = OrderedDict()
        self._msg_id = 0
        self.stats: Counter[str] = Counter()

    # ---- helpers -----------------------------------------------------
    def neighbors(self) -> list[str]:
        now = self._clock()
        return sorted(n for n, (t, sym) in self._heard.items() if sym and now - t <= self.hello_timeout)

    def _alive(self) -> list[str]:
        now = self._clock()
        return sorted(n for n, (t, _) in self._heard.items() if now - t <= self.hello_timeout)

    def _tx(self, to: str, pkt: dict[str, Any]) -> None:
        data = json.dumps(pkt, separators=(",", ":")).encode()
        if len(data) > MAX_PACKET:
            self.stats["oversize_dropped"] += 1
            return
        if not self._send(to, data):
            self.stats["send_failed"] += 1

    # ---- periodic work -----------------------------------------------
    def tick(self) -> None:
        now = self._clock()
        if now - self._last_hello >= self.hello_interval:
            self._last_hello = now
            heard = self._alive()
            for n in heard:
                self._tx(n, {"t": "h", "s": self.id, "h": heard})
            # also announce to unknown neighbours: the transport decides who can hear (broadcast medium)
            self._tx("*", {"t": "h", "s": self.id, "h": heard})
        sym = frozenset(self.neighbors())
        if sym != self._advertised or now - self._last_lsa >= self.lsa_interval:
            self._originate(sym, now)
        for o in [o for o, (_, _, at) in self._lsdb.items() if now - at > self.lsa_max_age]:
            del self._lsdb[o]
            self._dirty = True
        if self._dirty:
            self._recompute(sym)
        self._flush_pending()

    def _originate(self, sym: frozenset[str], now: float) -> None:
        if sym != self._advertised:
            self._dirty = True
        self._advertised, self._last_lsa = sym, now
        self._seq += 1
        for n in sym:
            self._tx(n, {"t": "l", "o": self.id, "q": self._seq, "n": sorted(sym)})

    def _recompute(self, own: frozenset[str]) -> None:
        self._dirty = False
        adj: dict[str, set[str]] = {self.id: set(own)}
        for o, (_, links, _) in self._lsdb.items():
            if o != self.id:
                adj[o] = set(links)
        routes: dict[str, str] = {}
        seen = {self.id}
        frontier = [(n, n) for n in sorted(own)]               # (node, first hop)
        while frontier:
            nxt = []
            for node, hop in frontier:
                if node in seen:
                    continue
                seen.add(node)
                routes[node] = hop
                for m in sorted(adj.get(node, ())):
                    # a link counts only if BOTH ends advertise it
                    if m not in seen and node in adj.get(m, ()):
                        nxt.append((m, hop))
            frontier = nxt
        self.routes = routes

    # ---- receive -------------------------------------------------------
    def on_receive(self, from_node: str, data: bytes) -> None:
        try:
            if len(data) > MAX_PACKET:
                raise ValueError
            p = json.loads(data)
            t = p["t"]
        except (ValueError, KeyError, TypeError):
            self.stats["malformed"] += 1
            return
        if not isinstance(p, dict):
            self.stats["malformed"] += 1
            return
        try:
            if t == "h":
                self._on_hello(from_node, p)
            elif t == "l":
                self._on_lsa(from_node, p)
            elif t == "d":
                self._on_data(p)
            else:
                self.stats["malformed"] += 1
        except (KeyError, TypeError, ValueError):
            self.stats["malformed"] += 1

    def _on_hello(self, frm: str, p: dict) -> None:
        sym = self.id in p["h"]
        old = self._heard.get(frm)
        self._heard[frm] = (self._clock(), sym)
        if old is None or old[1] != sym:
            self._dirty = True

    def _on_lsa(self, frm: str, p: dict) -> None:
        origin, seq, links = p["o"], int(p["q"]), frozenset(p["n"])
        if origin == self.id:
            return
        cur = self._lsdb.get(origin)
        if cur is not None and cur[0] >= seq:
            self.stats["lsa_duplicate"] += 1
            return
        if cur is None or cur[1] != links:
            self._dirty = True
        self._lsdb[origin] = (seq, links, self._clock())
        for n in self.neighbors():
            if n != frm:
                self._tx(n, p)                                    # flood
        self.stats["lsa_flooded"] += 1

    def _on_data(self, p: dict) -> None:
        src, dst, mid, ttl = p["s"], p["d"], int(p["i"]), int(p["l"])
        if dst == self.id:
            if (src, mid) in self._seen:
                self.stats["data_duplicate"] += 1
                return
            self._seen[(src, mid)] = None
            while len(self._seen) > 1024:
                self._seen.popitem(last=False)
            self.stats["delivered"] += 1
            if self.on_deliver:
                self.on_deliver(src, bytes.fromhex(p["p"]))
            return
        if ttl <= 1:
            self.stats["ttl_expired"] += 1
            return
        hop = self.routes.get(dst)
        if hop is None:
            self.stats["no_route_dropped"] += 1
            return
        self.stats["forwarded"] += 1
        self._tx(hop, {**p, "l": ttl - 1})

    # ---- send ----------------------------------------------------------
    def send(self, dst: str, payload: bytes) -> bool:
        """True if sent or queued; False if the bounded queue is full (caller decides what to do)."""
        if self._emit(dst, payload):
            return True
        if len(self._pending) >= self.queue_limit:
            self.stats["queue_overflow"] += 1
            return False
        self._pending.append((dst, payload))
        return True

    def _emit(self, dst: str, payload: bytes) -> bool:
        hop = self.routes.get(dst)
        if hop is None:
            return False
        self._msg_id += 1
        self._tx(hop, {"t": "d", "s": self.id, "d": dst, "i": self._msg_id, "l": self.ttl, "p": payload.hex()})
        self.stats["originated"] += 1
        return True

    def _flush_pending(self) -> None:
        for item in list(self._pending):
            if self._emit(*item):
                self._pending.remove(item)

    def pending(self) -> int:
        return len(self._pending)


class MeshSim:
    """Deterministic discrete-time network. Packets sent during a step arrive in the next step.
    Destination '*' means every up-link neighbour (broadcast medium)."""

    def __init__(self, edges: list[tuple[str, str]], dt: float = 0.1, loss: float = 0.0, seed: int = 0,
                 dup: float = 0.0, **node_kw: Any) -> None:
        self.now, self.dt, self.loss, self.dup = 0.0, dt, loss, dup
        self.rng = random.Random(seed)
        self.links = {frozenset(e) for e in edges}
        self.down: set[frozenset[str]] = set()
        names = sorted({n for e in edges for n in e})
        self.inbox: list[tuple[str, str, bytes]] = []
        self.delivered: dict[str, list[tuple[str, bytes]]] = {n: [] for n in names}
        self.nodes = {n: MeshNode(n, self._sender(n), lambda: self.now,
                                  on_deliver=lambda src, data, n=n: self.delivered[n].append((src, data)), **node_kw)
                      for n in names}

    def _sender(self, me: str):
        def send(to: str, data: bytes) -> bool:
            targets = [x for x in self.nodes if self._up(me, x)] if to == "*" else ([to] if self._up(me, to) else [])
            if to != "*" and not targets:
                return False
            for t in targets:
                if self.rng.random() >= self.loss:
                    self.inbox.append((me, t, data))
                    if self.rng.random() < self.dup:
                        self.inbox.append((me, t, data))
            return True
        return send

    def _up(self, a: str, b: str) -> bool:
        k = frozenset((a, b))
        return k in self.links and k not in self.down

    def link_down(self, a: str, b: str) -> None:
        self.down.add(frozenset((a, b)))

    def link_up(self, a: str, b: str) -> None:
        self.down.discard(frozenset((a, b)))

    def step(self) -> None:
        self.now += self.dt
        batch, self.inbox = self.inbox, []
        for frm, to, data in batch:
            if self._up(frm, to):
                self.nodes[to].on_receive(frm, data)
        for n in self.nodes.values():
            n.tick()

    def run(self, seconds: float) -> None:
        for _ in range(int(round(seconds / self.dt))):
            self.step()
