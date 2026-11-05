"""Whole-system fault injection (dossier Paper 11): REAL process kills (SIGKILL), a REAL 'disk full' (OS file-size
limit), and a REAL network peer dying and coming back. POSIX only."""
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import firebot_infra
from firebot_infra.akshara import read_run
from firebot_infra.ananta import Spool
from firebot_infra.dhara import DharaStore
from firebot_infra.pravah import Scheduler
from firebot_infra.sangam import Policy, Store, TypePolicy
from firebot_infra.setu import Node, SchemaRegistry, SetuError, TcpTransport

if os.name != "posix":
    raise unittest.SkipTest("fault-injection tests use POSIX signals and resource limits")

CHILD = Path(__file__).with_name("_child.py")
SRC = str(Path(firebot_infra.__file__).resolve().parent.parent)


def spawn(*args):
    env = dict(os.environ, PYTHONPATH=SRC)
    return subprocess.Popen([sys.executable, str(CHILD), *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, text=True)


def wait_for(p, token, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if select.select([p.stdout], [], [], 0.2)[0]:
            line = p.stdout.readline()
            if token in line:
                return
            if not line and p.poll() is not None:
                break
    raise AssertionError(f"child never printed {token!r}; stderr: {p.stderr.read() if p.poll() is not None else '(running)'}")


def kill9(p):
    p.send_signal(signal.SIGKILL)
    p.wait(10)


def run_to_end(*args, timeout=60):
    p = spawn(*args)
    out, err = p.communicate(timeout=timeout)
    assert p.returncode == 0, f"child failed ({p.returncode}): {err}"
    return json.loads(out.strip().splitlines()[-1])


class Tmp(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.dir = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()


class ProcessCrashTests(Tmp):
    def test_recording_survives_sigkill_and_is_marked_incomplete(self):
        p = spawn("akshara_emit", self.dir, 3000)
        wait_for(p, "EMITTED")
        time.sleep(0.8)                                  # the writer thread drains its queue
        kill9(p)
        events, rep = read_run(self.dir / "run")
        self.assertEqual(len(events), 3000)              # everything the writer had been handed is on disk
        self.assertFalse(rep.complete)                   # ...and the run is honestly flagged as not cleanly closed
        self.assertFalse(rep.clean)
        self.assertEqual(rep.corrupt_records, 0)
        self.assertEqual([e.seq for e in events], list(range(1, 3001)))

    def test_kill_in_the_middle_of_recording_leaves_a_clean_prefix(self):
        for delay in (0.0, 0.02, 0.05):
            d = self.dir / f"d{delay}"
            p = spawn("akshara_emit", d, 4000)
            time.sleep(0.15 + delay)                      # usually mid-stream
            kill9(p)
            if not (d / "run").exists():
                continue
            events, rep = read_run(d / "run")
            self.assertEqual([e.seq for e in events], list(range(1, len(events) + 1)), delay)   # prefix, no holes
            self.assertEqual(rep.corrupt_records, 0)
            self.assertFalse(rep.complete)

    def test_dhara_recovers_to_a_contiguous_prefix_after_sigkill(self):
        p = spawn("dhara_writer", self.dir / "db")
        wait_for(p, "READY")
        time.sleep(0.4)
        kill9(p)
        with DharaStore(self.dir / "db") as s:
            keys = [k.decode() for k in s.keys()]
        self.assertGreater(len(keys), 100)
        self.assertEqual(keys, [f"k{j:08d}" for j in range(len(keys))])   # no gaps, nothing out of order

    def test_sangam_state_file_is_never_half_written(self):
        p = spawn("sangam_writer", self.dir)
        wait_for(p, "READY")
        time.sleep(0.5)
        kill9(p)
        st = Store("robot", [TypePolicy("run_log", Policy.APPEND_ONLY)], path=self.dir / "s.json")
        n = st.version_vector().get("robot", 0)
        self.assertGreater(n, 0)
        self.assertEqual(len(st.items("run_log")), n)

    def test_pravah_resumes_only_what_is_safe_after_sigkill(self):
        p = spawn("pravah_blocker", self.dir)
        wait_for(p, "READY")
        kill9(p)
        s = Scheduler({"slow": lambda a, t: "done"}, store_path=self.dir / "tasks.json")
        try:
            self.assertTrue(s.wait(5))
            self.assertEqual(s.states(), {"idem": "succeeded", "side": "failed", "later": "succeeded"})
            self.assertIn("not safe to re-run", s.status("side")["error"])
        finally:
            s.stop()


class DiskFullTests(Tmp):
    def test_recorder_degrades_without_raising_and_leaves_only_whole_records(self):
        r = run_to_end("akshara_disk_full", self.dir, 300_000, 4000)
        self.assertEqual(r["emit_exceptions"], 0)         # the robot's code path never sees the failure
        self.assertGreater(r["write_errors"], 0)
        self.assertGreater(r["written"], 100)
        events, rep = read_run(self.dir / "run")
        self.assertEqual(rep.declared_write_errors, r["write_errors"])    # the loss is declared, not hidden
        self.assertFalse(rep.clean)
        self.assertEqual(rep.corrupt_records, 0)           # no half-written records left behind
        self.assertEqual(len(events), r["written"])

    def test_spool_never_garbles_later_rows_after_a_failed_write(self):
        r = run_to_end("spool_disk_full", self.dir, 12_000, 400)
        self.assertGreater(r["fails"], 0)
        rows, _ = Spool(self.dir).read_batch(100_000)
        self.assertEqual(len(rows), r["ok"])               # every row that was accepted is intact and present
        self.assertEqual(Spool(self.dir).corrupt, 0)

    def test_dhara_never_loses_an_acknowledged_write_after_disk_full(self):
        r = run_to_end("dhara_disk_full", self.dir / "db", 20_000, 400)
        self.assertEqual(r["failed"], 1)
        self.assertEqual(r["present"], sorted(r["acked"]))  # acknowledged == durable, the failed put is absent


class NetworkTests(Tmp):
    @staticmethod
    def free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_peer_process_killed_and_restarted(self):
        sport = self.free_port()
        t = TcpTransport()
        t.add_peer("server", "127.0.0.1", sport)
        reg = SchemaRegistry(); reg.register("ping", 1, {"n": int})
        client = Node("client", t, reg)
        server = spawn("tcp_server", sport, t.port)
        try:
            wait_for(server, "READY")
            self.assertEqual(client.request("server", "ping", {"n": 1}, timeout=2, retries=3, safe_to_retry=True).payload, {"n": 2})
            kill9(server)
            t0 = time.monotonic()
            with self.assertRaises(SetuError):             # fails fast and cleanly, never hangs
                client.request("server", "ping", {"n": 1}, timeout=0.5)
            self.assertLess(time.monotonic() - t0, 3)
            server = spawn("tcp_server", sport, t.port)    # the peer comes back
            wait_for(server, "READY")
            r = client.request("server", "ping", {"n": 41}, timeout=1, retries=6, safe_to_retry=True)
            self.assertEqual(r.payload, {"n": 42})          # and the link recovers by itself
        finally:
            client.close()
            if server.poll() is None:
                kill9(server)


class CorruptionTests(Tmp):
    def test_flipped_bytes_in_a_recording_are_reported_not_trusted(self):
        from firebot_infra.akshara import Recorder
        with Recorder(self.dir, "r", software_version="x") as rec:
            for i in range(200):
                rec.emit("s", "e", i=i)
        seg = next((self.dir / "r").glob("seg-*.jsonl"))
        data = bytearray(seg.read_bytes())
        data[len(data) // 2] ^= 0xFF
        seg.write_bytes(bytes(data))
        events, rep = read_run(self.dir / "r")
        self.assertFalse(rep.clean)
        self.assertTrue(rep.corrupt_records or rep.segment_hash_mismatches)
        self.assertLess(len(events), 200)
        self.assertTrue(rep.seq_gaps)                       # the hole is visible, not papered over


if __name__ == "__main__":
    unittest.main()
