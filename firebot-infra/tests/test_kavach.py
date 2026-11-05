import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

try:
    import cryptography  # noqa: F401
except ImportError:
    raise unittest.SkipTest("KAVACH tests need the optional 'cryptography' package: pip install .[crypto]")

from firebot_infra.kavach import (AuthError, Identity, InsecureKeyFile, ManifestError, Policy, Registry,
                                  SessionAuthorizer, Verifier, mutual_authenticate, scan_for_private_keys,
                                  sign_manifest, verify_manifest)
from firebot_infra.setu import MemoryHub, Node, SchemaRegistry, SetuRemoteError

POLICY = Policy({"robot": {"report"}, "station": {"report", "configure"}, "release": {"sign_artifacts"}})


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class KavachTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.reg = Registry()
        self.ids = {n: Identity.generate(n) for n in ("robot", "station", "release", "rogue")}
        for n, role in (("robot", "robot"), ("station", "station"), ("release", "release")):
            self.reg.enroll(n, self.ids[n].public_bytes(), role)
        self.events = []
        self.v = Verifier("station", self.reg, POLICY, clock=self.clock, on_event=lambda *a, **k: self.events.append((a[1], k)))

    def auth(self, who, v=None, audience=None):
        v = v or self.v
        n = v.challenge()
        return v.verify(who, n, self.ids[who].prove(n, audience or v.self_id))

    def test_auth_success_and_authorization_is_separate(self):
        peer = self.auth("robot")
        self.assertEqual((peer.device_id, peer.role), ("robot", "robot"))
        self.assertTrue(self.v.authorize(peer, "report"))
        self.assertFalse(self.v.authorize(peer, "configure"))   # authenticated but not authorized
        self.assertIn("authz_denied", [e[0] for e in self.events])

    def test_unknown_revoked_and_bad_signature_rejected(self):
        with self.assertRaises(AuthError) as c:
            self.auth("rogue")
        self.assertEqual(c.exception.reason, "unknown_device")
        self.reg.revoke("robot")
        with self.assertRaises(AuthError) as c:
            self.auth("robot")
        self.assertEqual(c.exception.reason, "revoked")
        n = self.v.challenge()
        with self.assertRaises(AuthError) as c:
            self.v.verify("station", n, self.ids["rogue"].prove(n, "station"))  # signed with someone else's key
        self.assertEqual(c.exception.reason, "bad_signature")

    def test_nonce_single_use_expiry_and_audience_binding(self):
        n = self.v.challenge()
        sig = self.ids["robot"].prove(n, "station")
        self.v.verify("robot", n, sig)
        with self.assertRaises(AuthError) as c:
            self.v.verify("robot", n, sig)                      # replay
        self.assertEqual(c.exception.reason, "unknown_nonce")
        n = self.v.challenge()
        self.clock.t += 11
        with self.assertRaises(AuthError) as c:
            self.v.verify("robot", n, self.ids["robot"].prove(n, "station"))
        self.assertEqual(c.exception.reason, "nonce_expired")
        with self.assertRaises(AuthError) as c:                 # signature meant for another verifier
            self.auth("robot", audience="other-station")
        self.assertEqual(c.exception.reason, "bad_signature")

    def test_outstanding_challenges_bounded(self):
        v = Verifier("s", self.reg, max_outstanding=3)
        for _ in range(3):
            v.challenge()
        with self.assertRaises(AuthError):
            v.challenge()

    def test_mutual_auth(self):
        rv = Verifier("robot", self.reg, POLICY)
        a, b = mutual_authenticate(self.ids["robot"], rv, self.ids["station"], self.v)
        self.assertEqual((a.device_id, b.device_id), ("robot", "station"))
        with self.assertRaises(AuthError):
            mutual_authenticate(self.ids["rogue"], rv, self.ids["station"], self.v)

    def test_key_rotation(self):
        old = self.ids["robot"]
        new = Identity.generate("robot")
        self.reg.rotate_key("robot", new.public_bytes())
        n = self.v.challenge()
        with self.assertRaises(AuthError):
            self.v.verify("robot", n, old.prove(n, "station"))
        n = self.v.challenge()
        self.assertEqual(self.v.verify("robot", n, new.prove(n, "station")).key_version, 2)

    def test_key_file_permissions_and_passphrase(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "robot.key"
            self.ids["robot"].save(p, passphrase=b"pw")
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                self.ids["robot"].save(p)                       # never overwrite a key
            loaded = Identity.load("robot", p, passphrase=b"pw")
            self.assertEqual(loaded.public_bytes(), self.ids["robot"].public_bytes())
            with self.assertRaises(Exception):
                Identity.load("robot", p, passphrase=b"wrong")
            os.chmod(p, 0o644)
            with self.assertRaises(InsecureKeyFile):
                Identity.load("robot", p, passphrase=b"pw")

    def test_registry_persists_public_keys_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "reg.json"
            r = Registry(path)
            r.enroll("robot", self.ids["robot"].public_bytes(), "robot")
            self.assertNotIn("PRIVATE", path.read_text())
            self.assertEqual(Registry(path).get("robot").role, "robot")
            self.assertEqual(scan_for_private_keys(d), [])
            self.ids["robot"].save(Path(d) / "leak.key")
            self.assertEqual(len(scan_for_private_keys(d)), 1)
            with self.assertRaises(ValueError):
                r.enroll("robot", self.ids["robot"].public_bytes(), "robot")   # no silent re-enrol
            with self.assertRaises(ValueError):
                r.enroll("x", b"short", "robot")

    def test_signed_manifest(self):
        files = {"fw.bin": b"firmware-v2", "cfg.json": b"{}"}
        m = sign_manifest(self.ids["release"], "fw", 2, files)
        self.assertEqual(verify_manifest(m, self.reg, POLICY, files, min_version=1)["version"], 2)
        bad = dict(files, **{"fw.bin": b"firmware-EVIL"})
        for args, reason in (
            ((m, self.reg, POLICY, bad), "hash_mismatch:fw.bin"),
            ((m, self.reg, POLICY, {"fw.bin": files["fw.bin"]}), "file_set_mismatch"),
            ((m, self.reg, POLICY, dict(files, extra=b"x")), "file_set_mismatch"),
        ):
            with self.assertRaises(ManifestError) as c:
                verify_manifest(*args)
            self.assertEqual(c.exception.reason, reason)
        with self.assertRaises(ManifestError) as c:
            verify_manifest(m, self.reg, POLICY, files, min_version=3)
        self.assertEqual(c.exception.reason, "version_rollback")
        forged = json.loads(json.dumps(m)); forged["body"]["version"] = 9
        with self.assertRaises(ManifestError) as c:
            verify_manifest(forged, self.reg, POLICY, files)
        self.assertEqual(c.exception.reason, "bad_signature")
        with self.assertRaises(ManifestError) as c:             # robot lacks sign_artifacts
            verify_manifest(sign_manifest(self.ids["robot"], "fw", 5, files), self.reg, POLICY, files)
        self.assertEqual(c.exception.reason, "signer_not_authorized")
        with self.assertRaises(ManifestError) as c:
            verify_manifest(sign_manifest(self.ids["rogue"], "fw", 5, files), self.reg, POLICY, files)
        self.assertEqual(c.exception.reason, "unknown_signer")
        self.reg.revoke("release")
        with self.assertRaises(ManifestError) as c:
            verify_manifest(m, self.reg, POLICY, files)
        self.assertEqual(c.exception.reason, "revoked_signer")
        with self.assertRaises(ManifestError):
            verify_manifest({"nope": 1}, self.reg, POLICY, files)

    def test_manifest_with_file_paths(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.bin"; p.write_bytes(b"abc")
            m = sign_manifest(self.ids["release"], "x", 1, {"a.bin": p})
            verify_manifest(m, self.reg, POLICY, {"a.bin": p})
            p.write_bytes(b"abd")
            with self.assertRaises(ManifestError):
                verify_manifest(m, self.reg, POLICY, {"a.bin": p})

    def test_setu_integration_session_authorizer(self):
        schema = SchemaRegistry()
        schema.register("config", 1, {"k": str})
        schema.register("status", 1, {"ok": bool})
        authz = SessionAuthorizer(self.reg, POLICY, {"config": "configure", "status": "report"}, clock=self.clock)
        hub = MemoryHub()
        robot = Node("robot", hub.transport("robot"), schema, authorize=authz)
        station = Node("station", hub.transport("station"), schema)
        rogue = Node("rogue", hub.transport("rogue"), schema)
        try:
            robot.on("config", lambda e: {}); robot.on("status", lambda e: {})
            with self.assertRaises(SetuRemoteError):             # no session yet: default deny
                station.request("robot", "status", {"ok": True})
            authz.grant(self.auth("station", Verifier("robot", self.reg, POLICY, clock=self.clock)))
            station.request("robot", "config", {"k": "v"})        # station may configure
            with self.assertRaises(SetuRemoteError):
                rogue.request("robot", "config", {"k": "v"})      # rogue has no session
            self.reg.revoke("station")
            with self.assertRaises(SetuRemoteError):              # revocation takes effect immediately
                station.request("robot", "config", {"k": "v"})
        finally:
            for n in (robot, station, rogue):
                n.close()

    def test_session_expiry_and_unmapped_type_denied(self):
        authz = SessionAuthorizer(self.reg, POLICY, {"status": "report"}, session_ttl_s=5, clock=self.clock)
        from firebot_infra.kavach import Peer
        from firebot_infra.setu import Envelope
        authz.grant(Peer("robot", "robot", 1))
        self.assertTrue(authz(Envelope("status", "robot", "x")))
        self.assertFalse(authz(Envelope("unmapped", "robot", "x")))
        self.clock.t += 6
        self.assertFalse(authz(Envelope("status", "robot", "x")))


if __name__ == "__main__":
    unittest.main()
