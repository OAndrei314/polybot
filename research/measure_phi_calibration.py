"""Measure realized Elon-count dispersion vs the model's phi curve.

Gate 2 showed the v4 model is systematically UNDER-confident — predicted win
rate ~70%, realized ~87%. Suspected cause: the NB dispersion phi is set wider
than reality, which spreads the predicted count distribution too thin and so
under-rates the bracket the count actually lands in.

This measures the realized index of dispersion (variance-to-mean of Elon's
tweet counts in fixed windows) over recent periods and compares it to the
model's assumed phi (data/calibration_rates.json dispersion curve).

  realized phi < model phi  ->  model over-disperses -> under-confidence.

Caveat: realized phi here is RAW variance-to-mean, which also absorbs rate
non-stationarity over the period — so it is an UPPER bound on the true
over-dispersion. If even this upper bound sits below the model phi, the
"model too wide" conclusion is robust.

Usage: python -m research.measure_phi_calibration
"""

from __future__ import annotations

import bisect
import json
import statistics
from datetime import datetime, timezone

import pandas as pd

PARQUET = "data/tweets_all.parquet"
CAL = "data/calibration_rates.json"
WINDOWS_H = [24, 48, 72, 168]
PERIODS_D = [60, 120, 240]


def main() -> int:
    cal = json.load(open(CAL))
    a = float(cal["dispersion_phi_intercept"])
    b = float(cal["dispersion_phi_slope_per_h"])

    df = pd.read_parquet(PARQUET)
    secs = sorted(
        int(t.timestamp())
        for t in pd.to_datetime(df["date"], utc=True, errors="coerce").dropna()
    )
    now = secs[-1]
    print(f"tweets through {datetime.fromtimestamp(now, timezone.utc).date()}")
    print(f"model phi curve: phi(h) = {a:.2f} + {b:.3f}*h")
    print(f"  -> model phi: 24h={a + b * 24:.0f}  48h={a + b * 48:.0f}  "
          f"72h={a + b * 72:.0f}  168h={a + b * 168:.0f}\n")

    print("realized phi / model phi  (ratio = how much the model over-disperses)")
    print("period   " + "".join(f"W={w}h".rjust(15) for w in WINDOWS_H))
    for days in PERIODS_D:
        recent = secs[bisect.bisect_left(secs, now - days * 86400):]
        cells = []
        for w_h in WINDOWS_H:
            w = w_h * 3600
            counts = []
            t = recent[0]
            while t + w <= recent[-1]:
                counts.append(
                    bisect.bisect_left(recent, t + w) - bisect.bisect_left(recent, t)
                )
                t += w
            mean = statistics.mean(counts) if counts else 0.0
            if len(counts) > 1 and mean > 0:
                realized = statistics.pvariance(counts) / mean
                model = a + b * w_h
                cells.append(f"{realized:.0f} / {model:.0f} = {model / realized:.1f}x")
            else:
                cells.append("(thin)")
        print(f"last {days:3d}d" + "".join(c.rjust(15) for c in cells))

    print()
    print("ratio > 1  ->  model phi is wider than reality  ->  under-confidence.")
    print("Longer periods = more windows = less noise; trust the 168h column least")
    print("(fewest windows). Realized phi is an upper bound (absorbs rate drift).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
