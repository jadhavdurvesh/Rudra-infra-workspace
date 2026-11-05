"""KAVACH message authentication for SETU: every envelope carries an Ed25519 signature over its canonical bytes.

What it gives you: the receiver knows WHO sent a message (a registered, non-revoked device), that nothing in it
(type, payload, destination, ids, timestamps, deadline) was altered, that it is recent, and that it is not a replay.
What it does NOT give you: confidentiality (messages are signed, not encrypted; use TLS for that on TCP links).

Wire up:   Node(..., sign=EnvelopeSigner(identity), authorize=verifier, verify_response=verifier.verify_response)

Rules enforced (default deny):
 - signature valid for the registered key of `auth.kid`, and kid == envelope.source_id (no claiming to be someone else)
 - signer not revoked (revocation takes effect on the very next message)
 - a deadline is mandatory and short (max_validity_s), created_at and deadline within clock-skew bounds
 - (kid, message_id) seen before inside the window -> replay, rejected (cache is bounded)
 - authorization is separate: the signer's role must hold the capability mapped to the message type
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import asdict
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .core import Identity, Policy, Registry

CTX = b"firebot-kavach-envelope-v1|"


def canonical_bytes(env: Any) -> bytes:
    """Everything except the `auth` block itself, in a stable encoding."""
    d = asdict(env)
    d.pop("auth", None)
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()


class EnvelopeSigner:
    def __init__(self, identity: Identity) -> None:
        self.identity = identity

    def __call__(self, env: Any) -> None:
        env.auth = {"kid": self.identity.device_id, "sig": self.identity.sign(CTX + canonical_bytes(env)).hex()}


class EnvelopeVerifier:
    def __init__(self, registry: Registry, policy: Policy, capability_map: dict[str, str], *,
                 clock: Callable[[], float] = time.time, max_validity_s: float = 60.0, max_skew_s: float = 5.0,
                 replay_window: int = 10_000, require_deadline: bool = True,
                 on_event: Callable[..., Any] | None = None) -> None:
        self.registry, self.policy, self.caps = registry, policy, dict(capability_map)
        self._clock, self.max_validity, self.max_skew = clock, max_validity_s, max_skew_s
        self._window, self.require_deadline, self._on_event = replay_window, require_deadline, on_event
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()

    def _audit(self, reason: str, env: Any) -> None:
        if self._on_event:
            try:
                self._on_event("kavach", "message_rejected", "warning", reason=reason,
                               source=getattr(env, "source_id", "?"), message_type=getattr(env, "message_type", "?"))
            except Exception:
                pass

    def verify(self, env: Any) -> tuple[bool, str]:
        """Authenticity + freshness + replay only (no capability check). Returns (ok, reason)."""
        auth = getattr(env, "auth", None)
        if not isinstance(auth, dict) or not isinstance(auth.get("kid"), str) or not isinstance(auth.get("sig"), str):
            return False, "unsigned"
        kid = auth["kid"]
        if kid != env.source_id:
            return False, "source_mismatch"
        rec = self.registry.get(kid)
        if rec is None:
            return False, "unknown_signer"
        if rec.revoked:
            return False, "revoked"
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(rec.public_key_hex)).verify(
                bytes.fromhex(auth["sig"]), CTX + canonical_bytes(env))
        except (InvalidSignature, ValueError):
            return False, "bad_signature"
        now = self._clock()
        if not isinstance(env.created_at, (int, float)) or abs(now - env.created_at) > self.max_validity + self.max_skew:
            return False, "bad_timestamp"
        if env.deadline is None:
            if self.require_deadline:
                return False, "no_deadline"
        else:
            if env.deadline - env.created_at > self.max_validity:
                return False, "validity_too_long"
            if now > env.deadline + self.max_skew:
                return False, "expired"
        key = (kid, env.message_id)
        if key in self._seen:
            return False, "replay"
        self._seen[key] = None
        while len(self._seen) > self._window:
            self._seen.popitem(last=False)
        return True, "ok"

    def __call__(self, env: Any) -> bool:
        """Node(authorize=...): authenticated AND the signer's role may send this message type."""
        ok, reason = self.verify(env)
        if not ok:
            self._audit(reason, env)
            return False
        rec = self.registry.get(env.source_id)
        cap = self.caps.get(env.message_type)
        if not (rec and cap and self.policy.allowed(rec.role, cap)):
            self._audit("not_authorized", env)
            return False
        return True

    def verify_response(self, env: Any) -> bool:
        """Node(verify_response=...): replies must be signed by a known device too (no capability needed)."""
        ok, reason = self.verify(env)
        if not ok:
            self._audit("response_" + reason, env)
        return ok
