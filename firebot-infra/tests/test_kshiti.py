import unittest

from firebot_infra.kshiti import (Alloc, Compute, Exit, Free, GetPid, Kernel, Kill, Recv, Send, Sleep, Spawn, Yield)


def finish_order(k):
    return [pid for _, pid, ev in k.trace if ev.startswith("exit")]


class KernelTests(unittest.TestCase):
    def test_priority_scheduling(self):
        k = Kernel()
        def work(n):
            def p():
                yield Compute(n)
            return p
        lo = k.spawn(work(5), "lo", priority=1)
        hi = k.spawn(work(5), "hi", priority=5)
        self.assertEqual(k.run(), "all_exited")
        self.assertEqual(finish_order(k), [hi, lo])

    def test_round_robin_interleaves_equal_priority(self):
        k = Kernel()
        def w():
            yield Compute(6)
        a, b = k.spawn(w, "a"), k.spawn(w, "b")
        k.run()
        self.assertGreater(len(finish_order(k)), 1)
        ran = [k.proc(a)["cpu"], k.proc(b)["cpu"]]
        self.assertLessEqual(abs(ran[0] - ran[1]), 1)       # fair share

    def test_sleep_and_wake(self):
        k = Kernel()
        def sleeper():
            yield Sleep(10)
        pid = k.spawn(sleeper)
        self.assertEqual(k.run(), "all_exited")
        self.assertGreaterEqual(k.tick, 10)
        self.assertGreater(k.idle_ticks, 5)

    def test_ipc_ping_pong_and_blocking_recv(self):
        k = Kernel()
        log = []
        def server():
            while True:
                msg = yield Recv()
                if msg == "stop":
                    return
                yield Send(msg[0], msg[1] * 2)
        def client(server_pid):
            def p():
                me = yield GetPid()
                for i in (1, 2, 3):
                    yield Send(server_pid, (me, i))
                    log.append((yield Recv()))
                yield Send(server_pid, "stop")
            return p
        s = k.spawn(server, "server")
        k.spawn(client(s), "client")
        self.assertEqual(k.run(), "all_exited")
        self.assertEqual(log, [2, 4, 6])

    def test_mailbox_bounded_sender_blocks_until_space(self):
        k = Kernel(mailbox_size=2)
        got = []
        def producer(target):
            def p():
                for i in range(6):
                    ok = yield Send(target, i)
                    assert ok
            return p
        def consumer():
            yield Sleep(20)                                # let the mailbox fill and the producer block
            for _ in range(6):
                got.append((yield Recv()))
        c = k.spawn(consumer, "c")
        pr = k.spawn(producer(c), "p")
        k.run()
        self.assertEqual(got, [0, 1, 2, 3, 4, 5])          # nothing lost, order kept
        self.assertEqual(k.proc(pr)["reason"], "exit")

    def test_nonblocking_send_reports_full_and_dead_target(self):
        k = Kernel(mailbox_size=1)
        res = []
        def idle():
            yield Sleep(100)
        def sender(t):
            def p():
                res.append((yield Send(t, "a", False)))
                res.append((yield Send(t, "b", False)))
                res.append((yield Send(999, "x")))
            return p
        t = k.spawn(idle)
        k.spawn(sender(t))
        k.run()
        self.assertEqual(res, [True, False, False])

    def test_memory_quota_isolated_per_process(self):
        k = Kernel()
        res = {}
        def hog():
            res["hog1"] = yield Alloc(80)
            res["hog2"] = yield Alloc(80)                  # over quota: denied, process keeps running
            yield Free(80)
            res["hog3"] = yield Alloc(80)
        def modest():
            res["ok"] = yield Alloc(50)
        h = k.spawn(hog, "hog", mem_quota=100)
        m = k.spawn(modest, "modest", mem_quota=100)
        k.run()
        self.assertEqual(res, {"hog1": True, "hog2": False, "hog3": True, "ok": True})
        self.assertTrue(any(ev == "alloc_denied" for _, pid, ev in k.trace if pid == h))

    def test_watchdog_kills_runaway_others_unaffected(self):
        k = Kernel()
        def spin():
            while True:
                yield Compute(1000)
        done = []
        def good():
            yield Compute(10)
            done.append(1)
        bad = k.spawn(spin, "spin", cpu_budget=50)
        k.spawn(good, "good")
        self.assertEqual(k.run(), "all_exited")
        self.assertEqual(k.proc(bad)["reason"], "watchdog")
        self.assertEqual(done, [1])

    def test_crash_is_isolated(self):
        k = Kernel()
        out = []
        def crasher():
            yield Compute(2)
            raise RuntimeError("boom")
        def fine():
            yield Compute(5)
            out.append("survived")
        c = k.spawn(crasher)
        k.spawn(fine)
        k.run()
        self.assertIn("crashed: RuntimeError", k.proc(c)["reason"])
        self.assertEqual(out, ["survived"])

    def test_deadlock_detected(self):
        k = Kernel()
        def waiter():
            yield Recv()
        k.spawn(waiter); k.spawn(waiter)
        self.assertEqual(k.run(), "deadlock")
        self.assertEqual({p["state"] for p in k.ps()}, {"recv_blocked"})

    def test_sender_to_blocked_on_dead_process_is_released(self):
        k = Kernel(mailbox_size=1)
        res = []
        def victim():
            yield Sleep(1000)
        def spammer(t):
            def p():
                res.append((yield Send(t, 1)))
                res.append((yield Send(t, 2)))            # blocks: mailbox full
            return p
        def killer(t):
            def p():
                yield Sleep(5)
                res.append(("killed", (yield Kill(t))))
            return p
        v = k.spawn(victim)
        k.spawn(spammer(v)); k.spawn(killer(v))
        self.assertEqual(k.run(), "all_exited")
        self.assertEqual(res[0], True)
        self.assertIn(False, res)                          # blocked sender got False instead of hanging
        self.assertIn(("killed", True), res)

    def test_spawn_and_exit_codes_and_bad_syscall(self):
        k = Kernel()
        kids = []
        def child():
            yield Compute(2)
            yield Exit(7)
        def parent():
            kids.append((yield Spawn(child, "kid", 2)))
            yield "not a syscall"
        p = k.spawn(parent)
        k.run()
        self.assertEqual(k.proc(kids[0])["exit_code"], 7)
        self.assertIn("bad syscall", k.proc(p)["reason"])

    def test_deterministic_traces(self):
        def build():
            k = Kernel()
            def a():
                for _ in range(3):
                    yield Compute(2); yield Yield()
            def b():
                yield Sleep(3); yield Compute(4)
            k.spawn(a, "a", priority=2); k.spawn(b, "b")
            k.run()
            return k.trace
        self.assertEqual(build(), build())

    def test_run_limits(self):
        k = Kernel()
        def forever():
            while True:
                yield Yield()
        k.spawn(forever)
        self.assertEqual(k.run(max_ticks=50), "max_ticks")


if __name__ == "__main__":
    unittest.main()
