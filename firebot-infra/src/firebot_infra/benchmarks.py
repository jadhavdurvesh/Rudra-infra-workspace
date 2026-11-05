"""Throughput benchmarks on SYNTHETIC data (dossier: never use real field data for performance tests).
Numbers are specific to the machine they ran on; measure on the actual robot computer before drawing conclusions."""
from __future__ import annotations

import json
import os
import platform
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

Result = dict[str, Any]


def _timed(fn: Callable[[], Any]) -> tuple[float, Any]:
    t0 = time.perf_counter()
    out = fn()
    return time.perf_counter() - t0, out


def _row(name: str, metric: str, value: float, unit: str, note: str = "") -> Result:
    return {"component": name, "metric": metric, "value": round(value, 2), "unit": unit, "note": note}


def bench_recorder(n: int) -> list[Result]:
    from .akshara import Recorder
    with tempfile.TemporaryDirectory() as d:
        rec = Recorder(d, "b", queue_size=n + 10)
        t_emit, _ = _timed(lambda: [rec.emit("bench", "tick", i=i, v=1.5) for i in range(n)])
        t_total, _ = _timed(rec.close)
    return [_row("AKSHARA", "emit() call rate (what the robot code pays)", n / t_emit, "events/s"),
            _row("AKSHARA", "end-to-end rate incl. disk write", n / (t_emit + t_total), "events/s")]


def bench_drishti(n: int) -> list[Result]:
    from .drishti import MeasurementSpec, Observation, StateEngine
    eng = StateEngine(clock=lambda: 100.0)
    eng.register(MeasurementSpec("t", "degC", max_age_s=5.0, min_value=-50, max_value=500, flatline_window_s=2.0))
    rng = random.Random(1)
    obs = [Observation(f"s{i % 8}", "t", 20 + rng.random(), "degC", 100.0 + i * 1e-6) for i in range(n)]
    t, _ = _timed(lambda: [eng.ingest(o) for o in obs])
    t2, _ = _timed(lambda: [eng.get(f"s{i % 8}", "t") for i in range(n)])
    return [_row("DRISHTI", "ingest (validate+store)", n / t, "obs/s"), _row("DRISHTI", "get (status + freshness)", n / t2, "reads/s")]


def bench_setu(n: int) -> list[Result]:
    from .setu import MemoryHub, Node, SchemaRegistry, TcpTransport
    reg = SchemaRegistry(); reg.register("ping", 1, {"n": int})
    out = []
    hub = MemoryHub()
    a, b = Node("a", hub.transport("a"), reg), Node("b", hub.transport("b"), reg)
    b.on("ping", lambda e: {"n": 1})
    t, _ = _timed(lambda: [a.request("b", "ping", {"n": i}, timeout=2) for i in range(n)])
    out.append(_row("SETU", "request/response, in-process", n / t, "req/s"))
    a.close(); b.close()
    ta, tb = TcpTransport(), TcpTransport()
    ta.add_peer("b", "127.0.0.1", tb.port); tb.add_peer("a", "127.0.0.1", ta.port)
    a, b = Node("a", ta, reg), Node("b", tb, reg)
    b.on("ping", lambda e: {"n": 1})
    t, _ = _timed(lambda: [a.request("b", "ping", {"n": i}, timeout=2) for i in range(n)])
    out.append(_row("SETU", "request/response, TCP loopback", n / t, "req/s"))
    a.close(); b.close()
    try:
        from .kavach import EnvelopeSigner, EnvelopeVerifier, Identity, Policy, Registry
    except ImportError:
        return out
    reg_k = Registry(); ids = {x: Identity.generate(x) for x in ("a", "b")}
    for x in ids:
        reg_k.enroll(x, ids[x].public_bytes(), "r")
    pol = Policy({"r": {"c"}})
    hub = MemoryHub()
    a = Node("a", hub.transport("a"), reg, sign=EnvelopeSigner(ids["a"]),
             verify_response=EnvelopeVerifier(reg_k, pol, {}, replay_window=n + 10).verify_response)
    b = Node("b", hub.transport("b"), reg, sign=EnvelopeSigner(ids["b"]),
             authorize=EnvelopeVerifier(reg_k, pol, {"ping": "c"}, replay_window=n + 10))
    b.on("ping", lambda e: {"n": 1})
    t, _ = _timed(lambda: [a.request("b", "ping", {"n": i}, timeout=2) for i in range(n)])
    out.append(_row("SETU+KAVACH", "request/response, every message signed (Ed25519)", n / t, "req/s"))
    a.close(); b.close()
    return out


def bench_framing(mb: float) -> list[Result]:
    from .framing import FrameParser, encode_frame
    payload = os.urandom(200)
    frame = encode_frame(payload)
    blob = frame * int(mb * 1e6 / len(frame))
    p = FrameParser()
    t, ev = _timed(lambda: p.feed(blob))
    assert sum(1 for e in ev if e.kind == "frame") == len(blob) // len(frame)
    return [_row("framing", "parse + CRC32 check", len(blob) / 1e6 / t, "MB/s")]


def bench_lipi(mb: float) -> list[Result]:
    from .lipi import benchmark
    rng = random.Random(2)
    lines = [json.dumps({"t": 1.8e9 + i * 0.1, "sensor": f"s{i % 5}", "v": round(20 + rng.random() * 3, 3), "ok": True}) + "\n"
             for i in range(int(mb * 1e6 / 70))]
    out = []
    for r in benchmark([("".join(lines)).encode()], repeat=1):
        out.append(_row("LIPI", f"{r['codec']}: ratio / compress / decompress", r["ratio"], "x",
                        f"{r['enc_mb_s']:.0f} MB/s enc, {r['dec_mb_s']:.0f} MB/s dec"))
    return out


def bench_dhara(n: int) -> list[Result]:
    from .dhara import compare
    with tempfile.TemporaryDirectory() as d:
        r = compare(d, n=n)
    return [_row("DHARA", f"{k} put", v["put_ops_s"], "ops/s", f"get {v['get_ops_s']:.0f} ops/s") for k, v in r.items()]


def bench_ananta(n: int) -> list[Result]:
    from .akshara import Recorder
    from .ananta import Connector, Spool, SqliteAnalytics, dashboards
    with tempfile.TemporaryDirectory() as d:
        db = SqliteAnalytics()
        conn = Connector(Spool(Path(d) / "s"), db, device_id="b", salt="s", batch_size=1000)
        rec = Recorder(Path(d) / "r", "b", queue_size=n + 10, tee=conn.log)
        for i in range(n):
            rec.emit("bench", "tick", severity="info" if i % 10 else "warning", i=i, duration_ms=1.0)
        rec.close()
        t, _ = _timed(lambda: [conn.flush(max_batches=1000)])
        tq, _ = _timed(lambda: dashboards.full_report(db))
        assert db.count() == n
    return [_row("ANANTA", "spool -> batches -> analytics store", n / t, "rows/s"),
            _row("ANANTA", f"full dashboard report over {n} rows", tq * 1000, "ms")]


def bench_spanda() -> list[Result]:
    from .spanda import compare_windows
    t = compare_windows(windows=(1, 32), n_messages=100, link_kw={"latency": 0.05})
    return [_row("SPANDA", "100 msgs over 100 ms RTT: stop-and-wait vs window 32", t[1] / t[32], "x faster",
                 f"{t[1]:.1f}s vs {t[32]:.2f}s simulated")]


def run_all(quick: bool = False, only: set[str] | None = None) -> dict[str, Any]:
    k = 1 if quick else 8
    plan: dict[str, Callable[[], list[Result]]] = {
        "recorder": lambda: bench_recorder(2000 * k), "drishti": lambda: bench_drishti(20000 * k),
        "setu": lambda: bench_setu(300 * k), "framing": lambda: bench_framing(1 * k),
        "lipi": lambda: bench_lipi(0.3 * k), "dhara": lambda: bench_dhara(1000 * k),
        "ananta": lambda: bench_ananta(3000 * k), "spanda": bench_spanda,
    }
    rows: list[Result] = []
    for name, fn in plan.items():
        if only and name not in only:
            continue
        rows += fn()
    return {"machine": {"python": sys.version.split()[0], "platform": platform.platform(), "cpus": os.cpu_count()},
            "quick": quick, "results": rows}


def to_markdown(res: dict[str, Any]) -> str:
    m = res["machine"]
    out = [f"Machine: Python {m['python']}, {m['platform']}, {m['cpus']} CPUs. Synthetic data. "
           f"{'Quick mode (small sizes).' if res['quick'] else ''}", "",
           "| component | metric | value | unit | note |", "|---|---|---|---|---|"]
    out += [f"| {r['component']} | {r['metric']} | {r['value']:g} | {r['unit']} | {r['note']} |" for r in res["results"]]
    return "\n".join(out)
