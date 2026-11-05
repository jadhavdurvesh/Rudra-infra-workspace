"""Helper processes for tests/test_fault_injection.py (run as: python _child.py <command> <args...>)."""
import json
import os
import signal
import sys
import time

cmd, args = sys.argv[1], sys.argv[2:]
try:
    import resource
except ImportError:  # pragma: no cover
    resource = None


def limit_file_size(n):
    """Make writes fail with EFBIG once any file would exceed n bytes (a faithful 'disk full')."""
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (n, resource.RLIM_INFINITY))


def lift_limit():
    resource.setrlimit(resource.RLIMIT_FSIZE, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))


if cmd == "akshara_emit":
    from firebot_infra.akshara import Recorder
    rec = Recorder(args[0], "run", software_version="child", fsync_interval_s=0.1)
    for i in range(int(args[1])):
        rec.emit("child", "tick", i=i)
    print("EMITTED", flush=True)
    time.sleep(120)                                   # never closed cleanly: the parent kills us

elif cmd == "akshara_disk_full":
    from firebot_infra.akshara import Recorder
    limit_file_size(int(args[1]))
    rec = Recorder(args[0], "run", fsync_interval_s=0.05)
    raised = 0
    for i in range(int(args[2])):
        try:
            rec.emit("child", "tick", i=i, pad="x" * 200)
        except Exception:
            raised += 1
    time.sleep(0.6)
    rec.close()
    print(json.dumps({"emit_exceptions": raised, "write_errors": rec._write_errors, "written": rec._written,
                      "dropped": rec._dropped}), flush=True)

elif cmd == "spool_disk_full":
    from firebot_infra.ananta import Spool
    sp = Spool(args[0], max_bytes=10 ** 9)
    limit_file_size(int(args[1]))
    ok = fails = 0
    for i in range(int(args[2])):
        if sp.append({"event_id": f"e{i}", "pad": "x" * 100}):
            ok += 1
        else:
            fails += 1
    lift_limit()
    for i in range(int(args[2]), int(args[2]) + 50):
        ok += 1 if sp.append({"event_id": f"e{i}", "pad": "x" * 100}) else 0
    print(json.dumps({"ok": ok, "fails": fails}), flush=True)

elif cmd == "dhara_disk_full":
    from firebot_infra.dhara import DharaStore
    s = DharaStore(args[0], sync="none")
    limit_file_size(int(args[1]))
    n = int(args[2])
    acked, failed = [], 0
    for i in range(n):
        try:
            s.put(f"k{i:05d}", b"v" * 100)
            acked.append(i)
        except OSError:
            failed += 1
            break
    lift_limit()                                      # space was freed; the application carries on
    for i in range(n, n + 50):
        s.put(f"k{i:05d}", b"v" * 100)
        acked.append(i)
    s.close()
    s2 = DharaStore(args[0])
    present = sorted(int(k[1:]) for k in s2.keys())
    s2.close()
    print(json.dumps({"acked": acked, "present": present, "failed": failed}), flush=True)

elif cmd == "dhara_writer":
    from firebot_infra.dhara import DharaStore
    s = DharaStore(args[0], sync="none")
    print("READY", flush=True)
    i = 0
    while True:
        s.put(f"k{i:08d}", b"x" * 50)
        i += 1

elif cmd == "sangam_writer":
    from pathlib import Path
    from firebot_infra.sangam import Policy, Store, TypePolicy
    st = Store("robot", [TypePolicy("run_log", Policy.APPEND_ONLY)], path=Path(args[0]) / "s.json")
    print("READY", flush=True)
    for i in range(20000):
        st.put("run_log", None, i)

elif cmd == "pravah_blocker":
    from pathlib import Path
    from firebot_infra.pravah import Scheduler, Task
    s = Scheduler({"slow": lambda a, t: t.wait(120)}, store_path=Path(args[0]) / "tasks.json", max_workers=2)
    s.submit(Task("idem", "slow", idempotent=True), Task("side", "slow"), Task("later", "slow", prerequisites=["idem"]))
    while not (s.states()["idem"] == "running" and s.states()["side"] == "running"):
        time.sleep(0.02)
    print("READY", flush=True)
    time.sleep(120)

elif cmd == "tcp_server":
    from firebot_infra.setu import Node, SchemaRegistry, TcpTransport
    t = TcpTransport(port=int(args[0]))
    t.add_peer("client", "127.0.0.1", int(args[1]))
    reg = SchemaRegistry()
    reg.register("ping", 1, {"n": int})
    n = Node("server", t, reg)
    n.on("ping", lambda e: {"n": e.payload["n"] + 1})
    print("READY", flush=True)
    time.sleep(120)
