"""KAVACH: device identity, authentication, authorization, signed artifacts.

Uses Ed25519 from the reviewed `cryptography` library. No custom cryptography.
- Registry stores PUBLIC keys only. Private keys live in a 0600 key file (optionally passphrase-encrypted);
  how well they are protected at rest depends on the host (no hardware key store is assumed).
- Authentication (who are you) is separate from authorization (what may you do).
- Challenge-response: single-use, expiring nonces; signatures are bound to the verifier's id.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

AUTH_CTX = b"firebot-kavach-auth-v1|"
MANIFEST_CTX = b"firebot-kavach-manifest-v1|"


class AuthError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ManifestError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InsecureKeyFile(Exception): ...


# ---------------------------------------------------------------- identity
class Identity:
    def __init__(self, device_id: str, private_key: Ed25519PrivateKey) -> None:
        self.device_id = device_id
        self._k = private_key

    @classmethod
    def generate(cls, device_id: str) -> "Identity":
        return cls(device_id, Ed25519PrivateKey.generate())

    def public_bytes(self) -> bytes:
        return self._k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(self, msg: bytes) -> bytes:
        return self._k.sign(msg)

    def prove(self, nonce: bytes, audience_id: str) -> bytes:
        """Answer a challenge. The signature is bound to this device and to the verifier."""
        return self.sign(AUTH_CTX + self.device_id.encode() + b"|" + audience_id.encode() + b"|" + nonce)

    def save(self, path: str | os.PathLike, passphrase: bytes | None = None) -> None:
        enc = serialization.BestAvailableEncryption(passphrase) if passphrase else serialization.NoEncryption()
        pem = self._k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never overwrite, never world-readable
        with os.fdopen(fd, "wb") as f:
            f.write(pem)

    @classmethod
    def load(cls, device_id: str, path: str | os.PathLike, passphrase: bytes | None = None) -> "Identity":
        if os.name == "posix" and os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise InsecureKeyFile(f"{path} is accessible by group/others; chmod 600")
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=passphrase)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("not an Ed25519 key")
        return cls(device_id, key)


# ---------------------------------------------------------------- registry + policy
@dataclass
class DeviceRecord:
    device_id: str
    public_key_hex: str
    role: str
    revoked: bool = False
    key_version: int = 1
    enrolled_at: float = field(default_factory=time.time)


class Registry:
    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path else None
        self._d: dict[str, DeviceRecord] = {}
        self._lock = threading.Lock()
        if self.path and self.path.exists():
            for rec in json.loads(self.path.read_text())["devices"]:
                self._d[rec["device_id"]] = DeviceRecord(**rec)

    @staticmethod
    def _check_pub(pub: bytes) -> None:
        Ed25519PublicKey.from_public_bytes(pub)  # raises ValueError if malformed

    def enroll(self, device_id: str, public_key: bytes, role: str) -> DeviceRecord:
        self._check_pub(public_key)
        with self._lock:
            if device_id in self._d:
                raise ValueError("device already enrolled (use rotate_key)")
            rec = DeviceRecord(device_id, public_key.hex(), role)
            self._d[device_id] = rec
            self._save()
            return rec

    def revoke(self, device_id: str) -> None:
        with self._lock:
            self._d[device_id].revoked = True
            self._save()

    def rotate_key(self, device_id: str, new_public_key: bytes) -> DeviceRecord:
        self._check_pub(new_public_key)
        with self._lock:
            rec = self._d[device_id]
            rec.public_key_hex, rec.key_version = new_public_key.hex(), rec.key_version + 1
            self._save()
            return rec

    def get(self, device_id: str) -> DeviceRecord | None:
        return self._d.get(device_id)

    def _save(self) -> None:
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"devices": [r.__dict__ for r in self._d.values()]}, indent=2))
        os.replace(tmp, self.path)


class Policy:
    """role -> capabilities. Default deny."""

    def __init__(self, roles: dict[str, set[str]]) -> None:
        self.roles = {r: set(c) for r, c in roles.items()}

    def allowed(self, role: str, capability: str) -> bool:
        return capability in self.roles.get(role, ())


# ---------------------------------------------------------------- authentication
@dataclass(frozen=True)
class Peer:
    device_id: str
    role: str
    key_version: int


class Verifier:
    def __init__(self, self_id: str, registry: Registry, policy: Policy | None = None, nonce_ttl_s: float = 10.0,
                 clock: Callable[[], float] = time.time, on_event: Callable[..., Any] | None = None,
                 max_outstanding: int = 1024) -> None:
        self.self_id, self.registry, self.policy = self_id, registry, policy
        self._ttl, self._clock, self._on_event, self._max = nonce_ttl_s, clock, on_event, max_outstanding
        self._nonces: dict[bytes, float] = {}
        self._lock = threading.Lock()

    def _audit(self, event_type: str, severity: str, **kw: Any) -> None:
        if self._on_event:
            try:
                self._on_event("kavach", event_type, severity, verifier=self.self_id, **kw)
            except Exception:
                pass

    def challenge(self) -> bytes:
        now = self._clock()
        with self._lock:
            for n in [n for n, exp in self._nonces.items() if exp < now]:
                del self._nonces[n]
            if len(self._nonces) >= self._max:
                raise AuthError("too_many_outstanding_challenges")
            nonce = secrets.token_bytes(16)
            self._nonces[nonce] = now + self._ttl
            return nonce

    def verify(self, device_id: str, nonce: bytes, signature: bytes) -> Peer:
        def fail(reason: str) -> AuthError:
            self._audit("auth_rejected", "warning", device_id=device_id, reason=reason)
            return AuthError(reason)

        with self._lock:
            exp = self._nonces.pop(nonce, None)       # single use, even when verification fails
        if exp is None:
            raise fail("unknown_nonce")
        if self._clock() > exp:
            raise fail("nonce_expired")
        rec = self.registry.get(device_id)
        if rec is None:
            raise fail("unknown_device")
        if rec.revoked:
            raise fail("revoked")
        msg = AUTH_CTX + device_id.encode() + b"|" + self.self_id.encode() + b"|" + nonce
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(rec.public_key_hex)).verify(signature, msg)
        except (InvalidSignature, ValueError):
            raise fail("bad_signature")
        self._audit("auth_ok", "info", device_id=device_id, role=rec.role)
        return Peer(device_id, rec.role, rec.key_version)

    def authorize(self, peer: Peer, capability: str) -> bool:
        rec = self.registry.get(peer.device_id)
        ok = bool(self.policy and rec and not rec.revoked and self.policy.allowed(rec.role, capability))
        if not ok:
            self._audit("authz_denied", "warning", device_id=peer.device_id, capability=capability)
        return ok


def mutual_authenticate(a: Identity, a_verifier: Verifier, b: Identity, b_verifier: Verifier) -> tuple[Peer, Peer]:
    """Both directions. Returns (A as seen by B, B as seen by A). Raises AuthError if either side fails."""
    n_b = b_verifier.challenge()                      # B challenges A
    a_seen_by_b = b_verifier.verify(a.device_id, n_b, a.prove(n_b, b_verifier.self_id))
    n_a = a_verifier.challenge()                      # A challenges B
    b_seen_by_a = a_verifier.verify(b.device_id, n_a, b.prove(n_a, a_verifier.self_id))
    return a_seen_by_b, b_seen_by_a


class SessionAuthorizer:
    """Glue for SETU: Node(authorize=SessionAuthorizer(...)). Default deny; checks the live registry each call."""

    def __init__(self, registry: Registry, policy: Policy, capability_map: dict[str, str],
                 session_ttl_s: float = 3600.0, clock: Callable[[], float] = time.time) -> None:
        self.registry, self.policy, self.caps = registry, policy, dict(capability_map)
        self._ttl, self._clock = session_ttl_s, clock
        self._sessions: dict[str, float] = {}

    def grant(self, peer: Peer) -> None:
        self._sessions[peer.device_id] = self._clock() + self._ttl

    def end(self, device_id: str) -> None:
        self._sessions.pop(device_id, None)

    def __call__(self, env: Any) -> bool:
        exp = self._sessions.get(env.source_id)
        if exp is None or self._clock() > exp:
            return False
        rec = self.registry.get(env.source_id)
        cap = self.caps.get(env.message_type)
        return bool(rec and not rec.revoked and cap and self.policy.allowed(rec.role, cap))


# ---------------------------------------------------------------- signed artifacts
def _digest(x: bytes | str | os.PathLike) -> str:
    data = x if isinstance(x, bytes) else Path(x).read_bytes()
    return hashlib.sha256(data).hexdigest()


def sign_manifest(identity: Identity, name: str, version: int,
                  files: dict[str, bytes | str | os.PathLike]) -> dict[str, Any]:
    body = {"name": name, "version": version, "signer": identity.device_id,
            "files": {k: _digest(v) for k, v in sorted(files.items())}}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return {"body": body, "signature": identity.sign(MANIFEST_CTX + canon).hex()}


def verify_manifest(manifest: dict[str, Any], registry: Registry, policy: Policy,
                    files: dict[str, bytes | str | os.PathLike], min_version: int = 0,
                    capability: str = "sign_artifacts") -> dict[str, Any]:
    try:
        body, sig = manifest["body"], bytes.fromhex(manifest["signature"])
        signer = body["signer"]
    except (KeyError, TypeError, ValueError):
        raise ManifestError("malformed")
    rec = registry.get(signer)
    if rec is None:
        raise ManifestError("unknown_signer")
    if rec.revoked:
        raise ManifestError("revoked_signer")
    if not policy.allowed(rec.role, capability):
        raise ManifestError("signer_not_authorized")
    canon = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(rec.public_key_hex)).verify(sig, MANIFEST_CTX + canon)
    except (InvalidSignature, ValueError):
        raise ManifestError("bad_signature")
    if not isinstance(body.get("version"), int) or body["version"] < min_version:
        raise ManifestError("version_rollback")
    if set(files) != set(body["files"]):
        raise ManifestError("file_set_mismatch")
    for name, src in files.items():
        if _digest(src) != body["files"][name]:
            raise ManifestError(f"hash_mismatch:{name}")
    return body


def scan_for_private_keys(root: str | os.PathLike) -> list[str]:
    """Pre-commit style check: files under root that contain PEM private key material."""
    hits = []
    for p in Path(root).rglob("*"):
        if p.is_file() and p.stat().st_size < 1_000_000 and ".git" not in p.parts:
            try:
                if b"PRIVATE KEY-----" in p.read_bytes():
                    hits.append(str(p))
            except OSError:
                pass
    return hits
