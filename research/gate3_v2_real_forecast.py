"""Gate 3 v2 - REAL historical-window forecast accuracy.

v1 had a bug: PredictionModel._calibrated_lambda returns 0 for any window
ending in the past, so the original Gate 3 measured nothing. v2 fixes this
by computing lambda directly from the calibration's hourly_rates +
weekday_multipliers, bypassing the early-return guard.

Method per weekly window:
  1. lambda_full = expected count for the full 7-day window, computed
     directly from cal['hourly_rates'][h] * cal['weekday_multipliers'][d]
     integrated hour-by-hour.
  2. realized = actual count in window from tweets_all.parquet.
  3. error = lambda_full - realized.
  4. Repeat for multiple weekly windows; aggregate.

Compare two calibrations:
  - data/calibration_rates.json (production - 16 years average)
  - data/calibration_rates_6mo.json (last 180 days only)

Usage:
  python -m research.gate3_v2_real_forecast --n-weeks 12
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd


def expected_count(cal: dict, start: datetime, end: datetime) -> float:
    """Hour-by-hour integration of the calibrated rate over [start, end]."""
    hourly = cal["hourly_rates"]
    weekday = cal["weekday_multipliers"]
    total = 0.0
    cursor = start
    while cursor < end:
        next_hour = (cursor + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        slot_end = min(next_hour, end)
        frac = (slot_end - cursor).total_seconds() / 3600.0
        h = cursor.hour
        d = cursor.weekday()
        rate = hourly[h] * weekday[d]
        total += rate * frac
        cursor = slot_end
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-weeks", type=int, default=12)
    ap.add_argument("--tweets-path", default="data/tweets_all.parquet")
    ap.add_argument("--cal-prod", default="data/calibration_rates.json")
    ap.add_argument("--cal-test", default="data/calibration_rates_6mo.json")
    args = ap.parse_args()

    df = pd.read_parquet(args.tweets_path)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.sort_values("date")
    df = df[~df["is_retweet"].astype(bool)]
    end_dt = df["date"].max().to_pydatetime()

    # Snap to most recent Friday 22:51 UTC
    days_back = (end_dt.weekday() - 4) % 7
    if days_back == 0 and (end_dt.hour, end_dt.minute) < (22, 51):
        days_back = 7
    last_resolution = (end_dt - timedelta(days=days_back)).replace(
        hour=22, minute=51, second=0, microsecond=0
    )
    windows = []
    for i in range(args.n_weeks):
        end_w = last_resolution - timedelta(weeks=i)
        start_w = end_w - timedelta(days=7)
        windows.append((start_w, end_w))
    windows = list(reversed(windows))  # oldest first

    cal_prod = json.loads(Path(args.cal_prod).read_text())
    cal_test = json.loads(Path(args.cal_test).read_text())
    print(f"prod cal source: {cal_prod.get('source')}")
    print(f"test cal source: {cal_test.get('source')}")
    print()

    print(f"{'week_end':>12s}  {'realized':>9s}  {'prod_pred':>10s}  {'prod_err%':>10s}  {'test_pred':>10s}  {'test_err%':>10s}")
    prod_errs = []
    test_errs = []
    for w_start, w_end in windows:
        realized = int(((df["date"] >= w_start) & (df["date"] < w_end)).sum())
        prod_pred = expected_count(cal_prod, w_start, w_end)
        test_pred = expected_count(cal_test, w_start, w_end)
        prod_err_pct = abs(prod_pred - realized) / max(realized, 1) * 100
        test_err_pct = abs(test_pred - realized) / max(realized, 1) * 100
        prod_errs.append(prod_err_pct)
        test_errs.append(test_err_pct)
        print(f"  {w_end.date()}    {realized:>9d}  "
              f"{prod_pred:>10.1f}  {prod_err_pct:>9.1f}%  "
              f"{test_pred:>10.1f}  {test_err_pct:>9.1f}%")

    print()
    print(f"=== aggregate over {len(windows)} weeks (lower error = better forecast) ===")
    print(f"  prod cal: median |err| = {statistics.median(prod_errs):>5.1f}%  "
          f"mean = {statistics.mean(prod_errs):>5.1f}%  "
          f"within +/-20% = {sum(1 for e in prod_errs if e<=20)}/{len(prod_errs)}")
    print(f"  test cal: median |err| = {statistics.median(test_errs):>5.1f}%  "
          f"mean = {statistics.mean(test_errs):>5.1f}%  "
          f"within +/-20% = {sum(1 for e in test_errs if e<=20)}/{len(test_errs)}")

    improvement = statistics.mean(prod_errs) - statistics.mean(test_errs)
    print()
    if improvement > 5:
        print(f"VERDICT: 6-month cal beats production by {improvement:.1f} pp on mean error.")
        print(f"  Worth deploying in a v8 arena to test if it translates to better paper P&L.")
    elif improvement > 1:
        print(f"VERDICT: 6-month cal is marginally better ({improvement:.1f} pp improvement).")
        print(f"  Direction is right but the magnitude is small. Optional deploy.")
    elif improvement < -1:
        print(f"VERDICT: 6-month cal is WORSE than production by {-improvement:.1f} pp.")
        print(f"  Don't deploy. The longer history was actually helping.")
    else:
        print(f"VERDICT: No meaningful difference ({improvement:+.1f} pp).")
        print(f"  Recalibration is a wash for forecast accuracy. The bias we thought")
        print(f"  was there is either explained by something else, or the 'right' baseline")
        print(f"  was already approximately captured by recency_weights in the prod cal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
