"""KSHITI: a deterministic microkernel SIMULATOR for research and experiments.

This is NOT an operating system: it does not boot, touch hardware, or replace the host OS (the dossier says a
real kernel is a separate research project). It models what a small kernel does, so scheduling, IPC and
isolation ideas can be tested deterministically: priority round-robin scheduling (1 tick quantum), message
passing with bounded mailboxes, per-process memory quotas, a CPU watchdog, fault isolation and deadlock detection.

A process is a generator that yields syscalls and receives the result:   ok = yield Alloc(100)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Generator


@dataclass(frozen=True)
class Compute: n: int
@dataclass(frozen=True)
class Sleep: n: int
@dataclass(frozen=True)
class Yield: pass
@dataclass(frozen=True)
class Send: pid: int; msg: Any; block: bool = True
@dataclass(frozen=True)
class Recv: pass
@dataclass(frozen=True)
class Alloc: n: int
@dataclass(frozen=True)
class Free: n: int
@dataclass(frozen=True)
class Spawn: fn: Callable[[], Generator]; name: str = "child"; priority: int = 1; mem_quota: int | None = None; cpu_budget: int | None = None
@dataclass(frozen=True)
class Kill: pid: int
@dataclass(frozen=True)
class GetPid: pass
@dataclass(frozen=True)
class Exit: code: int = 0


class _Proc:
    def __init__(self, pid: int, name: str, gen: Generator, priority: int, quota: int, budget: int | None) -> None:
        self.pid, self.name, self.gen, self.priority = pid, name, gen, priority
        self.state = "ready"            # ready | sleeping | recv_blocked | send_blocked | zombie
        self.quota, self.budget = quota, budget
        self.mem = self.cpu = self.compute_left = self.wake = 0
        self.mailbox: deque[Any] = deque()
        self.value: Any = None
        self.pending: tuple[int, Any] | None = None
        self.exit_code: int | None = None
        self.reason = ""


class Kernel:
    def __init__(self, mailbox_size: int = 8, default_quota: int = 4096, max_trace: int = 10_000) -> None:
        self.tick = 0
        self.idle_ticks = 0
        self.mailbox_size, self.default_quota = mailbox_size, default_quota
        self._procs: dict[int, _Proc] = {}
        self._rr: dict[int, deque[int]] = {}
        self._next_pid = 1
        self.trace: list[tuple[int, int, str]] = []
        self._max_trace = max_trace

    # ---- management ----------------------------------------------------------
    def spawn(self, fn: Callable[[], Generator], name: str = "proc", priority: int = 1,
              mem_quota: int | None = None, cpu_budget: int | None = None) -> int:
        pid = self._next_pid
        self._next_pid += 1
        self._procs[pid] = _Proc(pid, name, fn(), priority, self.default_quota if mem_quota is None else mem_quota, cpu_budget)
        self._ready(pid)
        self._log(pid, "spawn")
        return pid

    def _log(self, pid: int, ev: str) -> None:
        if len(self.trace) < self._max_trace:
            self.trace.append((self.tick, pid, ev))

    def _ready(self, pid: int) -> None:
        p = self._procs[pid]
        p.state = "ready"
        self._rr.setdefault(p.priority, deque()).append(pid)

    def _finish(self, p: _Proc, code: int, reason: str) -> None:
        if p.state == "zombie":
            return
        p.state, p.exit_code, p.reason = "zombie", code, reason
        for q in self._rr.values():
            if p.pid in q:
                q.remove(p.pid)
        for o in self._procs.values():                   # unblock senders waiting on this process
            if o.state == "send_blocked" and o.pending and o.pending[0] == p.pid:
                o.pending, o.value = None, False
                self._ready(o.pid)
        self._log(p.pid, f"exit:{reason}")

    def ps(self) -> list[dict[str, Any]]:
        return [{"pid": p.pid, "name": p.name, "state": p.state, "cpu": p.cpu, "mem": p.mem,
                 "exit_code": p.exit_code, "reason": p.reason} for p in self._procs.values()]

    def proc(self, pid: int) -> dict[str, Any]:
        return next(d for d in self.ps() if d["pid"] == pid)

    # ---- scheduler -----------------------------------------------------------------
    def _pick(self) -> int | None:
        for prio in sorted(self._rr, reverse=True):
            if self._rr[prio]:
                return self._rr[prio].popleft()
        return None

    def step(self) -> str | None:
        """One tick. Returns a stop reason ('all_exited' | 'deadlock') or None."""
        live = [p for p in self._procs.values() if p.state != "zombie"]
        if not live:
            return "all_exited"
        self.tick += 1
        for p in live:
            if p.state == "sleeping" and p.wake <= self.tick:
                self._ready(p.pid)
            elif p.state == "send_blocked" and p.pending:
                self._try_deliver(p)
        pid = self._pick()
        if pid is None:
            self.idle_ticks += 1
            if not any(p.state in ("sleeping", "ready") for p in self._procs.values()):
                return "deadlock"
            return None
        self._run(self._procs[pid])
        return None

    def run(self, max_ticks: int = 100_000) -> str:
        for _ in range(max_ticks):
            reason = self.step()
            if reason:
                return reason
        return "max_ticks"

    def _run(self, p: _Proc) -> None:
        p.cpu += 1
        if p.budget is not None and p.cpu > p.budget:
            self._finish(p, -1, "watchdog")
            return
        if p.compute_left > 0:
            p.compute_left -= 1
            self._ready(p.pid)
            return
        try:
            req = p.gen.send(p.value)
            p.value = None
        except StopIteration:
            self._finish(p, 0, "exit")
            return
        except Exception as e:                           # fault isolation: a crashing process can't stop the kernel
            self._finish(p, -2, f"crashed: {type(e).__name__}: {e}")
            return
        self._syscall(p, req)

    def _syscall(self, p: _Proc, req: Any) -> None:
        if isinstance(req, Compute):
            p.compute_left = max(0, req.n - 1)
            self._ready(p.pid)
        elif isinstance(req, Yield):
            self._ready(p.pid)
        elif isinstance(req, Sleep):
            p.state, p.wake = "sleeping", self.tick + max(1, req.n)
        elif isinstance(req, Send):
            self._send(p, req)
        elif isinstance(req, Recv):
            if p.mailbox:
                p.value = p.mailbox.popleft()
                self._ready(p.pid)
            else:
                p.state = "recv_blocked"
        elif isinstance(req, Alloc):
            ok = req.n >= 0 and p.mem + req.n <= p.quota
            if ok:
                p.mem += req.n
            else:
                self._log(p.pid, "alloc_denied")
            p.value = ok
            self._ready(p.pid)
        elif isinstance(req, Free):
            p.mem = max(0, p.mem - max(0, req.n))
            self._ready(p.pid)
        elif isinstance(req, Spawn):
            p.value = self.spawn(req.fn, req.name, req.priority, req.mem_quota, req.cpu_budget)
            self._ready(p.pid)
        elif isinstance(req, Kill):
            t = self._procs.get(req.pid)
            p.value = bool(t and t.state != "zombie" and t is not p)
            if p.value:
                self._finish(t, -3, "killed")            # type: ignore[arg-type]
            self._ready(p.pid)
        elif isinstance(req, GetPid):
            p.value = p.pid
            self._ready(p.pid)
        elif isinstance(req, Exit):
            self._finish(p, req.code, "exit")
        else:
            self._finish(p, -4, f"bad syscall {req!r}")

    # ---- IPC -------------------------------------------------------------------------
    def _deliver(self, target: _Proc, msg: Any) -> bool:
        if target.state == "recv_blocked":
            target.value = msg
            self._ready(target.pid)
            return True
        if len(target.mailbox) < self.mailbox_size:
            target.mailbox.append(msg)
            return True
        return False

    def _send(self, p: _Proc, req: Send) -> None:
        t = self._procs.get(req.pid)
        if t is None or t.state == "zombie":
            p.value = False
            self._ready(p.pid)
        elif self._deliver(t, req.msg):
            p.value = True
            self._ready(p.pid)
        elif req.block:
            p.state, p.pending = "send_blocked", (req.pid, req.msg)
        else:
            p.value = False
            self._log(p.pid, "mailbox_full")
            self._ready(p.pid)

    def _try_deliver(self, p: _Proc) -> None:
        pid, msg = p.pending  # type: ignore[misc]
        t = self._procs.get(pid)
        if t is not None and t.state != "zombie" and self._deliver(t, msg):
            p.pending, p.value = None, True
            self._ready(p.pid)
