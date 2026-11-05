"""Shared event envelope used by AKSHARA now, and ANANTA/SETU later."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1
SEVERITIES = ("debug", "info", "warning", "error", "critical")


@dataclass(frozen=True)
class Event:
    run_id: str
    subsystem: str
    event_type: str
    severity: str = "info"
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    event_time: float = field(default_factory=time.time)      # wall clock, human/cross-device use
    mono_ns: int = field(default_factory=time.monotonic_ns)   # monotonic, durations/ordering on one host
    seq: int = 0                                              # assigned by the recorder
    schema_version: int = SCHEMA_VERSION
    software_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        if d.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {d.get('schema_version')!r}")
        return cls(**d)
