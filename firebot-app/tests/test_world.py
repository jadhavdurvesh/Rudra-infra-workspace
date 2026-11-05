import math
import unittest

from firebot.config import Config
from firebot.geometry import ray_rect, segment_hits_rect, wrap
from firebot.perception import forward_clearance
from firebot.sim import Fault, FaultPlan, Fire, Rect, World


class WorldTests(unittest.TestCase):
    def test_ray_casting_bounds_and_obstacles(self):
        w = World(8, 6, (Rect(4, 0.5, 5, 1.5),), (), (1, 1, 0))
        self.assertAlmostEqual(w.ray(1, 1, 0, 10), 3.0)                 # block face at x=4
        self.assertAlmostEqual(w.ray(1, 1, math.pi, 10), 1.0)           # left wall
        self.assertAlmostEqual(w.ray(1, 1, math.pi / 2, 10), 5.0)       # top wall
        self.assertAlmostEqual(w.ray(1, 3, 0, 10), 7.0)                 # passes above the block
        self.assertEqual(w.ray(1, 1, 0, 2.0), 2.0)                      # max range cap

    def test_geometry_helpers(self):
        self.assertAlmostEqual(abs(wrap(3 * math.pi)), math.pi)
        self.assertAlmostEqual(wrap(0.5 + 2 * math.pi), 0.5)
        self.assertIsNone(ray_rect(0, 0, 1, 0, (2, 1, 3, 2)))
        self.assertTrue(segment_hits_rect((0, 0), (5, 0), (2, -1, 3, 1)))
        self.assertFalse(segment_hits_rect((0, 0), (1, 0), (2, -1, 3, 1)))

    def test_motion_and_collisions(self):
        w = World(4, 4, (Rect(2, 0, 3, 4),), (), (1, 2, 0))
        for _ in range(100):
            w.step(0.5, 0.0, 0.1)
        self.assertLess(w.x, 2 - w.radius + 1e-9)                      # stopped by the block
        self.assertGreater(w.collisions, 0)
        w2 = World(4, 4, (), (), (1, 1, 0))
        w2.step(0.0, 1.0, 0.5)
        self.assertAlmostEqual(w2.th, 0.5)
        self.assertEqual((w2.x, w2.collisions), (1, 0))                # rotating in place never collides
        self.assertAlmostEqual(w2.battery, 100 - 0.01 * 0.5)

    def test_heat_model(self):
        f = Fire(5, 5, 300, 1.2)
        w = World(10, 10, (Rect(6.5, 0, 7.5, 10),), (f,), (1, 1, 0))
        self.assertAlmostEqual(w.temperature(5, 5), 322.0)
        self.assertLess(w.temperature(5, 7), w.temperature(5, 6))      # decays with distance
        self.assertAlmostEqual(w.temperature(9, 5) - 22, 300 * math.exp(-16 / 2.88) * w.OCCLUDED, places=6)  # behind a wall
        self.assertLess(w.temperature(9, 5), 30)
        self.assertEqual(World(5, 5).temperature(1, 1), 22.0)

    def test_coverage_and_determinism(self):
        def run():
            w = World(4, 4, (), (), (0.5, 0.5, 0), seed=3)
            for _ in range(60):
                w.step(0.5, 0.0, 0.1)
            return w.coverage(), w.x, w.sensor_rng.random()
        self.assertEqual(run(), run())
        self.assertGreater(run()[0], 0.0)
        self.assertLess(run()[0], 0.5)

    def test_fault_plan(self):
        p = FaultPlan([Fault("a", "dropout", 1.0, 2.0)])
        self.assertIsNone(p.active("a", 0.5))
        self.assertEqual(p.active("a", 1.5), "dropout")
        self.assertIsNone(p.active("a", 2.0))
        self.assertIsNone(p.active("b", 1.5))


class ClearanceTests(unittest.TestCase):
    cfg = Config()

    def rays(self, **over):
        from firebot.config import RANGE
        r = {k: 4.0 for k in RANGE}
        r.update(over)
        return r

    def test_open_space_and_head_on_wall(self):
        self.assertEqual(forward_clearance(self.rays(), self.cfg), 4.0)
        fc = forward_clearance(self.rays(range_0=1.0), self.cfg)
        self.assertAlmostEqual(fc, 1.0 - (self.cfg.robot_radius_m + self.cfg.body_margin_m))

    def test_corner_beside_path_blocks_but_distant_side_object_does_not(self):
        self.assertEqual(forward_clearance(self.rays(range_p60=0.3), self.cfg), 0.0)   # inside the body's swept path
        self.assertEqual(forward_clearance(self.rays(range_p60=1.0), self.cfg), 4.0)   # 0.87 m to the side: fine
        self.assertEqual(forward_clearance(self.rays(range_p90=0.2), self.cfg), 4.0)   # beside, not ahead

    def test_other_headings_and_unknown(self):
        self.assertAlmostEqual(forward_clearance(self.rays(range_0=0.5), self.cfg, 1.5708), 4.0)   # path to the left is clear
        self.assertIsNone(forward_clearance(self.rays(), self.cfg, 3.1416))                          # nothing looks backwards


if __name__ == "__main__":
    unittest.main()
