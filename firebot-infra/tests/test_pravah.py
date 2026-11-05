import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from firebot_infra.pravah import Scheduler, State, Task, TaskError


def quick(args, tok):
    return args.get("v", "ok")


class PravahTests(unittest.TestCase):
    def setUp(self):
        self.s = []

    def tearDown(self):
        for s in self.s:
            s.stop()

    def mk(self, handlers, **kw):
        s = Scheduler(handlers, **kw)
        self.s.append(s)
        return s

    def test_dependency_order_and_results(self):
        order = []
        s = self.mk({"w": lambda a, t: order.append(a["n"]) or a["n"]})
        s.submit(Task("c", "w", {"n": "c"}, ["b"]), Task("a", "w", {"n": "a"}), Task("b", "w", {"n": "b"}, ["a"]))
        self.assertTrue(s.wait(3))
        self.assertEqual(order, ["a", "b", "c"])
        self.assertEqual(s.status("c")["result"], "c")

    def test_cycle_and_unknown_prereq_and_duplicates_rejected_atomically(self):
        s = self.mk({"w": quick})
        with self.assertRaises(TaskError):
            s.submit(Task("a", "w", prerequisites=["b"]), Task("b", "w", prerequisites=["a"]))
        with self.assertRaises(TaskError):
            s.submit(Task("a", "w", prerequisites=["ghost"]))
        with self.assertRaises(TaskError):
            s.submit(Task("x", "nohandler"))
        s.submit(Task("ok", "w"))
        with self.assertRaises(TaskError):
            s.submit(Task("ok", "w"))
        self.assertEqual(list(s.states()), ["ok"])  # failed submissions left nothing behind

    def test_side_effecting_task_cannot_be_retried(self):
        s = self.mk({"w": quick})
        with self.assertRaises(TaskError):
            s.submit(Task("a", "w", max_attempts=3))

    def test_idempotent_retry_until_success(self):
        calls = []
        def flaky(a, t):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("nope")
            return "done"
        s = self.mk({"f": flaky})
        s.submit(Task("a", "f", max_attempts=3, idempotent=True))
        self.assertTrue(s.wait(3))
        st = s.status("a")
        self.assertEqual((st["state"], st["attempts"], st["result"]), ("succeeded", 3, "done"))

    def test_failure_not_retried_when_not_idempotent_and_cancels_dependents(self):
        def boom(a, t): raise ValueError("bad")
        s = self.mk({"b": boom, "w": quick})
        s.submit(Task("a", "b"), Task("b", "w", prerequisites=["a"]))
        self.assertTrue(s.wait(3))
        self.assertEqual(s.states(), {"a": "failed", "b": "cancelled"})
        self.assertIn("ValueError", s.status("a")["error"])
        self.assertIn("prerequisite a", s.status("b")["error"])

    def test_timeout_cancels_cooperatively(self):
        done = threading.Event()
        def slow(a, tok):
            tok.wait(5)
            done.set()
        s = self.mk({"s": slow})
        s.submit(Task("a", "s", timeout_s=0.1))
        self.assertTrue(s.wait(3))
        self.assertEqual(s.status("a")["state"], "timed_out")
        self.assertTrue(done.wait(1))           # token was cancelled, handler exited

    def test_deadline_before_start(self):
        gate = threading.Event()
        s = self.mk({"g": lambda a, t: gate.wait(5), "w": quick})
        s.submit(Task("a", "g"), Task("b", "w", prerequisites=["a"], deadline_s=0.1))
        time.sleep(0.4)
        self.assertEqual(s.status("b")["state"], "timed_out")
        gate.set()

    def test_cancel_running_and_pending(self):
        gate = threading.Event()
        s = self.mk({"g": lambda a, tok: tok.wait(5), "w": quick})
        s.submit(Task("a", "g"), Task("b", "w", prerequisites=["a"]))
        time.sleep(0.15)
        self.assertTrue(s.cancel("a"))
        self.assertFalse(s.cancel("a"))
        self.assertTrue(s.wait(3))
        self.assertEqual(s.states(), {"a": "cancelled", "b": "cancelled"})

    def test_bounded_concurrency_and_resource_limits(self):
        lock = threading.Lock()
        cur = {"n": 0, "max": 0, "r": 0, "rmax": 0}
        def work(a, t):
            with lock:
                cur["n"] += 1; cur["max"] = max(cur["max"], cur["n"])
                if a["r"]: cur["r"] += 1; cur["rmax"] = max(cur["rmax"], cur["r"])
            time.sleep(0.05)
            with lock:
                cur["n"] -= 1
                if a["r"]: cur["r"] -= 1
        s = self.mk({"w": work}, max_workers=3, resource_limits={"cam": 1})
        s.submit(*[Task(f"t{i}", "w", {"r": i < 5}, resource="cam" if i < 5 else None) for i in range(10)])
        self.assertTrue(s.wait(5))
        self.assertLessEqual(cur["max"], 3)
        self.assertEqual(cur["rmax"], 1)
        self.assertTrue(all(v == "succeeded" for v in s.states().values()))

    def test_restart_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "tasks.json"
            gate = threading.Event()
            a = Scheduler({"g": lambda x, t: gate.wait(10), "w": quick}, store_path=store)
            a.submit(Task("idem", "g", idempotent=True), Task("side", "g"), Task("after", "w", prerequisites=["idem"]))
            time.sleep(0.2)
            self.assertEqual(json.loads(store.read_text())[0]["state"], "running")
            # "crash": a new process loads the store while the old one is gone
            b = Scheduler({"g": quick, "w": quick}, store_path=store)
            self.s.append(b)
            self.assertTrue(b.wait(3))
            self.assertEqual(b.states(), {"idem": "succeeded", "side": "failed", "after": "succeeded"})
            self.assertIn("not safe to re-run", b.status("side")["error"])
            gate.set(); a.stop(); b.stop()
            time.sleep(0.3)   # let worker threads finish writing the store before the temp dir is removed

    def test_safety_actuation_types_refused(self):
        for name in ("estop_trigger", "motor_set_speed", "Actuate_valve", "suppress_fire"):
            with self.assertRaises(ValueError):
                Scheduler({name: quick})

    def test_event_callback_failure_does_not_break_scheduler(self):
        s = self.mk({"w": quick}, on_event=lambda *a, **k: 1 / 0)
        s.submit(Task("a", "w"))
        self.assertTrue(s.wait(3))
        self.assertEqual(s.status("a")["state"], "succeeded")

    def test_transitions_reported(self):
        ev = []
        s = self.mk({"w": quick}, on_event=lambda sub, t, sev, **k: ev.append((k["old"], k["new"])))
        s.submit(Task("a", "w"))
        s.wait(3)
        time.sleep(0.05)
        self.assertEqual(ev, [("pending", "ready"), ("ready", "running"), ("running", "succeeded")])


if __name__ == "__main__":
    unittest.main()
