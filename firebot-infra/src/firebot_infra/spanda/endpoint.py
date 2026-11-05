"""SPANDA: an EXPERIMENTAL reliable, ordered, message-oriented transport for lossy datagram links.

Research prototype. Per the dossier, only adopt it if SETU benchmarks show a limitation that mature options
(TCP, QUIC, plain UDP + SETU retries) cannot meet; always benchmark against those first.

Selective-repeat ARQ: sequence numbers, cumulative ACK + SACK list, per-segment retransmission timers with an
RTT estimator (Karn's rule), fast retransmit on 3 SACK gaps, AIMD congestion window, flow window, message
fragmentation/reassembly, CRC32 on every packet. Pure logic: caller provides send(bytes) and a clock.

Packet: type u8 | seq u32 | ack u32 | flags u8 | len u16 | payload | crc32 u32 (over everything before it)
"""
from __future__ import annotations

import struct
import zlib
from collections import Counter, deque
from typing import Callable

DATA, ACK = 1, 2
FIRST, LAST = 1, 2
HDR = struct.Struct(">BIIBH")
MAX_SACKS = 16


def encode_packet(ptype: int, seq: int, ack: int, flags: int, payload: bytes) -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large")
    body = HDR.pack(ptype, seq, ack, flags, len(payload)) + payload
    return body + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")


def decode_packet(data: bytes) -> tuple[int, int, int, int, bytes] | None:
    """Returns (type, seq, ack, flags, payload) or None for anything malformed/corrupt."""
    if len(data) < HDR.size + 4:
        return None
    body, crc = data[:-4], int.from_bytes(data[-4:], "big")
    if zlib.crc32(body) & 0xFFFFFFFF != crc:
        return None
    ptype, seq, ack, flags, n = HDR.unpack_from(body)
    if ptype not in (DATA, ACK) or n != len(body) - HDR.size:
        return None
    return ptype, seq, ack, flags, body[HDR.size:]


class _Seg:
    __slots__ = ("payload", "flags", "sent_at", "first_sent", "tries", "sacked", "fast_done")

    def __init__(self, payload: bytes, flags: int) -> None:
        self.payload, self.flags = payload, flags
        self.sent_at = self.first_sent = 0.0
        self.tries = 0
        self.sacked = self.fast_done = False


class Endpoint:
    def __init__(self, send: Callable[[bytes], object], clock: Callable[[], float],
                 on_message: Callable[[bytes], None], *, mss: int = 512, window: int = 32,
                 rto_initial: float = 0.2, rto_min: float = 0.02, rto_max: float = 2.0, max_retries: int = 8,
                 queue_limit: int = 4096, max_message: int = 16 << 20,
                 on_error: Callable[[str], None] | None = None) -> None:
        self._send, self._clock, self._on_message, self._on_error = send, clock, on_message, on_error
        self.mss, self.window, self.max_retries = mss, window, max_retries
        self.rto_min, self.rto_max, self.queue_limit, self.max_message = rto_min, rto_max, queue_limit, max_message
        self.rto = rto_initial
        self.srtt: float | None = None
        self.rttvar = 0.0
        self.cwnd, self.ssthresh = 2.0, float(window)
        self._recover = 0
        self._queue: deque[tuple[bytes, int]] = deque()
        self._inflight: dict[int, _Seg] = {}
        self._next_seq = self._base = 0
        self._rcv_next = 0
        self._rcv_buf: dict[int, tuple[int, bytes]] = {}
        self._asm: list[bytes] = []
        self._asm_len = 0
        self.failed = False
        self.stats: Counter[str] = Counter()

    # ---- application API ---------------------------------------------
    def send_message(self, msg: bytes) -> bool:
        """Queue a message. False (never blocks) if failed or the bounded queue lacks room."""
        if len(msg) > self.max_message:
            raise ValueError("message too large")
        if self.failed:
            return False
        n = max(1, -(-len(msg) // self.mss))
        if len(self._queue) + n > self.queue_limit:
            self.stats["queue_full"] += 1
            return False
        for i in range(n):
            flags = (FIRST if i == 0 else 0) | (LAST if i == n - 1 else 0)
            self._queue.append((msg[i * self.mss:(i + 1) * self.mss], flags))
        self._pump()
        return True

    def idle(self) -> bool:
        return not self._queue and not self._inflight

    # ---- sender ---------------------------------------------------------
    def _pump(self) -> None:
        if self.failed:
            return
        while self._queue and len(self._inflight) < min(self.window, max(1, int(self.cwnd))):
            payload, flags = self._queue.popleft()
            seg = _Seg(payload, flags)
            seq = self._next_seq
            self._next_seq += 1
            self._inflight[seq] = seg
            self._tx(seq, seg)

    def _tx(self, seq: int, seg: _Seg) -> None:
        now = self._clock()
        if seg.tries == 0:
            seg.first_sent = now
        seg.tries += 1
        seg.sent_at = now
        self.stats["sent"] += 1
        self._send(encode_packet(DATA, seq, 0, seg.flags, seg.payload))

    def _loss_event(self, seq: int, to_one: bool) -> None:
        if seq >= self._recover:                      # at most one window reduction per loss episode
            self.ssthresh = max(2.0, self.cwnd / 2)
            self.cwnd = 1.0 if to_one else self.ssthresh
            self._recover = self._next_seq

    def _rtt_sample(self, r: float) -> None:
        if self.srtt is None:
            self.srtt, self.rttvar = r, r / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - r)
            self.srtt = 0.875 * self.srtt + 0.125 * r
        self.rto = min(self.rto_max, max(self.rto_min, self.srtt + 4 * self.rttvar))

    def tick(self) -> None:
        if self.failed:
            return
        now = self._clock()
        for seq in sorted(self._inflight):
            seg = self._inflight[seq]
            if seg.sacked:
                continue
            if now - seg.sent_at >= min(self.rto_max, self.rto * 2 ** (seg.tries - 1)):
                if seg.tries >= self.max_retries:
                    self.failed = True
                    if self._on_error:
                        self._on_error("max_retries")
                    return
                self.stats["timeouts"] += 1
                self.stats["retransmits"] += 1
                self._loss_event(seq, to_one=True)
                self._tx(seq, seg)
        self._pump()

    # ---- receive -----------------------------------------------------------
    def on_packet(self, data: bytes) -> None:
        pkt = decode_packet(data)
        if pkt is None:
            self.stats["corrupt_dropped"] += 1
            return
        ptype, seq, ack, flags, payload = pkt
        if ptype == DATA:
            self._on_data(seq, flags, payload)
        else:
            self._on_ack(ack, payload)

    def _on_data(self, seq: int, flags: int, payload: bytes) -> None:
        if seq < self._rcv_next or seq in self._rcv_buf:
            self.stats["duplicates"] += 1
        elif seq >= self._rcv_next + 2 * self.window:
            self.stats["out_of_window"] += 1
        else:
            self._rcv_buf[seq] = (flags, payload)
        while self._rcv_next in self._rcv_buf:
            fl, p = self._rcv_buf.pop(self._rcv_next)
            self._rcv_next += 1
            if fl & FIRST and self._asm:
                self.stats["protocol_resets"] += 1            # new message began before the last one ended
                self._asm, self._asm_len = [], 0
            self._asm.append(p)
            self._asm_len += len(p)
            if self._asm_len > self.max_message:
                self.stats["oversize_dropped"] += 1
                self._asm, self._asm_len = [], 0
            elif fl & LAST:
                msg, self._asm, self._asm_len = b"".join(self._asm), [], 0
                self.stats["messages_delivered"] += 1
                self._on_message(msg)
        sacks = sorted(self._rcv_buf)[:MAX_SACKS]
        self._send(encode_packet(ACK, 0, self._rcv_next, 0, struct.pack(f">{len(sacks)}I", *sacks)))

    def _on_ack(self, ack: int, sack_payload: bytes) -> None:
        if self.failed:
            return
        if ack > self._next_seq or len(sack_payload) % 4 or len(sack_payload) > 4 * MAX_SACKS:
            self.stats["bad_ack"] += 1
            return
        now = self._clock()
        self.stats["acks"] += 1
        for seq in range(self._base, ack):
            seg = self._inflight.pop(seq, None)
            if seg is None:
                continue
            if seg.tries == 1:                                 # Karn: never time retransmitted segments
                self._rtt_sample(now - seg.first_sent)
            self.cwnd = self.cwnd + 1 if self.cwnd < self.ssthresh else self.cwnd + 1 / self.cwnd
        self._base = max(self._base, ack)
        sacks = struct.unpack(f">{len(sack_payload) // 4}I", sack_payload)
        for s in sacks:
            seg = self._inflight.get(s)
            if seg:
                seg.sacked = True
        for seq in sorted(self._inflight):
            seg = self._inflight[seq]
            if seg.sacked or seg.fast_done:
                continue
            if sum(1 for s in sacks if s > seq) >= 3:         # three later segments arrived: this one was lost
                seg.fast_done = True
                self.stats["fast_retransmits"] += 1
                self.stats["retransmits"] += 1
                self._loss_event(seq, to_one=False)
                self._tx(seq, seg)
        self._pump()
