"""SETU transport over any byte stream with read(n)->bytes (b'' on timeout, EOFError on close) and write(bytes).
pyserial's Serial objects fit this shape; PipeStream is the in-process stand-in for tests and simulation.
Point-to-point: one StreamTransport talks to exactly one peer id."""
from __future__ import annotations

import queue
import threading
import time
from collections import Counter
from typing import Callable

from ..framing import FrameParser, MAX_PAYLOAD, encode_frame
from .transport import Receiver, Transport


class PipeStream:
    def __init__(self, rx: "queue.Queue[bytes | None]", tx: "queue.Queue[bytes | None]", timeout: float = 0.05) -> None:
        self._rx, self._tx, self._timeout = rx, tx, timeout
        self._pending = b""
        self.closed = False
        self.tx_fault: Callable[[bytes], bytes | None] | None = None   # corrupt or drop outgoing bytes
        self.record: list[bytes] | None = None                         # tap of what was actually written

    def write(self, data: bytes) -> None:
        if self.closed:
            raise OSError("stream closed")
        if self.tx_fault:
            data = self.tx_fault(data)  # type: ignore[assignment]
            if data is None:
                return
        if self.record is not None:
            self.record.append(data)
        self._tx.put(data)

    def read(self, n: int = 256) -> bytes:
        if not self._pending:
            try:
                item = self._rx.get(timeout=self._timeout)
            except queue.Empty:
                if self.closed:
                    raise EOFError
                return b""
            if item is None:
                self.closed = True
                raise EOFError
            self._pending = item
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def close(self) -> None:
        self.closed = True
        self._tx.put(None)


def make_pipe() -> tuple[PipeStream, PipeStream]:
    a_to_b: queue.Queue = queue.Queue()
    b_to_a: queue.Queue = queue.Queue()
    return PipeStream(b_to_a, a_to_b), PipeStream(a_to_b, b_to_a)


class StreamTransport(Transport):
    def __init__(self, stream, peer_id: str, max_frame: int = MAX_PAYLOAD, idle_resync_s: float = 0.1) -> None:
        self.stream, self.peer_id, self.max_frame = stream, peer_id, max_frame
        self.idle_resync_s = idle_resync_s
        self.stats: Counter[str] = Counter()
        self._rx: Receiver = lambda b: None
        self._parser = FrameParser(max_frame)
        self._stop = threading.Event()
        self._wlock = threading.Lock()
        self._t = threading.Thread(target=self._reader, name="setu-stream", daemon=True)
        self._t.start()

    def set_receiver(self, cb: Receiver) -> None:
        self._rx = cb

    def send(self, dest_id: str, data: bytes) -> bool:
        if dest_id != self.peer_id or len(data) > self.max_frame:
            return False
        try:
            with self._wlock:
                self.stream.write(encode_frame(data))
            return True
        except (OSError, ValueError):
            self.stats["write_errors"] += 1
            return False

    def _deliver(self, events) -> None:
        for ev in events:
            if ev.kind == "frame":
                self.stats["frames"] += 1
                self._rx(ev.payload)  # type: ignore[arg-type]
            else:
                self.stats[ev.kind] += 1  # bad_crc / garbage / oversize / truncated: dropped, never delivered

    def _reader(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            try:
                chunk = self.stream.read(256)
            except EOFError:
                return
            except OSError:
                self.stats["read_errors"] += 1
                time.sleep(0.05)
                continue
            if not chunk:
                if self._parser.has_partial and time.monotonic() - last > self.idle_resync_s:
                    self._deliver(self._parser.timeout())   # line idle mid-frame: don't let a false SOF hide real frames
                    last = time.monotonic()
                continue
            last = time.monotonic()
            self._deliver(self._parser.feed(chunk))

    def close(self) -> None:
        self._stop.set()
        try:
            self.stream.close()
        except Exception:
            pass
