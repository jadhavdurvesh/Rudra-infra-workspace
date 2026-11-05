"""Explicit drivers. Nothing is auto-detected: a driver exists only if it is registered by name."""
from __future__ import annotations

import struct
import threading
from collections import deque
from typing import Any, Callable, Iterable

from ..drishti import MeasurementSpec
from ..framing import FrameParser
from .runtime import Binding, DriverError, Reading, SensorDriver


class SimSensor(SensorDriver):
    """Scriptable sensor for simulation and tests, with fault injection."""

    def __init__(self, measurement_type: str, unit: str, source: Iterable[float] | Callable[[], float] = (),
                 default: float | None = None) -> None:
        self.measurement_type, self.unit = measurement_type, unit
        self._src = source if callable(source) else iter(source)
        self._default = default
        self._faults: deque[Callable[[], Reading | None]] = deque()
        self.resets = 0

    def inject_error(self, n: int = 1, message: str = "simulated device error") -> None:
        def boom() -> Reading | None:
            raise DriverError(message)
        self._faults.extend([boom] * n)

    def inject_no_data(self, n: int = 1) -> None:
        self._faults.extend([lambda: None] * n)

    def inject_value(self, value: Any, n: int = 1, **kw: Any) -> None:
        self._faults.extend([lambda: Reading(value, **kw)] * n)

    def block_until(self, event: threading.Event) -> None:
        def hang() -> Reading | None:
            event.wait()
            return None
        self._faults.append(hang)

    def read(self) -> Reading | None:
        if self._faults:
            return self._faults.popleft()()
        if callable(self._src):
            return Reading(self._src())
        try:
            return Reading(next(self._src))
        except StopIteration:
            if self._default is None:
                return None
            return Reading(self._default)

    def reset(self) -> None:
        self.resets += 1


def struct_parser(fmt: str) -> Callable[[bytes], float]:
    """Payload -> first struct field, e.g. struct_parser('>f') for a big-endian float32."""
    st = struct.Struct(fmt)
    return lambda payload: st.unpack(payload)[0]


class UartSensorDriver(SensorDriver):
    """Reads framed sensor packets (framing.py) from a byte stream; the newest valid packet wins."""

    def __init__(self, stream: Any, measurement_type: str, unit: str,
                 parse: Callable[[bytes], Any], max_payload: int = 64) -> None:
        self.stream, self.measurement_type, self.unit, self._parse = stream, measurement_type, unit, parse
        self._max = max_payload
        self._parser = FrameParser(max_payload)
        self.stats = {"frames": 0, "bad_frames": 0, "parse_errors": 0}

    def read(self) -> Reading | None:
        try:
            data = self.stream.read(256)
        except EOFError as e:
            raise DriverError("stream closed") from e
        latest = None
        for ev in self._parser.feed(data) if data else []:
            if ev.kind != "frame":
                self.stats["bad_frames"] += 1
                continue
            try:
                v = self._parse(ev.payload)  # type: ignore[arg-type]
            except (struct.error, ValueError, IndexError):
                self.stats["parse_errors"] += 1
                continue
            self.stats["frames"] += 1
            latest = v if isinstance(v, Reading) else Reading(v)
        return latest

    def reset(self) -> None:
        self._parser = FrameParser(self._max)

    def close(self) -> None:
        close = getattr(self.stream, "close", None)
        if close:
            close()


DRIVERS: dict[str, Callable[..., SensorDriver]] = {}


def register_driver(name: str, factory: Callable[..., SensorDriver]) -> None:
    if name in DRIVERS:
        raise ValueError(f"driver {name!r} already registered")
    DRIVERS[name] = factory


register_driver("sim", SimSensor)
register_driver("uart_frames", UartSensorDriver)


def build_bindings(config: list[dict[str, Any]], resources: dict[str, Any] | None = None) -> list[Binding]:
    """Build sensors from plain config. A param value like "@port1" is replaced by resources["port1"]
    (streams/handles can't live in a config file). Unknown drivers are an error, never a guess."""
    resources, out = resources or {}, []
    for c in config:
        name = c["driver"]
        if name not in DRIVERS:
            raise ValueError(f"unknown driver {name!r}: register it explicitly with register_driver()")
        params = {k: (resources[v[1:]] if isinstance(v, str) and v.startswith("@") else v)
                  for k, v in c.get("params", {}).items()}
        spec = MeasurementSpec(**c["spec"]) if c.get("spec") else None
        out.append(Binding(c["sensor_id"], DRIVERS[name](**params), c["period_s"], spec,
                           c.get("max_failures", 5), c.get("slow_read_s"), c.get("max_backoff_s", 5.0)))
    return out
