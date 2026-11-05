import time
import unittest

from firebot_infra.setu import (MemoryHub, Node, SchemaError, SchemaRegistry, SetuRemoteError, SetuTimeout,
                                SetuUnavailable, TcpTransport, Envelope, encode, decode,
                                MalformedMessage, UnsupportedVersion)


def registry():
    r = SchemaRegistry()
    r.register("ping", 1, {"n": int})
    r.register("reading", 1, {"value": float, "unit": str})
    return r


def pair(**kw):
    hub = MemoryHub()
    a = Node("a", hub.transport("a"), registry(), **kw)
    b = Node("b", hub.transport("b"), registry(), **kw)
    return hub, a, b


class SetuTests(unittest.TestCase):
    def tearDown(self):
        for n in getattr(self, "nodes", []):
            n.close()

    def mk(self, **kw):
        hub, a, b = pair(**kw)
        self.nodes = [a, b]
        return hub, a, b

    def test_request_response(self):
        _, a, b = self.mk()
        b.on("ping", lambda e: {"n": e.payload["n"] + 1})
        self.assertEqual(a.request("b", "ping", {"n": 1}).payload, {"n": 2})

    def test_producer_validates_schema(self):
        _, a, _ = self.mk()
        with self.assertRaises(SchemaError):
            a.request("b", "ping", {"n": "x"})
        with self.assertRaises(SchemaError):
            a.send_event("b", "reading", {"value": 1.0})          # missing unit
        with self.assertRaises(SchemaError):
            a.send_event("b", "ping", {"n": True})                # bool is not int

    def test_receiver_rejects_bad_payload_and_unknown_type(self):
        hub, a, b = self.mk()
        b.on("ping", lambda e: {})
        bad = Envelope("ping", "a", "b", {"n": "oops"}, kind="request")
        got = []
        hub._rx["a"] = lambda data: got.append(decode(data))     # observe raw replies
        hub._deliver("a", "b", encode(bad))
        unk = Envelope("reading", "a", "b", {"value": 1.0, "unit": "m"}, kind="request")
        hub._deliver("a", "b", encode(unk))
        time.sleep(0.3)
        errs = sorted(g.payload["error"][:10] for g in got)
        self.assertEqual(errs, ["schema: fi", "unknown_ty"])
        self.assertEqual(b.metrics["schema_errors"], 1)

    def test_remote_error_not_retried(self):
        _, a, b = self.mk()
        calls = []
        def boom(e):
            calls.append(1); raise RuntimeError("x")
        b.on("ping", boom)
        with self.assertRaises(SetuRemoteError):
            a.request("b", "ping", {"n": 1}, retries=3, safe_to_retry=True)
        self.assertEqual(len(calls), 1)

    def test_expired_deadline_rejected(self):
        hub, a, b = self.mk()
        ran = []
        b.on("ping", lambda e: ran.append(1) or {})
        old = Envelope("ping", "a", "b", {"n": 1}, kind="event", deadline=time.time() - 5)
        hub._deliver("a", "b", encode(old))
        time.sleep(0.2)
        self.assertEqual(ran, [])
        self.assertEqual(b.metrics["expired"], 1)

    def test_duplicate_delivery_runs_handler_once(self):
        hub, a, b = self.mk()
        ran = []
        b.on("ping", lambda e: ran.append(1) or {})
        hub.fault = lambda s, d, data: "dup"
        a.send_event("b", "ping", {"n": 1})
        time.sleep(0.3)
        self.assertEqual(len(ran), 1)
        self.assertEqual(b.metrics["duplicates"], 1)

    def test_lost_request_retried_only_when_declared_safe(self):
        hub, a, b = self.mk()
        b.on("ping", lambda e: {"n": 0})
        dropped = []
        def drop_first(s, d, data):
            if s == "a" and not dropped:
                dropped.append(1); return "drop"
        hub.fault = drop_first
        with self.assertRaises(SetuTimeout):                      # not declared safe: no retry
            a.request("b", "ping", {"n": 1}, timeout=0.2, retries=2)
        dropped.clear()
        self.assertEqual(a.request("b", "ping", {"n": 1}, timeout=0.2, retries=2, safe_to_retry=True).payload, {"n": 0})

    def test_lost_response_with_idempotency_key_executes_once(self):
        hub, a, b = self.mk()
        runs = []
        b.on("ping", lambda e: runs.append(1) or {"n": len(runs)})
        dropped = []
        def drop_first_response(s, d, data):
            if s == "b" and not dropped:
                dropped.append(1); return "drop"
        hub.fault = drop_first_response
        r = a.request("b", "ping", {"n": 1}, timeout=0.25, retries=2, idempotency_key="op-1")
        self.assertEqual(r.payload, {"n": 1})
        self.assertEqual(len(runs), 1)
        self.assertEqual(b.metrics["idempotent_replays"], 1)

    def test_slow_consumer_queue_stays_bounded(self):
        hub, a, b = self.mk(queue_size=10)
        b.on("ping", lambda e: time.sleep(0.05))
        for i in range(300):
            a.send_event("b", "ping", {"n": i})
        self.assertLessEqual(b._inbox.qsize(), 10)
        self.assertGreater(b.metrics["dropped_backpressure"], 0)

    def test_malformed_and_unsupported_version(self):
        hub, a, b = self.mk()
        hub._deliver("a", "b", b"\x00not json")
        hub._deliver("a", "b", encode(Envelope("ping", "a", "b", {"n": 1})).replace(b'"protocol_version":1', b'"protocol_version":99'))
        time.sleep(0.2)
        self.assertEqual(b.metrics["malformed"], 1)
        self.assertEqual(b.metrics["unsupported_version"], 1)
        with self.assertRaises(MalformedMessage):
            decode(b"[]")
        with self.assertRaises(UnsupportedVersion):
            decode(encode(Envelope("ping", "a", "b", {})).replace(b'"protocol_version":1', b'"protocol_version":2'))

    def test_disconnect_reports_unavailable_and_recovers(self):
        hub, a, b = self.mk()
        b.on("ping", lambda e: {"n": 0})
        hub.disconnect("b")
        with self.assertRaises(SetuUnavailable):
            a.request("b", "ping", {"n": 1}, timeout=0.1)
        hub.reconnect("b")
        self.assertEqual(a.request("b", "ping", {"n": 1}).payload, {"n": 0})

    def test_authorize_hook(self):
        _, a, b = self.mk(authorize=lambda e: e.source_id == "trusted")
        b.on("ping", lambda e: {})
        with self.assertRaises(SetuRemoteError):
            a.request("b", "ping", {"n": 1})

    def test_handler_requires_registered_schema(self):
        _, a, _ = self.mk()
        with self.assertRaises(SchemaError):
            a.on("nope", lambda e: {})

    def test_observability_failure_does_not_break_messaging(self):
        def bad_logger(*a, **k):
            raise RuntimeError("recorder down")
        hub, a, b = self.mk(on_event=bad_logger)
        b.on("ping", lambda e: {"n": 0})
        hub._deliver("a", "b", b"garbage")
        self.assertEqual(a.request("b", "ping", {"n": 1}).payload, {"n": 0})

    def test_tcp_transport_roundtrip(self):
        ta, tb = TcpTransport(), TcpTransport()
        ta.add_peer("b", "127.0.0.1", tb.port)
        tb.add_peer("a", "127.0.0.1", ta.port)
        a, b = Node("a", ta, registry()), Node("b", tb, registry())
        self.nodes = [a, b]
        b.on("ping", lambda e: {"n": e.payload["n"] * 2})
        self.assertEqual(a.request("b", "ping", {"n": 21}, timeout=2).payload, {"n": 42})

    def test_tcp_unknown_peer_and_dead_peer(self):
        ta = TcpTransport()
        a = Node("a", ta, registry())
        self.nodes = [a]
        self.assertFalse(ta.send("ghost", b"x"))
        ta.add_peer("dead", "127.0.0.1", 9)
        self.assertFalse(ta.send("dead", b"x"))


if __name__ == "__main__":
    unittest.main()
