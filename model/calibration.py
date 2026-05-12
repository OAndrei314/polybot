"""
Historical tweet rate calibration.

Reads data/tweets_all.parquet (built by ingestion/merger.py) and computes:
  - hourly_rates[0..23]       avg tweets per occurrence of that UTC hour
  - weekday_multipliers[0..6] Mon..Sun scaling relative to the weekly mean
  - overall_rate_per_hour     grand average across all hours / days
  - original_tweet_ratio      fraction of all tweets that are originals (not RTs)
  - rt_excitation_alpha       rolling RT fraction — feeds the Hawkes excitation term
  - sleep_profile[0..23]      probability of zero activity at each UTC hour (sleep proxy)

Results are cached to data/calibration_rates.json so the predictor starts instantly.
Call compute_and_cache(force=True) after ingesting new historical data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_PARQUET = Path("data/tweets_all.parquet")
_CACHE = Path("data/calibration_rates.json")
# Half-life chosen so the recent ~3 months dominate calibration: Elon's posting
# rate has accelerated from ~12/day (lifetime avg) to ~50/day (last 90d), so a
# long half-life massively underestimates current behavior.
_RECENCY_HALFLIFE_DAYS = 90.0
_RECENCY_MIN_WEIGHT = 0.02
# Window over which we measure dispersion. Recent enough to reflect the current
# regime, long enough to have statistical power for week-long buckets.
_DISPERSION_WINDOW_DAYS = 180


def _build_sentiment_analyzer() -> Any:
    try:
        from nltk.sentiment.vader import SentimentIntensityAnalyzer as _NltkSIA

        return _NltkSIA()
    except Exception:
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _PkgSIA

            return _PkgSIA()
        except Exception:
            return None


def _sentiment_scores(texts: pd.Series) -> pd.Series:
    analyzer = _build_sentiment_analyzer()
    if analyzer is None:
        return pd.Series(0.0, index=texts.index, dtype=float)
    return texts.fillna("").astype(str).map(
        lambda t: float(analyzer.polarity_scores(t).get("compound", 0.0))
    )


def _recency_weights(
    dt: pd.Series,
    half_life_days: float = _RECENCY_HALFLIFE_DAYS,
    min_weight: float = _RECENCY_MIN_WEIGHT,
) -> pd.Series:
    if dt.empty:
        return pd.Series(dtype=float)
    latest = dt.max()
    age_days = (latest - dt).dt.total_seconds().div(86400.0).clip(lower=0.0)
    hl = max(1e-6, float(half_life_days))
    w = np.power(0.5, age_days / hl)
    return pd.Series(np.maximum(float(min_weight), w), index=dt.index, dtype=float)


def _weighted_mean(values: pd.Series, weights: pd.Series) -> float:
    if values.empty:
        return 0.0
    v = pd.to_numeric(values, errors="coerce")
    w = pd.to_numeric(weights, errors="coerce")
    mask = (~v.isna()) & (~w.isna())
    if not mask.any():
        return 0.0
    v = v[mask].astype(float)
    w = w[mask].astype(float)
    den = float(w.sum())
    if den <= 0:
        return 0.0
    return float((v * w).sum() / den)


def _retweet_followup_probs(df: pd.DataFrame, window_min: int = 60) -> tuple[float, float]:
    if df.empty:
        return 0.0, 0.0
    seq = df[["dt", "is_retweet"]].sort_values("dt").reset_index(drop=True)
    if len(seq) < 2:
        return 0.0, 0.0
    seq["next_gap_min"] = (seq["dt"].shift(-1) - seq["dt"]).dt.total_seconds() / 60.0
    seq = seq.iloc[:-1].copy()

    rt_mask = seq["is_retweet"].astype(bool)
    after_rt = seq.loc[rt_mask, "next_gap_min"]
    after_orig = seq.loc[~rt_mask, "next_gap_min"]
    seq["w"] = _recency_weights(seq["dt"])

    rt_w = seq.loc[rt_mask, "w"]
    orig_w = seq.loc[~rt_mask, "w"]
    p_rt = _weighted_mean((after_rt <= window_min).astype(float), rt_w) if len(after_rt) else 0.0
    p_orig = (
        _weighted_mean((after_orig <= window_min).astype(float), orig_w) if len(after_orig) else 0.0
    )
    return p_rt, p_orig


def _default_rates() -> dict[str, Any]:
    """Flat defaults when no historical data is available."""
    return {
        "hourly_rates": [1.6] * 24,
        "weekday_multipliers": [1.0] * 7,
        "hourly_sentiment_mean": [0.0] * 24,
        "weekday_sentiment_mean": [0.0] * 7,
        "sentiment_mean": 0.0,
        "sentiment_std": 0.0,
        "sentiment_nonzero_ratio": 0.0,
        "retweet_followup_prob_60m": 0.0,
        "original_followup_prob_60m": 0.0,
        "overall_rate_per_hour": 1.6,
        "original_tweet_ratio": 0.43,
        "rt_excitation_alpha": 0.57,
        "sleep_profile": [0.0] * 24,
        "recency_half_life_days": _RECENCY_HALFLIFE_DAYS,
        "dispersion_phi_intercept": 6.5,
        "dispersion_phi_slope_per_h": 0.21,
        "dispersion_phi_points": [],
        "dispersion_window_days": _DISPERSION_WINDOW_DAYS,
        "dataset_days": 0,
        "total_tweets": 0,
        "source": "default",
    }


def compute_rates(parquet_path: str | Path = _PARQUET) -> dict[str, Any]:
    """Compute rate profiles from the merged parquet. Returns a plain dict."""
    p = Path(parquet_path)
    if not p.exists():
        return _default_rates()

    df = pd.read_parquet(p)
    df["dt"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df = df.dropna(subset=["dt"])
    # Drop bogus epoch-zero and pre-Twitter dates (Twitter launched March 2006)
    df = df[df["dt"].dt.year >= 2006]
    df = df.sort_values("dt")

    if df.empty:
        return _default_rates()

    if "text" not in df.columns:
        df["text"] = ""
    df["text"] = df["text"].fillna("").astype(str)
    df["sentiment"] = _sentiment_scores(df["text"])
    df["w"] = _recency_weights(df["dt"])
    recency_half_life_days = _RECENCY_HALFLIFE_DAYS

    # ── temporal features ─────────────────────────────────────────────────────
    df["hour"] = df["dt"].dt.hour
    df["weekday"] = df["dt"].dt.dayofweek  # 0=Mon … 6=Sun

    days_total = int((df["dt"].max() - df["dt"].min()).days) + 1

    # ── hourly rates ──────────────────────────────────────────────────────────
    # Count tweets at each UTC hour, divide by occurrences of that hour in the dataset.
    # Each hour h occurs once per calendar day → occurrences ≈ days_total.
    hourly_total = df.groupby("hour")["w"].sum()
    day_weights = (
        df[["date_only", "w"]]
        if "date_only" in df.columns
        else df.assign(date_only=df["dt"].dt.date)[["date_only", "w"]]
    )
    day_weights = day_weights.groupby("date_only")["w"].max()
    day_weight_sum = float(day_weights.sum())
    hourly_rates = [
        float(hourly_total.get(h, 0.0)) / max(day_weight_sum, 1e-9)
        for h in range(24)
    ]

    # ── weekday multipliers ───────────────────────────────────────────────────
    # Compute avg tweets/hour per weekday, then normalize to mean=1.
    day_meta = (
        df.assign(date_only=df["dt"].dt.date, weekday=df["dt"].dt.dayofweek)[["date_only", "weekday", "w"]]
        .groupby(["date_only", "weekday"], as_index=False)["w"]
        .max()
    )
    weekday_day_w = day_meta.groupby("weekday")["w"].sum()
    weekday_total = df.groupby("weekday")["w"].sum()
    weekday_rates_raw = [
        float(weekday_total.get(d, 0.0)) / max(float(weekday_day_w.get(d, 0.0)) * 24.0, 1e-9)
        for d in range(7)
    ]
    mean_wd = float(np.mean(weekday_rates_raw)) if any(weekday_rates_raw) else 1.0
    weekday_multipliers = [
        r / mean_wd if mean_wd > 0 else 1.0 for r in weekday_rates_raw
    ]

    # ── sentiment profiles ────────────────────────────────────────────────────
    hourly_sentiment_mean = []
    for h in range(24):
        grp = df[df["hour"] == h]
        hourly_sentiment_mean.append(_weighted_mean(grp["sentiment"], grp["w"]))

    weekday_sentiment_mean = []
    for d in range(7):
        grp = df[df["weekday"] == d]
        weekday_sentiment_mean.append(_weighted_mean(grp["sentiment"], grp["w"]))

    sentiment_mean = _weighted_mean(df["sentiment"], df["w"])
    sentiment_var = _weighted_mean((df["sentiment"] - sentiment_mean) ** 2, df["w"])
    sentiment_std = float(np.sqrt(max(0.0, sentiment_var)))
    sentiment_nonzero_ratio = _weighted_mean((df["sentiment"] != 0.0).astype(float), df["w"])

    # ── overall rate ──────────────────────────────────────────────────────────
    overall_rate = float(df["w"].sum()) / max(day_weight_sum * 24.0, 1e-9)

    # ── original tweet ratio ──────────────────────────────────────────────────
    rt_mask = df["is_retweet"].astype(bool)
    original_ratio = _weighted_mean((~rt_mask).astype(float), df["w"])
    rt_ratio = _weighted_mean(rt_mask.astype(float), df["w"])

    # ── sleep profile ─────────────────────────────────────────────────────────
    # For each UTC hour, fraction of day-slots with ZERO tweets → sleep probability.
    # Group by (date, hour), count per slot, find zero-tweet slots.
    df["date_only"] = df["dt"].dt.date
    slot_counts = df.groupby(["date_only", "hour"])["tweet_id"].count().reset_index()
    slot_counts.columns = ["date_only", "hour", "count"]

    # Build a full grid of (date, hour) slots
    all_dates = pd.date_range(df["dt"].min().date(), df["dt"].max().date(), freq="D")
    full_grid = pd.MultiIndex.from_product(
        [all_dates.date, range(24)], names=["date_only", "hour"]
    ).to_frame(index=False)
    merged = full_grid.merge(slot_counts, on=["date_only", "hour"], how="left")
    merged["count"] = merged["count"].fillna(0)
    day_weight_frame = day_weights.rename("day_w").reset_index()
    merged = merged.merge(day_weight_frame, on="date_only", how="left")
    merged["day_w"] = merged["day_w"].fillna(1.0)

    sleep_profile = [
        _weighted_mean(
            (merged[merged["hour"] == h]["count"] == 0).astype(float),
            merged[merged["hour"] == h]["day_w"],
        )
        for h in range(24)
    ]

    p_rt_60, p_orig_60 = _retweet_followup_probs(df, window_min=60)

    # ── over-dispersion (Var/Mean) of count vs window size ───────────────────
    # Elon's per-window count is heavily over-dispersed (clustering + day-to-day
    # regime changes). Poisson assumes phi=1; reality is phi=5-40 depending on
    # window length. Fit phi(T_h) ≈ a + b·ln(T_h) over recent data so the
    # predictor can use a Negative Binomial likelihood with empirical shape.
    disp_cutoff = df["dt"].max() - pd.Timedelta(days=_DISPERSION_WINDOW_DAYS)
    disp_df = df[df["dt"] >= disp_cutoff].copy()
    phi_points: list[tuple[int, float]] = []
    if len(disp_df) > 24:
        hourly = (
            disp_df.set_index("dt").resample("1h").size()
        )
        full_hours = pd.date_range(
            disp_df["dt"].min().floor("h"),
            disp_df["dt"].max().ceil("h"),
            freq="h",
        )
        hourly = hourly.reindex(full_hours, fill_value=0)
        for window_h in (1, 3, 6, 12, 24, 48, 72, 168):
            win = hourly.resample(f"{window_h}h").sum()
            mu = float(win.mean())
            var = float(win.var())
            if mu > 0 and len(win) >= 6 and var > 0:
                phi_points.append((window_h, var / mu))

    # Fit φ(T_h) = a + b·T_h. Linear-in-time fits the data well (variance from
    # day-to-day regime drift accumulates linearly with window length). The
    # log-T form was a worse fit empirically.
    if len(phi_points) >= 3:
        x = np.array([t for t, _ in phi_points], dtype=float)
        y = np.array([p for _, p in phi_points], dtype=float)
        b, a = np.polyfit(x, y, 1)
        phi_intercept = float(a)
        phi_slope_per_h = float(b)
    else:
        # Conservative defaults (matches Elon's recent 180d data)
        phi_intercept = 6.5
        phi_slope_per_h = 0.21

    return {
        "hourly_rates": hourly_rates,
        "weekday_multipliers": weekday_multipliers,
        "hourly_sentiment_mean": hourly_sentiment_mean,
        "weekday_sentiment_mean": weekday_sentiment_mean,
        "sentiment_mean": sentiment_mean,
        "sentiment_std": sentiment_std,
        "sentiment_nonzero_ratio": sentiment_nonzero_ratio,
        "retweet_followup_prob_60m": p_rt_60,
        "original_followup_prob_60m": p_orig_60,
        "overall_rate_per_hour": overall_rate,
        "original_tweet_ratio": original_ratio,
        "rt_excitation_alpha": rt_ratio,
        "sleep_profile": sleep_profile,
        "recency_half_life_days": recency_half_life_days,
        "dispersion_phi_intercept": phi_intercept,
        "dispersion_phi_slope_per_h": phi_slope_per_h,
        "dispersion_phi_points": [[int(t), float(p)] for t, p in phi_points],
        "dispersion_window_days": _DISPERSION_WINDOW_DAYS,
        "dataset_days": days_total,
        "total_tweets": int(len(df)),
        "source": str(parquet_path),
    }


def compute_and_cache(
    parquet_path: str | Path = _PARQUET,
    cache_path: str | Path = _CACHE,
    force: bool = False,
) -> dict[str, Any]:
    """Load from JSON cache if available; otherwise compute and save."""
    cp = Path(cache_path)
    if not force and cp.exists():
        try:
            with open(cp) as f:
                data = json.load(f)
            if data.get("total_tweets", 0) > 0:
                return data
        except Exception:
            pass

    rates = compute_rates(parquet_path)
    cp.parent.mkdir(parents=True, exist_ok=True)
    with open(cp, "w") as f:
        json.dump(rates, f, indent=2)
    return rates


def load_rates(cache_path: str | Path = _CACHE) -> dict[str, Any]:
    """
    Fast load from JSON cache. Falls back to compute_and_cache if missing.
    Call this at predictor startup — it's ~1ms when cache exists.
    """
    cp = Path(cache_path)
    if cp.exists():
        try:
            with open(cp) as f:
                return json.load(f)
        except Exception:
            pass
    return compute_and_cache(cache_path=cache_path)
