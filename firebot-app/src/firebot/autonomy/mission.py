"""First behavior: search the room, find the heat source, stop at a safe standoff and report where it is.

States: SEARCH -> APPROACH -> FOUND, with SAFE_HOLD (any sensor not VALID), NOT_FOUND (time out) and ABORTED.
Autonomy only emits velocity REQUESTS (Command); it never touches the world. It trusts a sensor only when
DRISHTI says VALID. It does not (and must not) fight fires: no suppression logic lives here.
"""
from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from firebot_infra.drishti import Status

from ..config import Config, RANGE, RANGE_ANGLES, SENSOR_TYPES, THERMAL
from ..geometry import clamp, wrap
from ..perception import forward_clearance, side_room


class State(str, Enum):
    SEARCH = "search"
    APPROACH = "approach"
    SAFE_HOLD = "safe_hold"
    FOUND = "found"
    NOT_FOUND = "not_found"
    ABORTED = "aborted"


TERMINAL = (State.FOUND, State.NOT_FOUND, State.ABORTED)


@dataclass(frozen=True)
class Command:
    v: float
    w: float
    state: str
    note: str = ""


@dataclass(frozen=True)
class FireReport:
    x: float
    y: float
    rise_c: float
    sim_time: float


class Autonomy:
    def __init__(self, view: Any, cfg: Config = Config(), rng: random.Random | None = None,
                 emit: Callable[..., Any] | None = None) -> None:
        self.view, self.cfg, self.rng, self._emit = view, cfg, rng or random.Random(0), emit
        self.state = State.SEARCH
        self.reason = ""
        self.report: FireReport | None = None
        self.holds = 0
        self.recoveries = 0
        self._abort: str | None = None
        self._t0: float | None = None
        self._resume: State | None = None
        self._hold_start = 0.0
        self._good = 0
        self.visits: dict[tuple[int, int], int] = {}
        self.occupied: set[tuple[int, int]] = set()
        self._target: float | None = None
        self._last_replan = 0.0
        self._prog: tuple[float, float, float] | None = None       # (t, x, y): "am I getting anywhere" check
        self._moves: deque[tuple[float, float, float]] = deque(maxlen=15)   # (x, y, commanded v) for jam detection
        self._rise_hist: deque[float] = deque(maxlen=5)
        self._lost_since: float | None = None
        self._avoid_until = 0.0
        self._avoid_dir = 1.0

    # ---- public --------------------------------------------------------------
    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def pending_abort(self) -> str | None:
        return self._abort

    def request_abort(self, reason: str) -> None:
        self._abort = reason

    def step(self, t: float) -> Command:
        cfg = self.cfg
        if self._t0 is None:
            self._t0 = t
        if self.terminal:
            return self._stop("terminal")
        if self._abort:
            self._enter(State.ABORTED, f"operator: {self._abort}", t)
            return self._stop("operator abort")
        s = {k: self.view.get(k, SENSOR_TYPES[k]) for k in SENSOR_TYPES}
        bad = sorted(k for k, sv in s.items() if sv.status is not Status.VALID)
        if s["battery"].status is Status.VALID and s["battery"].value < cfg.low_battery_pct:
            self._enter(State.ABORTED, "low_battery", t)
            return self._stop("low battery")
        if bad:
            if self.state is not State.SAFE_HOLD:
                self._resume, self._hold_start = self.state, t
                self.holds += 1
                self._enter(State.SAFE_HOLD, "untrusted: " + ",".join(bad), t)
            self._good = 0
            if t - self._hold_start > cfg.hold_timeout_s:
                self._enter(State.ABORTED, "sensor_failure", t)
            return self._stop("sensors untrusted")
        if self.state is State.SAFE_HOLD:
            self._good += 1
            if self._good < cfg.resume_after_steps:
                return self._stop("waiting for stable sensors")
            self._target = None
            self._moves.clear()
            self._enter(self._resume or State.SEARCH, "sensors_recovered", t)
        if t - self._t0 > cfg.max_mission_s:
            self._enter(State.NOT_FOUND, "time_limit", t)
            return self._stop("time limit")
        x, y, th = s["pose"].value
        ranges = {k: s[k].value for k in RANGE}
        temps = [s[k].value for k in THERMAL]
        self._update_map(x, y, th, ranges)
        if self.state is State.SEARCH:
            if max(temps) - cfg.ambient_c >= cfg.detect_rise_c:
                self._enter(State.APPROACH, f"heat_detected rise={max(temps) - cfg.ambient_c:.0f}C", t)
                self._rise_hist.clear()
                self._lost_since = None
                self._target = None
                return self._remember(self._approach(t, x, y, th, ranges, temps), x, y)
            return self._remember(self._search(t, x, y, th, ranges), x, y)
        return self._remember(self._approach(t, x, y, th, ranges, temps), x, y)

    # ---- helpers ---------------------------------------------------------------
    def _stop(self, note: str) -> Command:
        return Command(0.0, 0.0, self.state.value, note)

    def _enter(self, new: State, reason: str, t: float) -> None:
        old, self.state, self.reason = self.state, new, reason
        if self._emit:
            self._emit("autonomy", "state_change", "warning" if new in (State.SAFE_HOLD, State.ABORTED) else "info",
                       old=old.value, new=new.value, reason=reason, sim_t=round(t, 2))

    def _remember(self, cmd: Command, x: float, y: float) -> Command:
        self._moves.append((x, y, cmd.v))
        return cmd

    def _jammed(self) -> bool:
        """Commanded to drive for a while but the position hasn't changed: something is holding the robot."""
        if len(self._moves) < self._moves.maxlen:
            return False
        if min(v for _, _, v in self._moves) < 0.2:
            return False
        (x0, y0, _), (x1, y1, _) = self._moves[0], self._moves[-1]
        return math.hypot(x1 - x0, y1 - y0) < 0.05

    def _cell(self, x: float, y: float) -> tuple[int, int]:
        c = self.cfg.cell_m
        return (math.floor(x / c), math.floor(y / c))

    def _update_map(self, x: float, y: float, th: float, ranges: dict[str, float]) -> None:
        self.visits[self._cell(x, y)] = self.visits.get(self._cell(x, y), 0) + 1
        for name, ang in RANGE_ANGLES.items():
            d = ranges[name]
            if d < self.cfg.range_max_m - 0.1:           # a real return, not "nothing in range"
                a = th + ang
                self.occupied.add(self._cell(x + (d + 0.1) * math.cos(a), y + (d + 0.1) * math.sin(a)))

    # ---- search ----------------------------------------------------------------------
    def _score(self, x: float, y: float, heading: float) -> float:
        score, step = 0.0, self.cfg.cell_m
        for i in range(1, 7):
            c = self._cell(x + i * step * math.cos(heading), y + i * step * math.sin(heading))
            if c in self.occupied:
                break
            if self.visits.get(c, 0) == 0:
                score += 1
        return score

    def _choose(self, x: float, y: float, th: float, ranges: dict[str, float], avoiding: bool) -> float:
        cfg = self.cfg
        need = 0.9 if avoiding else 0.6
        boxed = (forward_clearance(ranges, cfg) or 0.0) < cfg.avoid_travel_m and max(side_room(ranges, cfg)) < 3.0
        best, best_h = -1e9, th
        for a in (0.0, 0.785, -0.785, 1.571, -1.571, 2.356, -2.356, 3.1416):
            room = forward_clearance(ranges, cfg, a)
            sc = self._score(x, y, th + a)
            if room is None:
                sc = sc - 2 if boxed else -100               # nothing sees that way: only when boxed in
            elif room < need:
                sc = -100
            sc += self.rng.random() * 0.4
            if sc > best:
                best, best_h = sc, wrap(th + a)
        return best_h

    def _turn(self, th: float) -> Command | None:
        err = wrap(self._target - th)  # type: ignore[operator]
        if abs(err) < 0.1:
            self._target = None
            return None
        return Command(0.0, clamp(2.5 * err, -1.2, 1.2), self.state.value, "turning")

    def _push_away(self, ranges: dict[str, float]) -> float:
        """Steering correction that nudges the robot away from anything close on either side."""
        w = 0.0
        for k in ("range_p30", "range_p60", "range_p90"):
            w -= 1.0 * max(0.0, 0.8 - ranges[k])
        for k in ("range_m30", "range_m60", "range_m90"):
            w += 1.0 * max(0.0, 0.8 - ranges[k])
        return w

    def _search(self, t: float, x: float, y: float, th: float, ranges: dict[str, float]) -> Command:
        cfg = self.cfg
        if self._target is not None:
            c = self._turn(th)
            if c:
                return c
        if self._jammed():
            self.recoveries += 1
            left, right = side_room(ranges, cfg)
            self._target = wrap(th + (1.7 if left >= right else -1.7))
            self._moves.clear()
            if self._emit:
                self._emit("autonomy", "jam_recovery", "warning", sim_t=round(t, 2))
            return self._turn(th) or self._stop("recovering")
        if self._prog is None or t - self._prog[0] >= 6.0:
            if self._prog and math.hypot(x - self._prog[1], y - self._prog[2]) < 0.4:   # not getting anywhere
                self._target = wrap(th + self.rng.choice((-1, 1)) * (1.6 + self.rng.random()))
            self._prog = (t, x, y)
        fc = forward_clearance(ranges, cfg)
        if fc is not None and fc < cfg.avoid_travel_m:
            self._target = self._choose(x, y, th, ranges, True)
            c = self._turn(th)
            if c:
                return c
        elif t - self._last_replan > 5.0:
            self._last_replan = t
            ahead = self._score(x, y, th)
            best = self._choose(x, y, th, ranges, False)
            if self._score(x, y, best) >= ahead + 2:
                self._target = best
                c = self._turn(th)
                if c:
                    return c
        v = cfg.cruise_v if (fc is None or fc > 1.2) else 0.2
        return Command(v, self._push_away(ranges), self.state.value, "exploring")

    # ---- approach ----------------------------------------------------------------------
    def _approach(self, t: float, x: float, y: float, th: float, ranges: dict[str, float],
                  temps: list[float]) -> Command:
        cfg = self.cfg
        tl, tc, tr = temps
        rise = tc - cfg.ambient_c
        peak = max(temps) - cfg.ambient_c
        self._rise_hist.append(rise)
        if peak < cfg.lost_rise_c:
            if self._lost_since is None:
                self._lost_since = t
            elif t - self._lost_since > cfg.lost_after_s:
                self._enter(State.SEARCH, "heat_lost", t)
                return self._stop("heat lost")
        else:
            self._lost_since = None
        if len(self._rise_hist) >= 3 and sum(list(self._rise_hist)[-3:]) / 3 >= cfg.standoff_rise_c:
            return self._found(t, x, y, th)
        if self._jammed():
            self.recoveries += 1
            left, right = side_room(ranges, cfg)
            self._avoid_dir, self._avoid_until = (1.0 if left >= right else -1.0), t + 1.5
            self._moves.clear()
        fc = forward_clearance(ranges, cfg)
        if (fc is not None and fc < cfg.avoid_travel_m) or t < self._avoid_until:   # something solid in the way
            if t >= self._avoid_until:
                left, right = side_room(ranges, cfg)
                self._avoid_dir = 1.0 if left >= right else -1.0
                self._avoid_until = t + 0.8
            return Command(0.0, self._avoid_dir * 1.0, self.state.value, "avoiding obstacle")
        w = clamp(3.0 * (tl - tr) / max(1.0, max(temps) - cfg.ambient_c), -1.2, 1.2) + 0.5 * self._push_away(ranges)
        return Command(cfg.approach_v if abs(w) < 0.8 else 0.1, w, self.state.value, "following heat")

    def _found(self, t: float, x: float, y: float, th: float) -> Command:
        cfg = self.cfg
        rise = sum(self._rise_hist) / len(self._rise_hist)
        d = cfg.cal_sigma_m * math.sqrt(2 * math.log(cfg.cal_amplitude_c / max(rise, 1e-6)))   # invert the thermal model
        px, py = x + cfg.probe_m * math.cos(th), y + cfg.probe_m * math.sin(th)
        self.report = FireReport(px + d * math.cos(th), py + d * math.sin(th), rise, t)
        self._enter(State.FOUND, f"standoff reached rise={rise:.0f}C", t)
        if self._emit:
            self._emit("autonomy", "fire_located", "warning", x=round(self.report.x, 2), y=round(self.report.y, 2),
                       rise_c=round(rise, 1), sim_t=round(t, 2))
        return self._stop("fire located")
