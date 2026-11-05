"""2-D simulated room: rectangular obstacles, point heat sources, a differential-drive robot.
Deterministic for a given seed. Units: metres, seconds, radians (counter-clockwise positive)."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from ..geometry import clamp, ray_rect, segment_hits_rect


@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    def t(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    def hits_circle(self, x: float, y: float, r: float) -> bool:
        cx, cy = clamp(x, self.x0, self.x1), clamp(y, self.y0, self.y1)
        return (x - cx) ** 2 + (y - cy) ** 2 < r * r


@dataclass(frozen=True)
class Fire:
    x: float
    y: float
    amplitude_c: float = 300.0
    sigma_m: float = 1.2


class World:
    OCCLUDED = 0.15      # fraction of heat reaching a point whose line of sight to the fire is blocked

    def __init__(self, width: float, height: float, obstacles: tuple[Rect, ...] = (), fires: tuple[Fire, ...] = (),
                 start: tuple[float, float, float] = (1.0, 1.0, 0.0), seed: int = 0, ambient_c: float = 22.0,
                 radius: float = 0.25, battery_start: float = 100.0) -> None:
        self.width, self.height, self.obstacles, self.fires = width, height, tuple(obstacles), tuple(fires)
        self.ambient_c, self.radius = ambient_c, radius
        self.x, self.y, self.th = start
        self.start = start
        self.t = 0.0
        self.battery = battery_start
        self.collisions = 0
        self.distance = 0.0
        self.path: list[tuple[float, float]] = [(self.x, self.y)]
        self.sensor_rng = random.Random(seed + 1)     # separate stream: reading sensors never changes the dynamics
        self.drift_rng = random.Random(seed + 2)

    # ---- dynamics ---------------------------------------------------------------
    def _blocked(self, x: float, y: float) -> bool:
        r = self.radius
        if x - r < 0 or y - r < 0 or x + r > self.width or y + r > self.height:
            return True
        return any(o.hits_circle(x, y, r) for o in self.obstacles)

    def step(self, v: float, w: float, dt: float) -> None:
        self.th = (self.th + w * dt + math.pi) % (2 * math.pi) - math.pi
        nx, ny = self.x + v * math.cos(self.th) * dt, self.y + v * math.sin(self.th) * dt
        if abs(v) > 1e-9:
            if self._blocked(nx, ny):
                self.collisions += 1
            else:
                self.distance += math.hypot(nx - self.x, ny - self.y)
                self.x, self.y = nx, ny
                self.path.append((nx, ny))
        self.battery = max(0.0, self.battery - (0.01 + 0.02 * abs(v)) * dt)
        self.t += dt

    # ---- physics queries ------------------------------------------------------------
    def ray(self, x: float, y: float, ang: float, max_range: float) -> float:
        dx, dy = math.cos(ang), math.sin(ang)
        best = max_range
        if dx > 1e-12:
            best = min(best, (self.width - x) / dx)
        elif dx < -1e-12:
            best = min(best, -x / dx)
        if dy > 1e-12:
            best = min(best, (self.height - y) / dy)
        elif dy < -1e-12:
            best = min(best, -y / dy)
        for o in self.obstacles:
            t = ray_rect(x, y, dx, dy, o.t())
            if t is not None and t < best:
                best = t
        return max(0.0, best)

    def line_clear(self, p: tuple[float, float], q: tuple[float, float]) -> bool:
        return not any(segment_hits_rect(p, q, o.t()) for o in self.obstacles)

    def temperature(self, x: float, y: float) -> float:
        rise = 0.0
        for f in self.fires:
            d2 = (x - f.x) ** 2 + (y - f.y) ** 2
            h = f.amplitude_c * math.exp(-d2 / (2 * f.sigma_m ** 2))
            rise += h if self.line_clear((x, y), (f.x, f.y)) else h * self.OCCLUDED
        return self.ambient_c + rise

    # ---- evaluation helpers (ground truth, never given to the robot) ----------------------
    def coverage(self, cell: float = 0.5) -> float:
        """Fraction of free floor cells the robot has passed through."""
        nx, ny = int(self.width / cell), int(self.height / cell)
        free = {(i, j) for i in range(nx) for j in range(ny)
                if not any(o.x0 <= (i + .5) * cell <= o.x1 and o.y0 <= (j + .5) * cell <= o.y1 for o in self.obstacles)}
        seen = {(int(px / cell), int(py / cell)) for px, py in self.path} & free
        return len(seen) / max(1, len(free))
