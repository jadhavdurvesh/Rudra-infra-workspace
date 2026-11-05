import datetime as dt
import os
import socket
import stat
import tempfile
import time
import unittest
from pathlib import Path

try:
    import cryptography  # noqa: F401
except ImportError:
    raise unittest.SkipTest("TLS tests need the optional 'cryptography' package: pip install .[crypto]")

from firebot_infra.kavach import (EnvelopeSigner, EnvelopeVerifier, Identity, Policy, Registry, create_ca, issue_cert,
                                  write_pem)
from firebot_infra.setu import (Node, SchemaRegistry, SetuError, TcpTransport, encode, make_contexts, Envelope)


def schema():
    r = SchemaRegistry(); r.register("ping", 1, {"n": int}); return r


class Fleet:
    """A throwaway private CA with a few node certificates on disk."""

    def __init__(self, root: Path):
        self.root = root
        self.ca_cert, self.ca_key = create_ca("test-ca")
        write_pem(root / "ca.pem", self.ca_cert)

    def node(self, cn, ca=None, **kw):
        ca_cert, ca_key = ca or (self.ca_cert, self.ca_key)
        cert, key = issue_cert(ca_cert, ca_key, cn, **kw)
        write_pem(self.root / f"{cn}.pem", cert)
        write_pem(self.root / f"{cn}.key", key, secret=True) if not (self.root / f"{cn}.key").exists() else None
        if (self.root / f"{cn}.key").read_bytes() != key:
            (self.root / f"{cn}.key").unlink(); write_pem(self.root / f"{cn}.key", key, secret=True)
        return make_contexts(self.root / "ca.pem", self.root / f"{cn}.pem", self.root / f"{cn}.key")


class TlsTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.fleet = Fleet(Path(self._t.name))
        self.closers = []

    def tearDown(self):
        for c in self.closers:
            c.close()
        self._t.cleanup()

    def link(self, a_ctx, b_ctx, a_id="a", b_id="b", **kw_a):
        ta = TcpTransport(server_context=a_ctx[0], client_context=a_ctx[1], **kw_a)
        tb = TcpTransport(server_context=b_ctx[0], client_context=b_ctx[1])
        ta.add_peer(b_id, "127.0.0.1", tb.port); tb.add_peer(a_id, "127.0.0.1", ta.port)
        a, b = Node(a_id, ta, schema()), Node(b_id, tb, schema())
        self.closers += [a, b]
        b.on("ping", lambda e: {"n": e.payload["n"] + 1})
        a.on("ping", lambda e: {"n": e.payload["n"] + 100})
        return a, b, ta, tb

    def test_mutual_tls_request_response_both_directions(self):
        a, b, ta, tb = self.link(self.fleet.node("a"), self.fleet.node("b"))
        self.assertEqual(a.request("b", "ping", {"n": 1}, timeout=3).payload, {"n": 2})
        self.assertEqual(b.request("a", "ping", {"n": 1}, timeout=3).payload, {"n": 101})
        self.assertEqual(ta.stats["tls_handshake_failed"] + tb.stats["tls_handshake_failed"], 0)

    def test_contexts_are_tls13_and_require_client_certificates(self):
        import ssl
        s, c = self.fleet.node("a")
        self.assertEqual((s.minimum_version, c.minimum_version), (ssl.TLSVersion.TLSv1_3, ssl.TLSVersion.TLSv1_3))
        self.assertEqual((s.verify_mode, c.verify_mode), (ssl.CERT_REQUIRED, ssl.CERT_REQUIRED))

    def test_plaintext_client_is_refused_by_a_tls_server(self):
        a, b, ta, tb = self.link(self.fleet.node("a"), self.fleet.node("b"))
        ran = []
        b.on("ping", lambda e: ran.append(1) or {})
        raw = socket.create_connection(("127.0.0.1", tb.port))
        env = Envelope("ping", "a", "b", {"n": 1}, deadline=time.time() + 5)
        data = encode(env)
        raw.sendall(len(data).to_bytes(4, "big") + data)                # a perfectly valid SETU frame, but unencrypted
        time.sleep(0.4)
        raw.close()
        self.assertEqual(ran, [])
        self.assertGreaterEqual(tb.stats["tls_handshake_failed"], 1)

    def test_client_without_a_certificate_or_with_a_foreign_ca_is_rejected(self):
        other = Fleet(Path(tempfile.mkdtemp()))
        foreign = other.node("a")                                      # valid cert, but from a different CA
        a, b, ta, tb = self.link(foreign, self.fleet.node("b"))
        with self.assertRaises(SetuError):
            a.request("b", "ping", {"n": 1}, timeout=0.6)
        self.assertGreaterEqual(ta.stats["tls_handshake_failed"] + tb.stats["tls_handshake_failed"], 1)

    def test_server_certificate_for_the_wrong_node_is_not_accepted(self):
        # "b" is reachable at the address a expects, but its certificate says it is "evil"
        a, b, ta, tb = self.link(self.fleet.node("a"), self.fleet.node("evil"))
        with self.assertRaises(SetuError):
            a.request("b", "ping", {"n": 1}, timeout=0.6)
        self.assertEqual(ta.stats["tls_identity_mismatch"], 1)

    def test_expired_certificate_is_rejected(self):
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
        old = self.fleet.node("a", not_before=past - dt.timedelta(days=30), not_after=past)
        a, b, ta, tb = self.link(old, self.fleet.node("b"))
        with self.assertRaises(SetuError):
            a.request("b", "ping", {"n": 1}, timeout=0.6)

    def test_allowed_peers_list_is_enforced_on_the_server_side(self):
        sa, ca_ = self.fleet.node("a"), None
        ta = TcpTransport(server_context=sa[0], client_context=sa[1])
        sb = self.fleet.node("b")
        tb = TcpTransport(server_context=sb[0], client_context=sb[1], allowed_peers={"somebody-else"})
        ta.add_peer("b", "127.0.0.1", tb.port)
        a, b = Node("a", ta, schema()), Node("b", tb, schema())
        self.closers += [a, b]
        b.on("ping", lambda e: {})
        with self.assertRaises(SetuError):
            a.request("b", "ping", {"n": 1}, timeout=0.6)
        self.assertGreaterEqual(tb.stats["tls_peer_rejected"], 1)

    def test_tls_plus_signed_messages_end_to_end(self):
        reg = Registry()
        ids = {n: Identity.generate(n) for n in ("a", "b")}
        for n, role in (("a", "station"), ("b", "robot")):
            reg.enroll(n, ids[n].public_bytes(), role)
        pol = Policy({"station": {"report"}, "robot": {"report"}})
        ca_, cb_ = self.fleet.node("a"), self.fleet.node("b")
        ta = TcpTransport(server_context=ca_[0], client_context=ca_[1])
        tb = TcpTransport(server_context=cb_[0], client_context=cb_[1])
        ta.add_peer("b", "127.0.0.1", tb.port); tb.add_peer("a", "127.0.0.1", ta.port)
        a = Node("a", ta, schema(), sign=EnvelopeSigner(ids["a"]), verify_response=EnvelopeVerifier(reg, pol, {}).verify_response)
        b = Node("b", tb, schema(), sign=EnvelopeSigner(ids["b"]), authorize=EnvelopeVerifier(reg, pol, {"ping": "report"}))
        self.closers += [a, b]
        b.on("ping", lambda e: {"n": 7})
        self.assertEqual(a.request("b", "ping", {"n": 1}, timeout=3).payload, {"n": 7})

    def test_private_keys_are_written_0600_and_never_overwritten(self):
        p = Path(self._t.name) / "k.key"
        write_pem(p, b"secret", secret=True)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            write_pem(p, b"again", secret=True)

    def test_many_messages_over_one_tls_connection(self):
        a, b, ta, tb = self.link(self.fleet.node("a"), self.fleet.node("b"))
        for i in range(50):
            self.assertEqual(a.request("b", "ping", {"n": i}, timeout=3).payload, {"n": i + 1})


if __name__ == "__main__":
    unittest.main()
