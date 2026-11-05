from __future__ import annotations

import math


def wrap(a: float) -> float:
    """Wrap an angle to [-pi, pi)."""
    return (a + math.pi) % (2 * math.pi) - math.pi


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def ray_rect(x: float, y: float, dx: float, dy: float, r: "tuple[float, float, float, float]") -> float | None:
    """Distance along a ray (unit dx,dy) to an axis-aligned rectangle (x0,y0,x1,y1); None if it misses.
    Returns 0 when the origin is inside."""
    x0, y0, x1, y1 = r
    tmin, tmax = 0.0, math.inf
    for o, d, lo, hi in ((x, dx, x0, x1), (y, dy, y0, y1)):
        if abs(d) < 1e-12:
            if o < lo or o > hi:
                return None
            continue
        t1, t2 = (lo - o) / d, (hi - o) / d
        if t1 > t2:
            t1, t2 = t2, t1
        tmin, tmax = max(tmin, t1), min(tmax, t2)
        if tmin > tmax:
            return None
    return tmin


def segment_hits_rect(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float, float, float]) -> bool:
    dx, dy = q[0] - p[0], q[1] - p[1]
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]
    t = ray_rect(p[0], p[1], dx / length, dy / length, r)
    return t is not None and t <= length
