"""Simulated sensors as TANTU drivers (so the robot software uses the same path real drivers will)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from firebot_infra.tantu import DriverError, Reading, SensorDriver

from ..config import Config, RANGE_ANGLES, THERMAL
from .world import World


@dataclass(frozen=True)
class Fault:
    sensor_id: str
    kind: str          # dropout (no data) | nan (garbage) | stuck (repeats last value: NOT detectable by freshness)
    t0: float
    t1: float


@dataclass
class FaultPlan:
    faults: list[Fault] = field(default_factory=list)

    def active(self, sensor_id: str, t: float) -> str | None:
        for f in self.faults:
            if f.sensor_id == sensor_id and f.t0 <= t < f.t1:
                return f.kind
        return None


class _SimSensor(SensorDriver):
    def __init__(self, world: World, sensor_id: str, plan: FaultPlan, mtype: str, unit: str) -> None:
        self.world, self.sensor_id, self.plan = world, sensor_id, plan
        self.measurement_type, self.unit = mtype, unit
        self.frame_id: str | None = None
        self._last = None

    def _measure(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def read(self) -> Reading | None:
        kind = self.plan.active(self.sensor_id, self.world.t)
        if kind == "dropout":
            raise DriverError(f"{self.sensor_id}: no response")
        if kind == "stuck" and self._last is not None:
            return Reading(self._last, frame_id=self.frame_id)
        v = self._measure()
        if kind == "nan":
            v = (math.nan,) * 3 if isinstance(v, tuple) else math.nan
        else:
            self._last = v
        return Reading(v, frame_id=self.frame_id)


class Thermal(_SimSensor):
    def __init__(self, world: World, sid: str, angle: float, cfg: Config, plan: FaultPlan) -> None:
        super().__init__(world, sid, plan, "temperature", "degC")
        self.angle, self.cfg = angle, cfg

    def _measure(self) -> float:
        w, a = self.world, self.world.th + self.angle
        px, py = w.x + self.cfg.probe_m * math.cos(a), w.y + self.cfg.probe_m * math.sin(a)
        return w.temperature(px, py) + w.sensor_rng.gauss(0, 0.5)


class RangeFinder(_SimSensor):
    def __init__(self, world: World, sid: str, angle: float, cfg: Config, plan: FaultPlan) -> None:
        super().__init__(world, sid, plan, "distance", "m")
        self.angle, self.cfg = angle, cfg

    def _measure(self) -> float:
        w = self.world
        d = w.ray(w.x, w.y, w.th + self.angle, self.cfg.range_max_m) + w.sensor_rng.gauss(0, 0.02)
        return min(self.cfg.range_max_m, max(0.0, d))


class Odometry(_SimSensor):
    """Pose in the 'odom' frame with slowly accumulating drift (like wheel odometry)."""

    def __init__(self, world: World, plan: FaultPlan) -> None:
        super().__init__(world, "pose", plan, "pose", "pose_xyt")
        self.frame_id = "odom"
        self._dx = self._dy = self._dth = 0.0

    def _measure(self):
        w = self.world
        self._dx += w.drift_rng.gauss(0, 0.002)
        self._dy += w.drift_rng.gauss(0, 0.002)
        self._dth += w.drift_rng.gauss(0, 0.001)
        return (w.x + self._dx, w.y + self._dy, w.th + self._dth)


class Battery(_SimSensor):
    def __init__(self, world: World, plan: FaultPlan) -> None:
        super().__init__(world, "battery", plan, "battery", "%")

    def _measure(self) -> float:
        return self.world.battery


def make_drivers(world: World, cfg: Config, plan: FaultPlan | None = None) -> dict[str, SensorDriver]:
    plan = plan or FaultPlan()
    ta = cfg.thermal_angle
    d: dict[str, SensorDriver] = {
        "thermal_l": Thermal(world, "thermal_l", +ta, cfg, plan), "thermal_c": Thermal(world, "thermal_c", 0.0, cfg, plan),
        "thermal_r": Thermal(world, "thermal_r", -ta, cfg, plan),
        "pose": Odometry(world, plan), "battery": Battery(world, plan),
    }
    for name, ang in RANGE_ANGLES.items():
        d[name] = RangeFinder(world, name, ang, cfg, plan)
    return d
