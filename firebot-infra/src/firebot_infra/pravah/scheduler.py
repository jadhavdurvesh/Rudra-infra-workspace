"""PRAVAH: explicit-state runtime for NONCRITICAL tasks (background jobs, test workflows).

Not allowed here: e-stop, motor control, suppression, or any safety-critical actuation.
Handler types that look like actuation are refused at registration. Cancellation and timeouts
are cooperative: a handler that ignores its CancelToken keeps its worker thread until it
returns, and keeps counting against max_workers and resource limits.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

FORBIDDEN_PREFIXES = ("estop", "e_stop", "emergency", "actuat", "suppress", "motor", "safety")


class State(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL = {State.SUCCEEDED, State.FAILED, State.CANCELLED, State.TIMED_OUT}


class TaskError(ValueError): ...


class CancelToken:
    def __init__(self) -> None:
        self._e = threading.Event()

    def cancel(self) -> None:
        self._e.set()

    @property
    def cancelled(self) -> bool:
        return self._e.is_set()

    def wait(self, timeout: float) -> bool:
        """Sleep up to timeout; returns True if cancelled."""
        return self._e.wait(timeout)


@dataclass
class Task:
    task_id: str
    type: str
    args: dict[str, Any] = field(default_factory=dict)
    prerequisites: list[str] = field(default_factory=list)
    deadline_s: float | None = None      # must START within this long after submission
    timeout_s: float | None = None       # max run time
    max_attempts: int = 1
    idempotent: bool = False             # only idempotent tasks may be retried or resumed after restart
    resource: str | None = None          # optional named resource limit
    state: State = State.PENDING
    attempts: int = 0
    result: Any = None
    error: str | None = None
    # runtime-only
    _submitted: float = 0.0
    _started: float = 0.0
    _run_id: int = 0
    _token: CancelToken | None = None


class Scheduler:
    def __init__(self, handlers: dict[str, Callable[[dict, CancelToken], Any]], max_workers: int = 2,
                 store_path: str | os.PathLike | None = None, on_event: Callable[..., Any] | None = None,
                 resource_limits: dict[str, int] | None = None, tick_s: float = 0.02,
                 clock: Callable[[], float] = time.monotonic) -> None:
        for name in handlers:
            if name.lower().startswith(FORBIDDEN_PREFIXES):
                raise ValueError(f"{name!r}: safety-critical actuation is outside PRAVAH's authority")
        self._handlers = dict(handlers)
        self._max_workers = max_workers
        self._limits = dict(resource_limits or {})
        self._on_event = on_event
        self._clock = clock
        self._tick = tick_s
        self._store = Path(store_path) if store_path else None
        self._tasks: dict[str, Task] = {}
        self._cond = threading.Condition(threading.RLock())
        self._active = 0
        self._res_in_use: dict[str, int] = {}
        self._next_run = 0
        self._stop = False
        if self._store and self._store.exists():
            self._load()
        self._thread = threading.Thread(target=self._loop, name="pravah", daemon=True)
        self._thread.start()

    # ---- public API ---------------------------------------------------
    def submit(self, *tasks: Task) -> None:
        with self._cond:
            ids = [t.task_id for t in tasks]
            if len(set(ids)) != len(ids) or any(i in self._tasks for i in ids):
                raise TaskError("duplicate task_id")
            new = {t.task_id: t for t in tasks}
            for t in tasks:
                if t.type not in self._handlers:
                    raise TaskError(f"no handler for type {t.type!r}")
                if t.max_attempts < 1:
                    raise TaskError("max_attempts must be >= 1")
                if t.max_attempts > 1 and not t.idempotent:
                    raise TaskError("side-effecting tasks are not retried without explicit idempotency (idempotent=True)")
                if t.resource is not None and t.resource not in self._limits:
                    raise TaskError(f"unknown resource {t.resource!r}")
                for p in t.prerequisites:
                    if p not in self._tasks and p not in new:
                        raise TaskError(f"unknown prerequisite {p!r} for {t.task_id!r}")
            self._check_cycles({**self._tasks, **new})
            now = self._clock()
            for t in tasks:
                t.state, t._submitted = State.PENDING, now
                self._tasks[t.task_id] = t
            self._save()
            self._cond.notify_all()

    def cancel(self, task_id: str) -> bool:
        with self._cond:
            t = self._tasks.get(task_id)
            if t is None or t.state in TERMINAL:
                return False
            if t._token:
                t._token.cancel()
            self._set(t, State.CANCELLED, "cancelled by request")
            self._cond.notify_all()
            return True

    def wait(self, timeout: float | None = None) -> bool:
        end = None if timeout is None else self._clock() + timeout
        with self._cond:
            while not all(t.state in TERMINAL for t in self._tasks.values()):
                left = None if end is None else end - self._clock()
                if left is not None and left <= 0:
                    return False
                self._cond.wait(self._tick if left is None else min(self._tick, left))
            return True

    def status(self, task_id: str) -> dict[str, Any]:
        with self._cond:
            t = self._tasks[task_id]
            return {"task_id": t.task_id, "state": t.state.value, "attempts": t.attempts,
                    "result": t.result, "error": t.error}

    def states(self) -> dict[str, str]:
        with self._cond:
            return {k: v.state.value for k, v in self._tasks.items()}

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            for t in self._tasks.values():
                if t._token:
                    t._token.cancel()
            self._cond.notify_all()
        self._thread.join(timeout=1)

    # ---- graph validation ----------------------------------------------
    @staticmethod
    def _check_cycles(tasks: dict[str, Task]) -> None:
        color: dict[str, int] = {}

        def visit(n: str) -> None:
            color[n] = 1
            for p in tasks[n].prerequisites:
                if color.get(p, 0) == 1:
                    raise TaskError(f"dependency cycle involving {p!r}")
                if color.get(p, 0) == 0:
                    visit(p)
            color[n] = 2

        for n in tasks:
            if color.get(n, 0) == 0:
                visit(n)

    # ---- scheduler loop -------------------------------------------------
    def _loop(self) -> None:
        with self._cond:
            while not self._stop:
                self._advance()
                self._cond.wait(self._tick)

    def _advance(self) -> None:
        now = self._clock()
        for t in list(self._tasks.values()):
            if t.state == State.PENDING:
                bad = next((p for p in t.prerequisites if self._tasks[p].state in
                            (State.FAILED, State.CANCELLED, State.TIMED_OUT)), None)
                if bad:
                    self._set(t, State.CANCELLED, f"prerequisite {bad} {self._tasks[bad].state.value}")
                elif all(self._tasks[p].state == State.SUCCEEDED for p in t.prerequisites):
                    self._set(t, State.READY)
            if t.state in (State.PENDING, State.READY) and t.deadline_s is not None \
                    and now - t._submitted > t.deadline_s:
                self._set(t, State.TIMED_OUT, "deadline expired before start")
            elif t.state == State.RUNNING and t.timeout_s is not None and now - t._started > t.timeout_s:
                if t._token:
                    t._token.cancel()
                if t.idempotent and t.attempts < t.max_attempts:
                    self._set(t, State.READY, "attempt timed out; will retry")
                else:
                    self._set(t, State.TIMED_OUT, "run time exceeded timeout_s")
        for t in list(self._tasks.values()):  # FIFO dispatch; resource-blocked tasks don't block others
            if self._active >= self._max_workers:
                break
            if t.state != State.READY:
                continue
            if t.resource and self._res_in_use.get(t.resource, 0) >= self._limits[t.resource]:
                continue
            self._start(t)

    def _start(self, t: Task) -> None:
        self._next_run += 1
        t._run_id, t._token, t._started = self._next_run, CancelToken(), self._clock()
        t.attempts += 1
        self._active += 1
        if t.resource:
            self._res_in_use[t.resource] = self._res_in_use.get(t.resource, 0) + 1
        self._set(t, State.RUNNING)
        threading.Thread(target=self._worker, args=(t, t._run_id, t._token), daemon=True).start()

    def _worker(self, t: Task, run_id: int, token: CancelToken) -> None:
        ok, res, err = True, None, None
        try:
            res = self._handlers[t.type](dict(t.args), token)
        except Exception as e:
            ok, err = False, f"{type(e).__name__}: {e}"
        with self._cond:
            self._active -= 1
            if t.resource:
                self._res_in_use[t.resource] -= 1
            if t._run_id == run_id and t.state == State.RUNNING:  # else: timed out / cancelled; result ignored
                if ok:
                    t.result = self._jsonable(res)
                    self._set(t, State.SUCCEEDED)
                elif t.idempotent and t.attempts < t.max_attempts:
                    self._set(t, State.READY, err)
                else:
                    self._set(t, State.FAILED, err)
            self._cond.notify_all()

    # ---- state + persistence ------------------------------------------
    def _set(self, t: Task, new: State, error: str | None = None) -> None:
        old = t.state
        t.state = new
        if error is not None:
            t.error = error
        if self._on_event:
            try:
                extra: dict[str, Any] = {}
                if new in TERMINAL and t._run_id:        # it actually ran: report how long and whether it worked
                    extra = {"duration_ms": round((self._clock() - t._started) * 1000, 3), "success": new == State.SUCCEEDED}
                self._on_event("pravah", "transition", "info" if new != State.FAILED else "warning",
                               task_id=t.task_id, type=t.type, old=old.value, new=new.value, attempts=t.attempts, **extra)
            except Exception:
                pass
        self._save()
        self._cond.notify_all()

    @staticmethod
    def _jsonable(x: Any) -> Any:
        try:
            json.dumps(x)
            return x
        except (TypeError, ValueError):
            return repr(x)

    def _save(self) -> None:
        if not self._store:
            return
        recs = []
        for t in self._tasks.values():
            d = {"task_id": t.task_id, "type": t.type, "args": t.args, "prerequisites": t.prerequisites,
                 "deadline_s": t.deadline_s, "timeout_s": t.timeout_s, "max_attempts": t.max_attempts,
                 "idempotent": t.idempotent, "resource": t.resource, "state": t.state.value,
                 "attempts": t.attempts, "result": t.result, "error": t.error}
            recs.append(d)
        tmp = self._store.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(recs))
            os.replace(tmp, self._store)
        except (OSError, TypeError, ValueError):
            pass  # persistence failure degrades recovery, never the running tasks

    def _load(self) -> None:
        try:
            recs = json.loads(self._store.read_text())  # type: ignore[union-attr]
        except (OSError, ValueError):
            return
        now = self._clock()
        for d in recs:
            try:
                d["state"] = State(d["state"])
                t = Task(**d)
            except (TypeError, ValueError, KeyError):
                continue
            t._submitted = now  # deadlines restart from process start
            if t.type not in self._handlers and t.state not in TERMINAL:
                t.state, t.error = State.FAILED, "no handler after restart"
            elif t.state == State.RUNNING:
                if t.idempotent:  # an interrupted attempt is not a task failure: don't count it
                    t.attempts = max(0, t.attempts - 1)
                    t.state, t.error = State.READY, "interrupted by restart; re-running (idempotent)"
                else:
                    t.state, t.error = State.FAILED, "interrupted by restart; not safe to re-run"
            self._tasks[t.task_id] = t
