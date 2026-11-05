"""Runs one simulated mission with the whole infrastructure wired in.

sim sensors -> TANTU runtime -> DRISHTI -> autonomy -> MotionGuard -> simulated robot
every system's events -> AKSHARA (black box, with per-step trace for replay) -> ANANTA connector (analytics rows)
robot <-> operator station over SETU (heartbeats, mission report, operator abort)
"""
from __future__ import annotations

import json
import math
import random
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from firebot_infra.akshara import Recorder
from firebot_infra.ananta import Connector, JsonlFileSink, Spool
from firebot_infra.drishti import StateEngine
from firebot_infra.setu import MemoryHub, Node, SchemaRegistry
from firebot_infra.tantu import Binding, PeripheralRuntime

from .autonomy import Autonomy, Command, MotionGuard, State
from .config import Config, specs
from .sim import SCENARIOS, Fault, FaultPlan, World, build_world, make_drivers


class SimClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def autonomy_rng(seed: int) -> random.Random:
    return random.Random(seed * 7919 + 13)


@dataclass
class MissionResult:
    scenario: str
    seed: int
    outcome: str                     # found | not_found | aborted
    reason: str
    sim_time: float
    distance_m: float
    coverage: float
    collisions: int
    holds: int
    vetoes: int
    estimated_fire: tuple[float, float] | None
    true_fire: tuple[float, float] | None
    error_m: float | None
    report_delivered: bool
    events_recorded: int = 0
    run_dir: str = ""
    analytics_rows: int = 0
    world: World | None = field(default=None, repr=False)
    station_reports: list = field(default_factory=list, repr=False)

    def summary(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("world"); d.pop("station_reports")
        return d


def _schema() -> SchemaRegistry:
    r = SchemaRegistry()
    r.register("mission_report", 1, {"outcome": str, "sim_time": float, "coverage": float, "x": float, "y": float},
               optional=("x", "y"))
    r.register("heartbeat", 1, {"t": float, "state": str})
    r.register("abort", 1, {"reason": str})
    return r


def run_mission(scenario: str = "obstacles", seed: int = 0, *, data_dir: str | Path | None = None,
                faults: tuple[Fault, ...] = (), battery_start: float = 100.0, abort_at: float | None = None,
                cfg: Config = Config(), max_time: float | None = None, randomize: bool = False) -> MissionResult:
    sc = SCENARIOS[scenario]
    world = build_world(sc, seed, battery_start, randomize)
    clock = SimClock()
    run_id = f"{scenario}-s{seed}-{int(time.time() * 1000) % 10_000_000}"

    rec = conn = None
    emit = lambda *a, **k: None  # noqa: E731
    if data_dir is not None:
        data_dir = Path(data_dir)
        conn = Connector(Spool(data_dir / "spool"), JsonlFileSink(data_dir / "analytics.jsonl"),
                         device_id="firebot-sim", salt="sim")
        rec = Recorder(data_dir / "runs", run_id, software_version="firebot-0.1.0",
                       config={"scenario": scenario, "seed": seed, "cfg": asdict(cfg), "battery_start": battery_start, "randomize": randomize,
                               "faults": [asdict(f) for f in faults]},
                       tee=lambda ev: None if ev.subsystem == "trace" else conn.log(ev))   # raw 10 Hz trace stays local
        emit = rec.emit

    engine = StateEngine(clock=clock, on_event=emit)
    for sp in specs():
        engine.register(sp)
    captured: list = []
    real_ingest = engine.ingest
    engine.ingest = lambda o: (captured.append(o), real_ingest(o))[1]      # keep raw inputs for replay
    runtime = PeripheralRuntime(engine, clock=clock, on_event=emit)
    for sid, drv in make_drivers(world, cfg, FaultPlan(list(faults))).items():
        runtime.bind(Binding(sid, drv, cfg.dt, max_failures=10_000))

    autonomy = Autonomy(engine, cfg, autonomy_rng(seed), emit)
    guard = MotionGuard(engine, cfg, emit)

    schema = _schema()
    hub = MemoryHub()
    robot = Node("robot", hub.transport("robot"), schema, on_event=emit)
    station = Node("station", hub.transport("station"), schema)
    reports: list[dict] = []
    station.on("mission_report", lambda e: reports.append(e.payload) or {})
    station.on("heartbeat", lambda e: {})
    robot.on("abort", lambda e: autonomy.request_abort(e.payload["reason"]) or {})

    out_v = out_w = 0.0
    last_hb = -1.0
    aborted_sent = False
    limit = max_time if max_time is not None else cfg.max_mission_s + 5
    try:
        while clock.now < limit:
            world.step(out_v, out_w, cfg.dt)
            clock.now = round(clock.now + cfg.dt, 6)
            world.t = clock.now
            captured.clear()
            for sid in runtime._b:
                runtime.poll(sid)
            if abort_at is not None and not aborted_sent and clock.now >= abort_at:
                aborted_sent = True
                station.request("robot", "abort", {"reason": "operator test"}, timeout=1.0)
            pending = autonomy.pending_abort
            cmd: Command = autonomy.step(clock.now)
            out_v, out_w, veto = guard.filter(cmd)
            emit("trace", "step", "debug", t=clock.now, abort=pending, state=cmd.state,
                 cmd=[cmd.v, cmd.w], out=[out_v, out_w], veto=veto,
                 obs=[[o.sensor_id, o.measurement_type, list(o.value) if isinstance(o.value, tuple) else o.value,
                       o.unit, o.measurement_time, o.quality] for o in captured])
            if clock.now - last_hb >= 1.0:
                last_hb = clock.now
                robot.send_event("station", "heartbeat", {"t": float(clock.now), "state": cmd.state})
            if autonomy.terminal:
                break
        outcome = {State.FOUND: "found", State.NOT_FOUND: "not_found"}.get(autonomy.state, "aborted")
        if autonomy.state not in (State.FOUND, State.NOT_FOUND, State.ABORTED):
            outcome, autonomy.reason = "aborted", "mission_time_limit"
        payload: dict[str, Any] = {"outcome": outcome, "sim_time": float(clock.now), "coverage": float(world.coverage())}
        if autonomy.report:
            payload.update(x=float(autonomy.report.x), y=float(autonomy.report.y))
        delivered = False
        try:
            robot.request("station", "mission_report", payload, timeout=1.0, retries=2, idempotency_key=run_id)
            delivered = True
        except Exception:
            delivered = False
        emit("mission", "result", "info", outcome=outcome, reason=autonomy.reason, sim_time=round(clock.now, 2),
             success=outcome == "found", delivered=delivered)
    finally:
        robot.close(); station.close()
        if rec:
            rec.close()
    rows = 0
    if conn:
        conn.flush()
        p = Path(data_dir) / "analytics.jsonl"          # type: ignore[arg-type]
        rows = len(p.read_text().splitlines()) if p.exists() else 0

    tf = (world.fires[0].x, world.fires[0].y) if world.fires else None
    est = (autonomy.report.x, autonomy.report.y) if autonomy.report else None
    err = math.hypot(est[0] - tf[0], est[1] - tf[1]) if est and tf else None
    return MissionResult(scenario, seed, outcome, autonomy.reason, round(clock.now, 2), round(world.distance, 2),
                         round(world.coverage(), 3), world.collisions, autonomy.holds, guard.vetoes, est, tf,
                         None if err is None else round(err, 3), delivered,
                         events_recorded=(rec._seq if rec else 0), run_dir=str(rec.dir) if rec else "",
                         analytics_rows=rows, world=world, station_reports=reports)
