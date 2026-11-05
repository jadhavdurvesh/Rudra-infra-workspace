from __future__ import annotations

from typing import Any


class SchemaError(ValueError): ...


class SchemaRegistry:
    """message_type -> version -> {field: type}. Every interface has an explicit version."""

    def __init__(self) -> None:
        self._s: dict[str, dict[int, tuple[dict[str, type | tuple], frozenset[str]]]] = {}

    def register(self, message_type: str, version: int, fields: dict[str, type | tuple],
                 optional: tuple[str, ...] = ()) -> None:
        self._s.setdefault(message_type, {})[version] = (dict(fields), frozenset(optional))

    def has(self, message_type: str) -> bool:
        return message_type in self._s

    def validate(self, message_type: str, version: int, payload: dict[str, Any]) -> None:
        versions = self._s.get(message_type)
        if versions is None:
            raise SchemaError(f"unknown message type {message_type!r}")
        if version not in versions:
            raise SchemaError(f"unsupported payload version {version} for {message_type!r}")
        fields, optional = versions[version]
        for name, typ in fields.items():
            if name not in payload:
                if name in optional:
                    continue
                raise SchemaError(f"missing field {name!r}")
            v = payload[name]
            allowed = typ if isinstance(typ, tuple) else (typ,)
            if isinstance(v, bool) and bool not in allowed:
                raise SchemaError(f"field {name!r} must not be bool")
            if float in allowed and int not in allowed and isinstance(v, int) and not isinstance(v, bool):
                continue  # ints are acceptable where floats are
            if not isinstance(v, allowed):
                raise SchemaError(f"field {name!r} has wrong type")
        extra = set(payload) - set(fields)
        if extra:
            raise SchemaError(f"unexpected fields {sorted(extra)}")
