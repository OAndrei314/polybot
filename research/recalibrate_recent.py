"""Re-fit the calibration baseline using only recent tweet history.

WHY
---
Gate 3 forecast accuracy test (2026-05-26) found the predictor consistently
under-predicts weekly tweet counts by ~50% early in each window, converging
to <10% error only in the last 12 hours. The diagnosis: the calibration
baseline averages 16 years of Elon's tweeting, much of which was at lower
rates than 2025-2026. The model's prior pulls forecasts DOWN toward
historical averages, then in-window observations correct it slowly.

The cheap fix: refit calibration using ONLY recent history. The recency
weight in calibration.py already down-weights old data, but the baseline
still leans on the long tail. Truncating the input to the last N days
should shift the baseline toward the current regime and reduce early-window
bias.

This writes a SEPARATE calibration file (data/calibration_rates_6mo.json)
WITHOUT touching the production calibration that the live arenas use.

Usage:
  python -m research.recalibrate_recent                       # default 180 days
  python -m research.recalibrate_recent --lookback-days 90
  python -m research.recalibrate_recent --apply              # also overwrite the production cal (CAREFUL)
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from model.calibration import compute_rates


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-days", type=int, default=180,
                    help="window of recent tweet history to use (default 180)")
    ap.add_argument("--parquet", default="data/tweets_all.parquet")
    ap.add_argument("--out", default="data/calibration_rates_6mo.json")
    ap.add_argument("--apply", action="store_true",
                    help="ALSO overwrite data/calibration_rates.json (LIVE production)")
    args = ap.parse_args()

    src = Path(args.parquet)
    if not src.exists():
        print(f"missing {src}")
        return 1

    df = pd.read_parquet(src)
    df["dt"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df = df.dropna(subset=["dt"]).sort_values("dt")
    cutoff = df["dt"].max() - timedelta(days=args.lookback_days)
    df_recent = df[df["dt"] >= cutoff].copy()
    print(f"full parquet: {len(df)} rows, {df['dt'].min().date()} -> {df['dt'].max().date()}")
    print(f"recent window: {len(df_recent)} rows, {df_recent['dt'].min().date()} -> {df_recent['dt'].max().date()} ({args.lookback_days} days)")

    # Compute calibration on recent slice. Easiest path: write to a temp
    # parquet and call compute_rates on it. compute_rates expects a parquet
    # path, not a DataFrame.
    with tempfile.TemporaryDirectory() as td:
        tmp_pq = Path(td) / "recent.parquet"
        df_recent.to_parquet(tmp_pq, index=False)
        new_rates = compute_rates(tmp_pq)

    # Stamp the source so we can tell apart from production cal
    new_rates["source"] = f"recalibrate_recent.py (last {args.lookback_days} days)"
    new_rates["calibration_window_days"] = args.lookback_days

    # Compare to the production calibration
    prod_path = Path("data/calibration_rates.json")
    if prod_path.exists():
        with prod_path.open() as f:
            prod = json.load(f)
        print()
        print("=== before / after comparison ===")
        keys = [
            ("overall_rate_per_hour", "tweets/h baseline"),
            ("rt_excitation_alpha", "Hawkes alpha"),
            ("dispersion_phi_intercept", "phi intercept"),
            ("dispersion_phi_slope_per_h", "phi slope per h"),
            ("original_tweet_ratio", "original-tweet share"),
            ("dataset_days", "calibration window days"),
            ("total_tweets", "total tweets in sample"),
        ]
        print(f"  {'key':<30s}  {'production':>14s}  {'recent':>14s}  {'change':>12s}")
        for k, lbl in keys:
            p = prod.get(k); n = new_rates.get(k)
            if isinstance(p, (int, float)) and isinstance(n, (int, float)):
                if p != 0:
                    pct = 100 * (n - p) / abs(p)
                    print(f"  {lbl:<30s}  {p:>14.4f}  {n:>14.4f}  {pct:>+11.1f}%")
                else:
                    print(f"  {lbl:<30s}  {p:>14.4f}  {n:>14.4f}  {'':>12s}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(new_rates, f, indent=2)
    print()
    print(f"wrote new calibration to {out_path}")
    print(f"  baseline rate: {new_rates['overall_rate_per_hour']:.3f} tweets/h")
    print(f"  phi(168h) = {new_rates['dispersion_phi_intercept'] + new_rates['dispersion_phi_slope_per_h']*168:.2f}")

    if args.apply:
        backup = prod_path.with_suffix(".json.before_recent_refit")
        if prod_path.exists() and not backup.exists():
            shutil.copy(prod_path, backup)
            print(f"  backed up production cal to {backup}")
        shutil.copy(out_path, prod_path)
        print(f"  APPLIED to production: {prod_path}")
        print("  NOTE: existing live arenas already loaded the old calibration into memory.")
        print("  They will keep using the old values until restarted. Only fresh launches")
        print("  pick up the new calibration.")
    else:
        print()
        print("This file is NOT applied to production. Use --apply to overwrite the live cal.")
        print(f"To test forecast accuracy with the new cal:")
        print(f"  python -m research.gate3_forecast_accuracy "
              f"--out-dir notebooks/png/gate3_recent")
        print(f"  (after temporarily pointing predictor at {out_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
