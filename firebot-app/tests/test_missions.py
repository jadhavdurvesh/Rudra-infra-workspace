import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from firebot_infra.akshara import read_run

from firebot.__main__ import main as cli
from firebot.replay import replay_decisions
from firebot.report import ascii_map, svg
from firebot.runner import run_mission
from firebot.sim import Fault


class MissionTests(unittest.TestCase):
    def test_finds_the_fire_accurately_without_collisions(self):
        for scenario in ("empty_room", "obstacles"):
            for seed in range(8):
                r = run_mission(scenario, seed, randomize=True)
                self.assertEqual(r.outcome, "found", (scenario, seed, r.reason))
                self.assertEqual(r.collisions, 0, (scenario, seed))
                self.assertLess(r.error_m, 0.7, (scenario, seed))
                self.assertTrue(r.report_delivered)
                self.assertLess(r.sim_time, 200)

    def test_fixed_layouts_too(self):
        for scenario in ("empty_room", "obstacles"):
            r = run_mission(scenario, 4)
            self.assertEqual((r.outcome, r.collisions), ("found", 0))
            self.assertLess(r.error_m, 0.5)

    def test_stops_at_a_safe_standoff_distance(self):
        import math
        r = run_mission("empty_room", 2)
        w = r.world
        d = math.hypot(w.x - r.true_fire[0], w.y - r.true_fire[1])
        self.assertGreater(d, 1.3)                       # never closer than ~1.5 m to the heat source
        self.assertLess(d, 2.2)

    def test_empty_room_reports_not_found_with_coverage(self):
        r = run_mission("no_fire", 1, max_time=60)       # (time-limited here to keep the test quick)
        self.assertNotEqual(r.outcome, "found")
        self.assertIsNone(r.estimated_fire)
        self.assertEqual(r.collisions, 0)
        full = run_mission("no_fire", 1)
        self.assertEqual((full.outcome, full.reason), ("not_found", "time_limit"))
        self.assertGreater(full.coverage, 0.5)
        self.assertTrue(full.report_delivered)

    def test_sensor_dropout_pauses_then_mission_continues(self):
        r = run_mission("empty_room", 1, faults=(Fault("thermal_c", "dropout", 5.0, 6.0),))
        self.assertEqual(r.outcome, "found")
        self.assertGreaterEqual(r.holds, 1)
        self.assertEqual(r.collisions, 0)

    def test_garbage_readings_are_rejected_not_trusted(self):
        r = run_mission("empty_room", 1, faults=(Fault("pose", "nan", 4.0, 5.5),))
        self.assertEqual(r.outcome, "found")
        self.assertGreaterEqual(r.holds, 1)

    def test_long_sensor_failure_aborts_safely(self):
        r = run_mission("empty_room", 1, faults=(Fault("range_0", "dropout", 3.0, 100.0),))
        self.assertEqual((r.outcome, r.reason), ("aborted", "sensor_failure"))
        self.assertLess(r.sim_time, 20)
        self.assertEqual(r.collisions, 0)

    def test_frozen_thermal_probe_is_caught_by_flatline_detection(self):
        # The probe keeps answering on time with the same number, so freshness alone can't see it. DRISHTI's flatline check can.
        r = run_mission("empty_room", 1, faults=(Fault("thermal_c", "stuck", 5.0, 8.0),))
        self.assertGreaterEqual(r.holds, 1)
        self.assertEqual((r.outcome, r.collisions), ("found", 0))

    def test_saturating_range_beams_are_deliberately_not_flatline_checked(self):
        # An open-space beam legitimately reads exactly its max range for long stretches, so that check is off for distance.
        r = run_mission("empty_room", 1, faults=(Fault("range_p30", "stuck", 5.0, 8.0),))
        self.assertEqual(r.holds, 0)

    def test_low_battery_aborts(self):
        r = run_mission("empty_room", 1, battery_start=12.0)
        self.assertEqual((r.outcome, r.reason), ("aborted", "low_battery"))

    def test_operator_abort_over_setu(self):
        r = run_mission("obstacles", 1, abort_at=5.0)
        self.assertEqual(r.outcome, "aborted")
        self.assertIn("operator", r.reason)
        self.assertLess(r.sim_time, 7)
        self.assertTrue(r.report_delivered)
        self.assertEqual(r.station_reports[0]["outcome"], "aborted")

    def test_same_seed_same_mission(self):
        a, b = run_mission("obstacles", 5).summary(), run_mission("obstacles", 5).summary()
        for k in ("run_dir", "events_recorded"):
            a.pop(k); b.pop(k)
        self.assertEqual(a, b)
        self.assertNotEqual(run_mission("obstacles", 5).sim_time, run_mission("obstacles", 6).sim_time)


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.dir = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def test_run_is_recorded_replayable_and_shipped_to_analytics(self):
        r = run_mission("obstacles", 3, data_dir=self.dir)
        self.assertEqual(r.outcome, "found")
        events, rep = read_run(r.run_dir)
        self.assertTrue(rep.clean, rep)
        kinds = {(e.subsystem, e.event_type) for e in events}
        self.assertTrue({("autonomy", "state_change"), ("autonomy", "fire_located"), ("mission", "result"),
                         ("trace", "step")} <= kinds, kinds)
        rows = [json.loads(l) for l in (self.dir / "analytics.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), r.analytics_rows)
        self.assertNotIn("trace", {row["subsystem"] for row in rows})            # raw 10 Hz data stays on the robot
        self.assertIn("mission", {row["subsystem"] for row in rows})
        self.assertTrue(all(row["device_id"] not in ("", "firebot-sim") for row in rows))   # pseudonymised
        res = replay_decisions(r.run_dir)
        self.assertTrue(res["identical"], res)
        self.assertGreater(res["steps"], 100)

    def test_replay_reproduces_faults_and_holds_and_detects_behaviour_changes(self):
        r = run_mission("empty_room", 1, data_dir=self.dir, faults=(Fault("thermal_c", "dropout", 5.0, 6.0),))
        self.assertGreaterEqual(r.holds, 1)
        self.assertTrue(replay_decisions(r.run_dir)["identical"])
        copy = self.dir / "changed"
        shutil.copytree(r.run_dir, copy)
        m = json.loads((copy / "manifest.json").read_text())
        m["config"]["cfg"]["cruise_v"] = 0.3                                     # "new software version" behaves differently
        (copy / "manifest.json").write_text(json.dumps(m))
        res = replay_decisions(copy)
        self.assertFalse(res["identical"])
        self.assertGreater(res["mismatches"], 0)

    def test_operator_abort_is_replayed_too(self):
        r = run_mission("obstacles", 2, data_dir=self.dir, abort_at=4.0)
        self.assertTrue(replay_decisions(r.run_dir)["identical"])


class ReportTests(unittest.TestCase):
    def test_ascii_and_svg(self):
        r = run_mission("obstacles", 1)
        a = ascii_map(r)
        for ch in "SFER#.":
            self.assertIn(ch, a)
        s = svg(r)
        self.assertTrue(s.startswith("<svg") and s.rstrip().endswith("</svg>"))
        for token in ("polyline", "found", "fire position error"):
            self.assertIn(token, s)

    def test_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli(["--scenario", "empty_room", "--seed", "2", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["outcome"], "found")
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as o:
            code = cli(["--scenario", "empty_room", "--seed", "2", "--data-dir", d, "--replay", "--svg", f"{d}/m.svg"])
            self.assertTrue((Path(d) / "m.svg").exists())
        self.assertEqual(code, 0)
        self.assertIn("'identical': True", o.getvalue())


if __name__ == "__main__":
    unittest.main()
