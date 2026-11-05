"""SETU node: versioned, validated, deadline-aware messaging with explicit delivery semantics.

Delivery: best-effort transport. Requests may be retried ONLY if declared safe
(safe_to_retry or an idempotency_key). Receivers dedupe by message_id and cache
responses by idempotency key, so retries behave as at-least-once + idempotent consumer.
Nothing here claims exactly-once delivery.
"""
from __future__ import annotations

import queue
import threading
import uuid
import time
from collections import Counter, OrderedDict
from typing import Any, Callable

from .envelope import (Envelope, MalformedMessage, PayloadTooLarge, UnsupportedVersion, decode, encode)
from .schema import SchemaError, SchemaRegistry
from .transport import Transport


class SetuError(Exception): ...
class SetuTimeout(SetuError): ...
class SetuUnavailable(SetuError): ...
class SetuRemoteError(SetuError): ...


class Node:
    def __init__(self, node_id: str, transport: Transport, registry: SchemaRegistry | None = None, *,
                 max_payload: int = 65_536, queue_size: int = 1000, dedup_window: int = 4096,
                 clock: Callable[[], float] = time.time,
                 authorize: Callable[[Envelope], bool] | None = None,
                 sign: Callable[[Envelope], None] | None = None,
                 verify_response: Callable[[Envelope], bool] | None = None,
                 trace_requests: bool = False,
                 default_deadline_s: float | None = None,
                 on_event: Callable[..., Any] | None = None) -> None:
        self.id = node_id
        self._t = transport
        self.registry = registry or SchemaRegistry()
        self._max_payload = max_payload
        self._window = dedup_window
        self._clock = clock
        self._authorize = authorize
        self._sign = sign                      # e.g. kavach.EnvelopeSigner: stamps env.auth on every outgoing message
        # signed deployments require a deadline on every message, so default one on when signing
        self._default_deadline = default_deadline_s if default_deadline_s is not None else (30.0 if sign else None)
        self._verify_response = verify_response  # e.g. EnvelopeVerifier.verify_response: authenticates replies
        self._trace_requests = trace_requests    # emit a setu/request_done event (latency + outcome) for every request
        self._on_event = on_event  # Recorder.emit-compatible; observability path only
        self._handlers: dict[str, Callable[[Envelope], dict | None]] = {}
        self._inbox: queue.Queue[bytes] = queue.Queue(maxsize=queue_size)
        self._pending: dict[str, tuple[threading.Event, list[Envelope]]] = {}
        self._plock = threading.Lock()
        self._seen: OrderedDict[str, bytes | None] = OrderedDict()
        self._idem: OrderedDict[str, bytes] = OrderedDict()
        self._slock = threading.Lock()
        self.metrics: Counter[str] = Counter()
        self._stop = threading.Event()
        transport.set_receiver(self._on_bytes)
        self._worker = threading.Thread(target=self._loop, name=f"setu-{node_id}", daemon=True)
        self._worker.start()

    # ---- public API -------------------------------------------------
    def on(self, message_type: str, handler: Callable[[Envelope], dict | None]) -> None:
        if not self.registry.has(message_type):
            raise SchemaError(f"register a schema for {message_type!r} before adding a handler")
        self._handlers[message_type] = handler

    def send_event(self, dest: str, message_type: str, payload: dict, *, version: int = 1,
                   deadline_s: float | None = None) -> bool:
        self.registry.validate(message_type, version, payload)  # producer validates
        deadline_s = self._default_deadline if deadline_s is None else deadline_s
        env = Envelope(message_type, self.id, dest, payload, kind="event", payload_schema_version=version,
                       deadline=None if deadline_s is None else self._clock() + deadline_s)
        ok = self._t.send(dest, self._enc(env))
        self.metrics["sent" if ok else "send_failures"] += 1
        return ok

    def request(self, dest: str, message_type: str, payload: dict, **kw: Any) -> Envelope:
        if not self._trace_requests:
            return self._request(dest, message_type, payload, **kw)
        t0 = time.monotonic()
        try:
            resp = self._request(dest, message_type, payload, **kw)
        except SetuError as e:
            self._note("request_done", "warning", message_type=message_type, dest=dest, success=False,
                       error=type(e).__name__, duration_ms=round((time.monotonic() - t0) * 1000, 3))
            raise
        self._note("request_done", "info", message_type=message_type, dest=dest, success=True,
                   duration_ms=round((time.monotonic() - t0) * 1000, 3))
        return resp

    def _request(self, dest: str, message_type: str, payload: dict, *, version: int = 1,
                 timeout: float = 1.0, retries: int = 0, safe_to_retry: bool = False,
                 idempotency_key: str | None = None) -> Envelope:
        self.registry.validate(message_type, version, payload)
        retry_ok = safe_to_retry or idempotency_key is not None
        attempts = 1 + (retries if retry_ok else 0)
        last: SetuError = SetuTimeout(f"{message_type} to {dest} timed out")
        for _ in range(attempts):
            env = Envelope(message_type, self.id, dest, payload, kind="request",
                           payload_schema_version=version, deadline=self._clock() + timeout,
                           idempotency_key=idempotency_key)
            ev: threading.Event = threading.Event()
            slot: list[Envelope] = []
            with self._plock:
                self._pending[env.message_id] = (ev, slot)
            try:
                if not self._t.send(dest, self._enc(env)):
                    self.metrics["send_failures"] += 1
                    last = SetuUnavailable(f"cannot reach {dest}")
                    continue
                self.metrics["sent"] += 1
                if ev.wait(timeout):
                    resp = slot[0]
                    if resp.kind == "error":
                        raise SetuRemoteError(str(resp.payload.get("error")))
                    return resp
                self.metrics["timeouts"] += 1
                last = SetuTimeout(f"{message_type} to {dest} timed out")
            finally:
                with self._plock:
                    self._pending.pop(env.message_id, None)
        raise last

    def close(self) -> None:
        self._stop.set()
        self._worker.join(timeout=1)
        self._t.close()

    # ---- receive path -----------------------------------------------
    def _enc(self, env: Envelope) -> bytes:
        if self._sign:
            self._sign(env)
        return encode(env, self._max_payload)

    def _note(self, event_type: str, severity: str = "warning", **kw: Any) -> None:
        if self._on_event:
            try:
                self._on_event("setu", event_type, severity, node=self.id, **kw)
            except Exception:
                pass  # observability must never affect messaging

    def _on_bytes(self, data: bytes) -> None:
        try:
            self._inbox.put_nowait(data)
        except queue.Full:
            self.metrics["dropped_backpressure"] += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._handle(data)
            except Exception:
                self.metrics["internal_errors"] += 1

    def _reply(self, req: Envelope, kind: str, payload: dict, cache_key: str | None = None) -> None:
        resp = Envelope(req.message_type, self.id, req.source_id, payload, kind=kind,
                        payload_schema_version=req.payload_schema_version, correlation_id=req.message_id,
                        deadline=req.deadline)           # a reply is only useful until the requester gives up
        data = self._enc(resp)
        with self._slock:
            self._seen[req.message_id] = data
            if cache_key:
                self._idem[cache_key] = data
                while len(self._idem) > self._window:
                    self._idem.popitem(last=False)
        self._t.send(req.source_id, data)

    def _handle(self, data: bytes) -> None:
        try:
            env = decode(data, self._max_payload)
        except UnsupportedVersion:
            self.metrics["unsupported_version"] += 1
            self._note("unsupported_version")
            return
        except (MalformedMessage, PayloadTooLarge):
            self.metrics["malformed"] += 1
            self._note("malformed")
            return
        if env.destination_id != self.id:
            self.metrics["misrouted"] += 1
            return
        if env.kind in ("response", "error"):
            if self._verify_response and not self._verify_response(env):
                self.metrics["bad_response_auth"] += 1
                self._note("bad_response_auth", source=env.source_id)
                return
            with self._plock:
                p = self._pending.get(env.correlation_id or "")
            if p is None:
                self.metrics["unmatched_response"] += 1
            else:
                p[1].append(env)
                p[0].set()
            return
        if env.deadline is not None and self._clock() > env.deadline:
            self.metrics["expired"] += 1
            self._note("expired", message_type=env.message_type)
            if env.kind == "request":
                self._reply(env, "error", {"error": "deadline_expired"})
            return
        with self._slock:
            dup = env.message_id in self._seen
            cached = self._seen.get(env.message_id)
            if not dup:
                self._seen[env.message_id] = None
                while len(self._seen) > self._window:
                    self._seen.popitem(last=False)
        if dup:
            self.metrics["duplicates"] += 1
            if cached:
                self._t.send(env.source_id, cached)  # original response may have been lost
            return
        handler = self._handlers.get(env.message_type)
        if handler is None:
            self.metrics["unknown_type"] += 1
            if env.kind == "request":
                self._reply(env, "error", {"error": "unknown_type"})
            return
        try:
            self.registry.validate(env.message_type, env.payload_schema_version, env.payload)
        except SchemaError as e:
            self.metrics["schema_errors"] += 1
            self._note("schema_error", message_type=env.message_type)
            if env.kind == "request":
                self._reply(env, "error", {"error": f"schema: {e}"})
            return
        if self._authorize and not self._authorize(env):
            self.metrics["forbidden"] += 1
            self._note("forbidden", message_type=env.message_type, source=env.source_id)
            if env.kind == "request":
                self._reply(env, "error", {"error": "forbidden"})
            return
        if env.kind == "request" and env.idempotency_key:
            with self._slock:
                cached_resp = self._idem.get(env.idempotency_key)
            if cached_resp is not None:
                self.metrics["idempotent_replays"] += 1
                replay = decode(cached_resp, self._max_payload)
                replay.correlation_id = env.message_id  # answer the retry, not the original request
                replay.message_id = uuid.uuid4().hex     # a fresh message of its own (so signatures/replay caches stay sound)
                replay.created_at, replay.deadline = time.time(), env.deadline
                data = self._enc(replay)
                with self._slock:
                    self._seen[env.message_id] = data
                self._t.send(env.source_id, data)
                return
        try:
            result = handler(env)
        except Exception as e:
            self.metrics["handler_errors"] += 1
            self._note("handler_error", severity="error", message_type=env.message_type)
            if env.kind == "request":
                self._reply(env, "error", {"error": f"handler_error: {type(e).__name__}"})
            return
        if env.kind == "request":
            self._reply(env, "response", result or {}, cache_key=env.idempotency_key)
