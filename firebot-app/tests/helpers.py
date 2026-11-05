from firebot_infra.drishti import Observation, StateEngine

from firebot.config import RANGE, SENSOR_TYPES, THERMAL, Config, specs

UNITS = {"temperature": "degC", "distance": "m", "pose": "pose_xyt", "battery": "%"}


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


def make_engine():
    c = Clock()
    e = StateEngine(clock=c)
    for s in specs():
        e.register(s)
    return c, e


def feed(engine, clock, t, *, drop=(), pose=(1.0, 1.0, 0.0), battery=90.0, **over):
    """Ingest one full, healthy sensor frame at time t (override or drop individual sensors)."""
    clock.now = t
    vals = {k: 22.0 for k in THERMAL}
    vals.update({k: 4.0 for k in RANGE})
    vals.update(pose=pose, battery=battery)
    vals.update(over)
    n = int(round(t * 10))
    for i, k in enumerate(THERMAL):                  # real probes always jitter; constant synthetic data would look "frozen"
        vals[k] = vals[k] + ((n * 7 + i) % 11) * 0.002
    for sid, mtype in SENSOR_TYPES.items():
        if sid in drop:
            continue
        engine.ingest(Observation(sid, mtype, vals[sid], UNITS[mtype], t, "odom" if mtype == "pose" else None))
