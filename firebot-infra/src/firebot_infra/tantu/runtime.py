"""TANTU: peripheral runtime. READ-ONLY sensor acquisition feeding DRISHTI.

There is deliberately no output/actuator API here: motors, valves and suppression are outside this
software's authority. Board/device specifics live in explicit driver classes; the runtime only knows
the SensorDriver contract. One thread per sensor, so a hung driver can't stall the others
(DRISHTI then reports that sensor STALE). Driver errors are contained, counted and rate-limited.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from ..drishti import MeasurementSpec, Observation, StateEngine


class DriverError(Exception): ...


@dataclass
class Reading:
    value: Any
    unit: str | None = None            # overrides driver.unit (DRISHTI converts known units)
    quality: str = "good"
    measurement_time: float | None = None   # use the real sample time when the device provides it
    frame_id: str | None = None
    config_ref: str | None = None


class SensorDriver(ABC):
    measurement_type: str
    unit: str

    @abstractmethod
    def read(self) -> Reading | None:
        """One acquisition. Return None for 'no new data'. Raise DriverError/OSError/TimeoutError on failure."""

    def reset(self) -> None:
        """Try to recover from a failed state (re-open port, flush buffers)."""

    def close(self) -> None: ...


@dataclass
class Binding:
    sensor_id: str
    driver: SensorDriver
    period_s: float
    spec: MeasurementSpec | None = None
    max_failures: int = 5
    slow_read_s: float | None = None
    max_backoff_s: float = 5.0


@dataclass
class SensorHealth:
    state: str = "starting"            # starting | ok | degraded | failed
    consecutive_failures: int = 0
    total_reads: int = 0
    total_errors: int = 0
    no_data: int = 0
    rejected: int = 0
    slow_reads: int = 0
    resets: int = 0
    last_success: float | None = None
    last_error: str = ""


class PeripheralRuntime:
    def __init__(self, engine: StateEngine, clock: Callable[[], float] = time.monotonic,
                 on_event: Callable[..., Any] | None = None) -> None:
        self.engine, self._clock, self._on_event = engine, clock, on_event
        self._b: dict[str, Binding] = {}
        self._h: dict[str, SensorHealth] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def bind(self, b: Binding) -> None:
        if b.sensor_id in self._b:
            raise ValueError(f"sensor {b.sensor_id!r} already bound")
        if b.spec is not None:
            self.engine.register(b.spec)
        self._b[b.sensor_id] = b
        self._h[b.sensor_id] = SensorHealth()

    def health(self) -> dict[str, SensorHealth]:
        with self._lock:
            return {k: SensorHealth(**v.__dict__) for k, v in self._h.items()}

    # ---- one acquisition cycle (also used directly by tests) ------------------
    def poll(self, sensor_id: str) -> bool:
        b, h = self._b[sensor_id], self._h[sensor_id]
        t0 = self._clock()
        try:
            r = b.driver.read()
        except Exception as e:  # any driver failure is contained here
            return self._fail(b, h, f"{type(e).__name__}: {e}")
        if b.slow_read_s is not None and self._clock() - t0 > b.slow_read_s:
            with self._lock:
                h.slow_reads += 1
        if r is None:
            with self._lock:
                h.no_data += 1
            return True
        obs = Observation(sensor_id, b.driver.measurement_type, r.value, r.unit or b.driver.unit,
                          self._clock() if r.measurement_time is None else r.measurement_time,
                          r.frame_id, r.quality, r.config_ref)
        res = self.engine.ingest(obs)
        if not res.accepted:
            with self._lock:
                h.rejected += 1
            return self._fail(b, h, f"rejected: {res.reason}", count_error=False)
        with self._lock:
            h.total_reads += 1
            h.consecutive_failures = 0
            h.last_success = self._clock()
        self._set_state(b, h, "ok")
        return True

    def _fail(self, b: Binding, h: SensorHealth, err: str, count_error: bool = True) -> bool:
        with self._lock:
            h.consecutive_failures += 1
            if count_error:
                h.total_errors += 1
            h.last_error = err
            failed = h.consecutive_failures >= b.max_failures
        self._set_state(b, h, "failed" if failed else "degraded")
        if failed:
            try:
                b.driver.reset()
                with self._lock:
                    h.resets += 1
            except Exception:
                pass
        return False

    def _set_state(self, b: Binding, h: SensorHealth, new: str) -> None:
        with self._lock:
            old, h.state = h.state, new
        if old != new and self._on_event:
            try:
                self._on_event("tantu", "sensor_state", "info" if new == "ok" else "warning",
                               sensor_id=b.sensor_id, old=old, new=new, error=h.last_error)
            except Exception:
                pass

    def next_delay(self, sensor_id: str) -> float:
        b, h = self._b[sensor_id], self._h[sensor_id]
        extra = max(0, h.consecutive_failures - b.max_failures + 1)
        return min(b.max_backoff_s, b.period_s * (2 ** min(extra, 10)))

    # ---- threads -----------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        for sid in self._b:
            t = threading.Thread(target=self._loop, args=(sid,), name=f"tantu-{sid}", daemon=True)
            t.start()
            self._threads.append(t)

    def _loop(self, sid: str) -> None:
        while not self._stop.is_set():
            try:
                self.poll(sid)
            except Exception:
                pass  # a bug in one sensor's path must not kill its thread
            if self._stop.wait(self.next_delay(sid)):
                break

    def stop(self, close_drivers: bool = True) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads.clear()
        if close_drivers:
            for b in self._b.values():
                try:
                    b.driver.close()
                except Exception:
                    pass
