"""Shared interpretation of the range fan (used by autonomy AND the motion guard)."""
from __future__ import annotations

import math

from .config import Config, RANGE_ANGLES


def forward_clearance(ranges: dict[str, float], cfg: Config, heading: float = 0.0) -> float | None:
    """How far the robot BODY (a circle with a safety margin) can travel along `heading` (radians relative to its
    current heading) before touching an obstacle point seen by the beams. None = the beams give no information there.
    Exact for point obstacles: travel = px - sqrt(r^2 - py^2) for every return with |py| < r."""
    r = cfg.robot_radius_m + cfg.body_margin_m
    best, informed = cfg.range_max_m, False
    for name, ang in RANGE_ANGLES.items():
        rel = ang - heading
        if abs(rel) > 1.1:
            continue                                   # looks beside/behind the intended path
        informed = True
        d = ranges[name]
        if d >= cfg.range_max_m - 0.1:
            continue                                   # no return
        px, py = d * math.cos(rel), d * math.sin(rel)
        if px > 0 and abs(py) < r:
            best = min(best, px - math.sqrt(r * r - py * py))
    return max(0.0, best) if informed else None


def side_room(ranges: dict[str, float], cfg: Config) -> tuple[float, float]:
    """(left, right) free space summed over the three beams on each side; larger = more open."""
    left = sum(ranges[k] for k in ("range_p30", "range_p60", "range_p90"))
    right = sum(ranges[k] for k in ("range_m30", "range_m60", "range_m90"))
    return left, right
