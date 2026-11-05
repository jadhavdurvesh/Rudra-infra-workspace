"""MotionGuard: a SIMULATION STAND-IN for the validated low-level safety layer.

Autonomy produces velocity REQUESTS; everything passes through the guard, which can only make a command safer
(slower or stopped) and never lets autonomy bypass it. On the real robot this job belongs to validated
hardware / low-level control (e-stop, motor drivers, interlocks), not to this Python code.
"""
from __future__ import annotations

from typing import Any, Callable

from firebot_infra.drishti import Status

from ..config import Config, RANGE
from ..geometry import clamp
from ..perception import forward_clearance
from .mission import Command


class MotionGuard:
    def __init__(self, view: Any, cfg: Config = Config(), emit: Callable[..., Any] | None = None) -> None:
        self.view, self.cfg, self._emit = view, cfg, emit
        self._v_prev = 0.0
        self._last_reason = ""
        self.vetoes = 0
        self.estop = False                     # latched stop (set by a human/hardware signal in a real system)

    def filter(self, cmd: Command) -> tuple[float, float, str]:
        cfg = self.cfg
        v = clamp(cmd.v, 0.0, cfg.max_v)       # no reversing: there is no rear sensor
        w = clamp(cmd.w, -cfg.max_w, cfg.max_w)
        reason = ""
        ranges = {k: self.view.get(k, "distance") for k in RANGE}
        if self.estop:
            v = w = 0.0
            reason = "estop"
        elif any(r.status is not Status.VALID for r in ranges.values()):
            v = w = 0.0
            reason = "range_untrusted"
        elif v > 0:
            fc = forward_clearance({k: r.value for k, r in ranges.items()}, cfg)
            if fc is not None and fc < cfg.stop_travel_m:
                v = 0.0
                reason = "front_blocked"
        v = min(v, self._v_prev + cfg.max_accel * cfg.dt)    # limit speeding up; stopping is immediate
        if reason and (abs(v - cmd.v) > 1e-9 or abs(w - cmd.w) > 1e-9):
            self.vetoes += 1
        self._v_prev = v
        if reason != self._last_reason and self._emit:
            self._last_reason = reason
            self._emit("guard", "veto" if reason else "clear", "warning" if reason else "info", reason=reason)
        return v, w, reason
