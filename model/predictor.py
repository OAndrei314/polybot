"""
Enhanced prediction model.

Improvements over the original flat-rate Poisson predictor:

1. Calibrated hourly rates — instead of a flat 1.6 tweets/hour, the expected
   remaining tweets are computed as Σ λ(h, weekday) for each remaining hour,
   where λ(h, d) is derived from the full historical tweet dataset.

2. Sleep-aware projection — hours with high sleep probability (from calibration)
   contribute near-zero to the remaining tweet expectation.

3. Hawkes excitation — recent tweet bursts (especially retweets) increase the
   short-term rate above the historical baseline, decaying exponentially.

4. Sentiment multiplier — unchanged from v1: VADER sentiment of recent tweets
   scales the base rate up/down by ±35%.

The Poisson CDF machinery for P(bracket | current_count, λ) is preserved exactly
so the rest of the system (paper_trade, terminal) sees no API changes.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Any

from model.calibration import load_rates
from model.hawkes import DEFAULT_ALPHA_ORIG, DEFAULT_BETA, recent_excitation

# Hawkes β is in tweets/min — convert to per-hour for the rate-projection math.
_HAWKES_BETA_PER_HOUR = DEFAULT_BETA * 60.0  # ≈ 10.8 /h, half-life ≈ 3.85 min

# Bayesian Gamma-Poisson update: how many hours of "equivalent observation"
# weight does the historical prior carry? Higher = trust historical more, lower
# = trust the in-event observed pace faster. 12h: data from the 2026-05-02 live
# run showed winners had median ~16h and losers ~24h — halving the default
# keeps the model responsive to burst activity like the 06:17 UTC Elon spike.
_RATE_PRIOR_STRENGTH_H = 12.0

# NB shape parameter floor: even tiny estimated k blows up the likelihood with
# very wide tails. Floor at 0.5 to keep predictions meaningful.
_NB_SHAPE_FLOOR = 0.5


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))


class PredictionModel:
    """
    Calibrated tweet-count predictor for Polymarket bracket markets.

    Drop-in replacement for the original predictor.PredictionModel —
    same predict() API, but backed by historical hourly/weekday rate profiles
    and Hawkes excitation from live tweet bursts.
    """

    _FALLBACK_POSITIVE_WORDS = {
        "great", "good", "amazing", "love", "wow", "new", "launch",
        "tesla", "spacex", "starship", "grok", "xai", "soon", "yes", "win", "bull",
    }
    _FALLBACK_NEGATIVE_WORDS = {
        "bad", "sad", "problem", "delay", "lawsuit", "war", "fail",
        "no", "loss", "down", "bear", "cancel",
    }

    def __init__(
        self,
        dataset_path: str = "tweet_dataset.jsonl",
        dataset_max_rows: int = 4000,
        dataset_max_bytes: int = 1_000_000,
        calibration_cache: str = "data/calibration_rates.json",
        rate_prior_strength_h: float = _RATE_PRIOR_STRENGTH_H,
        nb_phi_override: float | None = None,
        nb_phi_scale: float = 1.0,
        hawkes_alpha_scale: float = 1.0,
        hawkes_beta_per_min: float = DEFAULT_BETA,
    ) -> None:
        self.dataset_path = dataset_path
        self.dataset_max_rows = max(100, int(dataset_max_rows))
        self.dataset_max_bytes = max(50_000, int(dataset_max_bytes))
        self.rows: list[dict[str, Any]] = []
        self._seen_ids: set[str] = set()
        self._load_dataset()

        # Historical calibration — fast JSON load, falls back to defaults.
        self._cal = load_rates(calibration_cache)
        self._hourly_rates: list[float] = self._cal.get("hourly_rates", [1.6] * 24)
        self._weekday_mults: list[float] = self._cal.get("weekday_multipliers", [1.0] * 7)
        self._sleep_profile: list[float] = self._cal.get("sleep_profile", [0.0] * 24)
        self._hourly_sent_mean: list[float] = self._cal.get("hourly_sentiment_mean", [0.0] * 24)
        self._weekday_sent_mean: list[float] = self._cal.get("weekday_sentiment_mean", [0.0] * 7)
        self._sentiment_mean: float = float(self._cal.get("sentiment_mean", 0.0) or 0.0)
        self._retweet_followup_prob_60m: float = float(
            self._cal.get("retweet_followup_prob_60m", 0.0) or 0.0
        )
        self._original_followup_prob_60m: float = float(
            self._cal.get("original_followup_prob_60m", 0.0) or 0.0
        )
        self._overall_rate: float = self._cal.get("overall_rate_per_hour", 1.6)
        self._rt_alpha: float = self._cal.get("rt_excitation_alpha", 0.57)
        self._phi_intercept: float = float(self._cal.get("dispersion_phi_intercept", 6.5) or 6.5)
        self._phi_slope_per_h: float = float(
            self._cal.get("dispersion_phi_slope_per_h", 0.21) or 0.21
        )
        self._rate_prior_strength_h = max(1e-6, float(rate_prior_strength_h))
        self._nb_phi_override = (
            max(1.001, float(nb_phi_override)) if nb_phi_override is not None else None
        )
        self._nb_phi_scale = max(0.01, float(nb_phi_scale))
        self._hawkes_alpha_scale = max(0.0, float(hawkes_alpha_scale))
        self._hawkes_beta_per_min = max(1e-6, float(hawkes_beta_per_min))
        self._hawkes_beta_per_hour = self._hawkes_beta_per_min * 60.0
        self._sentiment_analyzer = self._init_sentiment_analyzer()

    # ── dataset persistence ───────────────────────────────────────────────────

    def _load_dataset(self) -> None:
        if not os.path.exists(self.dataset_path):
            return
        try:
            with open(self.dataset_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    tid = str(row.get("tweet_id") or "")
                    if tid:
                        self._seen_ids.add(tid)
                    self.rows.append(row)
        except Exception:
            self.rows = []
            self._seen_ids = set()

    def _append_row(self, row: dict[str, Any]) -> None:
        self.rows.append(row)
        with open(self.dataset_path, "a") as f:
            f.write(json.dumps(row) + "\n")
        self._trim_dataset_if_needed()

    def _trim_dataset_if_needed(self) -> None:
        if len(self.rows) > self.dataset_max_rows:
            self.rows = self.rows[-self.dataset_max_rows:]
            self._seen_ids = {str(r.get("tweet_id") or "") for r in self.rows if r.get("tweet_id")}
            with open(self.dataset_path, "w") as f:
                for r in self.rows:
                    f.write(json.dumps(r) + "\n")
            return
        try:
            size = os.path.getsize(self.dataset_path)
        except OSError:
            return
        if size <= self.dataset_max_bytes:
            return
        self.rows = self.rows[-self.dataset_max_rows:]
        self._seen_ids = {str(r.get("tweet_id") or "") for r in self.rows if r.get("tweet_id")}
        with open(self.dataset_path, "w") as f:
            for r in self.rows:
                f.write(json.dumps(r) + "\n")

    # ── sentiment ─────────────────────────────────────────────────────────────

    def _init_sentiment_analyzer(self) -> Any:
        try:
            from nltk.sentiment.vader import SentimentIntensityAnalyzer as _NltkSIA

            return _NltkSIA()
        except Exception:
            try:
                from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _PkgSIA

                return _PkgSIA()
            except Exception:
                return None

    def _fallback_sentiment_score(self, text: str) -> float:
        words = re.findall(r"[A-Za-z']+", text.lower())
        if not words:
            return 0.0
        pos = sum(1 for w in words if w in self._FALLBACK_POSITIVE_WORDS)
        neg = sum(1 for w in words if w in self._FALLBACK_NEGATIVE_WORDS)
        raw = (pos - neg) / max(len(words), 1)
        return _clamp(raw * 5.0, -1.0, 1.0)

    def _sentiment_score(self, text: str) -> float:
        txt = str(text or "")
        if self._sentiment_analyzer is not None:
            try:
                return float(self._sentiment_analyzer.polarity_scores(txt).get("compound", 0.0))
            except Exception:
                pass
        return self._fallback_sentiment_score(txt)

    @staticmethod
    def _event_ts(row: dict[str, Any]) -> datetime | None:
        return _parse_iso(row.get("tweet_created_at")) or _parse_iso(row.get("detected_at"))

    def _recent_rows(self, minutes: float) -> list[dict[str, Any]]:
        now = _now_utc()
        cutoff = now.timestamp() - (minutes * 60.0)
        out: list[dict[str, Any]] = []
        for row in self.rows:
            ts = self._event_ts(row)
            if ts is not None and ts.timestamp() >= cutoff:
                out.append(row)
        return out

    def _avg_sentiment(self, window_minutes: float = 180.0) -> float:
        recent = self._recent_rows(window_minutes)
        if not recent:
            return 0.0
        vals = [float(r.get("sentiment") or 0.0) for r in recent]
        return _clamp(sum(vals) / len(vals), -1.0, 1.0)

    def _recent_retweet_share(self, window_minutes: float = 180.0) -> float:
        recent = self._recent_rows(window_minutes)
        if not recent:
            return 0.0
        rt_count = 0
        for row in recent:
            is_rt = bool(row.get("is_retweet")) or str(row.get("tweet_text") or "").startswith("RT @")
            if is_rt:
                rt_count += 1
        return _clamp(rt_count / max(1, len(recent)), 0.0, 1.0)

    def _historical_sentiment_bias(self, ts: datetime) -> float:
        try:
            hour_mean = float(self._hourly_sent_mean[ts.hour])
        except Exception:
            hour_mean = 0.0
        try:
            weekday_mean = float(self._weekday_sent_mean[ts.weekday()])
        except Exception:
            weekday_mean = 0.0
        baseline = float(self._sentiment_mean)
        return _clamp(((hour_mean + weekday_mean) / 2.0) - baseline, -1.0, 1.0)

    def _density_per_hour(self, window_minutes: float = 60.0) -> float:
        recent = self._recent_rows(window_minutes)
        return len(recent) / (window_minutes / 60.0) if window_minutes > 0 else 0.0

    def update_dataset(self, tweet_state: dict[str, Any]) -> None:
        latest = tweet_state.get("latest_tweet") or {}
        tweet_id = str(latest.get("tweet_id") or "")
        if not tweet_id or tweet_id in self._seen_ids:
            return
        created = _parse_iso(latest.get("tweet_created_at"))
        detected = _parse_iso(latest.get("detected_at")) or _now_utc()
        text = str(latest.get("tweet_text") or "")
        row = {
            "tweet_id": tweet_id,
            "tweet_text": text,
            "tweet_created_at": created.isoformat() if created else None,
            "detected_at": detected.isoformat(),
            "is_retweet": bool(text.startswith("RT @")),
            "sentiment": round(self._sentiment_score(text), 4),
        }
        self._seen_ids.add(tweet_id)
        self._append_row(row)

    # ── calibrated rate projection ────────────────────────────────────────────

    def _calibrated_lambda(
        self,
        start: datetime,
        end: datetime,
        sentiment_mult: float,
        hawkes_excitation: float,
    ) -> float:
        """
        Compute the expected number of tweets in [start, end] using:
          1. Historical hourly rate profile (per UTC hour + weekday scaling)
          2. Sentiment multiplier on the historical baseline
          3. Hawkes excitation integrated with its calibrated decay rate

        Note: hourly_rates[h] is already the *average* tweets/hour at UTC hour h
        across all days (including silent ones), so it implicitly encodes sleep.
        Do NOT multiply by (1 - sleep_profile[h]) — that double-discounts.
        """
        now = _now_utc()
        if end <= now:
            return 0.0

        cursor = max(start, now)
        # Total Hawkes contribution = ∫₀^T_rem α·exp(-β·t) dt = α·(1 - exp(-β·T))/β
        # The Hawkes excitation decays with calibrated β (≈ 10.8/h, half-life
        # ≈ 3.85 min), so it contributes a small one-shot boost over ~tens of
        # minutes — not a slow 50%/hour decay across the whole window.
        remaining_h = (end - cursor).total_seconds() / 3600.0
        if remaining_h > 0 and hawkes_excitation > 0:
            hawkes_contrib = hawkes_excitation * (
                1.0 - math.exp(-self._hawkes_beta_per_hour * remaining_h)
            ) / self._hawkes_beta_per_hour
        else:
            hawkes_contrib = 0.0

        total = hawkes_contrib

        # Walk hour-by-hour summing the calibrated rate × hour fraction.
        while cursor < end:
            next_hour = cursor.replace(minute=0, second=0, microsecond=0)
            next_hour = next_hour.replace(hour=(next_hour.hour + 1) % 24)
            if next_hour.hour == 0:
                import datetime as _dt
                next_hour = next_hour + _dt.timedelta(days=1)
            slot_end = min(next_hour, end)
            frac = (slot_end - cursor).total_seconds() / 3600.0

            h = cursor.hour
            d = cursor.weekday()
            base_rate = self._hourly_rates[h] * self._weekday_mults[d]
            total += max(0.0, base_rate * sentiment_mult * frac)
            cursor = slot_end

        return total

    # ── Negative Binomial probability ────────────────────────────────────────
    #
    # Elon's per-window count is heavily over-dispersed (Var/Mean ≈ 5-40
    # depending on window size). Pure Poisson assumes variance = mean and so
    # under-estimates tail probabilities by 1-2 orders of magnitude. We use
    # NB(μ, k) where k (size param) shrinks dispersion: as k→∞, NB→Poisson.
    # k is calibrated empirically: k = μ / (φ - 1) with φ(T_h) ≈ a + b·ln(T_h).

    def _phi_for_window(self, hours: float) -> float:
        if self._nb_phi_override is not None:
            return self._nb_phi_override
        # φ(T) = a + b·T  (variance from regime drift accumulates linearly).
        # Floor at 1.05 to keep NB regime; cap at 80 to avoid pathologically
        # wide tails on very long windows (>2 weeks).
        phi = self._phi_intercept + self._phi_slope_per_h * max(1.0, hours)
        return max(1.05, min(80.0, phi * self._nb_phi_scale))

    def _nb_size_from_lambda(self, lam: float, hours_remaining: float) -> float:
        if lam <= 0:
            return float("inf")
        phi = self._phi_for_window(hours_remaining)
        if phi <= 1.001:
            return float("inf")  # Poisson regime
        return max(_NB_SHAPE_FLOOR, lam / (phi - 1.0))

    @staticmethod
    def _nb_log_pmf(k_obs: int, mu: float, size: float) -> float:
        # NB parameterized by mean μ and shape `size`:
        #   p = size / (size + μ),  q = 1 - p
        #   log P(N = k) = lgamma(k+size) - lgamma(k+1) - lgamma(size)
        #                  + size·log(p) + k·log(q)
        if k_obs < 0:
            return float("-inf")
        if size == float("inf"):
            # Pure Poisson fallback
            if mu <= 0:
                return 0.0 if k_obs == 0 else float("-inf")
            return k_obs * math.log(mu) - mu - math.lgamma(k_obs + 1)
        if mu <= 0:
            return 0.0 if k_obs == 0 else float("-inf")
        p = size / (size + mu)
        if p <= 0 or p >= 1:
            return float("-inf")
        return (
            math.lgamma(k_obs + size)
            - math.lgamma(k_obs + 1)
            - math.lgamma(size)
            + size * math.log(p)
            + k_obs * math.log(1.0 - p)
        )

    def _range_probability(
        self,
        low: int,
        high: int,
        current: int,
        lam: float,
        hours_remaining: float,
    ) -> float:
        if current > high:
            return 0.0
        min_more = max(0, low - current)
        max_more = high - current
        if max_more < 0:
            return 0.0
        if lam <= 0:
            return 1.0 if low <= current <= high else 0.0

        size = self._nb_size_from_lambda(lam, hours_remaining)

        # Dynamic upper bound for the open-tail bracket: 8 std-devs above mean
        # is enough for any practical purpose (NB std ≈ √(μ + μ²/k)).
        if high >= 9999:
            std = math.sqrt(lam + (lam * lam / size if size != float("inf") else 0.0))
            upper = max(min_more + 1, int(lam + 12.0 * std))
            cdf = sum(math.exp(self._nb_log_pmf(k, lam, size)) for k in range(min_more))
            return _clamp(1.0 - cdf, 0.0, 1.0)

        # Sum over [min_more, max_more] inclusive
        total = 0.0
        for k in range(min_more, max_more + 1):
            total += math.exp(self._nb_log_pmf(k, lam, size))
        return _clamp(total, 0.0, 1.0)

    @staticmethod
    def _parse_bounds(label: str) -> tuple[int, int] | None:
        m = re.match(r"^(\d+)-(\d+)$", label.strip())
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.match(r"^(\d+)\+$", label.strip())
        if m:
            return int(m.group(1)), 9999
        return None

    @staticmethod
    def _market_mid(yes_buy: float | None, yes_sell: float | None) -> float | None:
        if yes_buy is not None and yes_sell is not None:
            return (yes_buy + yes_sell) / 2.0
        return yes_buy or yes_sell

    # ── public API ────────────────────────────────────────────────────────────

    def predict(
        self,
        price_rows: list[dict[str, Any]],
        tweet_state: dict[str, Any],
        health_state: dict[str, Any],
        edge_threshold: float = 0.03,
    ) -> dict[str, Any]:
        self.update_dataset(tweet_state)

        now = _now_utc()
        live_sentiment = self._avg_sentiment(window_minutes=180.0)
        hist_bias = self._historical_sentiment_bias(now)
        sentiment = _clamp((0.7 * live_sentiment) + (0.3 * hist_bias), -1.0, 1.0)
        density_h = self._density_per_hour(window_minutes=240.0)
        sentiment_mult = 1.0 + (0.35 * sentiment)

        # Hawkes excitation from recent tweet bursts (tweets/hour contribution).
        hawkes = recent_excitation(
            recent_tweets=self.rows[-200:],
            alpha_orig=DEFAULT_ALPHA_ORIG * self._hawkes_alpha_scale,
            alpha_rt=self._rt_alpha * self._hawkes_alpha_scale,
            beta_per_min=self._hawkes_beta_per_min,
        ) * 60.0  # convert /min to /hour
        recent_rt_share = self._recent_retweet_share(window_minutes=180.0)
        followup_ratio = (
            self._retweet_followup_prob_60m / max(self._original_followup_prob_60m, 1e-6)
            if self._retweet_followup_prob_60m > 0 and self._original_followup_prob_60m > 0
            else 1.0
        )
        hawkes *= 1.0 + ((followup_ratio - 1.0) * recent_rt_share * 0.35)

        events = health_state.get("events") or {}
        xtracker_counts = tweet_state.get("xtracker_period_counts") or {}
        event_live_counts = tweet_state.get("event_live_counts") or {}

        predictions: list[dict[str, Any]] = []

        for row in price_rows:
            label = str(row.get("label") or "")
            bounds = self._parse_bounds(label)
            if bounds is None:
                continue

            low, high = bounds
            slug = str(row.get("event_slug") or "")
            evt = events.get(slug) or {}

            if isinstance(event_live_counts.get(slug), int):
                current_count = int(event_live_counts[slug])
            elif evt.get("ok") and isinstance(evt.get("count"), int):
                current_count = int(evt["count"])
            else:
                c = xtracker_counts.get(slug)
                current_count = int(c) if isinstance(c, int) else None

            if current_count is None:
                continue

            start = _parse_iso(evt.get("start"))
            end = _parse_iso(evt.get("end"))
            if start is None or end is None:
                continue

            lam_hist = self._calibrated_lambda(start, end, sentiment_mult, hawkes)

            # Gamma-Poisson Bayesian update of the rate level. The historical
            # projection is the prior mean; the in-event observed pace updates
            # it using closed-form conjugate posterior:
            #   prior λ ~ Gamma(α₀, β₀), with α₀/β₀ = historical_rate_per_h
            #   observe N tweets in T_obs hours
            #   posterior λ ~ Gamma(α₀+N, β₀+T_obs)
            #   posterior mean rate = (α₀+N)/(β₀+T_obs)
            # We then rescale the *shape* of the historical projection by the
            # ratio of posterior to prior mean. This preserves the hourly /
            # weekday seasonality while shifting the level.
            elapsed_h = max(0.0, (now - start).total_seconds() / 3600.0)
            remaining_h = max(0.0, (end - now).total_seconds() / 3600.0)
            historical_total_h = elapsed_h + remaining_h
            if (
                elapsed_h >= 1.0
                and remaining_h > 0.0
                and current_count >= 0
                and historical_total_h > 0
                and lam_hist > 0
            ):
                hist_rate_h = (lam_hist + (lam_hist * elapsed_h / max(remaining_h, 1e-9))) / (
                    historical_total_h
                ) if remaining_h > 0 else self._overall_rate
                # Cleaner alternative: use the per-event historical rate directly
                # (lam_hist/remaining_h) since the prior is built for the remaining
                # window already. Approximate prior mean per hour:
                prior_rate_h = max(1e-6, lam_hist / max(remaining_h, 1e-9))
                alpha_0 = prior_rate_h * self._rate_prior_strength_h
                # Posterior mean (per hour)
                posterior_rate_h = (alpha_0 + float(current_count)) / (
                    self._rate_prior_strength_h + elapsed_h
                )
                rescale = posterior_rate_h / prior_rate_h
                # Cap the rescale so a freak slow/burst day cannot completely
                # override the historical model.
                rescale = max(0.25, min(4.0, rescale))
                lam = lam_hist * rescale
            else:
                lam = lam_hist

            model_p = self._range_probability(low, high, current_count, lam, remaining_h)
            market_p = self._market_mid(row.get("yes_buy"), row.get("yes_sell"))
            if market_p is None:
                continue

            edge = model_p - market_p
            signal = "BUY_YES" if edge > edge_threshold else ("BUY_NO" if edge < -edge_threshold else "HOLD")

            predictions.append(
                {
                    "event_slug": slug,
                    "label": label,
                    "current_count": current_count,
                    "hours_left": round(max(0.0, (end - now).total_seconds() / 3600), 3),
                    "lambda_remaining": round(lam, 3),
                    "lambda_historical": round(lam_hist, 3),
                    "nb_size": (
                        round(self._nb_size_from_lambda(lam, remaining_h), 3)
                        if self._nb_size_from_lambda(lam, remaining_h) != float("inf")
                        else None
                    ),
                    "phi": round(self._phi_for_window(remaining_h), 3),
                    "model_p_yes": round(model_p, 6),
                    "market_p_yes": round(market_p, 6),
                    "edge": round(edge, 6),
                    "signal": signal,
                }
            )

        predictions.sort(key=lambda x: abs(float(x["edge"])), reverse=True)

        return {
            "sentiment": round(sentiment, 4),
            "live_sentiment": round(live_sentiment, 4),
            "historical_sentiment_bias": round(hist_bias, 4),
            "tweet_density_per_hour": round(density_h, 4),
            "base_rate_per_hour": round(self._overall_rate, 4),
            "hawkes_excitation_h": round(hawkes, 4),
            "recent_retweet_share": round(recent_rt_share, 4),
            "calibration_days": self._cal.get("dataset_days", 0),
            "dataset_rows": len(self.rows),
            "predictions": predictions,
        }


if __name__ == "__main__":
    model = PredictionModel()
    print(json.dumps(model.predict([], {}, {}), indent=2))
