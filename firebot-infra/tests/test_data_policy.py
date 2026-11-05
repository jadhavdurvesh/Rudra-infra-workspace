"""The dossier's data rules, as executable checks: what is logged, what never leaves the robot, who can read it,
and how long it is kept."""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from firebot_infra.akshara import Recorder, prune_runs, read_run
from firebot_infra.ananta import Connector, Spool, SqliteAnalytics
from firebot_infra.ananta.connector import DEFAULT_DENY, MAX_META_KEYS, MAX_STR
from firebot_infra.cli import main


class Tmp(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.dir = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()


@unittest.skipUnless(os.name == "posix", "permission bits are POSIX")
class AccessTests(Tmp):
    def test_recordings_are_owner_only(self):
        with Recorder(self.dir, "r", segment_bytes=2000) as rec:
            for i in range(100):
                rec.emit("s", "e", i=i, pad="x" * 50)
        run = self.dir / "r"
        self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
        files = list(run.iterdir())
        self.assertGreater(len(files), 2)                 # several segments plus the manifest
        for f in files:
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600, f.name)


class PrivacyTests(Tmp):
    def ship(self, payload, salt="s", device="bot"):
        db = SqliteAnalytics()
        self._n = getattr(self, "_n", 0) + 1                   # every call records into its own run folder
        conn = Connector(Spool(self.dir / f"sp{self._n}"), db, device_id=device, salt=salt)
        with Recorder(self.dir / "runs", f"r{self._n}", tee=conn.log) as rec:
            rec.emit("assistant", "turn", **payload)
        conn.flush()
        return db, db.search(subsystem="assistant")[0]

    def test_sensitive_fields_never_reach_analytics_even_when_nested(self):
        secret = {k: "SECRET-" + k for k in DEFAULT_DENY}
        db, row = self.ship({**secret, "nested": {"gps": "SECRET-gps", "ok": 1, "deeper": {"audio": "SECRET-audio"}},
                             "items": [{"transcript": "SECRET-t"}], "fine": 7})
        blob = json.dumps(row)
        self.assertNotIn("SECRET", blob)
        self.assertEqual(row["metadata"]["fine"], 7)
        self.assertEqual(row["metadata"]["nested"]["ok"], 1)

    def test_the_raw_recording_stays_local_and_complete(self):
        with Recorder(self.dir, "r") as rec:
            rec.emit("assistant", "turn", transcript="kept locally for debugging")
        events, _ = read_run(self.dir / "r")
        self.assertEqual(events[0].payload["transcript"], "kept locally for debugging")   # policy: local, access-controlled

    def test_metadata_is_bounded(self):
        _, row = self.ship({**{f"k{i}": i for i in range(50)}, "long": "x" * 5000})
        self.assertLessEqual(len(row["metadata"]), MAX_META_KEYS)
        self.assertLessEqual(len(row["metadata"].get("long", "")), MAX_STR + 20)

    def test_device_identity_is_pseudonymous_and_salt_dependent(self):
        _, a = self.ship({"x": 1}, salt="one")
        _, a2 = self.ship({"x": 1}, salt="one", device="bot")
        _, b = self.ship({"x": 1}, salt="two")
        self.assertNotIn("bot", a["device_id"])
        self.assertEqual(a["device_id"], a2["device_id"])          # stable within one deployment
        self.assertNotEqual(a["device_id"], b["device_id"])         # not linkable across deployments


class RetentionTests(Tmp):
    def make(self, name, age_days, complete=True, now=None):
        now = now or time.time()
        with Recorder(self.dir, name) as rec:
            rec.emit("s", "e")
        mp = self.dir / name / "manifest.json"
        m = json.loads(mp.read_text())
        m["started_wall"] = now - age_days * 86400
        m["complete"] = complete
        mp.write_text(json.dumps(m))

    def test_only_old_complete_recordings_are_removed(self):
        self.make("old", 40); self.make("new", 2); self.make("old-crashed", 90, complete=False)
        (self.dir / "not-a-run").mkdir(); (self.dir / "not-a-run" / "precious.txt").write_text("keep")
        (self.dir / "loose.txt").write_text("keep")
        (self.dir / "fake").mkdir(); (self.dir / "fake" / "manifest.json").write_text('{"format": "something-else"}')
        listed = prune_runs(self.dir, 30 * 86400)                   # default: dry run
        self.assertEqual([p.name for p in listed], ["old"])
        self.assertTrue((self.dir / "old").exists())
        prune_runs(self.dir, 30 * 86400, dry_run=False)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["fake", "loose.txt", "new", "not-a-run", "old-crashed"])
        prune_runs(self.dir, 30 * 86400, dry_run=False, include_incomplete=True)
        self.assertFalse((self.dir / "old-crashed").exists())
        self.assertTrue((self.dir / "not-a-run" / "precious.txt").exists())

    def test_symlinked_directories_are_never_followed(self):
        outside = Path(tempfile.mkdtemp())
        try:
            with Recorder(outside, "victim") as rec:
                rec.emit("s", "e")
            m = json.loads((outside / "victim" / "manifest.json").read_text()); m["started_wall"] = 1.0
            (outside / "victim" / "manifest.json").write_text(json.dumps(m))
            (self.dir / "link").symlink_to(outside / "victim", target_is_directory=True)
            prune_runs(self.dir, 1, dry_run=False)
            self.assertTrue((outside / "victim" / "manifest.json").exists())
        finally:
            import shutil
            shutil.rmtree(outside)

    def test_analytics_retention(self):
        db = SqliteAnalytics()
        db.send("b", [{"event_id": f"e{i}", "event_time": 1000.0 + i, "subsystem": "s", "event_type": "t"} for i in range(10)])
        self.assertEqual(db.prune(1005.0), 5)
        self.assertEqual(db.count(), 5)

    def test_cli_prune_is_dry_run_by_default(self):
        self.make("old", 40)
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["prune-runs", str(self.dir), "--older-than-days", "30"]), 0)
        self.assertIn("would delete", out.getvalue())
        self.assertTrue((self.dir / "old").exists())
        with contextlib.redirect_stdout(io.StringIO()):
            main(["prune-runs", str(self.dir), "--older-than-days", "30", "--yes"])
        self.assertFalse((self.dir / "old").exists())


if __name__ == "__main__":
    unittest.main()
