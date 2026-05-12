"""
Feature engineering for model ingestion.

Input:  standardized tweet DataFrame (BASE_COLS + optional sentiment).
Output: three DataFrames for different modelling granularities.

    per_tweet  — one row per tweet, all individual tweet features
    daily      — one row per day, aggregated features + sleep proxy
    weekly     — one row per week, with target tweet_count (for the Polymarket model)

Usage:
    from tweet_features import build_feature_dfs
    per_tweet, daily, weekly = build_feature_dfs(merged_df)
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

# Gap in seconds that marks a new tweet "session" (Elon went quiet for this long).
SESSION_GAP_S = 3 * 3600  # 3 hours
_SENTIMENT_ANALYZER = None
_SENTIMENT_ANALYZER_READY = False


# ── helpers ──────────────────────────────────────────────────────────────────

def _ensure_datetime_utc(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["datetime_utc"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    return df.dropna(subset=["datetime_utc"]).sort_values("datetime_utc").reset_index(drop=True)


def _vader_sentiment(texts: pd.Series) -> pd.Series:
    global _SENTIMENT_ANALYZER
    global _SENTIMENT_ANALYZER_READY

    if not _SENTIMENT_ANALYZER_READY:
        analyzer = None
        try:
            from nltk.sentiment.vader import SentimentIntensityAnalyzer as _NltkSIA

            analyzer = _NltkSIA()
        except Exception:
            try:
                from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _PkgSIA

                analyzer = _PkgSIA()
            except Exception:
                analyzer = None

        _SENTIMENT_ANALYZER = analyzer
        _SENTIMENT_ANALYZER_READY = True

    if _SENTIMENT_ANALYZER is None:
        return pd.Series(0.0, index=texts.index, dtype=float)

    return texts.apply(
        lambda t: float(_SENTIMENT_ANALYZER.polarity_scores(str(t)).get("compound", 0.0))
    )


# ── per-tweet features ────────────────────────────────────────────────────────

def add_temporal_features(df: pd.DataFrame) -> pd.DataFrame:
    dt = df["datetime_utc"]
    dt_naive = dt.dt.tz_convert("UTC").dt.tz_localize(None)
    df["hour_utc"] = dt.dt.hour
    df["weekday"] = dt.dt.dayofweek          # 0=Mon … 6=Sun
    df["weekday_name"] = dt.dt.day_name()
    df["month"] = dt.dt.month
    df["year"] = dt.dt.year
    df["iso_week"] = dt.dt.isocalendar().week.astype(int)
    df["week_start"] = dt_naive.dt.to_period("W").dt.start_time.dt.tz_localize("UTC")
    df["date_only"] = dt.dt.date
    return df


def add_gap_features(df: pd.DataFrame) -> pd.DataFrame:
    """Inter-tweet gap and session labelling."""
    dt = df["datetime_utc"]
    gap = (dt - dt.shift(1)).dt.total_seconds().fillna(0.0)
    df["gap_since_prev_s"] = gap.clip(lower=0)
    df["gap_since_prev_h"] = df["gap_since_prev_s"] / 3600.0

    # Session: new session whenever gap > SESSION_GAP_S.
    session_boundary = (df["gap_since_prev_s"] > SESSION_GAP_S) | (df.index == 0)
    df["session_id"] = session_boundary.cumsum()
    df["session_tweet_num"] = df.groupby("session_id").cumcount() + 1

    # Longest gap in the past 24 h rolling window (sleep proxy per tweet).
    df["max_gap_prev_24h_h"] = (
        df["gap_since_prev_h"]
        .rolling(window=200, min_periods=1)  # rough proxy; refined in daily agg
        .max()
    )
    return df


def add_rolling_counts(df: pd.DataFrame) -> pd.DataFrame:
    """Rolling tweet counts over fixed windows — works on sorted datetime index."""
    dt_idx = df.set_index("datetime_utc")

    for window, label in [("1h", "1h"), ("6h", "6h"), ("12h", "12h"), ("24h", "24h")]:
        counts = dt_idx["tweet_id"].resample("1min").count().rolling(window, min_periods=1).sum()
        df[f"tweet_count_{label}"] = (
            counts.reindex(df["datetime_utc"].dt.floor("1min"), method="nearest").values
        )

    # Retweet rolling count over last 1 h.
    rt_idx = dt_idx["is_retweet"].astype(int)
    rt_roll = rt_idx.resample("1min").sum().rolling("1h", min_periods=1).sum()
    df["rt_count_1h"] = (
        rt_roll.reindex(df["datetime_utc"].dt.floor("1min"), method="nearest").values
    )
    return df


def add_sentiment_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add VADER sentiment if not present; add rolling sentiment stats."""
    if "sentiment" not in df.columns:
        df["sentiment"] = _vader_sentiment(df["text"])

    df["sentiment_7d_roll"] = (
        df.set_index("datetime_utc")["sentiment"]
        .rolling("7D", min_periods=1)
        .mean()
        .values
    )
    df["sentiment_volatility_7d"] = (
        df.set_index("datetime_utc")["sentiment"]
        .rolling("7D", min_periods=1)
        .std()
        .fillna(0.0)
        .values
    )
    return df


def build_per_tweet(raw: pd.DataFrame) -> pd.DataFrame:
    """Run all per-tweet feature engineering steps."""
    df = _ensure_datetime_utc(raw)
    df = add_temporal_features(df)
    df = add_gap_features(df)
    df = add_rolling_counts(df)
    df = add_sentiment_features(df)
    return df


# ── daily aggregate features ──────────────────────────────────────────────────

def build_daily(per_tweet: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate per-tweet features to one row per calendar day (UTC).

    Sleep proxy: largest gap between consecutive tweets on each day.
    Active hours: distinct hours that had at least one tweet.
    """
    pt = per_tweet.copy()
    pt["date_only"] = pd.to_datetime(pt["datetime_utc"]).dt.date

    agg = (
        pt.groupby("date_only")
        .agg(
            tweet_count=("tweet_id", "nunique"),
            original_count=("is_retweet", lambda x: (~x.astype(bool)).sum()),
            rt_count=("is_retweet", "sum"),
            avg_sentiment=("sentiment", "mean"),
            std_sentiment=("sentiment", "std"),
            avg_gap_h=("gap_since_prev_h", "mean"),
            max_gap_h=("gap_since_prev_h", "max"),   # sleep proxy
            active_hours=("hour_utc", "nunique"),
            first_tweet_hour=("hour_utc", "min"),
            last_tweet_hour=("hour_utc", "max"),
            session_count=("session_id", "nunique"),
            avg_words=("text", lambda x: x.str.split().str.len().mean()),
        )
        .reset_index()
    )

    agg["rt_ratio"] = agg["rt_count"] / agg["tweet_count"].clip(lower=1)
    agg["std_sentiment"] = agg["std_sentiment"].fillna(0.0)

    # 7-day rolling features on the daily series.
    agg = agg.sort_values("date_only").reset_index(drop=True)
    agg["tweet_count_7d_avg"] = agg["tweet_count"].rolling(7, min_periods=1).mean()
    agg["tweet_count_7d_std"] = agg["tweet_count"].rolling(7, min_periods=1).std().fillna(0.0)
    agg["rt_ratio_7d_avg"] = agg["rt_ratio"].rolling(7, min_periods=1).mean()
    agg["sentiment_7d_avg"] = agg["avg_sentiment"].rolling(7, min_periods=1).mean()

    return agg


# ── weekly aggregate features ─────────────────────────────────────────────────

def build_weekly(daily: pd.DataFrame, per_tweet: pd.DataFrame) -> pd.DataFrame:
    """
    One row per ISO week. Target variable: tweet_count (total tweets that week).

    Includes lag features for the model:
      - prev_week_count, prev_2w_count
      - avg of daily max_gap_h (sleep regularity)
      - retweet ratio (excitation signal)
    """
    pt = per_tweet.copy()
    dt = pd.to_datetime(pt["datetime_utc"], utc=True, errors="coerce")
    dt_naive = dt.dt.tz_convert("UTC").dt.tz_localize(None)
    pt["week_start"] = dt_naive.dt.to_period("W").dt.start_time.dt.tz_localize("UTC")

    weekly_raw = (
        pt.groupby("week_start")
        .agg(
            tweet_count=("tweet_id", "nunique"),
            original_count=("is_retweet", lambda x: (~x.astype(bool)).sum()),
            rt_count=("is_retweet", "sum"),
            avg_sentiment=("sentiment", "mean"),
            std_sentiment=("sentiment", "std"),
            avg_max_gap_h=("gap_since_prev_h", "max"),
            active_days=("date_only", "nunique"),
            session_count=("session_id", "nunique"),
        )
        .reset_index()
    )

    weekly_raw = weekly_raw.sort_values("week_start").reset_index(drop=True)
    weekly_raw["rt_ratio"] = weekly_raw["rt_count"] / weekly_raw["tweet_count"].clip(lower=1)
    weekly_raw["std_sentiment"] = weekly_raw["std_sentiment"].fillna(0.0)

    # Lag features.
    weekly_raw["prev_week_count"] = weekly_raw["tweet_count"].shift(1)
    weekly_raw["prev_2w_count"] = weekly_raw["tweet_count"].shift(2)
    weekly_raw["prev_week_rt_ratio"] = weekly_raw["rt_ratio"].shift(1)
    weekly_raw["tweet_count_4w_avg"] = weekly_raw["tweet_count"].rolling(4, min_periods=1).mean()

    # Week-of-year for seasonality.
    weekly_raw["week_of_year"] = pd.to_datetime(weekly_raw["week_start"]).dt.isocalendar().week.astype(int)
    weekly_raw["day_of_week_bias"] = weekly_raw["week_start"].dt.dayofweek  # always 0=Mon for ISO week

    return weekly_raw


# ── top-level builder ─────────────────────────────────────────────────────────

def build_feature_dfs(
    raw: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Full feature engineering pipeline.

    Returns:
        per_tweet  — enriched tweet-level DataFrame
        daily      — daily aggregated features
        weekly     — weekly aggregated features (model input + target)
    """
    per_tweet = build_per_tweet(raw)
    daily = build_daily(per_tweet)
    weekly = build_weekly(daily, per_tweet)
    return per_tweet, daily, weekly


# ── Hawkes excitation helper ──────────────────────────────────────────────────

def hawkes_excitation(
    timestamps_s: np.ndarray,
    is_retweet: np.ndarray,
    mu: float = 0.04,       # base rate (tweets/min)
    alpha_orig: float = 0.6, # self-excitation from original tweets
    alpha_rt: float = 0.9,   # stronger excitation from retweets
    beta: float = 0.1,       # decay rate (1/min)
) -> np.ndarray:
    """
    Compute instantaneous Hawkes intensity λ(t) for each tweet.

    This captures the self-exciting nature of Elon's tweeting:
    each tweet (especially a retweet) raises the probability of the next.

    Parameters follow the standard univariate Hawkes process with two marks
    (original tweet vs retweet).
    """
    n = len(timestamps_s)
    intensity = np.full(n, mu)
    for i in range(1, n):
        dt = (timestamps_s[i] - timestamps_s[:i]) / 60.0  # convert to minutes
        alpha = np.where(is_retweet[:i], alpha_rt, alpha_orig)
        intensity[i] = mu + np.sum(alpha * np.exp(-beta * dt))
    return intensity


def add_hawkes_intensity(per_tweet: pd.DataFrame) -> pd.DataFrame:
    """Append hawkes_intensity column to per_tweet DataFrame."""
    ts = per_tweet["datetime_utc"].astype(np.int64) / 1e9  # epoch seconds
    rt = per_tweet["is_retweet"].astype(bool).values
    per_tweet = per_tweet.copy()
    per_tweet["hawkes_intensity"] = hawkes_intensity(ts.values, rt)
    return per_tweet


def hawkes_intensity(timestamps_s: np.ndarray, is_retweet: np.ndarray) -> np.ndarray:
    return hawkes_excitation(timestamps_s, is_retweet)
