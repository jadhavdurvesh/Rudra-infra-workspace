import time
import unittest

try:
    import cryptography  # noqa: F401
except ImportError:
    raise unittest.SkipTest("KAVACH tests need the optional 'cryptography' package: pip install .[crypto]")

from firebot_infra.kavach import (EnvelopeSigner, EnvelopeVerifier, Identity, Policy, Registry, canonical_bytes)
from firebot_infra.setu import (Envelope, MemoryHub, Node, SchemaRegistry, SetuRemoteError, SetuTimeout, StreamTransport,
                                encode, make_pipe)

POLICY = Policy({"station": {"report", "configure"}, "robot": {"report"}})
CAPS = {"ping": "report", "config": "configure", "unmapped": "nothing"}


def schema():
    r = SchemaRegistry()
    r.register("ping", 1, {"n": int}); r.register("config", 1, {"k": str}); r.register("unmapped", 1, {"n": int})
    return r


class Clock:
    def __init__(self): self.t = time.time()
    def __call__(self): return self.t


class EnvelopeAuthTests(unittest.TestCase):
    def setUp(self):
        self.reg = Registry()
        self.ids = {n: Identity.generate(n) for n in ("station", "robot", "attacker")}
        self.reg.enroll("station", self.ids["station"].public_bytes(), "station")
        self.reg.enroll("robot", self.ids["robot"].public_bytes(), "robot")
        self.events = []
        self.clock = Clock()
        self.ver = EnvelopeVerifier(self.reg, POLICY, CAPS, clock=self.clock, replay_window=50,
                                    on_event=lambda *a, **k: self.events.append(k.get("reason")))
        self.nodes = []

    def tearDown(self):
        for n in self.nodes:
            n.close()

    def pair(self, hub=None, **robot_kw):
        hub = hub or MemoryHub()
        robot = Node("robot", hub.transport("robot"), schema(), authorize=self.ver, sign=EnvelopeSigner(self.ids["robot"]),
                     **robot_kw)
        st_ver = EnvelopeVerifier(self.reg, POLICY, {}, clock=self.clock)
        station = Node("station", hub.transport("station"), schema(), sign=EnvelopeSigner(self.ids["station"]),
                       verify_response=st_ver.verify_response)
        self.nodes += [robot, station]
        return hub, robot, station

    def signed(self, who="station", dst="robot", mtype="ping", payload=None, **kw):
        env = Envelope(mtype, kw.pop("source", who), dst, payload or {"n": 1}, kind=kw.pop("kind", "event"),
                       created_at=kw.pop("created_at", self.clock()), deadline=kw.pop("deadline", self.clock() + 10), **kw)
        EnvelopeSigner(self.ids[who])(env)
        return env

    def deliver(self, hub, env, wait=0.15):
        hub._deliver("station", "robot", encode(env))
        time.sleep(wait)

    # ------------------------------------------------------------------
    def test_signed_round_trip_with_authenticated_response(self):
        hub, robot, station = self.pair()
        robot.on("ping", lambda e: {"n": e.payload["n"] + 1})
        self.assertEqual(station.request("robot", "ping", {"n": 1}, timeout=1).payload, {"n": 2})
        self.assertEqual(station.metrics["bad_response_auth"], 0)

    def test_unsigned_and_tampered_messages_rejected(self):
        hub, robot, station = self.pair()
        ran = []
        robot.on("ping", lambda e: ran.append(e.payload) or {})
        self.deliver(hub, Envelope("ping", "station", "robot", {"n": 1}, deadline=self.clock() + 5))   # no signature
        env = self.signed()
        env.payload = {"n": 999}                                                                          # altered after signing
        self.deliver(hub, env)
        self.assertEqual(ran, [])
        self.assertEqual(robot.metrics["forbidden"], 2)
        self.assertEqual(self.events, ["unsigned", "bad_signature"])

    def test_cannot_claim_to_be_another_device(self):
        hub, robot, station = self.pair()
        ran = []
        robot.on("config", lambda e: ran.append(1) or {})
        env = Envelope("config", "station", "robot", {"k": "x"}, deadline=self.clock() + 5)
        EnvelopeSigner(self.ids["robot"])(env)                           # robot's valid key, but claims to be the station
        self.deliver(hub, env)
        self.assertEqual((ran, self.events), ([], ["source_mismatch"]))
        env2 = Envelope("config", "attacker", "robot", {"k": "x"}, deadline=self.clock() + 5)
        EnvelopeSigner(self.ids["attacker"])(env2)                       # honest about who it is, but not enrolled
        self.deliver(hub, env2)
        self.assertEqual(self.events[-1], "unknown_signer")

    def test_revocation_applies_to_the_next_message(self):
        hub, robot, station = self.pair()
        robot.on("ping", lambda e: {})
        station.request("robot", "ping", {"n": 1}, timeout=1)
        self.reg.revoke("station")
        with self.assertRaises(SetuRemoteError):
            station.request("robot", "ping", {"n": 1}, timeout=1)
        self.assertIn("revoked", self.events)

    def test_replay_is_rejected_even_after_the_node_forgets_the_message(self):
        hub, robot, station = self.pair()
        ran = []
        robot.on("ping", lambda e: ran.append(1) or {})
        env = self.signed()
        self.deliver(hub, env)
        robot._seen.clear()                                              # dedup window rolled over / node restarted
        self.deliver(hub, env)                                           # attacker re-sends the captured bytes
        self.assertEqual(len(ran), 1)
        self.assertEqual(self.events, ["replay"])

    def test_freshness_rules(self):
        hub, robot, station = self.pair()
        robot.on("ping", lambda e: {})
        now = self.clock()
        cases = {"no_deadline": self.signed(deadline=None),
                 "validity_too_long": self.signed(deadline=now + 3600),
                 "bad_timestamp": self.signed(created_at=now - 3600, deadline=now + 5)}
        for env in cases.values():
            self.deliver(hub, env, wait=0.05)
        self.assertEqual(self.events, list(cases))
        env = self.signed(deadline=now + 10)                             # fine when sent...
        self.clock.t += 30                                               # ...but the verifier's clock moved on
        robot._clock = self.clock
        ok, reason = self.ver.verify(env)
        self.assertEqual((ok, reason), (False, "expired"))

    def test_roles_authorization_is_separate_from_identity(self):
        hub, robot, station = self.pair()
        robot.on("config", lambda e: {}); robot.on("ping", lambda e: {}); robot.on("unmapped", lambda e: {})
        env = self.signed("robot", "station", kind="request")            # robot -> station not relevant here; test verifier directly
        ok, _ = self.ver.verify(env)
        self.assertTrue(ok)
        self.assertFalse(self.ver(self.signed("robot", "robot", "config", {"k": "x"})))      # robot role lacks `configure`
        self.assertTrue(self.ver(self.signed("station", "robot", "config", {"k": "x"})))
        self.assertFalse(self.ver(self.signed("station", "robot", "unmapped")))              # unmapped capability: deny
        self.assertIn("not_authorized", self.events)

    def test_forged_response_is_ignored_and_real_one_still_arrives(self):
        hub, robot, station = self.pair()
        robot.on("ping", lambda e: {"n": 42})
        pending_ids = []
        real_send = station._t.send
        def spy(dest, data):
            import json
            pending_ids.append(json.loads(data)["message_id"])
            # an attacker injects an unsigned "response" first
            forged = Envelope("ping", "robot", "station", {"n": 666}, kind="response", correlation_id=pending_ids[-1])
            hub._deliver("robot", "station", encode(forged))
            return real_send(dest, data)
        station._t.send = spy
        self.assertEqual(station.request("robot", "ping", {"n": 1}, timeout=1).payload, {"n": 42})
        self.assertEqual(station.metrics["bad_response_auth"], 1)

    def test_signed_retry_with_lost_response_executes_once(self):
        hub, robot, station = self.pair()
        runs = []
        robot.on("ping", lambda e: runs.append(1) or {"n": len(runs)})
        dropped = []
        def drop_first_response(s, d, data):
            if s == "robot" and not dropped:
                dropped.append(1)
                return "drop"
        hub.fault = drop_first_response
        r = station.request("robot", "ping", {"n": 1}, timeout=0.3, retries=2, idempotency_key="op-1")
        self.assertEqual((r.payload, len(runs)), ({"n": 1}, 1))
        self.assertEqual(station.metrics["bad_response_auth"], 0)       # the replayed response was re-signed and verified

    def test_replay_cache_is_bounded(self):
        hub, robot, station = self.pair()
        robot.on("ping", lambda e: {})
        for _ in range(120):
            self.ver.verify(self.signed())
        self.assertLessEqual(len(self.ver._seen), 50)

    def test_works_over_a_serial_style_stream(self):
        a, b = make_pair = make_pipe()
        robot = Node("robot", StreamTransport(b, "station"), schema(), authorize=self.ver,
                     sign=EnvelopeSigner(self.ids["robot"]))
        st_ver = EnvelopeVerifier(self.reg, POLICY, {}, clock=self.clock)
        station = Node("station", StreamTransport(a, "robot"), schema(), sign=EnvelopeSigner(self.ids["station"]),
                       verify_response=st_ver.verify_response)
        self.nodes += [robot, station]
        robot.on("config", lambda e: {})
        station.request("robot", "config", {"k": "v"}, timeout=2)
        station2_missing_sig = Node("x", StreamTransport(make_pipe()[0], "robot"), schema())
        self.nodes.append(station2_missing_sig)

    def test_canonical_bytes_ignore_only_the_auth_block(self):
        env = self.signed()
        base = canonical_bytes(env)
        env.auth = {"kid": "other", "sig": "00"}
        self.assertEqual(canonical_bytes(env), base)
        env.deadline += 1
        self.assertNotEqual(canonical_bytes(env), base)


if __name__ == "__main__":
    unittest.main()
