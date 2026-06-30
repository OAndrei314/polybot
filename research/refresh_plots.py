"""Refresh every arena plot into notebooks/png (or any --out-dir).

Runs the orphan resolver first (so plots reflect TRUE P&L including positions
on events that dropped off the price feed), then the plotters:
  - research.resolve_orphans     (writes leaderboard_corrected.csv)
  - research.plot_competitor_pnl
  - research.plot_bets           (per-variation entry/exit/size view)
  - research.feed_latency_report
  - research.plot_arena_signal_latency
  - research.analyze_phi_ladder  (v5/replay runs only; phi_ladder.png + report)

Usage:
  python -m research.refresh_plots                       # latest v4/live run, into notebooks/png
  python -m research.refresh_plots --profile v5          # latest v5/replay run, into notebooks/png/v5
  python -m research.refresh_plots --run-dir <path>      # specific run
  python -m research.refresh_plots --out-dir <path>      # custom output dir
"""

from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path

from research.run_select import is_phi_ladder_run, is_replay_run, is_v6_run, is_v7_run, latest_run

# resolve_orphans runs first — it only takes --run-dir, no --out-dir.
_ORPHAN_RESOLVER = "research.resolve_orphans"
_SCRIPTS = [
    "research.plot_competitor_pnl",
    "research.plot_bets",
    "research.feed_latency_report",
    "research.plot_arena_signal_latency",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--base-dir", default="data/reports/live_competitor_tests")
    ap.add_argument(
        "--profile",
        choices=["v4", "v5", "v6", "v7", "latest"],
        default="v4",
        help="Which run to auto-select when --run-dir is omitted. Default v4 preserves the original plotting target.",
    )
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    run_dir = Path(args.run_dir) if args.run_dir else latest_run(Path(args.base_dir), args.profile)
    if args.out_dir:
        out_dir = Path(args.out_dir)
    elif args.profile == "v7" or is_v7_run(run_dir):
        out_dir = Path("notebooks/png/v7")
    elif args.profile == "v6" or is_v6_run(run_dir):
        out_dir = Path("notebooks/png/v6")
    elif args.profile == "v5" or is_phi_ladder_run(run_dir):
        out_dir = Path("notebooks/png/v5")
    else:
        out_dir = Path("notebooks/png/v4")
    out_dir.mkdir(parents=True, exist_ok=True)

    forwarded = ["--out-dir", str(out_dir), "--run-dir", str(run_dir)]

    print(f"selected_run={run_dir}", flush=True)
    if is_v7_run(run_dir):
        prof_tag = "v7"
    elif is_v6_run(run_dir):
        prof_tag = "v6"
    elif is_phi_ladder_run(run_dir):
        prof_tag = "v5"
    else:
        prof_tag = "v4"
    print(f"profile={prof_tag}"
          f"{'  (replay)' if is_replay_run(run_dir) else '  (live)'}", flush=True)
    print(f"out_dir={out_dir}", flush=True)

    # The phi-ladder analysis only applies to phi-ladder runs. On a v4 run it
    # exits gracefully ("not a phi-ladder pool"), so this is just to avoid the
    # noise of running it where it can never apply.
    scripts = list(_SCRIPTS)
    if args.profile == "v5" or is_phi_ladder_run(run_dir):
        scripts.append("research.analyze_phi_ladder")

    # Step 0: resolve orphaned positions → leaderboard_corrected.csv
    print(f"\n=== {_ORPHAN_RESOLVER} ===", flush=True)
    sys.argv = [_ORPHAN_RESOLVER, "--run-dir", str(run_dir)]
    try:
        runpy.run_module(_ORPHAN_RESOLVER, run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(f"  (orphan resolver exited with code {exc.code})", flush=True)
    except Exception as exc:
        print(f"  (orphan resolver failed: {exc})", flush=True)

    for module in scripts:
        print(f"\n=== {module} ===", flush=True)
        sys.argv = [module, *forwarded]
        try:
            runpy.run_module(module, run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (None, 0):
                print(f"  (script {module} exited with code {exc.code})", flush=True)
        except Exception as exc:
            print(f"  (script {module} failed: {exc})", flush=True)

    print(f"\n--- {out_dir} now contains ---")
    for p in sorted(out_dir.iterdir()):
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        print(f"  {p.name:50s} {size:>10d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
