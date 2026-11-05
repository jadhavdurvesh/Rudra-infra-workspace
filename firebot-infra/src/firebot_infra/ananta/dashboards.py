"""Dashboard queries over SqliteAnalytics. Every table states its units and the UTC time range of the data it covers."""
from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .analytics import SqliteAnalytics


def _utc(t: float | None) -> str:
    return "n/a" if t is None else datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)] if s else float("nan")


@dataclass
class Table:
    title: str
    columns: list[str]
    rows: list[list[Any]]
    units: str = ""
    time_range: tuple[float | None, float | None] = (None, None)
    note: str = ""

    def _fmt(self, v: Any) -> str:
        if isinstance(v, float):
            return "n/a" if math.isnan(v) else f"{v:.3f}".rstrip("0").rstrip(".")
        return "" if v is None else str(v)

    def caption(self) -> str:
        lo, hi = self.time_range
        return f"{self.units}  |  data from {_utc(lo)} to {_utc(hi)}" + (f"  |  {self.note}" if self.note else "")

    def to_markdown(self) -> str:
        out = [f"### {self.title}", f"_{self.caption()}_", "", "| " + " | ".join(self.columns) + " |",
               "|" + "---|" * len(self.columns)]
        out += ["| " + " | ".join(self._fmt(v) for v in r) + " |" for r in self.rows] or ["| (no data) |"]
        return "\n".join(out)

    def to_html(self) -> str:
        e = html.escape
        head = "".join(f"<th>{e(c)}</th>" for c in self.columns)
        body = "".join("<tr>" + "".join(f"<td>{e(self._fmt(v))}</td>" for v in r) + "</tr>" for r in self.rows)
        return (f"<h3>{e(self.title)}</h3><p class='cap'>{e(self.caption())}</p>"
                f"<table><thead><tr>{head}</tr></thead><tbody>{body or '<tr><td>(no data)</td></tr>'}</tbody></table>")


def _range(db: SqliteAnalytics, where: str = "1=1", args: tuple = ()) -> tuple[float | None, float | None]:
    r = db.query(f"SELECT MIN(event_time) AS lo, MAX(event_time) AS hi FROM events WHERE {where}", args)[0]
    return r["lo"], r["hi"]


def overview(db: SqliteAnalytics, run_id: str | None = None) -> Table:
    w, a = ("run_id = ?", (run_id,)) if run_id else ("1=1", ())
    r = db.query(f"""SELECT COUNT(*) AS events, COUNT(DISTINCT run_id) AS runs, COUNT(DISTINCT software_version) AS versions,
                     COUNT(DISTINCT device_id) AS devices, SUM(severity='warning') AS warnings,
                     SUM(severity IN ('error','critical')) AS errors FROM events WHERE {w}""", a)[0]
    return Table("Overview", ["events", "runs", "software versions", "devices", "warnings", "errors"],
                 [[r["events"], r["runs"], r["versions"], r["devices"], r["warnings"] or 0, r["errors"] or 0]],
                 "counts of events", _range(db, w, a))


def errors_by_subsystem(db: SqliteAnalytics) -> Table:
    rows = db.query("""SELECT subsystem, COUNT(*) AS n, SUM(severity='warning') AS w, SUM(severity IN ('error','critical')) AS e
                       FROM events GROUP BY subsystem ORDER BY e DESC, w DESC, n DESC""")
    return Table("Warnings and errors by subsystem", ["subsystem", "events", "warnings", "errors", "warn+error per 1000 events"],
                 [[r["subsystem"], r["n"], r["w"] or 0, r["e"] or 0, 1000 * ((r["w"] or 0) + (r["e"] or 0)) / r["n"]] for r in rows],
                 "counts; rate = (warnings+errors) per 1000 events of that subsystem", _range(db))


def sensor_dropouts(db: SqliteAnalytics) -> Table:
    rows = db.query("""SELECT json_extract(metadata,'$.sensor_id') AS sensor,
        SUM(json_extract(metadata,'$.new')='stale') AS stale, SUM(json_extract(metadata,'$.new')='unavailable') AS unavailable,
        SUM(json_extract(metadata,'$.new')='degraded' AND json_extract(metadata,'$.reason')='flatline') AS flatline,
        SUM(json_extract(metadata,'$.new')='degraded' AND json_extract(metadata,'$.reason')!='flatline') AS degraded_other,
        MAX(event_time) AS last_event
        FROM events WHERE subsystem='drishti' AND event_type='status_change' GROUP BY sensor
        ORDER BY (stale + unavailable + flatline) DESC, sensor""")
    return Table("Sensor freshness and dropouts (from DRISHTI state changes)",
                 ["sensor", "became stale", "became unavailable", "flatline detected", "degraded (other)", "last change"],
                 [[r["sensor"], r["stale"], r["unavailable"], r["flatline"], r["degraded_other"], _utc(r["last_event"])] for r in rows],
                 "number of transitions into each state", _range(db, "subsystem='drishti'"))


def sensor_failures(db: SqliteAnalytics) -> Table:
    rows = db.query("""SELECT json_extract(metadata,'$.sensor_id') AS sensor, COUNT(*) AS n FROM events
                       WHERE subsystem='tantu' AND event_type='sensor_state' AND json_extract(metadata,'$.new')='failed'
                       GROUP BY sensor ORDER BY n DESC, sensor""")
    return Table("Sensor drivers that entered the FAILED state (from TANTU)", ["sensor", "times failed"],
                 [[r["sensor"], r["n"]] for r in rows], "count of transitions to failed", _range(db, "subsystem='tantu'"))


def request_latency(db: SqliteAnalytics) -> Table:
    rows = db.query("""SELECT json_extract(metadata,'$.message_type') AS mt, duration_ms AS d, success AS ok FROM events
                       WHERE subsystem='setu' AND event_type='request_done'""")
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["mt"], []).append(r)
    out = []
    for mt, g in sorted(groups.items()):
        ds = [x["d"] for x in g if x["d"] is not None]
        ok = sum(1 for x in g if x["ok"])
        out.append([mt, len(g), 100.0 * ok / len(g), _pct(ds, 50), _pct(ds, 95), max(ds) if ds else float("nan")])
    return Table("SETU request latency and success (needs Node(trace_requests=True))",
                 ["message type", "requests", "success %", "p50 ms", "p95 ms", "max ms"], out,
                 "durations in milliseconds, nearest-rank percentiles", _range(db, "subsystem='setu' AND event_type='request_done'"))


def message_problems(db: SqliteAnalytics) -> Table:
    rows = db.query("""SELECT event_type, COUNT(*) AS n FROM events WHERE subsystem='setu' AND event_type != 'request_done'
                       GROUP BY event_type ORDER BY n DESC""")
    return Table("SETU message problems (dropped or refused messages)", ["problem", "count"], [[r["event_type"], r["n"]] for r in rows],
                 "counts of events", _range(db, "subsystem='setu' AND event_type != 'request_done'"))


def task_durations(db: SqliteAnalytics) -> Table:
    rows = db.query("""SELECT json_extract(metadata,'$.type') AS t, duration_ms AS d, success AS ok FROM events
                       WHERE subsystem='pravah' AND event_type='transition' AND success IS NOT NULL""")
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["t"], []).append(r)
    out = []
    for t, g in sorted(groups.items()):
        ds = [x["d"] for x in g if x["d"] is not None]
        out.append([t, len(g), sum(1 for x in g if not x["ok"]), sum(ds) / len(ds) if ds else float("nan"),
                    _pct(ds, 95), max(ds) if ds else float("nan")])
    return Table("Task durations (from PRAVAH)", ["task type", "finished", "not succeeded", "mean ms", "p95 ms", "max ms"], out,
                 "durations in milliseconds of tasks that reached a final state", _range(db, "subsystem='pravah'"))


def timeseries(db: SqliteAnalytics, subsystem: str, event_type: str | None = None, bucket_s: int = 60) -> Table:
    w, a = "subsystem = ?", [subsystem]
    if event_type:
        w += " AND event_type = ?"; a.append(event_type)
    rows = db.query(f"""SELECT CAST(event_time / ? AS INTEGER) * ? AS bucket, COUNT(*) AS n, AVG(duration_ms) AS avg_d
                        FROM events WHERE {w} GROUP BY bucket ORDER BY bucket""", [bucket_s, bucket_s, *a])
    return Table(f"Events over time: {subsystem}" + (f"/{event_type}" if event_type else ""),
                 ["bucket start (UTC)", "events", "avg duration ms"], [[_utc(r["bucket"]), r["n"], r["avg_d"]] for r in rows],
                 f"events per {bucket_s} s bucket", _range(db, w, tuple(a)))


def compare_versions(db: SqliteAnalytics, baseline: str, candidate: str, *, min_events: int = 20,
                     ratio: float = 1.5, min_abs_per_1000: float = 5.0) -> Table:
    """Did a software version make things worse? Flags subsystems whose warning+error rate rose by more than `ratio`
    (and by at least `min_abs_per_1000`) and event types whose mean duration rose by more than `ratio`.
    Versions with fewer than `min_events` events for a subsystem are not judged (too little evidence)."""
    def rates(v: str) -> dict[str, tuple[int, float]]:
        return {r["subsystem"]: (r["n"], 1000 * ((r["w"] or 0) + (r["e"] or 0)) / r["n"]) for r in db.query(
            """SELECT subsystem, COUNT(*) AS n, SUM(severity='warning') AS w, SUM(severity IN ('error','critical')) AS e
               FROM events WHERE software_version = ? GROUP BY subsystem""", [v])}

    def durs(v: str) -> dict[tuple[str, str], tuple[int, float]]:
        return {(r["subsystem"], r["event_type"]): (r["n"], r["m"]) for r in db.query(
            """SELECT subsystem, event_type, COUNT(*) AS n, AVG(duration_ms) AS m FROM events
               WHERE software_version = ? AND duration_ms IS NOT NULL GROUP BY subsystem, event_type""", [v])}

    rows: list[list[Any]] = []
    b, c = rates(baseline), rates(candidate)
    for sub in sorted(set(b) | set(c)):
        (bn, br), (cn, cr) = b.get(sub, (0, 0.0)), c.get(sub, (0, 0.0))
        if min(bn, cn) < min_events:
            flag = "not enough data"
        else:
            flag = "REGRESSION" if cr > br * ratio and cr - br >= min_abs_per_1000 else "ok"
        rows.append([f"{sub}: warn+error rate", br, cr, cr - br, flag, f"{bn} / {cn} events"])
    bd, cd = durs(baseline), durs(candidate)
    for key in sorted(set(bd) & set(cd)):
        (bn, bm), (cn, cm) = bd[key], cd[key]
        flag = "not enough data" if min(bn, cn) < min_events else ("REGRESSION" if cm > bm * ratio else "ok")
        rows.append([f"{key[0]}/{key[1]}: mean duration", bm, cm, cm - bm, flag, f"{bn} / {cn} samples"])
    return Table(f"Version comparison: {baseline} (baseline) vs {candidate} (candidate)",
                 ["metric", "baseline", "candidate", "change", "verdict", "evidence (baseline / candidate)"], rows,
                 "rates are warnings+errors per 1000 events; durations in milliseconds",
                 _range(db, "software_version IN (?, ?)", (baseline, candidate)),
                 note=f"regression = more than {ratio}x worse and at least {min_abs_per_1000}/1000 for rates")


def full_report(db: SqliteAnalytics, run_id: str | None = None) -> list[Table]:
    return [overview(db, run_id), errors_by_subsystem(db), sensor_dropouts(db), sensor_failures(db), message_problems(db),
            request_latency(db), task_durations(db)]


def to_markdown(tables: list[Table], title: str = "Fire Bot analytics report") -> str:
    return f"# {title}\n\n" + "\n\n".join(t.to_markdown() for t in tables) + "\n"


def to_html(tables: list[Table], title: str = "Fire Bot analytics report") -> str:
    css = ("body{font:14px system-ui,sans-serif;margin:24px;color:#222}table{border-collapse:collapse;margin-bottom:8px}"
           "th,td{border:1px solid #ccc;padding:4px 10px;text-align:left}th{background:#f1f1f1}.cap{color:#555;margin:2px 0 8px}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title><style>{css}</style></head>"
            f"<body><h1>{html.escape(title)}</h1>{''.join(t.to_html() for t in tables)}</body></html>")
