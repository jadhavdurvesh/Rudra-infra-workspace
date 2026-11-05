from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .world import Fire, Rect, World


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    width: float
    height: float
    obstacles: tuple[Rect, ...]
    fires: tuple[Fire, ...]
    start: tuple[float, float, float]


SCENARIOS: dict[str, Scenario] = {s.name: s for s in (
    Scenario("empty_room", "8 x 6 m empty room, one heat source", 8, 6, (), (Fire(6.2, 4.3),), (1.0, 1.0, 0.0)),
    Scenario("obstacles", "10 x 7 m room with furniture-like blocks, one heat source", 10, 7,
             (Rect(3, 1, 4, 3), Rect(5.5, 3.5, 7, 4.5), Rect(2, 4.5, 3, 6)), (Fire(8.2, 5.3),), (1.0, 1.0, 0.0)),
    Scenario("no_fire", "8 x 6 m room, nothing burning: the robot must say so", 8, 6,
             (Rect(3, 2, 4, 4),), (), (1.0, 1.0, 0.0)),
)}


def build_world(s: Scenario, seed: int = 0, battery_start: float = 100.0, randomize: bool = False) -> World:
    """randomize=True: put the fire at a seeded random free spot (at least 3.5 m from the start, 1 m from any
    obstacle or wall) and give the robot a random start heading, to test the search rather than one layout."""
    fires, start = s.fires, s.start
    if randomize and s.fires:
        rng = random.Random(seed + 99)
        start = (start[0], start[1], rng.uniform(-math.pi, math.pi))
        for _ in range(1000):
            x, y = rng.uniform(1.0, s.width - 1.0), rng.uniform(1.0, s.height - 1.0)
            near = any(o.x0 - 1 <= x <= o.x1 + 1 and o.y0 - 1 <= y <= o.y1 + 1 for o in s.obstacles)
            if not near and math.hypot(x - start[0], y - start[1]) >= 3.5:
                fires = (Fire(x, y),)
                break
    return World(s.width, s.height, s.obstacles, fires, start, seed=seed, battery_start=battery_start)
