"""Generic tools for using a recording as a regression test.

replay_run  : feed a recorded run's events into YOUR code (step function) and compare what it produces with what was
              recorded. Typical use: recorded inputs + recorded decisions -> "does the new software still decide the same?"
diff_runs   : compare two recordings event by event (e.g. before/after a software change, or real vs simulated).
summarize_run: a quick human-oriented summary of what happened in a run and whether the recording can be trusted.
None of this ever touches hardware; replay drives your own function or a simulator.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..events import Event
from .replay import ReplayReport, read_run


class RecordingNotClean(Exception):
    def __init__(self, report: ReplayReport) -> None:
        super().__init__(f"recording is not clean: {report}")
        self.report = report


def load_config(run_dir: str | Path) -> dict[str, Any]:
    """The configuration stored in the run's manifest (seed, parameters, software settings)."""
    try:
        return json.loads((Path(run_dir) / "manifest.json").read_text()).get("config", {})
    except (OSError, ValueError):
        return {}


def json_equal(a: Any, b: Any) -> bool:
    """Equality that ignores the list/tuple difference a JSON round trip introduces."""
    return json.loads(json.dumps(a, sort_keys=True)) == json.loads(json.dumps(b, sort_keys=True))


@dataclass
class Mismatch:
    event: Event
    expected: Any
    actual: Any


@dataclass
class HarnessResult:
    steps: int
    mismatches: list[Mismatch]
    report: ReplayReport
    config: dict[str, Any]

    @property
    def identical(self) -> bool:
        return self.steps > 0 and not self.mismatches

    @property
    def trustworthy(self) -> bool:
        """The recording itself was complete and uncorrupted, so a clean replay means something."""
        return self.report.clean


def replay_run(run_dir: str | Path, step: Callable[[Any, Event], Any], *,
               setup: Callable[[dict[str, Any]], Any] = lambda cfg: None,
               select: Callable[[Event], bool] | None = None,
               expected: Callable[[Event], Any] = lambda e: e.payload.get("out"),
               compare: Callable[[Any, Any], bool] = json_equal,
               stop_on_first: bool = False, require_clean: bool = False) -> HarnessResult:
    events, report = read_run(run_dir)
    if require_clean and not report.clean:
        raise RecordingNotClean(report)
    config = load_config(run_dir)
    ctx = setup(config)
    steps, bad = 0, []
    for ev in events:
        if select is not None and not select(ev):
            continue
        actual = step(ctx, ev)
        exp = expected(ev)
        steps += 1
        if not compare(exp, actual):
            bad.append(Mismatch(ev, exp, actual))
            if stop_on_first:
                break
    return HarnessResult(steps, bad, report, config)


def _default_key(e: Event) -> Any:
    return (e.subsystem, e.event_type, e.severity, json.dumps(e.payload, sort_keys=True, default=str))


@dataclass
class RunDiff:
    len_a: int
    len_b: int
    diffs: list[tuple[int, Any, Any]] = field(default_factory=list)     # (position, a, b); None where one run ended
    truncated: bool = False

    @property
    def identical(self) -> bool:
        return self.len_a == self.len_b and not self.diffs


def diff_runs(run_a: str | Path, run_b: str | Path, *, select: Callable[[Event], bool] | None = None,
              key: Callable[[Event], Any] = _default_key, max_diffs: int = 20) -> RunDiff:
    """Compare two recordings on a key (default: subsystem, type, severity and payload; never ids or timestamps)."""
    ea = [e for e in read_run(run_a)[0] if select is None or select(e)]
    eb = [e for e in read_run(run_b)[0] if select is None or select(e)]
    d = RunDiff(len(ea), len(eb))
    for i in range(max(len(ea), len(eb))):
        a = key(ea[i]) if i < len(ea) else None
        b = key(eb[i]) if i < len(eb) else None
        if a != b:
            if len(d.diffs) >= max_diffs:
                d.truncated = True
                break
            d.diffs.append((i, a, b))
    return d


def summarize_run(run_dir: str | Path) -> dict[str, Any]:
    events, rep = read_run(run_dir)
    by: dict[str, dict[str, int]] = {}
    sev: dict[str, int] = {}
    first_err = first_warn = None
    for e in events:
        by.setdefault(e.subsystem, {}).setdefault(e.event_type, 0)
        by[e.subsystem][e.event_type] += 1
        sev[e.severity] = sev.get(e.severity, 0) + 1
        if first_err is None and e.severity in ("error", "critical"):
            first_err = (e.seq, e.subsystem, e.event_type)
        if first_warn is None and e.severity == "warning":
            first_warn = (e.seq, e.subsystem, e.event_type)
    return {
        "events": len(events),
        "duration_wall_s": round(events[-1].event_time - events[0].event_time, 3) if events else 0.0,
        "by_subsystem": by, "by_severity": sev, "first_error": first_err, "first_warning": first_warn,
        "software_version": events[0].software_version if events else "",
        "config": load_config(run_dir),
        "recording": {"clean": rep.clean, "complete": rep.complete, "dropped": rep.declared_dropped,
                      "write_errors": rep.declared_write_errors, "truncated_tail": rep.truncated_tail,
                      "corrupt_records": rep.corrupt_records, "seq_gaps": rep.seq_gaps[:10]},
    }
