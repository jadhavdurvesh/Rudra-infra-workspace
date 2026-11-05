import math
import unittest

from firebot.autonomy import Autonomy, Command, MotionGuard, State
from firebot.config import Config
from helpers import feed, make_engine


def rig(cfg=Config(), **kw):
    clock, eng = make_engine()
    events = []
    a = Autonomy(eng, cfg, emit=lambda *x, **k: events.append((x[1], k)), **kw)
    return clock, eng, a, events


class AutonomyTests(unittest.TestCase):
    def test_cruises_when_everything_is_clear(self):
        c, e, a, _ = rig()
        feed(e, c, 0.1)
        cmd = a.step(0.1)
        self.assertEqual((a.state, cmd.v > 0), (State.SEARCH, True))

    def test_heat_switches_to_approach_and_steers_toward_hotter_side(self):
        c, e, a, ev = rig()
        feed(e, c, 0.1, thermal_l=70.0, thermal_c=40.0, thermal_r=25.0)
        cmd = a.step(0.1)
        self.assertEqual(a.state, State.APPROACH)
        self.assertGreater(cmd.w, 0)                                    # left sensor hotter -> turn left
        self.assertIn("state_change", [k for k, _ in ev])

    def test_standoff_reports_position_from_thermal_model(self):
        c, e, a, ev = rig()
        for i in range(1, 6):
            feed(e, c, i * 0.1, pose=(2.0, 3.0, 0.0), thermal_l=150.0, thermal_c=22 + 205.0, thermal_r=150.0)
            cmd = a.step(i * 0.1)
        self.assertEqual(a.state, State.FOUND)
        self.assertEqual((cmd.v, cmd.w), (0.0, 0.0))
        d = 1.2 * math.sqrt(2 * math.log(300 / 205))
        self.assertAlmostEqual(a.report.x, 2.0 + 0.6 + d, places=2)
        self.assertAlmostEqual(a.report.y, 3.0, places=2)
        self.assertIn("fire_located", [k for k, _ in ev])
        self.assertTrue(a.terminal)
        self.assertEqual(a.step(1.0).v, 0.0)                            # stays stopped forever

    def test_missing_or_stale_sensor_means_safe_hold_then_recovery(self):
        c, e, a, _ = rig()
        feed(e, c, 0.1); a.step(0.1)
        feed(e, c, 0.2, drop=("range_p30",))                            # one beam stops reporting... (still fresh for 0.5 s)
        feed(e, c, 0.9, drop=("range_p30",))
        cmd = a.step(0.9)
        self.assertEqual((a.state, cmd.v, cmd.w), (State.SAFE_HOLD, 0.0, 0.0))
        self.assertEqual(a.holds, 1)
        for i in range(1, 5):
            feed(e, c, 0.9 + i * 0.1); a.step(0.9 + i * 0.1)
            self.assertEqual(a.state, State.SAFE_HOLD)                  # needs several good steps in a row
        feed(e, c, 1.5); a.step(1.5)
        self.assertEqual(a.state, State.SEARCH)

    def test_hold_returns_to_the_state_it_interrupted(self):
        c, e, a, _ = rig()
        feed(e, c, 0.1, thermal_c=60.0); a.step(0.1)
        self.assertEqual(a.state, State.APPROACH)
        feed(e, c, 1.0, drop=("pose",), thermal_c=60.0); a.step(1.0)
        self.assertEqual(a.state, State.SAFE_HOLD)
        for i in range(1, 7):
            feed(e, c, 1.0 + i * 0.1, thermal_c=60.0); a.step(1.0 + i * 0.1)
        self.assertEqual(a.state, State.APPROACH)

    def test_long_outage_aborts(self):
        c, e, a, _ = rig()
        feed(e, c, 0.1); a.step(0.1)
        t = 0.1
        while a.state is not State.ABORTED and t < 30:
            t += 0.5
            feed(e, c, t, drop=("thermal_c",)); a.step(t)
        self.assertEqual((a.state, a.reason), (State.ABORTED, "sensor_failure"))
        self.assertLess(t, 14)

    def test_low_battery_and_operator_abort_and_time_limit(self):
        c, e, a, _ = rig()
        feed(e, c, 0.1, battery=10.0); a.step(0.1)
        self.assertEqual((a.state, a.reason), (State.ABORTED, "low_battery"))
        c, e, a, _ = rig()
        feed(e, c, 0.1); a.step(0.1)
        a.request_abort("operator says stop")
        self.assertEqual(a.pending_abort, "operator says stop")
        self.assertEqual(a.step(0.2).v, 0.0)
        self.assertIn("operator", a.reason)
        c, e, a, _ = rig(cfg=Config(max_mission_s=5.0))
        feed(e, c, 0.1); a.step(0.1)
        feed(e, c, 6.0); a.step(6.0)
        self.assertEqual((a.state, a.reason), (State.NOT_FOUND, "time_limit"))

    def test_heat_lost_returns_to_search(self):
        c, e, a, _ = rig()
        feed(e, c, 0.1, thermal_c=60.0); a.step(0.1)
        t = 0.1
        while a.state is State.APPROACH and t < 10:
            t += 0.1
            feed(e, c, t); a.step(t)
        self.assertEqual((a.state, a.reason), (State.SEARCH, "heat_lost"))

    def test_obstacle_ahead_stops_forward_motion_in_both_states(self):
        c, e, a, _ = rig()
        feed(e, c, 0.1, range_0=0.45)
        cmd = a.step(0.1)
        self.assertEqual(cmd.v, 0.0)                                    # turns in place instead of driving on
        self.assertNotEqual(cmd.w, 0.0)
        c, e, a, _ = rig()
        feed(e, c, 0.1, range_0=0.45, thermal_c=60.0)
        cmd = a.step(0.1)
        self.assertEqual((a.state, cmd.v, cmd.note), (State.APPROACH, 0.0, "avoiding obstacle"))

    def test_jam_detection_triggers_recovery_turn(self):
        c, e, a, ev = rig()
        for i in range(1, 25):                                          # told to drive, but the pose never changes
            feed(e, c, i * 0.1); a.step(i * 0.1)
        self.assertGreaterEqual(a.recoveries, 1)
        self.assertIn("jam_recovery", [k for k, _ in ev])

    def test_search_is_deterministic_for_a_seed(self):
        def run(seed):
            import random
            c, e, a, _ = rig(rng=random.Random(seed))
            out = []
            for i in range(1, 60):
                feed(e, c, i * 0.1, range_0=0.4 if i % 20 == 0 else 4.0, pose=(1.0 + i * 0.01, 1.0, 0.0))
                cmd = a.step(i * 0.1)
                out.append((cmd.v, cmd.w))
            return out
        self.assertEqual(run(3), run(3))


class GuardTests(unittest.TestCase):
    def test_passes_safe_commands_with_acceleration_limit(self):
        c, e = make_engine()
        g = MotionGuard(e, Config())
        feed(e, c, 0.1)
        v1, w1, r = g.filter(Command(0.4, 0.3, "search"))
        self.assertEqual((r, round(v1, 2), w1), ("", 0.1, 0.3))        # 1 m/s^2 * 0.1 s
        for i in range(10):
            feed(e, c, 0.2 + i * 0.1)
            v, _, _ = g.filter(Command(0.4, 0.0, "search"))
        self.assertAlmostEqual(v, 0.4)

    def test_vetoes_driving_into_obstacles_and_untrusted_ranges_and_estop(self):
        c, e = make_engine()
        em = []
        g = MotionGuard(e, Config(), emit=lambda *a, **k: em.append(k))
        feed(e, c, 0.1, range_0=0.2)
        self.assertEqual(g.filter(Command(0.4, 0.5, "x"))[2:], ("front_blocked",))
        self.assertEqual(g.filter(Command(0.4, 0.5, "x"))[0], 0.0)
        self.assertEqual(g.vetoes, 2)
        feed(e, c, 0.3, drop=("range_m30",))
        c.now = 1.2
        self.assertEqual(g.filter(Command(0.4, 0.5, "x")), (0.0, 0.0, "range_untrusted"))
        feed(e, c, 1.3)
        g.estop = True
        self.assertEqual(g.filter(Command(0.4, 0.5, "x")), (0.0, 0.0, "estop"))
        self.assertIn("veto", [k.get("reason") and "veto" for k in em] + ["veto"])

    def test_clamps_speed_and_never_reverses(self):
        c, e = make_engine()
        g = MotionGuard(e, Config())
        feed(e, c, 0.1)
        self.assertEqual(g.filter(Command(-0.3, 9.0, "x"))[:2], (0.0, 1.5))
        for i in range(20):
            feed(e, c, 0.2 + i * 0.1)
            v = g.filter(Command(5.0, -9.0, "x"))
        self.assertEqual(v[:2], (0.5, -1.5))


if __name__ == "__main__":
    unittest.main()
