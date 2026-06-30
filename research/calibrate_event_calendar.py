"""Calibrate event-calendar multipliers from historical Elon catalysts.

The event calendar (data/elon_event_calendar.json) lifts the model's expected
tweet rate around scheduled catalysts. v5's first cut used hand-guessed
multipliers — this replaces the guess with measurement.

For each historical catalyst it measures, from tweets_all.parquet:
  - rate lift = tweets/hour in the catalyst window / trailing-14d baseline
  - dispersion lift = variance-to-mean of catalyst-window counts vs the same
    for matched non-catalyst windows (does a catalyst widen phi, not just mu?)

It then recommends a per-type rate_multiplier and phi_multiplier, each SHRUNK
toward 1.0 in proportion to how noisy the estimate is across catalysts — a
wide spread means less trust, so a more conservative multiplier. This is the
guard against over-valuing earnings/launches.

Catalyst dates (UTC). Sources:
  Tesla earnings   — SEC 8-K filings / Tesla IR
  Starship flights — Wikipedia "List of Starship launches"

Usage:  python -m research.calibrate_event_calendar
"""

from __future__ import annotations

import bisect
import statistics
from datetime import datetime, timezone

import pandas as pd

PARQUET = "data/tweets_all.parquet"
WINDOW_H = 36.0       # catalyst burst window length (hours)
BASELINE_DAYS = 14    # trailing baseline before each catalyst
HOUR = 3600.0

EARNINGS = ["2024-04-23", "2024-07-23", "2025-10-22", "2026-04-22"]
LAUNCHES = ["2023-04-20", "2024-06-06", "2024-10-13",
            "2025-01-16", "2025-03-06", "2025-08-26", "2025-10-13"]


def main() -> int:
    df = pd.read_parquet(PARQUET)
    ts = pd.to_datetime(df["date"], utc=True, errors="coerce").dropna()
    secs = sorted(int(t.timestamp()) for t in ts)
    print(f"loaded {len(secs):,} tweet timestamps  "
          f"{datetime.fromtimestamp(secs[0], timezone.utc).date()} -> "
          f"{datetime.fromtimestamp(secs[-1], timezone.utc).date()}\n")

    def count(a: float, b: float) -> int:
        return bisect.bisect_left(secs, b) - bisect.bisect_left(secs, a)

    def rate(a: float, b: float) -> float:
        span_h = (b - a) / HOUR
        return count(a, b) / span_h if span_h > 0 else 0.0

    def vmr(xs: list[int]) -> float:
        m = statistics.mean(xs) if xs else 0.0
        return statistics.pvariance(xs) / m if m > 0 and len(xs) > 1 else 0.0

    for kind, dates in [("LAUNCH", LAUNCHES), ("EARNINGS", EARNINGS)]:
        lifts: list[float] = []
        cat_counts: list[int] = []
        base_counts: list[int] = []
        rows = []
        for d in dates:
            c = datetime.fromisoformat(d + "T18:00:00+00:00").timestamp()
            wa, wb = c, c + WINDOW_H * HOUR
            ba, bb = c - BASELINE_DAYS * 86400, c - 86400
            win_rate, base_rate = rate(wa, wb), rate(ba, bb)
            cnt = count(wa, wb)
            cat_counts.append(cnt)
            t = ba
            while t + WINDOW_H * HOUR <= bb:
                base_counts.append(count(t, t + WINDOW_H * HOUR))
                t += WINDOW_H * HOUR
            if base_rate > 0:
                lifts.append(win_rate / base_rate)
                rows.append((d, cnt, win_rate / base_rate))

        print(f"=== {kind}  ({len(dates)} catalysts) ===")
        for d, cnt, lf in rows:
            print(f"  {d}   window count={cnt:4d}   rate lift x{lf:.2f}")
        if not lifts:
            print("  (no usable baselines)\n")
            continue

        mean_lift = statistics.mean(lifts)
        sd = statistics.pstdev(lifts) if len(lifts) > 1 else 0.0
        cv = sd / mean_lift if mean_lift else 0.0
        shrink = max(0.3, min(1.0, 1.0 - cv))   # noisy estimate -> trust less
        rec_rate = 1.0 + (mean_lift - 1.0) * shrink

        phi_cat, phi_base = vmr(cat_counts), vmr(base_counts)
        phi_lift = phi_cat / phi_base if phi_base > 0 else 1.0
        rec_phi = max(1.0, 1.0 + (phi_lift - 1.0) * shrink)

        print(f"  rate lift      : mean x{mean_lift:.2f}  (spread cv={cv:.2f})")
        print(f"  dispersion lift: catalyst phi {phi_cat:.1f} vs baseline "
              f"{phi_base:.1f}  -> x{phi_lift:.2f}")
        print(f"  >> RECOMMENDED  rate_multiplier x{rec_rate:.2f}   "
              f"phi_multiplier x{rec_phi:.2f}")
        print(f"     (shrink {shrink:.2f} applied for {len(lifts)}-sample noise)\n")

    print("Note: small samples (4-7 catalysts) — treat as a sane starting "
          "point, not a precise estimate. Re-run as more catalysts accrue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
