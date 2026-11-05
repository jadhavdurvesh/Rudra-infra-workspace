import json
import random
import time
import unittest

from firebot_infra.sangam import (BackgroundSync, Policy, Store, SyncClient, SyncService, TypePolicy)
from firebot_infra.setu import MemoryHub, Node, SchemaRegistry, StreamTransport, TcpTransport, make_pipe

TYPES = [TypePolicy("run_log", Policy.APPEND_ONLY), TypePolicy("robot_status", Policy.SINGLE_WRITER, owner="robot"),
         TypePolicy("settings", Policy.REJECT_AND_REVIEW), TypePolicy("notes", Policy.LAST_WRITER_WINS)]


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): self.t += 0.001; return self.t


def rig(hub=None, **client_kw):
    hub = hub or MemoryHub()
    c = Clock()
    robot_store, station_store = Store("robot", TYPES, clock=c), Store("station", TYPES, clock=c)
    robot = Node("robot", hub.transport("robot"), SchemaRegistry())
    station = Node("station", hub.transport("station"), SchemaRegistry())
    SyncService(robot_store, robot); SyncService(station_store, station)
    client = SyncClient(station_store, station, timeout=0.5, **client_kw)
    return hub, robot_store, station_store, robot, station, client


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.nodes = []

    def tearDown(self):
        for n in self.nodes:
            n.close()

    def mk(self, *a, **k):
        r = rig(*a, **k)
        self.nodes += [r[3], r[4]]
        return r

    def test_offline_writes_reconcile_over_setu(self):
        hub, rs, ss, robot, station, client = self.mk()
        for i in range(5):
            rs.put("run_log", None, {"msg": f"r{i}"})
        rs.put("robot_status", "battery", 80)
        ss.put("run_log", None, {"msg": "s0"})
        rep = client.sync_with("robot")
        self.assertEqual((rep.error, rep.converged), ("", True))
        self.assertEqual((rep.pulled, rep.pushed), (6, 1))
        self.assertEqual(rs.items("run_log"), ss.items("run_log"))
        self.assertEqual(ss.get("robot_status", "battery"), 80)

    def test_second_sync_moves_nothing(self):
        hub, rs, ss, robot, station, client = self.mk()
        rs.put("run_log", None, 1)
        client.sync_with("robot")
        rep = client.sync_with("robot")
        self.assertEqual((rep.pulled, rep.pushed, rep.converged, rep.rounds), (0, 0, True, 1))

    def test_large_backlog_is_chunked_within_message_limits(self):
        hub, rs, ss, robot, station, client = self.mk(max_entries=50, max_bytes=4000)
        for i in range(400):
            rs.put("run_log", None, {"i": i, "pad": "x" * 100})
        for i in range(120):
            ss.put("run_log", None, {"i": i, "pad": "y" * 100})
        rep = client.sync_with("robot")
        self.assertEqual((rep.error, rep.converged), ("", True))
        self.assertGreater(rep.rounds, 4)
        self.assertEqual(len(rs.items("run_log")), 520)
        self.assertEqual(rs.items("run_log"), ss.items("run_log"))
        self.assertTrue(all(station.metrics[m] == 0 for m in ("malformed", "schema_errors")))

    def test_conflicts_surface_on_both_sides_and_resolution_syncs(self):
        hub, rs, ss, robot, station, client = self.mk()
        rs.put("settings", "mode", "a"); client.sync_with("robot")
        rs.put("settings", "mode", "robot-edit"); ss.put("settings", "mode", "station-edit")
        rep = client.sync_with("robot")
        self.assertEqual((rep.conflicts_local, rep.conflicts_remote), (1, 1))
        self.assertEqual((len(rs.conflicts), len(ss.conflicts)), (1, 1))
        ss.resolve("settings", "mode", "merged")
        client.sync_with("robot")
        self.assertEqual((rs.get("settings", "mode"), rs.conflicts), ("merged", []))

    def test_lossy_link_still_converges_and_never_duplicates(self):
        hub, rs, ss, robot, station, client = self.mk(max_entries=20)
        rng = random.Random(5)
        hub.fault = lambda s, d, data: "drop" if rng.random() < 0.3 else ("dup" if rng.random() < 0.2 else None)
        for i in range(60):
            rs.put("run_log", None, i)
        for _ in range(12):
            if client.sync_with("robot").converged:
                break
        self.assertEqual(rs.items("run_log"), ss.items("run_log"))
        self.assertEqual(len(ss.items("run_log")), 60)

    def test_unreachable_peer_returns_a_bounded_error_and_local_state_is_untouched(self):
        hub, rs, ss, robot, station, client = self.mk()
        ss.put("run_log", None, "mine")
        hub.disconnect("robot")
        t0 = time.monotonic()
        rep = client.sync_with("robot")
        self.assertLess(time.monotonic() - t0, 3)
        self.assertNotEqual(rep.error, "")
        self.assertEqual(ss.items("run_log"), {ss.items("run_log").popitem()[0]: "mine"})
        hub.reconnect("robot")
        self.assertTrue(client.sync_with("robot").converged)

    def test_rejections_are_reported_and_do_not_loop_forever(self):
        hub, rs, ss, robot, station, client = self.mk()
        ss.put("settings", "s", 1)
        # the robot refuses this origin for this type
        rs._authorize = lambda origin, t: not (origin == "station" and t == "settings")
        rep = client.sync_with("robot")
        self.assertEqual(rep.rejected_remote[0][1], "unauthorized")
        self.assertLessEqual(rep.rounds, 3)
        self.assertIsNone(rs.get("settings", "s"))

    def test_background_sync_keeps_two_stores_converged(self):
        hub, rs, ss, robot, station, client = self.mk()
        bg = BackgroundSync(client, ["robot"], interval_s=0.1).start()
        try:
            for i in range(10):
                rs.put("run_log", None, i)
                time.sleep(0.03)
            deadline = time.monotonic() + 3
            while rs.items("run_log") != ss.items("run_log") and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(len(ss.items("run_log")), 10)
            self.assertTrue(bg.last["robot"].converged)
        finally:
            bg.stop()

    def test_sync_over_tcp_and_over_a_serial_style_stream(self):
        for kind in ("tcp", "stream"):
            c = Clock()
            rs, ss = Store("robot", TYPES, clock=c), Store("station", TYPES, clock=c)
            if kind == "tcp":
                tr, ts = TcpTransport(), TcpTransport()
                tr.add_peer("station", "127.0.0.1", ts.port); ts.add_peer("robot", "127.0.0.1", tr.port)
            else:
                a, b = make_pipe()
                tr, ts = StreamTransport(b, "station"), StreamTransport(a, "robot")
            robot, station = Node("robot", tr, SchemaRegistry()), Node("station", ts, SchemaRegistry())
            self.nodes += [robot, station]
            SyncService(rs, robot, max_bytes=3000)
            for i in range(30):
                rs.put("run_log", None, {"i": i, "pad": "z" * 60})
            rep = SyncClient(ss, station, timeout=2.0, max_bytes=3000).sync_with("robot")
            self.assertEqual((rep.error, rep.converged, len(ss.items("run_log"))), ("", True, 30), kind)

    def test_signed_and_role_checked_sync(self):
        try:
            from firebot_infra.kavach import EnvelopeSigner, EnvelopeVerifier, Identity, Policy as KPolicy, Registry
        except ImportError:
            self.skipTest("needs cryptography")
        reg = Registry()
        ids = {n: Identity.generate(n) for n in ("robot", "station", "rogue")}
        reg.enroll("robot", ids["robot"].public_bytes(), "robot")
        reg.enroll("station", ids["station"].public_bytes(), "station")
        reg.enroll("rogue", ids["rogue"].public_bytes(), "reader")
        pol = KPolicy({"robot": {"sync_read", "sync_write"}, "station": {"sync_read", "sync_write"}, "reader": {"sync_read"}})
        caps = {"sangam.hello": "sync_read", "sangam.pull": "sync_read", "sangam.push": "sync_write"}
        hub, c = MemoryHub(), Clock()
        rs = Store("robot", TYPES, clock=c)
        robot = Node("robot", hub.transport("robot"), SchemaRegistry(), sign=EnvelopeSigner(ids["robot"]),
                     authorize=EnvelopeVerifier(reg, pol, caps))
        SyncService(rs, robot)
        self.nodes.append(robot)
        for name in ("station", "rogue"):
            ver = EnvelopeVerifier(reg, pol, {}).verify_response
            st = Store(name, TYPES, clock=c)
            node = Node(name, hub.transport(name), SchemaRegistry(), sign=EnvelopeSigner(ids[name]), verify_response=ver)
            self.nodes.append(node)
            st.put("run_log", None, f"from-{name}")
            rep = SyncClient(st, node, timeout=0.5, retries=0).sync_with("robot")
            if name == "station":
                self.assertEqual((rep.error, rep.converged), ("", True))
            else:
                self.assertNotEqual(rep.error, "")           # a read-only role may not push
        self.assertEqual(list(rs.items("run_log").values()).count("from-rogue"), 0)
        self.assertEqual(list(rs.items("run_log").values()).count("from-station"), 1)


if __name__ == "__main__":
    unittest.main()
