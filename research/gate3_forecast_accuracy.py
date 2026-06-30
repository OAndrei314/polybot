"""Gate 3 v1 — cross-week forecast accuracy on historical tweet data.

THE QUESTION
------------
Does the model's count forecast actually predict Elon's weekly tweet count
across multiple historical weeks? This is the model-side half of "do we
have edge" — independent of any market structure.

We have tweet history (data/tweets_all.parquet) through 2026-05-01 but
NO historical Polymarket prices, so a profitability backtest isn't
possible. What IS possible: simulate the model's count forecast at
sample points within each historical weekly window and compare predicted
vs realized final count.

If the model can forecast Elon's weekly count to within sensible bounds
across 10+ historical weeks, the model has real signal and the bot's
weak resolution accuracy comes from market structure (prices already
reflecting that signal). If the model can't even forecast the count
well, the model itself is the problem and no exit strategy or phi tweak
helps.

CAVEAT (LOCKED, ACKNOWLEDGED)
-----------------------------
The calibration in data/calibration_rates.json was fit using tweet data
through 2026-05-01, which includes the weeks we're now testing on. So
there's mild look-ahead bias. A strict out-of-sample test would refit
calibration per-week. For v1 we accept the bias because Elon's tweet
rate has been relatively stable across 2025-2026 — the calibration is
mostly a baseline rate, not week-specific tuning. If v1 results are
already bad, strict OOS would be worse, not better.

OUTPUT
------
  <out-dir>/gate3_accuracy.csv     - per-window metrics
  <out-dir>/gate3_accuracy.png     - predicted vs realized scatter
  stdout                            - verdict

Usage:
  python -m research.gate3_forecast_accuracy
  python -m research.gate3_forecast_accuracy --n-weeks 10 --out-dir notebooks/png/gate3
"""

from __future__ import annotations

import argparse
import csv
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from model.predictor import PredictionModel


def weekly_windows(end_dt: datetime, n: int) -> list[tuple[datetime, datetime]]:
    """N weekly windows ending Friday 22:51 UTC at or before end_dt.
    Polymarket weekly markets typically resolve Friday late evening UTC."""
    # Snap end_dt back to the previous Friday 22:51 UTC.
    days_back = (end_dt.weekday() - 4) % 7  # 4 = Friday
    if days_back == 0 and (end_dt.hour, end_dt.minute) < (22, 51):
        days_back = 7
    last_resolution = (end_dt - timedelta(days=days_back)).replace(
        hour=22, minute=51, second=0, microsecond=0
    )
    windows = []
    for i in range(n):
        end_w = last_resolution - timedelta(weeks=i)
        start_w = end_w - timedelta(days=7)
        windows.append((start_w, end_w))
    return list(reversed(windows))  # oldest first


def forecast_count(
    model: PredictionModel,
    historical_tweets_before_sample: list[datetime],
    sample_time: datetime,
    window_end: datetime,
) -> float:
    """Model's expected total count for [sample_time, window_end] given
    we've observed historical tweets up to sample_time.

    Combines the calibrated baseline rate with a Bayesian update on the
    in-window observed pace (mirrors the production lambda computation
    in PredictionModel.predict()).
    """
    # Inject historical tweets into model.rows so Hawkes excitation uses
    # the right context. The model only looks at recent tweets so this is
    # bounded in size.
    cutoff = sample_time - timedelta(hours=12)  # Hawkes window
    recent = [
        {"created_at": dt.isoformat(), "is_retweet": False}
        for dt in historical_tweets_before_sample
        if dt >= cutoff
    ]
    model.rows = recent  # short-circuit dataset

    # Baseline calibrated lambda for [sample_time, window_end]
    sentiment_mult = 1.0  # neutral; sentiment effect is small
    # hawkes excitation in tweets/hour (rough proxy)
    hawkes_per_h = 0.0
    if recent:
        # Simple: count tweets in last 1h * a decay factor
        tweets_1h = sum(
            1 for r in recent
            if datetime.fromisoformat(r["created_at"]) >= sample_time - timedelta(hours=1)
        )
        hawkes_per_h = tweets_1h * 0.3  # very rough; ok for v1
    lam = model._calibrated_lambda(sample_time, window_end, sentiment_mult, hawkes_per_h)

    # Bayesian rate update on in-window observed pace, mirroring predict()
    # to make this representative of what the live bot computes.
    window_start = window_end - timedelta(days=7)
    elapsed_h = max(0.0, (sample_time - window_start).total_seconds() / 3600.0)
    remaining_h = max(1e-6, (window_end - sample_time).total_seconds() / 3600.0)
    in_window_count = sum(1 for dt in historical_tweets_before_sample if dt >= window_start)
    if elapsed_h >= 1.0 and lam > 0:
        prior_rate_h = max(1e-6, lam / remaining_h)
        alpha_0 = prior_rate_h * model._rate_prior_strength_h
        posterior_rate_h = (alpha_0 + float(in_window_count)) / (
            model._rate_prior_strength_h + elapsed_h
        )
        rescale = max(0.25, min(4.0, posterior_rate_h / prior_rate_h))
        lam *= rescale

    return float(lam)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-weeks", type=int, default=10)
    ap.add_argument("--samples-per-week", type=int, default=14,
                    help="Sample times per week (default 14 = every 12h)")
    ap.add_argument("--tweets-path", default="data/tweets_all.parquet")
    ap.add_argument("--out-dir", default="notebooks/png/gate3")
    ap.add_argument("--cal-path", default="data/calibration_rates.json",
                    help="calibration JSON to test (default = production)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(args.tweets_path)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.sort_values("date").reset_index(drop=True)
    # Skip retweets - the bot predicts original tweets primarily
    df = df[~df["is_retweet"].astype(bool)]
    end_dt = df["date"].max().to_pydatetime()
    print(f"tweets dataset: {len(df)} non-retweet rows, ending {end_dt}")

    windows = weekly_windows(end_dt, args.n_weeks)
    print(f"testing {len(windows)} weekly windows:")
    for s, e in windows:
        n = ((df["date"] >= s) & (df["date"] < e)).sum()
        print(f"  {s.date()} -> {e.date()}    realized count: {n}")
    print()

    print(f"using calibration: {args.cal_path}")
    model = PredictionModel(calibration_cache=args.cal_path)
    samples = args.samples_per_week
    rows_out: list[dict] = []
    all_pred_total: list[float] = []
    all_realized_total: list[int] = []
    per_week_mae: list[tuple[str, float, int, float]] = []

    for w_start, w_end in windows:
        realized = int(((df["date"] >= w_start) & (df["date"] < w_end)).sum())
        all_realized_total.append(realized)

        # Sample times within the window (skip the very first - no history yet)
        sample_hours = [
            12 * (i + 1) for i in range(samples)
            if 12 * (i + 1) < 168
        ]
        forecasts: list[tuple[float, float]] = []  # (predicted_total, hours_elapsed)
        for h in sample_hours:
            sample_time = w_start + timedelta(hours=h)
            # tweets observed UP TO sample_time (datetime list)
            hist_dts = df.loc[df["date"] < sample_time, "date"].dt.to_pydatetime().tolist()
            in_window_so_far = sum(1 for dt in hist_dts if dt >= w_start)
            try:
                lam_remaining = forecast_count(model, hist_dts, sample_time, w_end)
            except Exception as exc:
                print(f"  forecast failed at h={h} in {w_start.date()}: {exc}")
                continue
            predicted_total = in_window_so_far + lam_remaining
            forecasts.append((predicted_total, h))
            rows_out.append({
                "week_end": w_end.date().isoformat(),
                "hours_elapsed": h,
                "in_window_so_far": in_window_so_far,
                "lambda_remaining": round(lam_remaining, 2),
                "predicted_total": round(predicted_total, 2),
                "realized_total": realized,
                "error": round(predicted_total - realized, 2),
                "abs_error_pct": round(abs(predicted_total - realized) / max(realized, 1) * 100, 2),
            })

        # Per-week aggregate metric (mean over sample times)
        if forecasts:
            mean_pred = statistics.mean(p for p, _ in forecasts)
            mae = abs(mean_pred - realized)
            per_week_mae.append((w_end.date().isoformat(), mean_pred, realized, mae))
            all_pred_total.append(mean_pred)

    # Write CSV
    csv_path = out_dir / "gate3_accuracy.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)
    print(f"wrote {csv_path}")

    # Per-week report
    print()
    print("=== per-week forecast accuracy (mean across sample times) ===")
    print(f"{'week_end':>12s}  {'mean_pred':>10s}  {'realized':>9s}  {'error':>8s}  {'abs_err%':>9s}")
    abs_errs = []
    for wid, mp, r, mae in per_week_mae:
        err_pct = (mae / max(r, 1)) * 100
        abs_errs.append(err_pct)
        print(f"  {wid}    {mp:>10.1f}  {r:>9d}  {mp-r:>+8.1f}  {err_pct:>8.1f}%")

    # Aggregate metrics
    print()
    if abs_errs:
        mean_abs_err_pct = statistics.mean(abs_errs)
        med_abs_err_pct = statistics.median(abs_errs)
        within_20 = sum(1 for e in abs_errs if e <= 20)
        within_10 = sum(1 for e in abs_errs if e <= 10)
        # Correlation
        if len(all_pred_total) == len(all_realized_total) and len(all_pred_total) >= 3:
            r_corr = np.corrcoef(all_pred_total, all_realized_total)[0, 1]
        else:
            r_corr = float("nan")
        print(f"=== aggregate over {len(per_week_mae)} weeks ===")
        print(f"  mean |error| as % of realized:   {mean_abs_err_pct:.1f}%")
        print(f"  median |error| as % of realized: {med_abs_err_pct:.1f}%")
        print(f"  weeks within +/-10%:             {within_10}/{len(abs_errs)}")
        print(f"  weeks within +/-20%:             {within_20}/{len(abs_errs)}")
        print(f"  Pearson r (predicted, realized): {r_corr:.3f}")

        # Verdict
        print()
        if within_20 >= 0.8 * len(abs_errs) and r_corr > 0.5:
            verdict = ("PASS: model forecast tracks realized weekly counts. "
                       "Model has signal at the count level - any bot losses "
                       "come from market structure, not bad forecasting.")
        elif within_20 >= 0.5 * len(abs_errs):
            verdict = ("PARTIAL: forecast is roughly right-direction but high "
                       "variance. Model has SOME signal; not strong enough on "
                       "its own.")
        else:
            verdict = ("FAIL: forecast errors are large/uncorrelated. The model "
                       "doesn't reliably predict weekly counts, which means the "
                       "bot has no model-driven edge to extract from market "
                       "mispricings. Time to pivot strategy.")
        print(f"VERDICT: {verdict}")

    # Plot: predicted vs realized
    fig, ax = plt.subplots(figsize=(7, 7))
    if all_pred_total and all_realized_total:
        ax.scatter(all_realized_total, all_pred_total, s=80, color="#2563eb", edgecolor="black", zorder=3)
        lo = min(min(all_pred_total), min(all_realized_total)) * 0.85
        hi = max(max(all_pred_total), max(all_realized_total)) * 1.15
        ax.plot([lo, hi], [lo, hi], color="#9ca3af", ls="--", lw=1.5, label="perfect")
        ax.set_xlim(lo, hi); ax.set_ylim(lo, hi)
    ax.set_xlabel("realized weekly count")
    ax.set_ylabel("model mean predicted total (across sample times)")
    ax.set_title(f"Gate 3 forecast accuracy: {len(per_week_mae)} weekly windows", fontsize=11)
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    png_path = out_dir / "gate3_accuracy.png"
    fig.savefig(png_path, dpi=160)
    plt.close(fig)
    print(f"wrote {png_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
