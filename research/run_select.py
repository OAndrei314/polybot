from __future__ import annotations

import json
from pathlib import Path

from research import jsonl_io


def read_manifest(run_dir: Path) -> dict:
    path = run_dir / "run_manifest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def is_replay_run(run_dir: Path) -> bool:
    return bool(read_manifest(run_dir).get("replay_input_archive"))


def is_phi_ladder_run(run_dir: Path) -> bool:
    """v5 phi-ladder runs are identified by competitor IDs of the form
    `b##_phi###` (from research.generate_v5_pool). This works for both
    replay AND live launches of the ladder pool, which `is_replay_run`
    cannot distinguish once v5 starts running live."""
    import re
    pat = re.compile(r"^b\d+_phi\d+$")
    for cid in read_manifest(run_dir).get("competitors", []) or []:
        if pat.match(str(cid)):
            return True
    return False


def is_v6_run(run_dir: Path) -> bool:
    """v6 TP/SL-ladder vol-capture runs use IDs like `c##_h_tp###` (hard
    exit) or `c##_y_tp###` (hybrid). From research.generate_v6_pool."""
    import re
    pat = re.compile(r"^c\d+_[hy]_tp\d+$")
    for cid in read_manifest(run_dir).get("competitors", []) or []:
        if pat.match(str(cid)):
            return True
    return False


def is_v7_run(run_dir: Path) -> bool:
    """v7 inversion-test runs use matched-pair IDs i##_norm / i##_inv.
    From research.generate_v7_pool."""
    import re
    pat = re.compile(r"^i\d+_(norm|inv)$")
    for cid in read_manifest(run_dir).get("competitors", []) or []:
        if pat.match(str(cid)):
            return True
    return False


def has_trade_activity(run_dir: Path) -> bool:
    comp_dir = run_dir / "competitors"
    if not comp_dir.exists():
        return False
    try:
        for path in comp_dir.glob("*/trades.jsonl"):
            try:
                if path.stat().st_size > 0:
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def run_activity_mtime(run_dir: Path) -> float:
    candidates = [
        run_dir / "loop_metrics.jsonl",
        run_dir / "portfolio_snapshots.jsonl",
        run_dir / "leaderboard_latest.csv",
        run_dir / "real_input_ticks.jsonl.gz",
        run_dir / "run_manifest.json",
    ]
    mtimes: list[float] = []
    for path in candidates:
        ts = jsonl_io.mtime(path)
        if ts is not None:
            mtimes.append(ts)
    try:
        mtimes.append(run_dir.stat().st_mtime)
    except OSError:
        pass
    return max(mtimes) if mtimes else 0.0


def latest_run(base_dir: Path, profile: str = "v4") -> Path:
    runs = sorted(
        (p for p in base_dir.glob("*_real_48h") if p.is_dir()),
        key=run_activity_mtime,
        reverse=True,
    )
    if profile == "v4":
        # v4 = the original 96h_pool live arena. Exclude v5/v6/v7 pools so
        # the v4 profile stays pinned to the 96h_pool arenas.
        runs = [p for p in runs
                if not is_phi_ladder_run(p)
                and not is_v6_run(p)
                and not is_v7_run(p)]
    elif profile == "v5":
        runs = [p for p in runs if is_phi_ladder_run(p)]
        traded_runs = [p for p in runs if has_trade_activity(p)]
        if traded_runs:
            runs = traded_runs
    elif profile == "v6":
        runs = [p for p in runs if is_v6_run(p)]
        traded_runs = [p for p in runs if has_trade_activity(p)]
        if traded_runs:
            runs = traded_runs
    elif profile == "v7":
        runs = [p for p in runs if is_v7_run(p)]
        traded_runs = [p for p in runs if has_trade_activity(p)]
        if traded_runs:
            runs = traded_runs
    elif profile != "latest":
        raise ValueError(f"unknown profile: {profile}")
    if not runs:
        raise FileNotFoundError(f"No {profile} runs found under {base_dir}")
    return runs[0]
