"""Transports carry opaque bytes. Semantics (versions, deadlines, dedup) live in Node."""
from __future__ import annotations

import socket
import ssl
import struct
import threading
from collections import Counter
from abc import ABC, abstractmethod
from typing import Callable

Receiver = Callable[[bytes], None]
MAX_FRAME = 1_048_576


class Transport(ABC):
    @abstractmethod
    def send(self, dest_id: str, data: bytes) -> bool: ...
    @abstractmethod
    def set_receiver(self, cb: Receiver) -> None: ...
    def close(self) -> None: ...


class MemoryHub:
    """In-process 'network' for tests/simulation with fault injection.

    hub.fault = lambda src, dst, data: "drop" | "dup" | None
    """

    def __init__(self) -> None:
        self._rx: dict[str, Receiver] = {}
        self._down: set[str] = set()
        self.fault: Callable[[str, str, bytes], str | None] | None = None

    def transport(self, node_id: str) -> "MemoryTransport":
        return MemoryTransport(self, node_id)

    def disconnect(self, node_id: str) -> None:
        self._down.add(node_id)

    def reconnect(self, node_id: str) -> None:
        self._down.discard(node_id)

    def _deliver(self, src: str, dst: str, data: bytes) -> bool:
        if src in self._down or dst in self._down:
            return False
        rx = self._rx.get(dst)
        if rx is None:
            return False
        action = self.fault(src, dst, data) if self.fault else None
        if action == "drop":
            return True  # like UDP loss: the sender cannot tell
        rx(data)
        if action == "dup":
            rx(data)
        return True


class MemoryTransport(Transport):
    def __init__(self, hub: MemoryHub, node_id: str) -> None:
        self.hub, self.id = hub, node_id

    def send(self, dest_id: str, data: bytes) -> bool:
        return self.hub._deliver(self.id, dest_id, data)

    def set_receiver(self, cb: Receiver) -> None:
        self.hub._rx[self.id] = cb

    def close(self) -> None:
        self.hub._rx.pop(self.id, None)


def _read_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class TcpTransport(Transport):
    """Length-prefixed frames over TCP. Peers are looked up by node id.

    Optional mutual TLS: pass server_context/client_context from setu.tls.make_contexts. With TLS on, the peer's
    certificate must chain to your CA and its Common Name must equal the node id you meant to reach (outgoing) or be in
    allowed_peers (incoming). Plain-text connections to a TLS transport are dropped."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, connect_timeout: float = 0.5, *,
                 server_context: "ssl.SSLContext | None" = None, client_context: "ssl.SSLContext | None" = None,
                 allowed_peers: "set[str] | None" = None, handshake_timeout: float = 3.0) -> None:
        self._rx: Receiver = lambda b: None
        self._peers: dict[str, tuple[str, int]] = {}
        self._conns: dict[str, socket.socket] = {}
        self._lock = threading.Lock()
        self._timeout = connect_timeout
        self._server_ctx, self._client_ctx = server_context, client_context
        self._allowed, self._hs_timeout = allowed_peers, handshake_timeout
        self.stats: Counter[str] = Counter()
        self._closed = False
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self.host = host
        threading.Thread(target=self._accept, daemon=True).start()

    def add_peer(self, node_id: str, host: str, port: int) -> None:
        self._peers[node_id] = (host, port)

    def set_receiver(self, cb: Receiver) -> None:
        self._rx = cb

    def _connect(self, dest_id: str, addr: tuple[str, int]) -> "socket.socket | None":
        try:
            s = socket.create_connection(addr, timeout=self._timeout)
        except OSError:
            return None
        if self._client_ctx is None:
            return s
        from .tls import peer_cn
        try:
            s.settimeout(self._hs_timeout)
            ss = self._client_ctx.wrap_socket(s, server_hostname=dest_id)
            ss.settimeout(None)
        except (ssl.SSLError, OSError):
            s.close()
            self.stats["tls_handshake_failed"] += 1
            return None
        if peer_cn(ss) != dest_id:
            ss.close()
            self.stats["tls_identity_mismatch"] += 1
            return None
        return ss

    def send(self, dest_id: str, data: bytes) -> bool:
        frame = struct.pack(">I", len(data)) + data
        for _ in range(2):  # one reconnect attempt
            with self._lock:
                s = self._conns.get(dest_id)
                if s is None:
                    addr = self._peers.get(dest_id)
                    if addr is None:
                        return False
                    s = self._connect(dest_id, addr)
                    if s is None:
                        return False
                    self._conns[dest_id] = s
                try:
                    s.sendall(frame)
                    return True
                except OSError:
                    s.close()
                    self._conns.pop(dest_id, None)
        return False

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._reader, args=(conn,), daemon=True).start()

    def _reader(self, conn: socket.socket) -> None:
        if self._server_ctx is not None:
            from .tls import peer_cn
            try:
                conn.settimeout(self._hs_timeout)
                conn = self._server_ctx.wrap_socket(conn, server_side=True)
                conn.settimeout(None)
            except (ssl.SSLError, OSError):
                self.stats["tls_handshake_failed"] += 1
                conn.close()
                return
            if self._allowed is not None and peer_cn(conn) not in self._allowed:
                self.stats["tls_peer_rejected"] += 1
                conn.close()
                return
        with conn:
            while not self._closed:
                try:
                    hdr = _read_exact(conn, 4)
                    if hdr is None:
                        return
                    (n,) = struct.unpack(">I", hdr)
                    if n > MAX_FRAME:
                        return  # oversize frame: drop the connection
                    body = _read_exact(conn, n)
                    if body is None:
                        return
                except OSError:
                    return
                self._rx(body)

    def close(self) -> None:
        self._closed = True
        try:
            self._srv.close()
        except OSError:
            pass
        with self._lock:
            for s in self._conns.values():
                try:
                    s.close()
                except OSError:
                    pass
            self._conns.clear()
