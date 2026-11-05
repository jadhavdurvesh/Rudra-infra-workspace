"""DRISHTI: qualified sensor state. Provides data quality, never decisions or actuation.

- Every observation carries units, measurement time, frame and quality.
- Stale values are returned marked STALE, never silently presented as current.
- Units/frames that do not match the spec are rejected unless an explicit conversion/transform exists.
- Rejected observations are preserved (bounded) for diagnosis.
Times are monotonic seconds on the robot host; use the injected clock in tests.
"""
from __future__ import annotations

import math
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

Number = float
Value = Any  # float or tuple[float, ...]


class Status(str, Enum):
    VALID = "valid"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Observation:
    sensor_id: str
    measurement_type: str
    value: Value
    unit: str
    measurement_time: float
    frame_id: str | None = None
    quality: str = "good"            # good | degraded | bad
    config_ref: str | None = None    # calibration/config identifier
    receive_time: float | None = None


@dataclass(frozen=True)
class MeasurementSpec:
    measurement_type: str
    unit: str
    max_age_s: float
    dims: int = 1
    min_value: float | None = None
    max_value: float | None = None
    frame_id: str | None = None
    max_future_s: float = 0.5        # tolerated clock skew for timestamps "in the future"
    # Opt-in stuck-sensor check: a value that stays within flatline_epsilon of the same reference for
    # flatline_window_s (and at least flatline_min_samples readings) is reported DEGRADED with reason "flatline".
    # Only enable it for measurements that always jitter (noisy analog sensors). A legitimately constant or
    # saturated reading (digital counter, beam at max range) would raise false alarms.
    flatline_window_s: float | None = None
    flatline_epsilon: float = 0.0
    flatline_min_samples: int = 5


@dataclass(frozen=True)
class StateValue:
    status: Status
    value: Value | None = None
    unit: str | None = None
    age_s: float | None = None
    measurement_time: float | None = None
    frame_id: str | None = None
    quality: str | None = None
    config_ref: str | None = None
    reason: str | None = None        # why the status is not VALID: stale | driver_quality | flatline


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    reason: str | None = None
    status: Status | None = None


DEFAULT_CONVERSIONS: dict[tuple[str, str], Callable[[float], float]] = {
    ("mm", "m"): lambda v: v / 1000.0, ("cm", "m"): lambda v: v / 100.0, ("km", "m"): lambda v: v * 1000.0,
    ("ms", "s"): lambda v: v / 1000.0, ("degF", "degC"): lambda v: (v - 32.0) * 5.0 / 9.0,
    ("K", "degC"): lambda v: v - 273.15, ("kPa", "Pa"): lambda v: v * 1000.0,
    ("mV", "V"): lambda v: v / 1000.0, ("mA", "A"): lambda v: v / 1000.0,
}


def _normalize(value: Any, dims: int) -> Value:
    def one(x: Any) -> float:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise ValueError("non-numeric or non-finite")
        return float(x)
    if dims == 1:
        return one(value)
    if not isinstance(value, (tuple, list)) or len(value) != dims:
        raise ValueError("wrong dimensionality")
    return tuple(one(x) for x in value)


def _diff(a: Value, b: Value) -> float:
    if isinstance(a, tuple):
        return max(abs(x - y) for x, y in zip(a, b))
    return abs(a - b)


def _map(fn: Callable[[float], float], v: Value) -> Value:
    return tuple(fn(x) for x in v) if isinstance(v, tuple) else fn(v)


class StateEngine:
    def __init__(self, clock: Callable[[], float] = time.monotonic, on_event: Callable[..., Any] | None = None,
                 rejected_keep: int = 200, auto_convert: bool = True, emit_interval_s: float = 5.0) -> None:
        self._clock = clock
        self._on_event = on_event      # Recorder.emit-compatible (observability path)
        self._specs: dict[str, MeasurementSpec] = {}
        self._state: dict[tuple[str, str], Observation] = {}
        self._last_status: dict[tuple[str, str], Status] = {}
        self._flat: dict[tuple[str, str], list] = {}        # key -> [reference value, reference time, sample count]
        self._conv = dict(DEFAULT_CONVERSIONS) if auto_convert else {}
        self._transforms: dict[tuple[str, str], Callable[[Value], Value]] = {}
        self._subs: list[Callable[[str, str, StateValue], None]] = []
        self._lock = threading.RLock()
        self.rejected: deque[tuple[float, str, Observation]] = deque(maxlen=rejected_keep)
        self.counts: Counter[str] = Counter()
        self._emit_interval = emit_interval_s
        self._last_emit: dict[tuple[str, str], float] = {}

    # ---- configuration -----------------------------------------------
    def register(self, spec: MeasurementSpec) -> None:
        self._specs[spec.measurement_type] = spec

    def add_conversion(self, from_unit: str, to_unit: str, fn: Callable[[float], float]) -> None:
        self._conv[(from_unit, to_unit)] = fn

    def add_transform(self, src_frame: str, dst_frame: str, fn: Callable[[Value], Value]) -> None:
        self._transforms[(src_frame, dst_frame)] = fn

    def subscribe(self, cb: Callable[[str, str, StateValue], None]) -> None:
        self._subs.append(cb)

    # ---- ingest -------------------------------------------------------
    def ingest(self, obs: Observation) -> IngestResult:
        now = self._clock()
        spec = self._specs.get(obs.measurement_type) if isinstance(obs.measurement_type, str) else None
        if spec is None:
            return self._reject("unknown_type", obs, now)
        if not isinstance(obs.sensor_id, str) or not obs.sensor_id or not isinstance(obs.unit, str):
            return self._reject("malformed", obs, now)
        if isinstance(obs.measurement_time, bool) or not isinstance(obs.measurement_time, (int, float)) \
                or not math.isfinite(obs.measurement_time):
            return self._reject("malformed", obs, now)
        try:
            value = _normalize(obs.value, spec.dims)
        except ValueError:
            return self._reject("malformed", obs, now)
        if obs.unit != spec.unit:
            fn = self._conv.get((obs.unit, spec.unit))
            if fn is None:
                return self._reject("unit_mismatch", obs, now)
            value = _map(fn, value)
        if spec.frame_id is not None and obs.frame_id != spec.frame_id:
            if obs.frame_id is None:
                return self._reject("frame_missing", obs, now)
            tf = self._transforms.get((obs.frame_id, spec.frame_id))
            if tf is None:
                return self._reject("frame_mismatch", obs, now)  # never combine frames without a transform
            value = tf(value)
        vals = value if isinstance(value, tuple) else (value,)
        if (spec.min_value is not None and any(v < spec.min_value for v in vals)) or \
           (spec.max_value is not None and any(v > spec.max_value for v in vals)):
            return self._reject("out_of_range", obs, now)
        if obs.measurement_time > now + spec.max_future_s:
            return self._reject("future_timestamp", obs, now)
        if obs.quality not in ("good", "degraded", "bad"):
            return self._reject("malformed", obs, now)
        if obs.quality == "bad":
            return self._reject("bad_quality", obs, now)
        key = (obs.sensor_id, obs.measurement_type)
        with self._lock:
            prev = self._state.get(key)
            if prev is not None and obs.measurement_time < prev.measurement_time:
                return self._reject("out_of_order", obs, now)
            stored = Observation(obs.sensor_id, obs.measurement_type, value, spec.unit, float(obs.measurement_time),
                                 spec.frame_id, obs.quality, obs.config_ref, receive_time=now)
            self._state[key] = stored
            self.counts["accepted"] += 1
            if spec.flatline_window_s is not None:
                f = self._flat.get(key)
                if f is None or _diff(f[0], value) > spec.flatline_epsilon:
                    self._flat[key] = [value, float(obs.measurement_time), 1]
                else:
                    f[2] += 1
        sv = self.get(*key)
        self._transition(key, sv)
        for cb in list(self._subs):
            try:
                cb(obs.sensor_id, obs.measurement_type, sv)
            except Exception:
                self.counts["subscriber_errors"] += 1
        return IngestResult(True, None, sv.status)

    def _reject(self, reason: str, obs: Observation, now: float) -> IngestResult:
        with self._lock:
            self.counts["rejected"] += 1
            self.counts[f"rejected_{reason}"] += 1
            self.rejected.append((now, reason, obs))
        k = (str(getattr(obs, "sensor_id", "?")), reason)
        if now - self._last_emit.get(k, -1e18) >= self._emit_interval:
            self._last_emit[k] = now
            self._emit("rejected", "warning", sensor_id=k[0], reason=reason,
                       measurement_type=str(getattr(obs, "measurement_type", "?")))
        return IngestResult(False, reason, None)

    # ---- query ----------------------------------------------------------
    def get(self, sensor_id: str, measurement_type: str) -> StateValue:
        with self._lock:
            o = self._state.get((sensor_id, measurement_type))
        if o is None:
            return StateValue(Status.UNAVAILABLE)
        spec = self._specs[measurement_type]
        age = self._clock() - o.measurement_time
        reason = None
        if age > spec.max_age_s:
            status, reason = Status.STALE, "stale"
        elif o.quality == "degraded":
            status, reason = Status.DEGRADED, "driver_quality"
        else:
            status = Status.VALID
            f = self._flat.get((sensor_id, measurement_type)) if spec.flatline_window_s is not None else None
            if f and f[2] >= spec.flatline_min_samples and o.measurement_time - f[1] >= spec.flatline_window_s:
                status, reason = Status.DEGRADED, "flatline"
        return StateValue(status, o.value, o.unit, age, o.measurement_time, o.frame_id, o.quality, o.config_ref, reason)

    def latest_valid(self, sensor_id: str, measurement_type: str) -> Value | None:
        """Value only if currently VALID; otherwise None. Use get() to see degraded/stale values."""
        sv = self.get(sensor_id, measurement_type)
        return sv.value if sv.status == Status.VALID else None

    def snapshot(self) -> dict[tuple[str, str], StateValue]:
        with self._lock:
            keys = list(self._state)
        return {k: self.get(*k) for k in keys}

    def check_freshness(self) -> list[tuple[str, str, Status]]:
        """Call periodically: reports (and emits) status transitions such as VALID -> STALE."""
        changed = []
        for key in list(self._state):
            sv = self.get(*key)
            if self._transition(key, sv):
                changed.append((key[0], key[1], sv.status))
        return changed

    def health(self) -> dict[str, Any]:
        snap = self.snapshot()
        by = Counter(v.status.value for v in snap.values())
        why = Counter(v.reason for v in snap.values() if v.reason)
        return {"sensors": len(snap), "by_status": dict(by), "by_reason": dict(why), "counts": dict(self.counts)}

    # ---- internals ------------------------------------------------------
    def _transition(self, key: tuple[str, str], sv: StateValue) -> bool:
        status = sv.status
        with self._lock:
            old = self._last_status.get(key)
            self._last_status[key] = status
        if old is not None and old != status:
            sev = "info" if status in (Status.VALID,) else "warning"
            self._emit("status_change", sev, sensor_id=key[0], measurement_type=key[1],
                       old=old.value, new=status.value, reason=sv.reason or "")
            return True
        return False

    def _emit(self, event_type: str, severity: str, **kw: Any) -> None:
        if self._on_event:
            try:
                self._on_event("drishti", event_type, severity, **kw)
            except Exception:
                pass  # observability must never affect state handling
