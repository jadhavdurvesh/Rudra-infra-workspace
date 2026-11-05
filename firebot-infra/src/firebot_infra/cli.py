"""firebot-infra command line: check, inspect and measure the infrastructure without writing code."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__


# ------------------------------------------------------------------ selftest
def _t_akshara(tmp: Path) -> str:
    from .akshara import Recorder, read_run
    with Recorder(tmp, "r") as rec:
        for i in range(50):
            rec.emit("x", "y", i=i)
    ev, rep = read_run(tmp / "r")
    assert rep.clean and len(ev) == 50, rep
    return "50 events recorded and verified"


def _t_drishti(tmp: Path) -> str:
    from .drishti import MeasurementSpec, Observation, StateEngine, Status
    now = [0.0]
    e = StateEngine(clock=lambda: now[0])
    e.register(MeasurementSpec("t", "degC", max_age_s=1.0, flatline_window_s=1.0))
    for i in range(20):
        now[0] = i * 0.1
        e.ingest(Observation("s", "t", 21.0, "degC", now[0]))
    assert e.get("s", "t").reason == "flatline"
    now[0] = 10.0
    assert e.get("s", "t").status is Status.STALE
    return "flatline and stale detection"


def _t_setu(tmp: Path) -> str:
    from .setu import MemoryHub, Node, SchemaRegistry
    reg = SchemaRegistry(); reg.register("ping", 1, {"n": int})
    hub = MemoryHub()
    a, b = Node("a", hub.transport("a"), reg), Node("b", hub.transport("b"), reg)
    ran = []
    b.on("ping", lambda e: ran.append(1) or {"n": e.payload["n"] + 1})
    try:
        assert a.request("b", "ping", {"n": 1}, timeout=2).payload == {"n": 2}
        hub.fault = lambda s, d, data: "dup"
        a.send_event("b", "ping", {"n": 1}); time.sleep(0.2)
        assert len(ran) == 2, ran                       # one request + one event; the duplicate delivery was ignored
    finally:
        a.close(); b.close()
    return "request/response, duplicate suppression"


def _t_tantu(tmp: Path) -> str:
    from .drishti import MeasurementSpec, StateEngine, Status
    from .tantu import Binding, PeripheralRuntime, SimSensor
    e = StateEngine()
    e.register(MeasurementSpec("t", "degC", max_age_s=5.0))
    rt = PeripheralRuntime(e)
    rt.bind(Binding("s", SimSensor("t", "degC", default=5.0), 0.1))
    assert rt.poll("s") and e.get("s", "t").status is Status.VALID
    return "simulated sensor -> DRISHTI"


def _t_pravah(tmp: Path) -> str:
    from .pravah import Scheduler, Task
    s = Scheduler({"w": lambda a, t: 1})
    try:
        s.submit(Task("a", "w"), Task("b", "w", prerequisites=["a"]))
        assert s.wait(3) and s.states() == {"a": "succeeded", "b": "succeeded"}
    finally:
        s.stop()
    return "dependent tasks ran in order"


def _t_lipi(tmp: Path) -> str:
    from .lipi import BlockReader, BlockWriter, RecordSchema
    sch = RecordSchema([("a", "u"), ("b", "str")])
    assert sch.decode(sch.encode({"a": 7, "b": "hi"})) == {"a": 7, "b": "hi"}
    with BlockWriter(tmp / "f.lipi", block_bytes=500) as w:
        for i in range(100):
            w.append(sch.encode({"a": i, "b": "x" * (i % 9)}))
    with BlockReader(tmp / "f.lipi") as r:
        assert sch.decode(r.get(57))["a"] == 57 and len(r) == 100
    return "schema codec + random-access block file"


def _t_dhara(tmp: Path) -> str:
    from .dhara import DharaStore
    with DharaStore(tmp / "db") as s:
        s.put("k", b"v"); s.write_batch([("put", "a", b"1"), ("delete", "k")])
    with DharaStore(tmp / "db") as s:
        assert s.get("a") == b"1" and s.get("k") is None
    return "write, batch, reopen"


def _t_sangam(tmp: Path) -> str:
    from .sangam import Policy, Store, TypePolicy, sync
    t = [TypePolicy("log", Policy.APPEND_ONLY)]
    a, b = Store("a", t), Store("b", t)
    a.put("log", None, 1); b.put("log", None, 2)
    sync(a, b)
    assert a.items("log") == b.items("log") and len(a.items("log")) == 2
    return "two peers reconciled"


def _t_tarang(tmp: Path) -> str:
    from .framing import encode_frame
    from .tarang import Capture, RawDecoder, decode_capture
    rep = decode_capture(Capture.from_bytes(encode_frame(b"a") + encode_frame(b"b")), RawDecoder())
    assert rep.clean and len(rep.frames) == 2
    return "frames decoded"


def _t_jala(tmp: Path) -> str:
    from .jala import MeshSim
    sim = MeshSim([("A", "B"), ("B", "C")])
    sim.run(12); sim.nodes["A"].send("C", b"x"); sim.run(2)
    assert sim.delivered["C"] == [("A", b"x")]
    return "2-hop mesh delivery"


def _t_spanda(tmp: Path) -> str:
    from .spanda import make_pair
    from .spanda.sim import run_until
    a, b, link, _, _, got, _ = make_pair({"latency": 0.01, "loss": 0.1, "seed": 1})
    msgs = [bytes([i]) * 700 for i in range(10)]
    for m in msgs:
        a.send_message(m)
    run_until(a, b, link, lambda: len(got) == 10)
    assert got == msgs
    return "10 messages over a 10% loss link"


def _t_kshiti(tmp: Path) -> str:
    from .kshiti import Compute, Kernel
    k = Kernel()
    def p():
        yield Compute(3)
    k.spawn(p)
    assert k.run() == "all_exited"
    return "kernel simulator ran a process"


def _t_edge(tmp: Path) -> str:
    from .setu_edge import ModbusClient, ModbusServer, ServerLink
    srv = ModbusServer(1, holding=4)
    srv.holding[1] = 99
    assert ModbusClient(ServerLink(srv), 1).read_holding_registers(1, 1) == [99]
    return "Modbus RTU loopback"


def _t_ananta(tmp: Path) -> str:
    from .akshara import Recorder
    from .ananta import Connector, Spool, SqliteAnalytics, dashboards
    db = SqliteAnalytics()
    conn = Connector(Spool(tmp / "sp"), db)
    with Recorder(tmp / "runs", "r", tee=conn.log) as rec:
        for i in range(20):
            rec.emit("x", "y", i=i)
    conn.flush()
    assert db.count() == 20 and "Overview" in dashboards.to_markdown(dashboards.full_report(db))
    return "events -> analytics -> report"


def _t_kavach(tmp: Path) -> str:
    try:
        from .kavach import Identity, Policy, Registry, Verifier
    except ImportError:
        raise _Skip("needs the optional 'cryptography' package: pip install firebot-infra[crypto]")
    ident, reg = Identity.generate("d"), Registry()
    reg.enroll("d", ident.public_bytes(), "r")
    v = Verifier("v", reg, Policy({"r": {"x"}}))
    n = v.challenge()
    assert v.verify("d", n, ident.prove(n, "v")).role == "r"
    return "device identity challenge-response"


class _Skip(Exception):
    pass


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("AKSHARA recorder", _t_akshara), ("DRISHTI state quality", _t_drishti), ("SETU messaging", _t_setu),
    ("TANTU sensors", _t_tantu), ("PRAVAH tasks", _t_pravah), ("LIPI encoding", _t_lipi), ("DHARA storage", _t_dhara),
    ("SANGAM sync", _t_sangam), ("TARANG diagnostics", _t_tarang), ("JALA mesh", _t_jala), ("SPANDA transport", _t_spanda),
    ("KSHITI simulator", _t_kshiti), ("SETU-EDGE Modbus", _t_edge), ("ANANTA analytics", _t_ananta), ("KAVACH identity", _t_kavach),
]


def cmd_selftest(a: argparse.Namespace) -> int:
    failed = 0
    print(f"firebot-infra {__version__} self-test (quick field check, not the full test suite)\n")
    for name, fn in CHECKS:
        with tempfile.TemporaryDirectory() as d:
            t0 = time.perf_counter()
            try:
                detail, status = fn(Path(d)), "PASS"
            except _Skip as e:
                detail, status = str(e), "SKIP"
            except Exception as e:  # noqa: BLE001
                detail, status = f"{type(e).__name__}: {e}", "FAIL"
                failed += 1
        print(f"  {status:4}  {name:24} {detail}  ({(time.perf_counter() - t0) * 1000:.0f} ms)")
    print("\n" + ("ALL CHECKS PASSED" if not failed else f"{failed} CHECK(S) FAILED"))
    return 1 if failed else 0


# ------------------------------------------------------------------ other commands
def _need_run_dir(path: str) -> str:
    """A recorded run is a directory that contains manifest.json or segment files; anything else is a typo."""
    p = Path(path)
    if not p.is_dir():
        raise FileNotFoundError(f"no such run directory: {path}")
    if not (p / "manifest.json").exists() and not any(p.glob("seg-*.jsonl")):
        raise ValueError(f"{path} does not look like a recorded run (no manifest.json or seg-*.jsonl)")
    return path


def cmd_run_info(a: argparse.Namespace) -> int:
    from .akshara import summarize_run
    _need_run_dir(a.run_dir)
    print(json.dumps(summarize_run(a.run_dir), indent=2, default=str))
    return 0


def cmd_diff_runs(a: argparse.Namespace) -> int:
    from .akshara import diff_runs
    d = diff_runs(_need_run_dir(a.run_a), _need_run_dir(a.run_b))
    print("identical" if d.identical else f"DIFFERENT: {d}")
    return 0 if d.identical else 1


def cmd_decode(a: argparse.Namespace) -> int:
    from .tarang import RawDecoder, SetuDecoder, decode_capture, load_capture
    cap, bad = load_capture(a.capture)
    rep = decode_capture(cap, SetuDecoder() if a.setu else RawDecoder())
    print(f"{rep.total_bytes} bytes, {len(rep.frames)} frames, {len(rep.annotations)} problems, "
          f"capture loss: {rep.capture_loss}, timestamps: {rep.timestamps}, unreadable capture lines: {bad}")
    for f in rep.frames[:a.max]:
        print(f"  frame @{f.offset} ({f.length} B) t={f.t}: {f.decoded}")
    for an in rep.annotations[:a.max]:
        print(f"  PROBLEM {an.kind} @{an.offset} ({an.length} B): {an.detail}")
    return 0 if rep.clean else 1


def _load_analytics(path: str):
    from .ananta import SqliteAnalytics
    return SqliteAnalytics.from_jsonl(path)


def cmd_report(a: argparse.Namespace) -> int:
    from .ananta import dashboards
    tables = dashboards.full_report(_load_analytics(a.events), a.run_id)
    if a.html:
        Path(a.html).write_text(dashboards.to_html(tables), encoding="utf-8")
        print(f"HTML report written to {a.html}")
    print(dashboards.to_markdown(tables))
    return 0


def cmd_compare(a: argparse.Namespace) -> int:
    from .ananta import dashboards
    t = dashboards.compare_versions(_load_analytics(a.events), a.baseline, a.candidate)
    print(t.to_markdown())
    return 1 if any(r[4] == "REGRESSION" for r in t.rows) else 0


def cmd_bench(a: argparse.Namespace) -> int:
    from . import benchmarks
    res = benchmarks.run_all(quick=a.quick, only=set(a.only) if a.only else None)
    print(benchmarks.to_markdown(res))
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2))
    return 0


def cmd_prune(a: argparse.Namespace) -> int:
    from .akshara import prune_runs
    if not Path(a.root).is_dir():
        raise FileNotFoundError(f"no such folder: {a.root}")
    hit = prune_runs(a.root, a.older_than_days * 86400, dry_run=not a.yes, include_incomplete=a.include_incomplete)
    for d in hit:
        print(("deleted " if a.yes else "would delete ") + str(d))
    print(f"{len(hit)} run(s) {'deleted' if a.yes else 'would be deleted (nothing changed: add --yes to delete)'}")
    return 0


def cmd_schema(a: argparse.Namespace) -> int:
    from .ananta import CLICKHOUSE_DDL
    print(CLICKHOUSE_DDL.strip())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="firebot-infra", description="Check, inspect and measure the Fire Bot infrastructure")
    ap.add_argument("--version", action="version", version=f"firebot-infra {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="quick check that every subsystem works on this machine").set_defaults(fn=cmd_selftest)
    p = sub.add_parser("run-info", help="summarise a recorded run (AKSHARA directory)"); p.add_argument("run_dir"); p.set_defaults(fn=cmd_run_info)
    p = sub.add_parser("diff-runs", help="compare two recorded runs"); p.add_argument("run_a"); p.add_argument("run_b"); p.set_defaults(fn=cmd_diff_runs)
    p = sub.add_parser("decode", help="decode a TARANG capture file"); p.add_argument("capture")
    p.add_argument("--setu", action="store_true", help="also decode payloads as SETU messages"); p.add_argument("--max", type=int, default=20)
    p.set_defaults(fn=cmd_decode)
    p = sub.add_parser("report", help="analytics report from ANANTA rows (JSONL)"); p.add_argument("events")
    p.add_argument("--run-id"); p.add_argument("--html", help="also write an HTML report here"); p.set_defaults(fn=cmd_report)
    p = sub.add_parser("compare-versions", help="did a software version make things worse?"); p.add_argument("events")
    p.add_argument("baseline"); p.add_argument("candidate"); p.set_defaults(fn=cmd_compare)
    p = sub.add_parser("bench", help="synthetic-data benchmarks"); p.add_argument("--quick", action="store_true")
    p.add_argument("--only", nargs="*"); p.add_argument("--json"); p.set_defaults(fn=cmd_bench)
    p = sub.add_parser("prune-runs", help="delete recordings older than N days (dry run unless --yes)"); p.add_argument("root")
    p.add_argument("--older-than-days", type=float, required=True); p.add_argument("--yes", action="store_true")
    p.add_argument("--include-incomplete", action="store_true", help="also remove runs that were never cleanly closed")
    p.set_defaults(fn=cmd_prune)
    sub.add_parser("schema", help="print the ClickHouse table definition").set_defaults(fn=cmd_schema)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, NotADirectoryError, ValueError, KeyError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
