"""
Hawkes process utilities for tweet rate estimation.

Key insight: Elon's tweeting is self-exciting — each tweet (especially a retweet)
raises the probability of another tweet arriving soon. This models:
  - Burst behavior (long sessions of rapid-fire tweets)
  - Retweet → original tweet chains (RT α > orig α)
  - Exponential decay back to baseline

The intensity at time t (tweets/min) is:
    λ(t) = μ + Σ_{t_i < t} α_i * exp(-β * (t - t_i))

where α_i = alpha_rt if tweet i was a retweet, else alpha_orig.

Default parameters are rough estimates from the 183-day dataset.
Fit more precisely by calling fit_params() once you have enough historical data.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Sequence

import numpy as np

# Default parameters (empirically reasonable for Elon's 2025-2026 activity)
DEFAULT_MU = 0.032          # base rate tweets/min  (~1.9/hour)
DEFAULT_ALPHA_ORIG = 0.45   # self-excitation from original tweet
DEFAULT_ALPHA_RT = 0.80     # stronger excitation from retweet
DEFAULT_BETA = 0.18         # decay rate /min  (half-life ≈ 3.8 min)


def intensity_at(
    t_s: float,
    event_times_s: Sequence[float],
    is_retweet: Sequence[bool],
    mu: float = DEFAULT_MU,
    alpha_orig: float = DEFAULT_ALPHA_ORIG,
    alpha_rt: float = DEFAULT_ALPHA_RT,
    beta: float = DEFAULT_BETA,
) -> float:
    """
    Compute Hawkes intensity λ(t) at a single time t (epoch seconds).

    Args:
        t_s: query time in epoch seconds
        event_times_s: past event timestamps (epoch seconds), must be < t_s
        is_retweet: boolean mask parallel to event_times_s
        mu/alpha_orig/alpha_rt/beta: Hawkes parameters (per-minute units)

    Returns:
        λ(t) in tweets/minute
    """
    lam = mu
    for ts, rt in zip(event_times_s, is_retweet):
        if ts >= t_s:
            continue
        dt_min = (t_s - ts) / 60.0
        alpha = alpha_rt if rt else alpha_orig
        lam += alpha * math.exp(-beta * dt_min)
    return max(0.0, lam)


def expected_count(
    now_s: float,
    end_s: float,
    event_times_s: Sequence[float],
    is_retweet: Sequence[bool],
    mu_per_hour: float = DEFAULT_MU * 60,
    alpha_orig: float = DEFAULT_ALPHA_ORIG,
    alpha_rt: float = DEFAULT_ALPHA_RT,
    beta_per_hour: float = DEFAULT_BETA * 60,
    n_steps: int = 60,
) -> float:
    """
    Estimate E[tweets from now until end] by numerically integrating λ(t).

    Uses a trapezoidal approximation over n_steps evenly spaced in (now, end).
    The Hawkes excitation decays quickly (half-life ~3.8 min) so the integral
    converges well with ~60 steps per remaining hour.

    Args:
        now_s: current epoch seconds
        end_s: window end epoch seconds
        event_times_s: recent past event timestamps (within last ~30 min is enough)
        is_retweet: boolean mask parallel to event_times_s
        mu_per_hour: base rate in tweets/hour (converted internally to /min)
        alpha_orig/alpha_rt/beta_per_hour: Hawkes params (per-hour units)
        n_steps: integration steps

    Returns:
        Expected additional tweet count in [now, end]
    """
    if end_s <= now_s:
        return 0.0

    # Convert per-hour params to per-minute for internal computation.
    mu = mu_per_hour / 60.0
    beta = beta_per_hour / 60.0

    hours_left = (end_s - now_s) / 3600.0
    steps = max(n_steps, int(hours_left * 60))  # at least 1 step per minute
    ts_grid = np.linspace(now_s, end_s, steps)
    dt = (end_s - now_s) / max(steps - 1, 1)  # seconds per step

    # Vectorised computation of Σ alpha_i * exp(-beta * dt_min)
    et = np.asarray(event_times_s, dtype=np.float64)
    rt = np.asarray(is_retweet, dtype=bool)
    alphas = np.where(rt, alpha_rt, alpha_orig)

    total = 0.0
    for t in ts_grid:
        past = et < t
        if past.any():
            dt_min = (t - et[past]) / 60.0
            excitation = float(np.sum(alphas[past] * np.exp(-beta * dt_min)))
        else:
            excitation = 0.0
        lam = mu + excitation  # tweets/min
        total += lam * (dt / 60.0)  # convert dt from seconds to minutes

    return max(0.0, total)


def recent_excitation(
    recent_tweets: list[dict],
    now_s: float | None = None,
    alpha_orig: float = DEFAULT_ALPHA_ORIG,
    alpha_rt: float = DEFAULT_ALPHA_RT,
    beta_per_min: float = DEFAULT_BETA,
    window_hours: float = 4.0,
) -> float:
    """
    Compute the current Hawkes excitation term from a list of recent tweet dicts.

    Each dict should have:
        'tweet_created_at' or 'detected_at': ISO timestamp string
        (optional) 'tweet_text': used to infer is_retweet via 'RT @' prefix

    Returns:
        Excitation contribution λ_excitation in tweets/min at `now_s`.
    """
    if not recent_tweets:
        return 0.0

    if now_s is None:
        now_s = time.time()

    cutoff = now_s - window_hours * 3600

    excitation = 0.0
    for row in recent_tweets:
        ts_str = row.get("tweet_created_at") or row.get("detected_at")
        if not ts_str:
            continue
        try:
            dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).astimezone(timezone.utc)
            ts = dt.timestamp()
        except Exception:
            continue

        if ts < cutoff or ts >= now_s:
            continue

        text = str(row.get("tweet_text") or "")
        is_rt = bool(row.get("is_retweet")) or text.startswith("RT @")
        alpha = alpha_rt if is_rt else alpha_orig
        dt_min = (now_s - ts) / 60.0
        excitation += alpha * math.exp(-beta_per_min * dt_min)

    return excitation
