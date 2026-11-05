import tempfile
import unittest
from pathlib import Path

from firebot_infra.sangam import ConflictPending, Policy, Store, TypePolicy, sync

TYPES = [TypePolicy("run_log", Policy.APPEND_ONLY),
         TypePolicy("robot_status", Policy.SINGLE_WRITER, owner="robot"),
         TypePolicy("settings", Policy.REJECT_AND_REVIEW),
         TypePolicy("notes", Policy.LAST_WRITER_WINS)]


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): self.t += 1; return self.t


def pair(**kw):
    c = Clock()
    return Store("robot", TYPES, clock=c, **kw), Store("station", TYPES, clock=c, **kw), c


class SangamTests(unittest.TestCase):
    def test_offline_then_reconcile(self):
        r, s, _ = pair()
        r.put("run_log", None, {"msg": "r1"}); r.put("run_log", None, {"msg": "r2"})
        r.put("robot_status", "battery", 80)
        s.put("run_log", None, {"msg": "s1"})
        res = sync(r, s)
        self.assertEqual(res.rejected, [])
        self.assertEqual(r.items("run_log"), s.items("run_log"))
        self.assertEqual(len(r.items("run_log")), 3)
        self.assertEqual(s.get("robot_status", "battery"), 80)

    def test_only_missing_entries_are_sent_and_sync_is_idempotent(self):
        r, s, _ = pair()
        for i in range(5):
            r.put("run_log", None, i)
        sync(r, s)
        r.put("run_log", None, 5)
        delta = r.delta_for(s.version_vector())
        self.assertEqual(len(delta), 1)
        self.assertEqual(r.pending_for(s.version_vector()), 1)
        sync(r, s)
        again = sync(r, s)
        self.assertEqual((again.applied, again.conflicts), (0, 0))
        self.assertEqual(r.pending_for(s.version_vector()), 0)

    def test_bounded_batches_converge_over_rounds(self):
        r, s, _ = pair()
        for i in range(23):
            r.put("run_log", None, i)
        sync(r, s, batch=5)
        self.assertEqual(len(s.items("run_log")), 23)
        self.assertEqual(len(r.delta_for({}, max_entries=7)), 7)

    def test_single_writer_enforced_locally_and_remotely(self):
        r, s, _ = pair()
        with self.assertRaises(PermissionError):
            s.put("robot_status", "battery", 1)
        forged = {"peer": "station", "seq": 1, "type": "robot_status", "key": "battery", "value": 1,
                  "base": None, "resolves": [], "wall": 5.0, "expires_at": None}
        res = r.apply([forged])
        self.assertEqual(res.rejected, [("station:1", "not_owner")])
        self.assertIsNone(r.get("robot_status", "battery"))

    def test_reject_and_review_conflict_surfaced_then_resolved_everywhere(self):
        r, s, _ = pair()
        r.put("settings", "mode", "a"); sync(r, s)
        r.put("settings", "mode", "robot-edit")          # concurrent offline edits
        s.put("settings", "mode", "station-edit")
        res = sync(r, s)
        self.assertEqual(res.conflicts, 2)
        self.assertEqual(r.get("settings", "mode"), "robot-edit")     # neither side silently overwritten
        self.assertEqual(s.get("settings", "mode"), "station-edit")
        self.assertEqual((len(r.conflicts), len(s.conflicts)), (1, 1))
        with self.assertRaises(ConflictPending):
            r.put("settings", "mode", "again")
        r.resolve("settings", "mode", "merged")
        sync(r, s)
        self.assertEqual((r.get("settings", "mode"), s.get("settings", "mode")), ("merged", "merged"))
        self.assertEqual((r.conflicts, s.conflicts), ([], []))
        s.put("settings", "mode", "later")               # normal editing resumes cleanly
        sync(r, s)
        self.assertEqual(r.get("settings", "mode"), "later")
        self.assertEqual(r.conflicts, [])

    def test_fast_forward_is_not_a_conflict(self):
        r, s, _ = pair()
        r.put("settings", "mode", "a"); sync(r, s)
        s.put("settings", "mode", "b"); sync(r, s)
        r.put("settings", "mode", "c"); res = sync(r, s)
        self.assertEqual((res.conflicts, s.get("settings", "mode")), (0, "c"))

    def test_lww_deterministic_regardless_of_direction(self):
        for first in ("rs", "sr"):
            c = Clock()
            r, s = Store("robot", TYPES, clock=c), Store("station", TYPES, clock=c)
            r.put("notes", "n", "from-robot"); s.put("notes", "n", "from-station")   # station is later
            sync(*((r, s) if first == "rs" else (s, r)))
            self.assertEqual(r.get("notes", "n"), s.get("notes", "n"))
            self.assertEqual(r.get("notes", "n"), "from-station")

    def test_append_only_duplicate_key_surfaced(self):
        r, s, _ = pair()
        r.put("run_log", "same", 1); s.put("run_log", "same", 2)
        res = sync(r, s)
        self.assertEqual(res.conflicts, 2)
        with self.assertRaises(KeyError):
            r.put("run_log", "same", 3)

    def test_expired_entries_never_applied_but_sync_converges(self):
        r, s, c = pair()
        r.put("notes", "temp", "ephemeral", ttl_s=2)
        c.t += 50
        res = sync(r, s)
        self.assertEqual(res.expired, 1)
        self.assertIsNone(s.get("notes", "temp"))
        self.assertIsNone(r.get("notes", "temp"))          # hidden at read time too
        self.assertEqual(r.pending_for(s.version_vector()), 0)
        self.assertEqual(sync(r, s).expired, 0)            # not re-sent forever

    def test_operational_command_types_refused(self):
        for name in ("command_queue", "motor_cmd", "estop", "actuator_setpoints", "suppress_fire"):
            with self.assertRaises(ValueError):
                TypePolicy(name, Policy.APPEND_ONLY)

    def test_gap_forged_origin_unknown_type_malformed_and_authz(self):
        r, s, _ = pair(authorize=None)
        base = {"peer": "x", "seq": 2, "type": "run_log", "key": "k", "value": 1, "base": None,
                "resolves": [], "wall": 1.0, "expires_at": None}
        self.assertEqual(r.apply([base]).rejected, [("x:2", "gap")])
        self.assertEqual(r.apply([{**base, "peer": "robot", "seq": 5}]).rejected, [("robot:5", "forged_origin")])
        self.assertEqual(r.apply([{**base, "seq": 1, "type": "nope"}]).rejected, [("x:1", "unknown_type")])
        self.assertEqual(r.apply([{"junk": 1}, "str"]).rejected, [("?", "malformed"), ("?", "malformed")])
        r2 = Store("robot", TYPES, authorize=lambda peer, t: peer != "evil")
        self.assertEqual(r2.apply([{**base, "peer": "evil", "seq": 1}]).rejected, [("evil:1", "unauthorized")])
        with self.assertRaises(PermissionError):
            Store("evil", TYPES, authorize=lambda p, t: False).put("run_log", None, 1)

    def test_persistence_roundtrip_including_conflicts(self):
        with tempfile.TemporaryDirectory() as d:
            c = Clock()
            r = Store("robot", TYPES, path=Path(d) / "r.json", clock=c)
            s = Store("station", TYPES, clock=c)
            r.put("settings", "m", "a"); sync(r, s)
            r.put("settings", "m", "r"); s.put("settings", "m", "s"); sync(r, s)
            r2 = Store("robot", TYPES, path=Path(d) / "r.json", clock=c)
            self.assertEqual(r2.version_vector(), r.version_vector())
            self.assertEqual(len(r2.conflicts), 1)
            self.assertEqual(r2.get("settings", "m"), "r")
            r2.resolve("settings", "m", "final"); sync(r2, s)
            self.assertEqual(s.get("settings", "m"), "final")

    def test_values_must_be_json(self):
        r, _, _ = pair()
        with self.assertRaises(TypeError):
            r.put("notes", "x", object())


if __name__ == "__main__":
    unittest.main()
