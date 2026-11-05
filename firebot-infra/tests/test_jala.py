import unittest

from firebot_infra.jala import MeshNode, MeshSim

LINE = [("A", "B"), ("B", "C"), ("C", "D")]
RING = [("A", "B"), ("B", "C"), ("C", "D"), ("D", "A")]


class MeshTests(unittest.TestCase):
    def test_line_converges_and_delivers_multi_hop(self):
        sim = MeshSim(LINE)
        sim.run(12)
        self.assertEqual(sim.nodes["A"].routes, {"B": "B", "C": "B", "D": "B"})
        self.assertTrue(sim.nodes["A"].send("D", b"hello"))
        sim.run(2)
        self.assertEqual(sim.delivered["D"], [("A", b"hello")])
        self.assertEqual(sim.nodes["B"].stats["forwarded"], 1)

    def test_route_recovery_after_link_failure(self):
        sim = MeshSim(RING)
        sim.run(12)
        self.assertEqual(sim.nodes["A"].routes["C"], "B")             # tie-break: lexicographic first hop
        sim.link_down("A", "B")
        sim.run(8)                                                     # > hello timeout: detected and re-routed
        self.assertEqual(sim.nodes["A"].routes["C"], "D")
        sim.nodes["A"].send("C", b"after-failure")
        sim.run(2)
        self.assertEqual(sim.delivered["C"], [("A", b"after-failure")])

    def test_partition_queues_then_delivers_after_heal(self):
        sim = MeshSim(LINE)
        sim.run(12)
        sim.link_down("B", "C")
        sim.run(8)
        self.assertNotIn("D", sim.nodes["A"].routes)
        self.assertTrue(sim.nodes["A"].send("D", b"queued"))
        self.assertEqual(sim.nodes["A"].pending(), 1)
        sim.run(3)
        self.assertEqual(sim.delivered["D"], [])
        sim.link_up("B", "C")
        sim.run(12)
        self.assertEqual(sim.delivered["D"], [("A", b"queued")])
        self.assertEqual(sim.nodes["A"].pending(), 0)

    def test_queue_is_bounded(self):
        sim = MeshSim([("A", "B")], queue_limit=3)
        results = [sim.nodes["A"].send("Z", b"x") for _ in range(6)]
        self.assertEqual(results, [True, True, True, False, False, False])
        self.assertEqual(sim.nodes["A"].stats["queue_overflow"], 3)

    def test_ttl_limits_forwarding(self):
        sim = MeshSim(LINE, ttl=2)
        sim.run(12)
        sim.nodes["A"].send("D", b"too far")
        sim.run(3)
        self.assertEqual(sim.delivered["D"], [])
        self.assertEqual(sim.nodes["C"].stats["ttl_expired"] + sim.nodes["B"].stats["ttl_expired"], 1)

    def test_lsa_flood_suppresses_duplicates_in_ring(self):
        sim = MeshSim(RING)
        sim.run(12)
        self.assertGreater(sum(n.stats["lsa_duplicate"] for n in sim.nodes.values()), 0)

    def test_duplicated_packets_delivered_once(self):
        sim = MeshSim(LINE, dup=1.0)
        sim.run(12)
        sim.nodes["A"].send("D", b"once")
        sim.run(3)
        self.assertEqual(sim.delivered["D"], [("A", b"once")])
        self.assertGreater(sim.nodes["D"].stats["data_duplicate"], 0)

    def test_lossy_links_still_converge_and_deliver_with_retries(self):
        sim = MeshSim(RING, loss=0.2, seed=7)
        sim.run(30)
        got = 0
        for i in range(10):
            sim.nodes["A"].send("C", bytes([i]))
            sim.run(1)
        sim.run(3)
        got = len(sim.delivered["C"])
        self.assertGreaterEqual(got, 5)                               # best-effort: some loss expected, no corruption
        self.assertTrue(all(len(d) == 1 for _, d in sim.delivered["C"]))

    def test_one_way_link_is_not_used(self):
        sim = MeshSim(LINE)
        sim.run(12)
        # B can hear C but C cannot hear B: link must not be trusted
        sim.down.add(frozenset(("B", "C")))
        orig = sim._up
        sim._up = lambda a, b: True if (a, b) == ("C", "B") else orig(a, b)
        sim.run(10)
        self.assertNotIn("C", sim.nodes["B"].routes)

    def test_malformed_packets_ignored(self):
        sim = MeshSim(LINE)
        n = sim.nodes["A"]
        for junk in (b"\x00", b"[]", b'{"t":"zzz"}', b'{"t":"d"}', b"x" * 5000, b'{"t":"l","o":"B","q":"nan","n":5}'):
            n.on_receive("B", junk)
        self.assertEqual(n.stats["malformed"], 6)
        sim.run(12)
        self.assertEqual(n.routes["D"], "B")                          # still works

    def test_unknown_destination_forward_dropped_not_looped(self):
        sim = MeshSim(LINE)
        sim.run(12)
        sim.nodes["B"].on_receive("A", b'{"t":"d","s":"A","d":"Q","i":1,"l":9,"p":"00"}')
        self.assertEqual(sim.nodes["B"].stats["no_route_dropped"], 1)


if __name__ == "__main__":
    unittest.main()
