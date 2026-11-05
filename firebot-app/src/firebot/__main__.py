import argparse
import json
import sys
from pathlib import Path

from .replay import replay_decisions
from .report import ascii_map, svg
from .runner import run_mission
from .sim import SCENARIOS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="firebot", description="Run a simulated Fire Bot mission")
    ap.add_argument("--scenario", default="obstacles", choices=sorted(SCENARIOS))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--data-dir", type=Path, default=None, help="record the run (AKSHARA) and analytics rows here")
    ap.add_argument("--svg", type=Path, default=None, help="write a picture of the run")
    ap.add_argument("--replay", action="store_true", help="after the run, replay the recording and verify decisions")
    ap.add_argument("--randomize", action="store_true", help="random fire position and start heading (by seed)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = run_mission(a.scenario, a.seed, data_dir=a.data_dir, randomize=a.randomize)
    if a.json:
        print(json.dumps(res.summary(), indent=2))
    else:
        print(ascii_map(res))
        print(f"\n{res.scenario} seed {res.seed}: {res.outcome.upper()} ({res.reason}) after {res.sim_time}s, "
              f"{res.distance_m} m driven, coverage {res.coverage:.0%}, collisions {res.collisions}")
        if res.estimated_fire:
            print(f"fire estimated at ({res.estimated_fire[0]:.2f}, {res.estimated_fire[1]:.2f}), "
                  f"true ({res.true_fire[0]:.2f}, {res.true_fire[1]:.2f}), error {res.error_m} m; "
                  f"report delivered to station: {res.report_delivered}")
    if a.svg:
        a.svg.write_text(svg(res))
        print(f"picture written to {a.svg}")
    if a.replay:
        if not a.data_dir:
            print("--replay needs --data-dir", file=sys.stderr)
            return 2
        print("replay:", replay_decisions(res.run_dir))
    return 0 if res.outcome in ("found", "not_found") else 1


if __name__ == "__main__":
    raise SystemExit(main())
