"""Retention for recordings. Only ever deletes directories that carry a valid AKSHARA manifest."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path


def prune_runs(root: str | Path, older_than_s: float, *, now: float | None = None, dry_run: bool = True,
               include_incomplete: bool = False) -> list[Path]:
    """Delete (or, with dry_run=True, just list) recorded runs that started more than `older_than_s` seconds ago.

    Safety rules: only direct sub-directories of `root` with a valid `manifest.json` (format akshara-1) are considered;
    runs not cleanly closed (possibly still in progress) are skipped unless include_incomplete=True; anything else
    in the folder is never touched. dry_run defaults to True so a mistake only produces a list.
    """
    now = time.time() if now is None else now
    hit: list[Path] = []
    for d in sorted(Path(root).iterdir()):
        mp = d / "manifest.json"
        if d.is_symlink() or not d.is_dir() or not mp.is_file():
            continue
        try:
            m = json.loads(mp.read_text())
            if m.get("format") != "akshara-1":
                continue
            started, complete = float(m["started_wall"]), bool(m.get("complete"))
        except (ValueError, KeyError, TypeError, OSError):
            continue
        if (complete or include_incomplete) and now - started > older_than_s:
            hit.append(d)
            if not dry_run:
                shutil.rmtree(d)
    return hit
