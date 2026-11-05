"""Black-box replay for the test workload, built on firebot_infra.akshara.replay_run: re-feed a recorded run's raw sensor
inputs into a FRESH autonomy + guard and check they decide exactly what was recorded."""
from __future__ import annotations

from pathlib import Path

from firebot_infra.akshara import replay_run
from firebot_infra.drishti import Observation, StateEngine

from .autonomy import Autonomy, MotionGuard
from .config import Config, specs
from .runner import SimClock, autonomy_rng


def replay_decisions(run_dir: str | Path) -> dict:
    def setup(conf):
        cfg = Config(**conf["cfg"])
        clock, engine = SimClock(), None
        engine = StateEngine(clock=clock)
        for sp in specs():
            engine.register(sp)
        return {"clock": clock, "engine": engine, "autonomy": Autonomy(engine, cfg, autonomy_rng(conf["seed"])),
                "guard": MotionGuard(engine, cfg)}

    def step(ctx, ev):
        p = ev.payload
        ctx["clock"].now = p["t"]
        for sid, mtype, value, unit, mt, quality in p["obs"]:
            ctx["engine"].ingest(Observation(sid, mtype, value, unit, mt, "odom" if mtype == "pose" else None, quality))
        if p["abort"]:
            ctx["autonomy"].request_abort(p["abort"])
        cmd = ctx["autonomy"].step(ctx["clock"].now)
        out = ctx["guard"].filter(cmd)
        return {"state": cmd.state, "cmd": [cmd.v, cmd.w], "out": list(out[:2]), "veto": out[2]}

    res = replay_run(run_dir, step, setup=setup, select=lambda e: e.subsystem == "trace",
                     expected=lambda e: {k: e.payload[k] for k in ("state", "cmd", "out", "veto")})
    first = res.mismatches[0].event.payload["t"] if res.mismatches else None
    return {"steps": res.steps, "mismatches": len(res.mismatches), "first_mismatch_t": first,
            "recording_clean": res.trustworthy, "identical": res.identical}
