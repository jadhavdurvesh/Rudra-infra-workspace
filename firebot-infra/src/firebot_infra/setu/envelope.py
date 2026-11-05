from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
KINDS = ("event", "request", "response", "error")


class MalformedMessage(ValueError): ...
class UnsupportedVersion(ValueError): ...
class PayloadTooLarge(ValueError): ...


@dataclass
class Envelope:
    message_type: str
    source_id: str
    destination_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    kind: str = "event"
    payload_schema_version: int = 1
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    correlation_id: str | None = None
    created_at: float = field(default_factory=time.time)
    deadline: float | None = None          # absolute wall-clock epoch seconds; expired => rejected
    idempotency_key: str | None = None
    protocol_version: int = PROTOCOL_VERSION
    auth: dict[str, Any] | None = None     # optional authentication metadata (KAVACH)


_REQUIRED = {"message_type": str, "source_id": str, "destination_id": str, "payload": dict,
             "kind": str, "message_id": str, "protocol_version": int, "payload_schema_version": int}


def encode(env: Envelope, max_bytes: int = 65_536) -> bytes:
    data = json.dumps(asdict(env), separators=(",", ":"), sort_keys=True).encode()
    if len(data) > max_bytes:
        raise PayloadTooLarge(f"{len(data)} > {max_bytes}")
    return data


def decode(data: bytes, max_bytes: int = 65_536) -> Envelope:
    if len(data) > max_bytes:
        raise PayloadTooLarge(f"{len(data)} > {max_bytes}")
    try:
        d = json.loads(data)
    except ValueError as e:
        raise MalformedMessage(str(e)) from e
    if not isinstance(d, dict):
        raise MalformedMessage("not an object")
    for k, t in _REQUIRED.items():
        if not isinstance(d.get(k), t) or isinstance(d.get(k), bool):
            raise MalformedMessage(f"bad field {k}")
    if d["protocol_version"] != PROTOCOL_VERSION:
        raise UnsupportedVersion(str(d["protocol_version"]))
    if d["kind"] not in KINDS:
        raise MalformedMessage("bad kind")
    try:
        return Envelope(**d)
    except TypeError as e:
        raise MalformedMessage(str(e)) from e
