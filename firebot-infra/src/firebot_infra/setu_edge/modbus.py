"""SETU-EDGE: Modbus RTU/TCP codec, client, simulated server and a TANTU sensor driver.

Control boundary: the client is READ-ONLY by default. Writes need allow_write=True AND (optionally) a
write_guard that approves each one; writes are never auto-retried. Field-bus writes can move machinery:
keep them out of anything that is not validated for it.
"""
from __future__ import annotations

import struct
import threading
import time
from typing import Any, Callable, Protocol

from ..tantu.runtime import DriverError, Reading, SensorDriver

READ_COILS, READ_DISCRETE, READ_HOLDING, READ_INPUT = 1, 2, 3, 4
WRITE_COIL, WRITE_REG, WRITE_COILS, WRITE_REGS = 5, 6, 15, 16
WRITE_FCS = (WRITE_COIL, WRITE_REG, WRITE_COILS, WRITE_REGS)


class ModbusError(Exception): ...
class ModbusTransient(ModbusError): ...             # worth retrying for reads: crc, mismatch, timeout
class ModbusTimeout(ModbusTransient): ...
class WriteDisabled(ModbusError): ...
class WriteDenied(ModbusError): ...


class ModbusException(ModbusError):
    NAMES = {1: "illegal function", 2: "illegal data address", 3: "illegal data value", 4: "server device failure",
             5: "acknowledge", 6: "server device busy", 10: "gateway path unavailable", 11: "gateway target failed to respond"}

    def __init__(self, code: int) -> None:
        super().__init__(f"modbus exception {code}: {self.NAMES.get(code, 'unknown')}")
        self.code = code


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


# ---- framing -----------------------------------------------------------------
def rtu_frame(unit: int, pdu: bytes) -> bytes:
    adu = bytes([unit]) + pdu
    return adu + crc16(adu).to_bytes(2, "little")


def rtu_parse(frame: bytes) -> tuple[int, bytes]:
    if len(frame) < 4 or crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise ModbusTransient("bad RTU frame/crc")
    return frame[0], frame[1:-2]


def tcp_frame(tid: int, unit: int, pdu: bytes) -> bytes:
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


def tcp_parse(frame: bytes) -> tuple[int, int, bytes]:
    if len(frame) < 8:
        raise ModbusTransient("short TCP frame")
    tid, pid, length, unit = struct.unpack(">HHHB", frame[:7])
    if pid != 0 or length != len(frame) - 6:
        raise ModbusTransient("bad MBAP header")
    return tid, unit, frame[7:]


# ---- PDUs --------------------------------------------------------------------
def _check(addr: int, qty: int, lo: int, hi: int) -> None:
    if not (0 <= addr <= 0xFFFF and lo <= qty <= hi and addr + qty <= 0x10000):
        raise ValueError("address/quantity out of range")


def pdu_read(fc: int, addr: int, qty: int) -> bytes:
    _check(addr, qty, 1, 2000 if fc in (READ_COILS, READ_DISCRETE) else 125)
    return struct.pack(">BHH", fc, addr, qty)


def pdu_write_coil(addr: int, on: bool) -> bytes:
    _check(addr, 1, 1, 1)
    return struct.pack(">BHH", WRITE_COIL, addr, 0xFF00 if on else 0)


def pdu_write_reg(addr: int, value: int) -> bytes:
    _check(addr, 1, 1, 1)
    if not 0 <= value <= 0xFFFF:
        raise ValueError("register value out of range")
    return struct.pack(">BHH", WRITE_REG, addr, value)


def pdu_write_coils(addr: int, values: list[bool]) -> bytes:
    _check(addr, len(values), 1, 1968)
    data = _pack_bits(values)
    return struct.pack(">BHHB", WRITE_COILS, addr, len(values), len(data)) + data


def pdu_write_regs(addr: int, values: list[int]) -> bytes:
    _check(addr, len(values), 1, 123)
    if any(not 0 <= v <= 0xFFFF for v in values):
        raise ValueError("register value out of range")
    return struct.pack(">BHHB", WRITE_REGS, addr, len(values), 2 * len(values)) + struct.pack(f">{len(values)}H", *values)


def _pack_bits(bits: list[bool]) -> bytes:
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def parse_response(req: bytes, resp: bytes) -> Any:
    """Decode a response PDU for the given request PDU. Raises ModbusException / ModbusError, never IndexError."""
    if len(resp) < 2 or len(req) < 5:
        raise ModbusError("short response")
    fc = req[0]
    if resp[0] == fc | 0x80:
        raise ModbusException(resp[1])
    if resp[0] != fc:
        raise ModbusTransient("function code mismatch")
    if fc in (READ_COILS, READ_DISCRETE, READ_HOLDING, READ_INPUT):
        qty = struct.unpack(">H", req[3:5])[0]
        n = (qty + 7) // 8 if fc <= 2 else 2 * qty
        if resp[1] != n or len(resp) != 2 + n:
            raise ModbusTransient("bad byte count")
        if fc <= 2:
            return [bool(resp[2 + i // 8] >> (i % 8) & 1) for i in range(qty)]
        return list(struct.unpack(f">{qty}H", resp[2:]))
    if fc in (WRITE_COIL, WRITE_REG):
        if resp != req[:5]:
            raise ModbusTransient("write echo mismatch")
        return True
    if fc in (WRITE_COILS, WRITE_REGS):
        if resp != req[:5]:
            raise ModbusTransient("write echo mismatch")
        return True
    raise ModbusError("unsupported function")


# ---- simulated server --------------------------------------------------------
class _Exc(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


class ModbusServer:
    def __init__(self, unit_id: int = 1, coils: int = 0, discrete: int = 0, holding: int = 0, input_regs: int = 0) -> None:
        self.unit = unit_id
        self.coils, self.discrete = [False] * coils, [False] * discrete
        self.holding, self.input = [0] * holding, [0] * input_regs
        self.writes = 0

    def handle_pdu(self, pdu: bytes) -> bytes:
        if not pdu:
            return b""
        fc = pdu[0]
        try:
            return self._dispatch(fc, pdu)
        except _Exc as e:
            return bytes([fc | 0x80, e.code])
        except struct.error:
            return bytes([fc | 0x80, 3])

    def _dispatch(self, fc: int, pdu: bytes) -> bytes:
        if fc in (1, 2, 3, 4):
            if len(pdu) != 5:
                raise _Exc(3)
            addr, qty = struct.unpack(">HH", pdu[1:])
            if not 1 <= qty <= (2000 if fc <= 2 else 125):
                raise _Exc(3)
            arr = {1: self.coils, 2: self.discrete, 3: self.holding, 4: self.input}[fc]
            if addr + qty > len(arr):
                raise _Exc(2)
            vals = arr[addr:addr + qty]
            if fc <= 2:
                data = _pack_bits(vals)  # type: ignore[arg-type]
                return bytes([fc, len(data)]) + data
            return bytes([fc, 2 * qty]) + struct.pack(f">{qty}H", *vals)
        if fc in (5, 6):
            if len(pdu) != 5:
                raise _Exc(3)
            addr, val = struct.unpack(">HH", pdu[1:])
            if fc == 5:
                if val not in (0, 0xFF00):
                    raise _Exc(3)
                if addr >= len(self.coils):
                    raise _Exc(2)
                self.coils[addr] = val == 0xFF00
            else:
                if addr >= len(self.holding):
                    raise _Exc(2)
                self.holding[addr] = val
            self.writes += 1
            return pdu
        if fc in (15, 16):
            if len(pdu) < 6:
                raise _Exc(3)
            addr, qty, bc = struct.unpack(">HHB", pdu[1:6])
            data = pdu[6:]
            if fc == 15:
                if not 1 <= qty <= 1968 or bc != (qty + 7) // 8 or len(data) != bc:
                    raise _Exc(3)
                if addr + qty > len(self.coils):
                    raise _Exc(2)
                for i in range(qty):
                    self.coils[addr + i] = bool(data[i // 8] >> (i % 8) & 1)
            else:
                if not 1 <= qty <= 123 or bc != 2 * qty or len(data) != bc:
                    raise _Exc(3)
                if addr + qty > len(self.holding):
                    raise _Exc(2)
                self.holding[addr:addr + qty] = list(struct.unpack(f">{qty}H", data))
            self.writes += 1
            return pdu[:5]
        raise _Exc(1)

    def handle_rtu(self, frame: bytes) -> bytes | None:
        try:
            unit, pdu = rtu_parse(frame)
        except ModbusTransient:
            return None                      # a real device stays silent on a bad CRC
        return rtu_frame(self.unit, self.handle_pdu(pdu)) if unit == self.unit else None

    def handle_tcp(self, frame: bytes) -> bytes | None:
        try:
            tid, unit, pdu = tcp_parse(frame)
        except ModbusTransient:
            return None
        return tcp_frame(tid, self.unit, self.handle_pdu(pdu)) if unit == self.unit else None


# ---- links ---------------------------------------------------------------------
class Link(Protocol):
    def exchange(self, frame: bytes, timeout: float) -> bytes:
        """Send a request frame, return the response frame; raise ModbusTimeout if none arrives."""


class ServerLink:
    """Direct link to a simulated server, with fault injection (drop / corrupt the next N exchanges)."""

    def __init__(self, server: ModbusServer, mode: str = "rtu") -> None:
        self.server, self.mode = server, mode
        self.drop = self.corrupt = self.calls = 0

    def exchange(self, frame: bytes, timeout: float) -> bytes:
        self.calls += 1
        if self.drop > 0:
            self.drop -= 1
            raise ModbusTimeout("no response (dropped)")
        resp = self.server.handle_rtu(frame) if self.mode == "rtu" else self.server.handle_tcp(frame)
        if resp is None:
            raise ModbusTimeout("no response")
        if self.corrupt > 0:
            self.corrupt -= 1
            resp = resp[:-1] + bytes([resp[-1] ^ 0xFF])
        return resp


def _rtu_response_len(buf: bytes) -> int | None:
    if len(buf) < 2:
        return None
    fc = buf[1]
    if fc & 0x80:
        return 5
    if fc in (1, 2, 3, 4):
        return None if len(buf) < 3 else 3 + buf[2] + 2
    if fc in (5, 6, 15, 16):
        return 8
    raise ModbusTransient("unknown function in response")


def _rtu_request_len(buf: bytes) -> int | None:
    if len(buf) < 2:
        return None
    fc = buf[1]
    if fc in (1, 2, 3, 4, 5, 6):
        return 8
    if fc in (15, 16):
        return None if len(buf) < 7 else 9 + buf[6]
    return 4 if len(buf) >= 4 else None


class RtuStreamLink:
    """RTU over a byte stream (read(n)->bytes, b'' on timeout; write(bytes)). Frame ends are inferred from the
    function code's length rules, since real RTU uses inter-frame silence."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream

    def exchange(self, frame: bytes, timeout: float) -> bytes:
        try:
            self.stream.write(frame)
        except OSError as e:
            raise ModbusTransient(f"write failed: {e}") from e
        deadline, buf = time.monotonic() + timeout, b""
        while True:
            try:
                buf += self.stream.read(256)
            except EOFError as e:
                raise ModbusError("stream closed") from e
            n = _rtu_response_len(buf)
            if n is not None and len(buf) >= n:
                return buf[:n]
            if time.monotonic() > deadline:
                raise ModbusTimeout("no response")


def serve_rtu_stream(server: ModbusServer, stream: Any, stop: threading.Event) -> threading.Thread:
    def loop() -> None:
        buf = b""
        while not stop.is_set():
            try:
                buf += stream.read(256)
            except (EOFError, OSError):
                return
            n = _rtu_request_len(buf)
            if n is not None and len(buf) >= n:
                req, buf = buf[:n], buf[n:]
                resp = server.handle_rtu(req)
                if resp:
                    stream.write(resp)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


# ---- client ----------------------------------------------------------------------
class ModbusClient:
    def __init__(self, link: Link, unit_id: int = 1, mode: str = "rtu", timeout: float = 0.5, retries: int = 2,
                 allow_write: bool = False, write_guard: Callable[[int, int, Any], bool] | None = None) -> None:
        if mode not in ("rtu", "tcp"):
            raise ValueError("mode must be rtu|tcp")
        self.link, self.unit, self.mode, self.timeout, self.retries = link, unit_id, mode, timeout, retries
        self.allow_write, self.write_guard = allow_write, write_guard
        self._tid = 0
        self.stats = {"requests": 0, "retries": 0, "transient_errors": 0}

    def _call(self, pdu: bytes, retry: bool) -> bytes:
        last: Exception = ModbusTimeout("no attempt")
        for attempt in range(1 + (self.retries if retry else 0)):
            if attempt:
                self.stats["retries"] += 1
            self.stats["requests"] += 1
            if self.mode == "tcp":
                self._tid = (self._tid + 1) & 0xFFFF
                frame = tcp_frame(self._tid, self.unit, pdu)
            else:
                frame = rtu_frame(self.unit, pdu)
            try:
                raw = self.link.exchange(frame, self.timeout)
                if self.mode == "tcp":
                    tid, unit, resp = tcp_parse(raw)
                    if tid != self._tid:
                        raise ModbusTransient("transaction id mismatch")
                else:
                    unit, resp = rtu_parse(raw)
                if unit != self.unit:
                    raise ModbusTransient("unit id mismatch")
                parse_response(pdu, resp)              # validate now so malformed replies are retried too
                return resp
            except ModbusException:
                raise                                   # the device answered: don't retry
            except ModbusTransient as e:
                self.stats["transient_errors"] += 1
                last = e
        raise last

    def _read(self, fc: int, addr: int, qty: int) -> list:
        pdu = pdu_read(fc, addr, qty)
        return parse_response(pdu, self._call(pdu, retry=True))

    def read_coils(self, addr: int, qty: int) -> list[bool]: return self._read(READ_COILS, addr, qty)
    def read_discrete_inputs(self, addr: int, qty: int) -> list[bool]: return self._read(READ_DISCRETE, addr, qty)
    def read_holding_registers(self, addr: int, qty: int) -> list[int]: return self._read(READ_HOLDING, addr, qty)
    def read_input_registers(self, addr: int, qty: int) -> list[int]: return self._read(READ_INPUT, addr, qty)

    def _write(self, pdu: bytes, fc: int, addr: int, values: Any) -> bool:
        if not self.allow_write:
            raise WriteDisabled("client is read-only; construct with allow_write=True to enable writes")
        if self.write_guard is not None and not self.write_guard(fc, addr, values):
            raise WriteDenied(f"write guard refused function {fc} at address {addr}")
        return parse_response(pdu, self._call(pdu, retry=False))   # side effects: never auto-retried

    def write_single_coil(self, addr: int, on: bool) -> bool:
        return self._write(pdu_write_coil(addr, on), WRITE_COIL, addr, on)

    def write_single_register(self, addr: int, value: int) -> bool:
        return self._write(pdu_write_reg(addr, value), WRITE_REG, addr, value)

    def write_multiple_coils(self, addr: int, values: list[bool]) -> bool:
        return self._write(pdu_write_coils(addr, values), WRITE_COILS, addr, list(values))

    def write_multiple_registers(self, addr: int, values: list[int]) -> bool:
        return self._write(pdu_write_regs(addr, values), WRITE_REGS, addr, list(values))


# ---- TANTU driver ------------------------------------------------------------------
class ModbusSensorDriver(SensorDriver):
    """Reads register(s) and maps them to a DRISHTI measurement: value = decoded * scale + offset."""

    TYPES = {"u16": (1, ">H"), "i16": (1, ">h"), "u32": (2, ">I"), "i32": (2, ">i"), "f32": (2, ">f")}

    def __init__(self, client: ModbusClient, address: int, measurement_type: str, unit: str, kind: str = "holding",
                 data_type: str = "u16", scale: float = 1.0, offset: float = 0.0, word_order: str = "big") -> None:
        if data_type not in self.TYPES or kind not in ("holding", "input") or word_order not in ("big", "little"):
            raise ValueError("bad driver configuration")
        self.client, self.address, self.kind = client, address, kind
        self.measurement_type, self.unit = measurement_type, unit
        self.data_type, self.scale, self.offset, self.word_order = data_type, scale, offset, word_order

    def read(self) -> Reading | None:
        n, fmt = self.TYPES[self.data_type]
        try:
            regs = (self.client.read_holding_registers if self.kind == "holding"
                    else self.client.read_input_registers)(self.address, n)
        except ModbusError as e:
            raise DriverError(str(e)) from e
        if self.word_order == "little":
            regs = regs[::-1]
        raw = struct.unpack(fmt, struct.pack(f">{n}H", *regs))[0]
        return Reading(raw * self.scale + self.offset)
