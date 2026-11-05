from __future__ import annotations

import heapq
import random
import socket
import threading
import time
from typing import Any, Callable

from .endpoint import Endpoint


class SimClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class SimLink:
    """Deterministic lossy datagram link: latency, jitter (=> reordering), loss, duplication, bit corruption."""

    def __init__(self, clock: SimClock, latency: float = 0.02, jitter: float = 0.0, loss: float = 0.0,
                 dup: float = 0.0, corrupt: float = 0.0, seed: int = 0) -> None:
        self.clock, self.latency, self.jitter, self.loss, self.dup, self.corrupt = clock, latency, jitter, loss, dup, corrupt
        self.rng = random.Random(seed)
        self._q: list[tuple[float, int, Callable[[bytes], None], bytes]] = []
        self._n = 0
        self.sent = 0

    def sender(self, deliver: Callable[[bytes], None]) -> Callable[[bytes], None]:
        def send(data: bytes) -> None:
            self.sent += 1
            if self.rng.random() < self.loss:
                return
            for _ in range(2 if self.rng.random() < self.dup else 1):
                d = data
                if self.rng.random() < self.corrupt:
                    i = self.rng.randrange(len(d))
                    d = d[:i] + bytes([d[i] ^ (1 << self.rng.randrange(8))]) + d[i + 1:]
                self._n += 1
                heapq.heappush(self._q, (self.clock.now + self.latency + self.rng.random() * self.jitter, self._n, deliver, d))
        return send

    def advance(self, dt: float) -> None:
        self.clock.now += dt
        while self._q and self._q[0][0] <= self.clock.now:
            _, _, deliver, data = heapq.heappop(self._q)
            deliver(data)


def make_pair(link_kw: dict[str, Any] | None = None, **ep_kw: Any):
    """Two endpoints over one SimLink. Returns (a, b, link, clock, got_a, got_b, errors)."""
    clock = SimClock()
    link = SimLink(clock, **(link_kw or {}))
    got_a: list[bytes] = []
    got_b: list[bytes] = []
    errors: list[str] = []
    holder: dict[str, Endpoint] = {}
    a = Endpoint(link.sender(lambda d: holder["b"].on_packet(d)), clock, got_a.append, on_error=errors.append, **ep_kw)
    b = Endpoint(link.sender(lambda d: holder["a"].on_packet(d)), clock, got_b.append, on_error=errors.append, **ep_kw)
    holder["a"], holder["b"] = a, b
    return a, b, link, clock, got_a, got_b, errors


def run_until(a: Endpoint, b: Endpoint, link: SimLink, done: Callable[[], bool], dt: float = 0.005,
              max_time: float = 120.0) -> float:
    """Advance simulated time until done() or max_time. Returns elapsed simulated seconds."""
    start = link.clock.now
    while link.clock.now - start < max_time and not done():
        link.advance(dt)
        a.tick()
        b.tick()
    return link.clock.now - start


def compare_windows(windows: tuple[int, ...] = (1, 4, 32), n_messages: int = 100, size: int = 500,
                    link_kw: dict[str, Any] | None = None) -> dict[int, float]:
    """Simulated completion time vs flow window; window=1 is the stop-and-wait baseline."""
    out = {}
    for w in windows:
        a, b, link, clock, _, got_b, _ = make_pair(link_kw or {"latency": 0.05}, window=w)
        for i in range(n_messages):
            a.send_message(bytes([i % 256]) * size)
        out[w] = run_until(a, b, link, lambda: len(got_b) >= n_messages)
    return out


class UdpTransport:
    """Real UDP adapter (localhost/LAN). Endpoint isn't thread-safe, so every call goes through one lock."""

    def __init__(self, bind: tuple[str, int] = ("127.0.0.1", 0), peer: tuple[str, int] | None = None,
                 on_message: Callable[[bytes], None] = lambda m: None, **ep_kw: Any) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(bind)
        self.sock.settimeout(0.01)
        self.addr = self.sock.getsockname()
        self.peer = peer
        self.tx_filter: Callable[[bytes], bool] | None = None      # return False to drop (loss injection)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.endpoint = Endpoint(self._tx, time.monotonic, on_message, **ep_kw)
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _tx(self, data: bytes) -> None:
        if self.peer and (self.tx_filter is None or self.tx_filter(data)):
            try:
                self.sock.sendto(data, self.peer)
            except OSError:
                pass

    def send_message(self, msg: bytes) -> bool:
        with self._lock:
            return self.endpoint.send_message(msg)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                data = None
            except OSError:
                return
            with self._lock:
                if data:
                    self.endpoint.on_packet(data)
                self.endpoint.tick()

    def idle(self) -> bool:
        with self._lock:
            return self.endpoint.idle()

    def close(self) -> None:
        self._stop.set()
        self._t.join(timeout=1)
        self.sock.close()
