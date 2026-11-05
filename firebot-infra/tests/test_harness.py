import json
import shutil
import tempfile
import unittest
from pathlib import Path

from firebot_infra.akshara import (Recorder, RecordingNotClean, diff_runs, json_equal, load_config, replay_run,
                                   summarize_run)


def record(root, run_id, fn, n=20, config=None, tamper=None):
    """A toy 'controller': input x -> output fn(x), both recorded."""
    with Recorder(root, run_id, software_version="v1", config=config or {"gain": 3}) as r:
        for i in range(n):
            r.emit("ctl", "step", x=i, out=fn(i))
        r.emit("ctl", "note", "warning", text="hello")
    return Path(root) / run_id


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def test_replay_identical_when_code_unchanged(self):
        d = record(self.root, "a", lambda x: x * 3)
        res = replay_run(d, lambda ctx, ev: ev.payload["x"] * ctx["gain"], setup=lambda cfg: cfg,
                         select=lambda e: e.event_type == "step")
        self.assertTrue(res.identical)
        self.assertTrue(res.trustworthy)
        self.assertEqual((res.steps, res.config), (20, {"gain": 3}))

    def test_replay_finds_behaviour_changes_with_the_event_that_differs(self):
        d = record(self.root, "a", lambda x: x * 3)
        res = replay_run(d, lambda ctx, ev: ev.payload["x"] * 3 + (1 if ev.payload["x"] == 7 else 0),
                         select=lambda e: e.event_type == "step")
        self.assertFalse(res.identical)
        self.assertEqual(len(res.mismatches), 1)
        m = res.mismatches[0]
        self.assertEqual((m.event.payload["x"], m.expected, m.actual), (7, 21, 22))
        res2 = replay_run(d, lambda ctx, ev: -1, select=lambda e: e.event_type == "step", stop_on_first=True)
        self.assertEqual((res2.steps, len(res2.mismatches)), (1, 1))

    def test_json_equal_ignores_tuple_list_and_key_order(self):
        self.assertTrue(json_equal({"a": (1, 2), "b": 1.5}, {"b": 1.5, "a": [1, 2]}))
        self.assertFalse(json_equal([1, 2], [1, 3]))

    def test_custom_expected_and_compare(self):
        d = record(self.root, "a", lambda x: x * 3)
        res = replay_run(d, lambda ctx, ev: ev.payload["x"] * 3.0001, select=lambda e: e.event_type == "step",
                         compare=lambda a, b: abs(a - b) < 0.01)
        self.assertTrue(res.identical)
        res = replay_run(d, lambda ctx, ev: ev.payload["x"], select=lambda e: e.event_type == "step",
                         expected=lambda e: e.payload["x"])
        self.assertTrue(res.identical)

    def test_nothing_selected_is_not_a_pass(self):
        d = record(self.root, "a", lambda x: x)
        res = replay_run(d, lambda c, e: None, select=lambda e: False)
        self.assertEqual((res.steps, res.identical), (0, False))

    def test_damaged_recording_is_flagged_untrustworthy_and_can_be_refused(self):
        d = record(self.root, "a", lambda x: x * 3)
        seg = sorted(d.glob("seg-*.jsonl"))[0]
        seg.write_bytes(seg.read_bytes()[:-9])
        res = replay_run(d, lambda ctx, ev: ev.payload["x"] * 3, select=lambda e: e.event_type == "step")
        self.assertTrue(res.identical)                       # what survived still matches...
        self.assertFalse(res.trustworthy)                    # ...but the harness says the recording is damaged
        with self.assertRaises(RecordingNotClean):
            replay_run(d, lambda ctx, ev: 0, require_clean=True)

    def test_diff_runs(self):
        a = record(self.root, "a", lambda x: x * 3)
        b = record(self.root, "b", lambda x: x * 3)
        self.assertTrue(diff_runs(a, b).identical)           # different ids/timestamps are ignored
        c = record(self.root, "c", lambda x: x * 3 + (5 if x == 4 else 0))
        d = diff_runs(a, c)
        self.assertEqual([i for i, _, _ in d.diffs], [4])
        e = record(self.root, "e", lambda x: x * 3, n=15)
        d = diff_runs(a, e)
        self.assertEqual((d.len_a, d.len_b, d.identical), (21, 16, False))
        self.assertEqual(d.diffs[0][0], 15)
        many = diff_runs(a, record(self.root, "f", lambda x: -x - 1), max_diffs=3)
        self.assertEqual((len(many.diffs), many.truncated), (3, True))
        only_steps = diff_runs(a, c, select=lambda e: e.event_type == "note")
        self.assertTrue(only_steps.identical)

    def test_summarize_run(self):
        d = record(self.root, "a", lambda x: x)
        with Recorder(self.root, "g", software_version="v9") as r:
            r.emit("net", "link_up")
            r.emit("net", "link_down", "error")
            r.emit("net", "retry", "warning")
        s = summarize_run(self.root / "g")
        self.assertEqual((s["events"], s["software_version"]), (3, "v9"))
        self.assertEqual(s["by_subsystem"], {"net": {"link_up": 1, "link_down": 1, "retry": 1}})
        self.assertEqual((s["first_error"][1:], s["first_warning"][1:]), (("net", "link_down"), ("net", "retry")))
        self.assertTrue(s["recording"]["clean"])
        s2 = summarize_run(d)
        self.assertEqual((s2["by_severity"], s2["config"]), ({"info": 20, "warning": 1}, {"gain": 3}))

    def test_missing_manifest_config_is_empty(self):
        d = record(self.root, "a", lambda x: x)
        (d / "manifest.json").unlink()
        self.assertEqual(load_config(d), {})


if __name__ == "__main__":
    unittest.main()
