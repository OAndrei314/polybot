from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import random
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from model.fees import FeeModel


SECONDS_PER_HOUR = 3600.0
SECONDS_PER_DAY = 86400.0


TWO_DAY_LABELS = [
    "40-64",
    "65-89",
    "90-114",
    "115-139",
    "140-164",
    "165-189",
    "190-214",
    "215-239",
    "240+",
]
WEEKLY_LABELS = [
    "0-19",
    "20-39",
    "40-59",
    "60-79",
    "80-99",
    "100-119",
    "120-139",
    "140-159",
    "160-179",
    "180-199",
    "200-219",
    "220-239",
    "240-259",
    "260-279",
    "280-299",
    "300-319",
    "320-339",
    "340-359",
    "360-379",
    "380-399",
    "400-419",
    "420-439",
    "440-459",
    "460-479",
    "480-499",
    "500+",
]
MONTHLY_LABELS = [
    "0-19",
    "20-39",
    "40-59",
    "60-79",
    "80-99",
    "100-119",
    "120-139",
    "140-159",
    "160-179",
    "180-199",
    "200-219",
    "220-239",
    "240-259",
    "260-279",
    "280-299",
    "300-319",
    "320-339",
    "340-359",
    "360-379",
    "380-399",
    "400-419",
    "420-439",
    "440-459",
    "460-479",
    "480-499",
    "500-519",
    "520-539",
    "540-559",
    "560-579",
    "580-599",
    "600-619",
    "620-639",
    "640-659",
    "660-679",
    "680-699",
    "700-719",
    "720-739",
    "740-759",
    "760-779",
    "780-799",
    "800-839",
    "840-879",
    "880-919",
    "920-959",
    "960-999",
    "1000-1039",
    "1040-1079",
    "1080-1119",
    "1120-1159",
    "1160-1199",
    "1200-1239",
    "1240-1279",
    "1280-1319",
    "1320-1359",
    "1360-1399",
    "1400-1439",
    "1440-1479",
    "1480-1519",
    "1520-1559",
    "1560-1599",
    "1600-1679",
    "1680-1759",
    "1760-1839",
    "1840-1919",
    "1920-1999",
    "2000+",
]


def _parse_label(label: str) -> tuple[int, int]:
    txt = str(label).strip()
    if txt.endswith("+"):
        return int(txt[:-1]), 9999
    low, high = txt.split("-", 1)
    return int(low), int(high)


BRACKET_SETS: dict[str, list[tuple[str, int, int]]] = {
    "2d": [(label, *_parse_label(label)) for label in TWO_DAY_LABELS],
    "weekly": [(label, *_parse_label(label)) for label in WEEKLY_LABELS],
    "monthly": [(label, *_parse_label(label)) for label in MONTHLY_LABELS],
}


@dataclass(frozen=True)
class EventTemplate:
    name: str
    horizon_h: float
    stride_h: float
    bracket_set: str


DEFAULT_TEMPLATES = (
    EventTemplate("2d", 48.0, 24.0, "2d"),
    EventTemplate("weekly", 168.0, 168.0, "weekly"),
)
MONTHLY_TEMPLATE = EventTemplate("monthly", 30.0 * 24.0, 30.0 * 24.0, "monthly")


@dataclass(frozen=True)
class Window:
    name: str
    train_start_s: float
    train_end_s: float
    validate_start_s: float
    validate_end_s: float
    regime: str


@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    template: str
    start_s: float
    end_s: float
    bracket_set: str
    final_count: int


@dataclass(frozen=True)
class StrategyParams:
    competitor_id: str
    stage: str
    nb_phi: float
    hawkes_beta: float
    hawkes_alpha: float
    rate_prior_strength_h: float
    uncertainty: float
    entry_edge: float
    spread_floor_multiplier: float
    min_entry_price: float
    stop_loss: float
    take_profit: float
    exit_edge: float
    signal_flip_min_edge_gap: float
    signal_flip_max_loss_pct: float
    key_stop_loss_cooldown_s: float
    event_stop_loss_cooldown_s: float
    key_lock_streak_count: int
    key_lock_duration_s: float
    risk_per_trade_pct: float
    micro_price_max_cost: float
    max_event_exposure_pct: float
    stop_loss_dwell_s: float = 600.0
    entry_alignment_max_gap: float = 0.55
    position_size_disagreement_penalty: float = 1.5
    max_positions: int = 8
    min_trade_cost: float = 25.0
    micro_price_threshold: float = 0.20
    slippage_rate: float = 0.005
    fee_category: str = "mentions"
    bankroll: float = 1_000.0


@dataclass
class Position:
    key: str
    event_id: str
    event_template: str
    label: str
    low: int
    high: int
    side: str
    entry_price: float
    shares: float
    trade_value: float
    entry_fee: float
    cost: float
    entry_edge: float
    entry_ts_s: float


@dataclass
class CloseRecord:
    competitor_id: str
    window: str
    event_id: str
    event_template: str
    label: str
    side: str
    reason: str
    pnl: float
    gross_pnl_before_fees: float
    entry_fee: float
    exit_fee: float
    hold_h: float
    ts_s: float


class TweetHistory:
    def __init__(self, df: pd.DataFrame) -> None:
        dt = pd.to_datetime(df["date"], utc=True, errors="coerce")
        work = df.assign(dt=dt).dropna(subset=["dt"]).sort_values("dt")
        if "is_retweet" not in work.columns:
            work["is_retweet"] = False
        self.df = work
        self.ts_s = (work["dt"].astype("int64").to_numpy(dtype=np.float64) / 1e9)
        self.is_retweet = work["is_retweet"].astype(bool).to_numpy()
        self.min_s = float(self.ts_s[0])
        self.max_s = float(self.ts_s[-1])

    def count_between(self, start_s: float, end_s: float) -> int:
        left = int(np.searchsorted(self.ts_s, start_s, side="left"))
        right = int(np.searchsorted(self.ts_s, end_s, side="left"))
        return max(0, right - left)

    def recent_excitation(
        self,
        asof_s: float,
        alpha: float,
        beta_per_min: float,
        window_h: float = 4.0,
    ) -> float:
        start_s = asof_s - (window_h * SECONDS_PER_HOUR)
        left = int(np.searchsorted(self.ts_s, start_s, side="left"))
        right = int(np.searchsorted(self.ts_s, asof_s, side="left"))
        if right <= left or alpha <= 0.0:
            return 0.0
        ages_min = (asof_s - self.ts_s[left:right]) / 60.0
        rt = self.is_retweet[left:right]
        alphas = np.where(rt, alpha * 1.4, alpha)
        return float(np.sum(alphas * np.exp(-beta_per_min * ages_min)))


class RateModel:
    def __init__(
        self,
        hourly_rates: list[float],
        weekday_multipliers: list[float],
        overall_rate_h: float,
    ) -> None:
        self.hourly_rates = hourly_rates
        self.weekday_multipliers = weekday_multipliers
        self.overall_rate_h = overall_rate_h

    @staticmethod
    def fit(history: TweetHistory, train_start_s: float, train_end_s: float) -> "RateModel":
        df = history.df
        start = datetime.fromtimestamp(train_start_s, tz=timezone.utc)
        end = datetime.fromtimestamp(train_end_s, tz=timezone.utc)
        sub = df[(df["dt"] >= start) & (df["dt"] < end)].copy()
        train_days = max((train_end_s - train_start_s) / SECONDS_PER_DAY, 1.0)
        if sub.empty:
            return RateModel([1.6] * 24, [1.0] * 7, 1.6)

        sub["hour"] = sub["dt"].dt.hour
        sub["weekday"] = sub["dt"].dt.dayofweek
        hourly_counts = sub.groupby("hour").size()
        hourly_rates = [
            float(hourly_counts.get(h, 0.0)) / train_days for h in range(24)
        ]

        weekday_counts = sub.groupby("weekday").size()
        weekday_rates = []
        for d in range(7):
            occurrences = max(train_days / 7.0, 1e-9)
            weekday_rates.append(float(weekday_counts.get(d, 0.0)) / (occurrences * 24.0))
        mean_weekday = float(np.mean(weekday_rates)) if any(weekday_rates) else 1.0
        weekday_multipliers = [
            (r / mean_weekday) if mean_weekday > 0 else 1.0 for r in weekday_rates
        ]
        overall = float(len(sub)) / max((train_end_s - train_start_s) / SECONDS_PER_HOUR, 1e-9)
        return RateModel(hourly_rates, weekday_multipliers, overall)

    def lambda_between(self, start_s: float, end_s: float) -> float:
        if end_s <= start_s:
            return 0.0
        cursor = datetime.fromtimestamp(start_s, tz=timezone.utc)
        end = datetime.fromtimestamp(end_s, tz=timezone.utc)
        total = 0.0
        while cursor < end:
            next_hour = cursor.replace(minute=0, second=0, microsecond=0)
            next_hour = next_hour + pd.Timedelta(hours=1)
            slot_end = min(next_hour, end)
            frac_h = (slot_end - cursor).total_seconds() / SECONDS_PER_HOUR
            base = self.hourly_rates[cursor.hour] * self.weekday_multipliers[cursor.weekday()]
            total += max(0.0, base * frac_h)
            cursor = slot_end
        return max(0.0, total)


def _clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))


def _nb_log_pmf(k_obs: int, mu: float, phi: float) -> float:
    if k_obs < 0:
        return float("-inf")
    if mu <= 0:
        return 0.0 if k_obs == 0 else float("-inf")
    if phi <= 1.001:
        return k_obs * math.log(mu) - mu - math.lgamma(k_obs + 1)
    size = max(0.05, mu / (phi - 1.0))
    p = size / (size + mu)
    return (
        math.lgamma(k_obs + size)
        - math.lgamma(k_obs + 1)
        - math.lgamma(size)
        + size * math.log(p)
        + k_obs * math.log(1.0 - p)
    )


def range_probability(low: int, high: int, current: int, mu_more: float, phi: float) -> float:
    if current > high:
        return 0.0
    min_more = max(0, low - current)
    max_more = high - current
    if max_more < 0:
        return 0.0
    if mu_more <= 0:
        return 1.0 if low <= current <= high else 0.0

    if high >= 9999:
        if phi <= 1.001:
            std = math.sqrt(mu_more)
        else:
            size = max(0.05, mu_more / (phi - 1.0))
            std = math.sqrt(mu_more + (mu_more * mu_more / size))
        upper = max(min_more, int(mu_more + 12.0 * std))
        cdf = sum(math.exp(_nb_log_pmf(k, mu_more, phi)) for k in range(min_more))
        return _clamp(1.0 - cdf, 0.0, 1.0)

    total = 0.0
    for k in range(min_more, max_more + 1):
        total += math.exp(_nb_log_pmf(k, mu_more, phi))
    return _clamp(total, 0.0, 1.0)


def expected_remaining(
    history: TweetHistory,
    rates: RateModel,
    params: StrategyParams,
    event: MarketEvent,
    asof_s: float,
    current_count: int,
) -> float:
    remaining_h = max(0.0, (event.end_s - asof_s) / SECONDS_PER_HOUR)
    elapsed_h = max(0.0, (asof_s - event.start_s) / SECONDS_PER_HOUR)
    lam_hist = rates.lambda_between(asof_s, event.end_s)

    excitation_per_min = history.recent_excitation(
        asof_s=asof_s,
        alpha=params.hawkes_alpha,
        beta_per_min=params.hawkes_beta,
    )
    beta_h = max(1e-6, params.hawkes_beta * 60.0)
    hawkes_h = excitation_per_min * 60.0
    if remaining_h > 0 and hawkes_h > 0:
        lam_hist += hawkes_h * (1.0 - math.exp(-beta_h * remaining_h)) / beta_h

    if elapsed_h >= 1.0 and remaining_h > 0.0 and lam_hist > 0.0:
        prior_rate_h = max(1e-6, lam_hist / max(remaining_h, 1e-9))
        alpha_0 = prior_rate_h * params.rate_prior_strength_h
        posterior_rate_h = (alpha_0 + float(current_count)) / (
            params.rate_prior_strength_h + elapsed_h
        )
        rescale = _clamp(posterior_rate_h / prior_rate_h, 0.20, 5.0)
        return max(0.0, lam_hist * rescale)
    return max(0.0, lam_hist)


def model_probability(
    history: TweetHistory,
    rates: RateModel,
    params: StrategyParams,
    event: MarketEvent,
    asof_s: float,
    low: int,
    high: int,
) -> tuple[float, float, int]:
    current = history.count_between(event.start_s, min(asof_s, event.end_s))
    mu_more = expected_remaining(history, rates, params, event, asof_s, current)
    p_yes = range_probability(low, high, current, mu_more, params.nb_phi)
    return p_yes, mu_more, current


def synthetic_market(
    baseline_p_yes: float,
    event: MarketEvent,
    asof_s: float,
) -> dict[str, float]:
    remaining_h = max(0.0, (event.end_s - asof_s) / SECONDS_PER_HOUR)
    mid = _clamp(float(baseline_p_yes), 0.001, 0.999)
    extremity = abs(mid - 0.5) * 2.0
    time_pressure = 1.0 / (1.0 + max(remaining_h, 0.0) / 72.0)
    spread = _clamp(0.010 + 0.030 * extremity + 0.010 * time_pressure, 0.006, 0.090)
    yes_sell = _clamp(mid - spread / 2.0, 0.001, 0.999)
    yes_buy = _clamp(mid + spread / 2.0, 0.001, 0.999)
    no_mid = 1.0 - mid
    no_sell = _clamp(no_mid - spread / 2.0, 0.001, 0.999)
    no_buy = _clamp(no_mid + spread / 2.0, 0.001, 0.999)
    return {
        "yes_buy": yes_buy,
        "yes_sell": yes_sell,
        "no_buy": no_buy,
        "no_sell": no_sell,
        "spread": spread,
    }


_MONTH_MAP = {m: i for i, m in enumerate(
    ["january","february","march","april","may","june",
     "july","august","september","october","november","december"], 1
)}


def _parse_event_title_window(title: str) -> tuple[float, float] | None:
    """Return (start_s, end_s) UTC from titles like:
      'Elon Musk # tweets April 28 - May 5, 2026?'
      'tweets from April 28 to May 5, 2026'
    """
    m = re.search(
        r"tweets?\s+(?:from\s+)?(\w+)\s+(\d{1,2})\s*(?:[-–]|to)\s*(\w+)\s+(\d{1,2}),?\s*(\d{4})",
        title, re.IGNORECASE,
    )
    if not m:
        return None
    m1, d1, m2, d2, yr = m.group(1).lower(), int(m.group(2)), m.group(3).lower(), int(m.group(4)), int(m.group(5))
    mon1, mon2 = _MONTH_MAP.get(m1), _MONTH_MAP.get(m2)
    if mon1 is None or mon2 is None:
        return None
    try:
        s = datetime(yr, mon1, d1, tzinfo=timezone.utc).timestamp()
        e = datetime(yr, mon2, d2, tzinfo=timezone.utc).timestamp()
        return s, e
    except ValueError:
        return None


class RealPriceIndex:
    """Loads real CLOB BBO prices from live-run tick logs and serves them for backtest replay.

    Prices are indexed by (event_slug, label) and looked up by the nearest tick
    to a requested timestamp.  Events are matched to backtest MarketEvent objects
    by comparing their parsed date windows (from the event_title field).
    """

    def __init__(self, tick_paths: list[Path]) -> None:
        # {(event_slug, label): sorted list of (ts_s, prices_dict)}
        self._by_slug: dict[tuple[str, str], list[tuple[float, dict[str, float]]]] = defaultdict(list)
        # {event_slug: (start_s, end_s)}  — from event_title
        self._slug_windows: dict[str, tuple[float, float]] = {}
        for path in tick_paths:
            self._load(path)
        for key in self._by_slug:
            self._by_slug[key].sort(key=lambda x: x[0])

    def _load(self, path: Path) -> None:
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt") as f:  # type: ignore[arg-type]
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).timestamp()
                for pr in row.get("price_rows") or []:
                    if pr.get("is_synthetic_price"):
                        continue
                    slug = pr.get("event_slug", "")
                    label = pr.get("label", "")
                    if not slug or not label:
                        continue
                    prices = {k: pr[k] for k in ("yes_buy", "yes_sell", "no_buy", "no_sell") if k in pr}
                    if len(prices) < 4:
                        continue
                    self._by_slug[(slug, label)].append((ts, prices))
                    if slug not in self._slug_windows:
                        window = _parse_event_title_window(pr.get("event_title", ""))
                        if window:
                            self._slug_windows[slug] = window

    def _matching_slugs(self, event_start_s: float, event_end_s: float) -> list[str]:
        """Return slugs whose date window overlaps the backtest event window."""
        matches = []
        for slug, (ws, we) in self._slug_windows.items():
            overlap = min(event_end_s, we) - max(event_start_s, ws)
            if overlap > 0:
                matches.append(slug)
        return matches

    def lookup(
        self,
        event_start_s: float,
        event_end_s: float,
        label: str,
        asof_s: float,
        max_age_s: float = 7200.0,
    ) -> dict[str, float] | None:
        """Return the closest real BBO prices for (event_window, label, timestamp), or None."""
        for slug in self._matching_slugs(event_start_s, event_end_s):
            series = self._by_slug.get((slug, label))
            if not series:
                continue
            # Binary-search for nearest tick at or before asof_s
            lo, hi = 0, len(series) - 1
            best = None
            while lo <= hi:
                mid = (lo + hi) // 2
                if series[mid][0] <= asof_s:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best is not None and (asof_s - series[best][0]) <= max_age_s:
                p = series[best][1]
                return {**p, "spread": p["yes_buy"] - p["yes_sell"]}
        return None

    @property
    def is_empty(self) -> bool:
        return not self._by_slug


def generate_windows(
    history: TweetHistory,
    train_days: int,
    validate_days: int,
    step_days: int,
    lookback_days: int,
    holdout_days: int,
) -> tuple[list[Window], list[Window]]:
    data_end = math.floor(history.max_s / SECONDS_PER_DAY) * SECONDS_PER_DAY
    first_validate = max(
        history.min_s + train_days * SECONDS_PER_DAY,
        data_end - (lookback_days + holdout_days) * SECONDS_PER_DAY,
    )
    dev_end = data_end - holdout_days * SECONDS_PER_DAY

    dev: list[Window] = []
    i = 0
    val_start = first_validate
    while val_start + validate_days * SECONDS_PER_DAY <= dev_end:
        val_end = val_start + validate_days * SECONDS_PER_DAY
        train_end = val_start
        train_start = train_end - train_days * SECONDS_PER_DAY
        dev.append(
            Window(
                name=f"dev_{i:03d}",
                train_start_s=train_start,
                train_end_s=train_end,
                validate_start_s=val_start,
                validate_end_s=val_end,
                regime="development",
            )
        )
        i += 1
        val_start += step_days * SECONDS_PER_DAY

    holdout: list[Window] = []
    i = 0
    val_start = max(dev_end, history.min_s + train_days * SECONDS_PER_DAY)
    while val_start + validate_days * SECONDS_PER_DAY <= data_end:
        val_end = val_start + validate_days * SECONDS_PER_DAY
        train_end = val_start
        train_start = train_end - train_days * SECONDS_PER_DAY
        holdout.append(
            Window(
                name=f"holdout_{i:03d}",
                train_start_s=train_start,
                train_end_s=train_end,
                validate_start_s=val_start,
                validate_end_s=val_end,
                regime="held_out",
            )
        )
        i += 1
        val_start += step_days * SECONDS_PER_DAY
    return dev, holdout


def generate_events_for_span(
    history: TweetHistory,
    start_s: float,
    end_s: float,
    templates: Iterable[EventTemplate],
) -> list[MarketEvent]:
    events: list[MarketEvent] = []
    anchor = math.floor(start_s / SECONDS_PER_DAY) * SECONDS_PER_DAY
    for template in templates:
        horizon_s = template.horizon_h * SECONDS_PER_HOUR
        stride_s = template.stride_h * SECONDS_PER_HOUR
        first_start = anchor - horizon_s
        idx = 0
        t = first_start
        while t < end_s:
            event_end = t + horizon_s
            if event_end > start_s and t < end_s:
                final_count = history.count_between(t, event_end)
                dt = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y%m%d%H")
                event_id = f"{template.name}_{dt}_{idx}"
                events.append(
                    MarketEvent(
                        event_id=event_id,
                        template=template.name,
                        start_s=t,
                        end_s=event_end,
                        bracket_set=template.bracket_set,
                        final_count=final_count,
                    )
                )
            idx += 1
            t += stride_s
    return events


def baseline_params() -> StrategyParams:
    return StrategyParams(
        competitor_id="baseline",
        stage="baseline",
        nb_phi=20.0,
        hawkes_beta=0.18,
        hawkes_alpha=0.30,
        rate_prior_strength_h=12.0,
        uncertainty=0.05,
        entry_edge=0.08,
        spread_floor_multiplier=0.7,
        min_entry_price=0.04,
        stop_loss=0.30,
        take_profit=0.30,
        exit_edge=0.015,
        signal_flip_min_edge_gap=0.06,
        signal_flip_max_loss_pct=0.03,
        key_stop_loss_cooldown_s=7200.0,
        event_stop_loss_cooldown_s=3600.0,
        key_lock_streak_count=2,
        key_lock_duration_s=21600.0,
        risk_per_trade_pct=0.06,
        micro_price_max_cost=60.0,
        max_event_exposure_pct=0.40,
        stop_loss_dwell_s=600.0,
        entry_alignment_max_gap=0.55,
        position_size_disagreement_penalty=1.5,
    )


def market_maker_params() -> StrategyParams:
    """Deterministic synthetic market maker used when no CLOB history exists."""
    return replace(
        baseline_params(),
        competitor_id="synthetic_market_maker",
        stage="market",
        nb_phi=1.001,
        hawkes_alpha=0.25,
        uncertainty=0.0,
        entry_edge=999.0,
    )


RANGES: dict[str, tuple[float, float, str]] = {
    "nb_phi": (1.05, 60.0, "log"),
    "hawkes_beta": (0.03, 0.60, "log"),
    # Cap hawkes_alpha at 0.6 — historical run showed alpha>0.7 made models
    # over-react to bursts and led to position-flipping during noise.
    "hawkes_alpha": (0.0, 0.60, "linear"),
    "rate_prior_strength_h": (4.0, 48.0, "log"),
    "uncertainty": (0.01, 0.10, "linear"),
    "entry_edge": (0.05, 0.18, "linear"),
    # Higher floor on spread multiplier — aggressive spread-takers got hurt.
    "spread_floor_multiplier": (0.40, 1.30, "linear"),
    "min_entry_price": (0.02, 0.10, "linear"),
    # Wider stops only. Anything tighter than 0.20 gets whipsawed in thin
    # markets — confirmed by the May-2 run where every <20% stop blew up.
    "stop_loss": (0.20, 0.50, "linear"),
    "take_profit": (0.15, 0.50, "linear"),
    "exit_edge": (0.00, 0.05, "linear"),
    "signal_flip_min_edge_gap": (0.00, 0.15, "linear"),
    "signal_flip_max_loss_pct": (0.00, 0.10, "linear"),
    "key_stop_loss_cooldown_s": (300.0, 14400.0, "log"),
    "event_stop_loss_cooldown_s": (0.0, 7200.0, "linear"),
    "key_lock_duration_s": (1800.0, 43200.0, "log"),
    # Cap risk-per-trade at 10% — 12% accounts compound losses too fast under
    # correlated mass stops.
    "risk_per_trade_pct": (0.02, 0.10, "linear"),
    "micro_price_max_cost": (10.0, 80.0, "linear"),
    "max_event_exposure_pct": (0.15, 0.55, "linear"),
    # New noise/disagreement controls.
    "stop_loss_dwell_s": (180.0, 1800.0, "log"),
    "entry_alignment_max_gap": (0.30, 0.70, "linear"),
    "position_size_disagreement_penalty": (0.5, 3.0, "linear"),
}


def _sample_range(rng: random.Random, spec: tuple[float, float, str]) -> float:
    lo, hi, mode = spec
    if mode == "log":
        if lo <= 0:
            return rng.uniform(lo, hi)
        return math.exp(rng.uniform(math.log(lo), math.log(hi)))
    return rng.uniform(lo, hi)


def random_params(rng: random.Random, idx: int, stage: str = "A") -> StrategyParams:
    vals = {name: _sample_range(rng, spec) for name, spec in RANGES.items()}
    return StrategyParams(
        competitor_id=f"{stage.lower()}_{idx:04d}",
        stage=stage,
        key_lock_streak_count=rng.randint(2, 4),
        **vals,
    )


def focused_params(
    rng: random.Random,
    parent: StrategyParams,
    idx: int,
    stage: str = "B",
) -> StrategyParams:
    vals = asdict(parent)
    vals["competitor_id"] = f"{stage.lower()}_{idx:04d}"
    vals["stage"] = stage
    for name, (lo, hi, mode) in RANGES.items():
        cur = float(vals[name])
        if mode == "log" and cur > 0 and lo > 0:
            cur = math.exp(math.log(cur) + rng.gauss(0.0, 0.25))
        else:
            cur = cur + rng.gauss(0.0, (hi - lo) * 0.08)
        vals[name] = _clamp(cur, lo, hi)
    vals["key_lock_streak_count"] = int(_clamp(round(parent.key_lock_streak_count + rng.choice([-1, 0, 1])), 2, 4))
    return StrategyParams(**vals)


def evaluate_candidate(
    params: StrategyParams,
    history: TweetHistory,
    windows: list[Window],
    templates: tuple[EventTemplate, ...],
    decision_interval_h: float,
    price_index: RealPriceIndex | None = None,
) -> tuple[dict[str, Any], list[CloseRecord], list[dict[str, Any]]]:
    fee_model = FeeModel(category=params.fee_category)
    baseline = market_maker_params()

    cash = params.bankroll
    positions: dict[str, Position] = {}
    close_records: list[CloseRecord] = []
    equity_curve: list[dict[str, Any]] = []
    last_closed_s: dict[str, float] = {}
    last_closed_reason: dict[str, str] = {}
    last_stoploss_s: dict[str, float] = {}
    last_event_stoploss_s: dict[str, float] = {}
    key_loss_streak: dict[str, int] = defaultdict(int)
    key_lock_until_s: dict[str, float] = defaultdict(float)
    window_returns: dict[str, float] = defaultdict(float)

    for window in windows:
        rates = RateModel.fit(history, window.train_start_s, window.train_end_s)
        events = generate_events_for_span(
            history,
            window.validate_start_s,
            window.validate_end_s,
            templates,
        )
        events_by_id = {e.event_id: e for e in events}
        t = window.validate_start_s
        while t <= window.validate_end_s:
            active_events = [e for e in events if e.start_s <= t < e.end_s]
            expired_keys = [
                key for key, pos in positions.items() if events_by_id[pos.event_id].end_s <= t
            ]
            for key in expired_keys:
                pos = positions.pop(key)
                ev = events_by_id[pos.event_id]
                won_yes = pos.low <= ev.final_count <= pos.high
                won = won_yes if pos.side == "YES" else not won_yes
                payout = pos.shares if won else 0.0
                pnl = payout - pos.cost
                cash += payout
                record = CloseRecord(
                    competitor_id=params.competitor_id,
                    window=window.name,
                    event_id=pos.event_id,
                    event_template=pos.event_template,
                    label=pos.label,
                    side=pos.side,
                    reason="resolution_win" if won else "resolution_loss",
                    pnl=pnl,
                    gross_pnl_before_fees=payout - pos.trade_value,
                    entry_fee=pos.entry_fee,
                    exit_fee=0.0,
                    hold_h=(t - pos.entry_ts_s) / SECONDS_PER_HOUR,
                    ts_s=t,
                )
                close_records.append(record)
                window_returns[window.name] += pnl

            price_cache: dict[str, dict[str, Any]] = {}
            pred_cache: dict[str, dict[str, Any]] = {}
            for ev in active_events:
                current = history.count_between(ev.start_s, min(t, ev.end_s))
                base_mu = expected_remaining(history, rates, baseline, ev, t, current)
                mu_more = expected_remaining(history, rates, params, ev, t, current)
                for label, low, high in BRACKET_SETS[ev.bracket_set]:
                    base_p = range_probability(low, high, current, base_mu, baseline.nb_phi)
                    if price_index is not None:
                        market = price_index.lookup(ev.start_s, ev.end_s, label, t)
                    else:
                        market = synthetic_market(base_p, ev, t)
                    if market is None:
                        continue
                    p_yes = range_probability(low, high, current, mu_more, params.nb_phi)
                    model_p = p_yes
                    model_p_no = 1.0 - model_p
                    spread = market["spread"]
                    yes_fee = fee_model.per_share(market["yes_buy"])
                    no_fee = fee_model.per_share(market["no_buy"])
                    true_edge_yes = (
                        model_p
                        - market["yes_buy"]
                        - yes_fee
                        - params.slippage_rate
                        - (0.5 * spread)
                        - params.uncertainty
                    )
                    true_edge_no = (
                        model_p_no
                        - market["no_buy"]
                        - no_fee
                        - params.slippage_rate
                        - (0.5 * spread)
                        - params.uncertainty
                    )
                    if true_edge_yes >= true_edge_no:
                        side = "YES"
                        true_edge = true_edge_yes
                        entry = market["yes_buy"]
                        fee_ps = yes_fee
                    else:
                        side = "NO"
                        true_edge = true_edge_no
                        entry = market["no_buy"]
                        fee_ps = no_fee
                    threshold = params.entry_edge + params.spread_floor_multiplier * (
                        2.0 * params.slippage_rate + 2.0 * fee_ps + spread
                    )
                    signal = f"BUY_{side}" if true_edge >= threshold else "HOLD"
                    if entry < params.min_entry_price:
                        signal = "HOLD"
                    key = f"{ev.event_id}|{label}"
                    price_cache[key] = {"market": market, "event": ev, "label": label, "low": low, "high": high}
                    pred_cache[key] = {
                        "side": side,
                        "true_edge": true_edge,
                        "true_edge_yes": true_edge_yes,
                        "true_edge_no": true_edge_no,
                        "entry_price": entry,
                        "threshold": threshold,
                        "signal": signal,
                        "model_p_yes": p_yes,
                        "mu_more": mu_more,
                        "current_count": current,
                    }

            # Manage existing positions using the current event/bracket signal.
            for key in list(positions.keys()):
                pos = positions.get(key)
                if pos is None:
                    continue
                row = price_cache.get(key)
                pred = pred_cache.get(key)
                if row is None or pred is None:
                    continue
                market = row["market"]
                if pos.side == "YES":
                    mark = (market["yes_buy"] + market["yes_sell"]) / 2.0
                    exit_price = market["yes_sell"]
                    same_edge = pred["true_edge_yes"]
                    flip_edge = pred["true_edge_no"]
                else:
                    mark = (market["no_buy"] + market["no_sell"]) / 2.0
                    exit_price = market["no_sell"]
                    same_edge = pred["true_edge_no"]
                    flip_edge = pred["true_edge_yes"]
                pnl_pct = (mark - pos.entry_price) / max(pos.entry_price, 1e-9)
                aligned = pred["signal"] == f"BUY_{pos.side}"
                flip_gap = flip_edge - same_edge
                reason = None
                if pnl_pct >= params.take_profit:
                    reason = "take_profit"
                elif pnl_pct <= -params.stop_loss:
                    reason = "stop_loss"
                elif not aligned and same_edge <= params.exit_edge:
                    reason = "edge_decay"
                elif (
                    not aligned
                    and flip_gap >= params.signal_flip_min_edge_gap
                    and pnl_pct >= -params.signal_flip_max_loss_pct
                ):
                    reason = "signal_flip"
                if reason is None:
                    continue

                exit_fee = fee_model.fee_usdc(pos.shares, exit_price)
                proceeds = (pos.shares * exit_price) - exit_fee
                pnl = proceeds - pos.cost
                cash += proceeds
                positions.pop(key, None)
                last_closed_s[key] = t
                last_closed_reason[key] = reason
                if reason == "stop_loss":
                    last_stoploss_s[key] = t
                    last_event_stoploss_s[pos.event_id] = t
                if pnl < 0:
                    key_loss_streak[key] += 1
                    if key_loss_streak[key] >= params.key_lock_streak_count:
                        key_lock_until_s[key] = t + params.key_lock_duration_s
                else:
                    key_loss_streak[key] = 0
                record = CloseRecord(
                    competitor_id=params.competitor_id,
                    window=window.name,
                    event_id=pos.event_id,
                    event_template=pos.event_template,
                    label=pos.label,
                    side=pos.side,
                    reason=reason,
                    pnl=pnl,
                    gross_pnl_before_fees=(pos.shares * exit_price) - pos.trade_value,
                    entry_fee=pos.entry_fee,
                    exit_fee=exit_fee,
                    hold_h=(t - pos.entry_ts_s) / SECONDS_PER_HOUR,
                    ts_s=t,
                )
                close_records.append(record)
                window_returns[window.name] += pnl

            actionable = [
                (key, pred)
                for key, pred in pred_cache.items()
                if pred["signal"] in ("BUY_YES", "BUY_NO") and key not in positions
            ]
            actionable.sort(key=lambda item: item[1]["true_edge"], reverse=True)

            opened_events: set[str] = set()
            for key, pred in actionable:
                if len(positions) >= params.max_positions:
                    break
                row = price_cache[key]
                ev = row["event"]
                if ev.event_id in opened_events:
                    continue
                if key_lock_until_s[key] and t < key_lock_until_s[key]:
                    continue
                if key in last_closed_s and (t - last_closed_s[key]) < 60.0:
                    continue
                if key in last_stoploss_s and (t - last_stoploss_s[key]) < params.key_stop_loss_cooldown_s:
                    continue
                if (
                    ev.event_id in last_event_stoploss_s
                    and (t - last_event_stoploss_s[ev.event_id]) < params.event_stop_loss_cooldown_s
                ):
                    continue
                if last_closed_reason.get(key) == "stop_loss":
                    reentry_floor = pred["threshold"] + 0.02
                    if pred["true_edge"] < reentry_floor:
                        continue

                event_used = sum(p.cost for p in positions.values() if p.event_id == ev.event_id)
                event_cap = params.bankroll * params.max_event_exposure_pct
                edge_mult = _clamp(pred["true_edge"] / max(params.entry_edge, 1e-9), 0.6, 2.0)
                budget = min(cash, params.bankroll * params.risk_per_trade_pct * edge_mult)
                if pred["entry_price"] < params.micro_price_threshold:
                    budget = min(budget, params.micro_price_max_cost)
                budget = min(budget, max(0.0, event_cap - event_used))
                if budget < params.min_trade_cost:
                    continue

                entry = pred["entry_price"]
                fee_ps = fee_model.per_share(entry)
                shares = budget / max(entry + fee_ps, 1e-9)
                trade_value = shares * entry
                entry_fee = fee_model.fee_usdc(shares, entry)
                cost = trade_value + entry_fee
                if cost > budget and cost > 0:
                    scale = budget / cost
                    shares *= scale
                    trade_value = shares * entry
                    entry_fee = fee_model.fee_usdc(shares, entry)
                    cost = trade_value + entry_fee
                if cost < params.min_trade_cost:
                    continue
                side = pred["side"]
                cash -= cost
                positions[key] = Position(
                    key=key,
                    event_id=ev.event_id,
                    event_template=ev.template,
                    label=row["label"],
                    low=row["low"],
                    high=row["high"],
                    side=side,
                    entry_price=entry,
                    shares=shares,
                    trade_value=trade_value,
                    entry_fee=entry_fee,
                    cost=cost,
                    entry_edge=pred["true_edge"],
                    entry_ts_s=t,
                )
                opened_events.add(ev.event_id)

            invested = sum(p.cost for p in positions.values())
            mtm = 0.0
            for key, pos in positions.items():
                row = price_cache.get(key)
                if row is None:
                    continue
                market = row["market"]
                if pos.side == "YES":
                    mark = (market["yes_buy"] + market["yes_sell"]) / 2.0
                else:
                    mark = (market["no_buy"] + market["no_sell"]) / 2.0
                mtm += (pos.shares * mark) - pos.cost
            equity = cash + invested + mtm
            equity_curve.append(
                {
                    "window": window.name,
                    "ts_s": t,
                    "equity": equity,
                    "open_positions": len(positions),
                    "cash": cash,
                }
            )
            t += decision_interval_h * SECONDS_PER_HOUR

        # Keep walk-forward folds independent. Any still-open positions are
        # marked out at the validation boundary using the same synthetic market
        # layer; if the event has resolved, use settlement payout instead.
        boundary_s = window.validate_end_s
        for key in list(positions.keys()):
            pos = positions.pop(key)
            ev = events_by_id.get(pos.event_id)
            if ev is None:
                continue
            if ev.end_s <= boundary_s:
                won_yes = pos.low <= ev.final_count <= pos.high
                won = won_yes if pos.side == "YES" else not won_yes
                payout = pos.shares if won else 0.0
                pnl = payout - pos.cost
                gross_before_fees = payout - pos.trade_value
                exit_fee = 0.0
                reason = "resolution_win" if won else "resolution_loss"
                cash += payout
            else:
                base_p, _, _ = model_probability(history, rates, baseline, ev, boundary_s, pos.low, pos.high)
                if price_index is not None:
                    market = price_index.lookup(ev.start_s, ev.end_s, pos.label, boundary_s)
                else:
                    market = synthetic_market(base_p, ev, boundary_s)
                if market is None:
                    market = synthetic_market(base_p, ev, boundary_s)
                exit_price = market["yes_sell"] if pos.side == "YES" else market["no_sell"]
                exit_fee = fee_model.fee_usdc(pos.shares, exit_price)
                proceeds = (pos.shares * exit_price) - exit_fee
                pnl = proceeds - pos.cost
                gross_before_fees = (pos.shares * exit_price) - pos.trade_value
                reason = "validation_end"
                cash += proceeds
            record = CloseRecord(
                competitor_id=params.competitor_id,
                window=window.name,
                event_id=pos.event_id,
                event_template=pos.event_template,
                label=pos.label,
                side=pos.side,
                reason=reason,
                pnl=pnl,
                gross_pnl_before_fees=gross_before_fees,
                entry_fee=pos.entry_fee,
                exit_fee=exit_fee,
                hold_h=(boundary_s - pos.entry_ts_s) / SECONDS_PER_HOUR,
                ts_s=boundary_s,
            )
            close_records.append(record)
            window_returns[window.name] += pnl
        equity_curve.append(
            {
                "window": window.name,
                "ts_s": boundary_s,
                "equity": cash,
                "open_positions": 0,
                "cash": cash,
            }
        )

    # Force-close leftovers at their final resolution if a window ended before expiry.
    for key, pos in list(positions.items()):
        # This only affects events overlapping the final validation boundary.
        won = False
        payout = 0.0
        pnl = -pos.cost
        cash += payout
        positions.pop(key, None)
        close_records.append(
            CloseRecord(
                competitor_id=params.competitor_id,
                window="forced_end",
                event_id=pos.event_id,
                event_template=pos.event_template,
                label=pos.label,
                side=pos.side,
                reason="forced_end",
                pnl=pnl,
                gross_pnl_before_fees=-pos.trade_value if not won else payout - pos.trade_value,
                entry_fee=pos.entry_fee,
                exit_fee=0.0,
                hold_h=0.0,
                ts_s=history.max_s,
            )
        )

    metrics = compute_metrics(params, close_records, equity_curve, window_returns)
    return metrics, close_records, equity_curve


def _max_drawdown(equity_values: list[float]) -> float:
    peak = None
    max_dd = 0.0
    for eq in equity_values:
        peak = eq if peak is None else max(peak, eq)
        if peak and peak > 0:
            max_dd = min(max_dd, (eq - peak) / peak)
    return abs(max_dd)


def compute_metrics(
    params: StrategyParams,
    closes: list[CloseRecord],
    equity_curve: list[dict[str, Any]],
    window_returns: dict[str, float],
) -> dict[str, Any]:
    realized = sum(r.pnl for r in closes)
    fees = sum(r.entry_fee + r.exit_fee for r in closes)
    gross = sum(r.gross_pnl_before_fees for r in closes)
    eq_values = [float(r["equity"]) for r in equity_curve] or [params.bankroll + realized]
    pnl = eq_values[-1] - params.bankroll if eq_values else realized
    step_returns = np.diff(eq_values) / np.maximum(eq_values[:-1], 1e-9) if len(eq_values) > 1 else np.array([])
    mean_r = float(np.mean(step_returns)) if len(step_returns) else 0.0
    std_r = float(np.std(step_returns)) if len(step_returns) else 0.0
    downside = step_returns[step_returns < 0]
    down_std = float(np.std(downside)) if len(downside) else 0.0
    sharpe = mean_r / std_r * math.sqrt(365.0) if std_r > 0 else 0.0
    sortino = mean_r / down_std * math.sqrt(365.0) if down_std > 0 else 0.0
    max_dd = _max_drawdown(eq_values)
    win_returns = list(window_returns.values())
    window_std = float(np.std(win_returns)) if len(win_returns) > 1 else 0.0
    worst_window = min(win_returns) if win_returns else 0.0
    worst_trade = min((r.pnl for r in closes), default=0.0)
    by_event: dict[str, float] = defaultdict(float)
    by_key: dict[str, float] = defaultdict(float)
    for r in closes:
        by_event[r.event_id] += r.pnl
        by_key[f"{r.event_id}|{r.label}|{r.side}"] += r.pnl
    positive_total = sum(max(0.0, v) for v in by_key.values())
    if positive_total > 0:
        top_key_share = max((max(0.0, v) for v in by_key.values()), default=0.0) / positive_total
        hhi = sum((max(0.0, v) / positive_total) ** 2 for v in by_key.values())
    else:
        top_key_share = 1.0 if closes else 0.0
        hhi = 1.0 if closes else 0.0
    reason_counts = Counter(r.reason for r in closes)
    stop_loss_churn = reason_counts.get("stop_loss", 0) / max(1, len(closes))

    return {
        "competitor_id": params.competitor_id,
        "stage": params.stage,
        "net_pnl": pnl,
        "realized_pnl": realized,
        "gross_pnl_before_fees": gross,
        "fee_drag": fees,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "sortino": sortino,
        "window_return_std": window_std,
        "worst_window_pnl": worst_window,
        "worst_trade_pnl": worst_trade,
        "trades": len(closes),
        "unique_events": len(by_event),
        "unique_event_brackets": len(by_key),
        "top_key_pnl_share": top_key_share,
        "pnl_hhi": hhi,
        "stop_loss_churn": stop_loss_churn,
        "take_profit_count": reason_counts.get("take_profit", 0),
        "stop_loss_count": reason_counts.get("stop_loss", 0),
        "edge_decay_count": reason_counts.get("edge_decay", 0),
        "signal_flip_count": reason_counts.get("signal_flip", 0),
        "resolution_count": reason_counts.get("resolution_win", 0)
        + reason_counts.get("resolution_loss", 0),
        **{f"param_{k}": v for k, v in asdict(params).items() if k not in {"competitor_id", "stage"}},
    }


def rank_leaderboard(rows: list[dict[str, Any]], baseline: dict[str, Any] | None) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    def pct_rank(col: str, ascending: bool) -> pd.Series:
        return df[col].rank(pct=True, ascending=ascending).fillna(0.0)

    df["rank_return"] = pct_rank("net_pnl", ascending=True)
    df["rank_sharpe"] = pct_rank("sharpe", ascending=True)
    df["rank_sortino"] = pct_rank("sortino", ascending=True)
    df["rank_drawdown"] = pct_rank("max_drawdown", ascending=False)
    df["rank_stability"] = pct_rank("window_return_std", ascending=False)
    df["rank_concentration"] = pct_rank("top_key_pnl_share", ascending=False)
    df["rank_trade_quality"] = pct_rank("unique_event_brackets", ascending=True)
    df["rank_tail"] = pct_rank("worst_trade_pnl", ascending=True)
    df["rank_churn"] = pct_rank("stop_loss_churn", ascending=False)
    df["composite_score"] = (
        0.24 * df["rank_return"]
        + 0.20 * ((df["rank_sharpe"] + df["rank_sortino"]) / 2.0)
        + 0.18 * df["rank_drawdown"]
        + 0.14 * df["rank_stability"]
        + 0.10 * df["rank_concentration"]
        + 0.08 * df["rank_trade_quality"]
        + 0.04 * df["rank_tail"]
        + 0.02 * df["rank_churn"]
    )
    if baseline:
        base_sharpe = float(baseline.get("sharpe", 0.0))
        base_dd = float(baseline.get("max_drawdown", 1.0))
        df["beats_baseline_guardrail"] = (df["sharpe"] > base_sharpe) & (
            df["max_drawdown"] <= base_dd
        )
    else:
        df["beats_baseline_guardrail"] = False
    return df.sort_values(["beats_baseline_guardrail", "composite_score"], ascending=[False, False])


def explain_failures(df: pd.DataFrame) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if df.empty:
        return out
    worst = df.sort_values("composite_score", ascending=True).head(10)
    for _, row in worst.iterrows():
        reasons = []
        if row["trades"] < 5:
            reasons.append("trade starvation from entry/min-price/spread filters")
        if row["max_drawdown"] > 0.10:
            reasons.append("drawdown too large")
        if row["top_key_pnl_share"] > 0.60:
            reasons.append("PnL dominated by one event/bracket")
        if row["stop_loss_churn"] > 0.30:
            reasons.append("stop-loss churn")
        if row["fee_drag"] > max(1.0, abs(row["gross_pnl_before_fees"]) * 0.35):
            reasons.append("cost drag too high versus gross alpha")
        if not reasons:
            reasons.append("weak composite balance versus peers")
        out.append(
            {
                "competitor_id": row["competitor_id"],
                "net_pnl": float(row["net_pnl"]),
                "max_drawdown": float(row["max_drawdown"]),
                "sharpe": float(row["sharpe"]),
                "trades": int(row["trades"]),
                "diagnosis": "; ".join(reasons),
            }
        )
    return out


def attribution_for_top(
    top_ids: list[str],
    closes_by_id: dict[str, list[CloseRecord]],
    metrics_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    out = []
    for cid in top_ids:
        closes = closes_by_id.get(cid, [])
        by_reason: dict[str, float] = defaultdict(float)
        by_template: dict[str, float] = defaultdict(float)
        for rec in closes:
            by_reason[rec.reason] += rec.pnl
            by_template[rec.event_template] += rec.pnl
        m = metrics_by_id[cid]
        out.append(
            {
                "competitor_id": cid,
                "net_pnl": m["net_pnl"],
                "gross_pnl_before_fees": m["gross_pnl_before_fees"],
                "fee_drag": m["fee_drag"],
                "risk_rule_impact_by_reason": dict(sorted(by_reason.items())),
                "pnl_by_market_template": dict(sorted(by_template.items())),
                "concentration": {
                    "top_key_pnl_share": m["top_key_pnl_share"],
                    "unique_event_brackets": m["unique_event_brackets"],
                },
            }
        )
    return out


def paper_cli_args(params: StrategyParams) -> list[str]:
    return [
        "--entry-edge",
        f"{params.entry_edge:.6f}",
        "--exit-edge",
        f"{params.exit_edge:.6f}",
        "--take-profit",
        f"{params.take_profit:.6f}",
        "--stop-loss",
        f"{params.stop_loss:.6f}",
        "--stop-loss-cooldown-s",
        f"{params.key_stop_loss_cooldown_s:.0f}",
        "--event-stop-loss-cooldown-s",
        f"{params.event_stop_loss_cooldown_s:.0f}",
        "--signal-flip-min-edge-gap",
        f"{params.signal_flip_min_edge_gap:.6f}",
        "--signal-flip-max-loss-pct",
        f"{params.signal_flip_max_loss_pct:.6f}",
        "--key-loss-streak-lock-count",
        str(params.key_lock_streak_count),
        "--key-loss-lock-s",
        f"{params.key_lock_duration_s:.0f}",
        "--min-entry-price",
        f"{params.min_entry_price:.6f}",
        "--risk-per-trade",
        f"{params.risk_per_trade_pct:.6f}",
        "--micro-price-max-cost",
        f"{params.micro_price_max_cost:.2f}",
        "--max-event-exposure-pct",
        f"{params.max_event_exposure_pct:.6f}",
        "--uncertainty",
        f"{params.uncertainty:.6f}",
        "--spread-floor-multiplier",
        f"{params.spread_floor_multiplier:.6f}",
        "--fee-category",
        params.fee_category,
        "--slippage-rate",
        f"{params.slippage_rate:.6f}",
        "--nb-phi",
        f"{params.nb_phi:.6f}",
        "--hawkes-beta",
        f"{params.hawkes_beta:.6f}",
        "--hawkes-alpha",
        f"{params.hawkes_alpha:.6f}",
        "--rate-prior-strength-h",
        f"{params.rate_prior_strength_h:.6f}",
    ]


def write_finalist_configs(
    out_dir: Path,
    top_params: list[StrategyParams],
    metrics_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    config_dir = out_dir / "finalist_configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for rank, params in enumerate(top_params, start=1):
        args = paper_cli_args(params)
        payload = {
            "rank": rank,
            "competitor_id": params.competitor_id,
            "metrics": metrics_by_id.get(params.competitor_id, {}),
            "params": asdict(params),
            "paper_cli_args": args,
            "command": "python3 run.py paper " + " ".join(args),
        }
        path = config_dir / f"rank_{rank:02d}_{params.competitor_id}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n")
        paths.append(str(path))
    return paths


def load_tweets(path: str) -> TweetHistory:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if p.suffix == ".parquet":
        try:
            df = pd.read_parquet(p)
        except Exception:
            csv_path = p.with_suffix(".csv")
            if not csv_path.exists():
                csv_path = Path("data/tweets_all.csv")
            df = pd.read_csv(csv_path)
    else:
        df = pd.read_csv(p)
    if "date" not in df.columns:
        raise ValueError(f"{path} must contain a 'date' column")
    return TweetHistory(df)


def perturb_for_sensitivity(params: StrategyParams) -> list[StrategyParams]:
    keys = [
        "nb_phi",
        "hawkes_beta",
        "hawkes_alpha",
        "rate_prior_strength_h",
        "uncertainty",
        "entry_edge",
        "spread_floor_multiplier",
        "min_entry_price",
        "stop_loss",
        "take_profit",
        "risk_per_trade_pct",
        "max_event_exposure_pct",
    ]
    variants = []
    idx = 0
    for key in keys:
        lo, hi, _ = RANGES[key]
        for mult in (0.90, 1.10):
            vals = asdict(params)
            vals["competitor_id"] = f"sens_{idx:03d}_{key}_{mult:.2f}"
            vals["stage"] = "sensitivity"
            vals[key] = _clamp(float(vals[key]) * mult, lo, hi)
            variants.append(StrategyParams(**vals))
            idx += 1
    return variants


def run_sweep(args: argparse.Namespace) -> dict[str, Any]:
    rng = random.Random(args.seed)
    out_dir = Path(args.out_dir) / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    history = load_tweets(args.tweets)
    templates = list(DEFAULT_TEMPLATES)
    if args.include_monthly:
        templates.append(MONTHLY_TEMPLATE)
    templates_t = tuple(templates)
    dev_windows, holdout_windows = generate_windows(
        history,
        train_days=args.train_days,
        validate_days=args.validate_days,
        step_days=args.step_days,
        lookback_days=args.lookback_days,
        holdout_days=args.holdout_days,
    )
    if args.max_windows:
        dev_windows = dev_windows[-args.max_windows :]
        holdout_windows = holdout_windows[-max(1, min(args.max_windows, len(holdout_windows))) :]

    candidates: list[StrategyParams] = [baseline_params()]
    candidates.extend(random_params(rng, i, stage="A") for i in range(args.stage_a))

    rows: list[dict[str, Any]] = []
    closes_by_id: dict[str, list[CloseRecord]] = {}
    params_by_id: dict[str, StrategyParams] = {}
    curves_by_id: dict[str, list[dict[str, Any]]] = {}

    price_index: RealPriceIndex | None = None
    if getattr(args, "price_logs", None):
        paths = [Path(p) for p in args.price_logs]
        price_index = RealPriceIndex(paths)
        if price_index.is_empty:
            print("WARNING: --price-logs provided but no real (non-synthetic) prices found in them.")

    def eval_and_store(params: StrategyParams, windows: list[Window]) -> dict[str, Any]:
        metrics, closes, curve = evaluate_candidate(
            params,
            history,
            windows,
            templates_t,
            decision_interval_h=args.decision_interval_h,
            price_index=price_index,
        )
        rows.append(metrics)
        closes_by_id[params.competitor_id] = closes
        curves_by_id[params.competitor_id] = curve
        params_by_id[params.competitor_id] = params
        return metrics

    for params in candidates:
        eval_and_store(params, dev_windows)

    base_metric = next((r for r in rows if r["competitor_id"] == "baseline"), None)
    stage_a_df = rank_leaderboard(rows, base_metric)
    top_a_ids = [
        str(cid)
        for cid in stage_a_df[stage_a_df["competitor_id"] != "baseline"]
        .head(max(1, args.stage_b_parents))["competitor_id"]
        .tolist()
    ]
    stage_b: list[StrategyParams] = []
    for i in range(args.stage_b):
        parent = params_by_id[top_a_ids[i % len(top_a_ids)]]
        stage_b.append(focused_params(rng, parent, i, stage="B"))
    for params in stage_b:
        eval_and_store(params, dev_windows)

    dev_df = rank_leaderboard(rows, base_metric)
    finalist_ids = [
        str(cid)
        for cid in dev_df[dev_df["competitor_id"] != "baseline"]
        .head(max(1, args.finalists))["competitor_id"]
        .tolist()
    ]

    holdout_rows = []
    holdout_closes_by_id: dict[str, list[CloseRecord]] = {}
    holdout_metrics_by_id: dict[str, dict[str, Any]] = {}
    holdout_candidates = [baseline_params()] + [params_by_id[cid] for cid in finalist_ids]
    for params in holdout_candidates:
        metrics, closes, _ = evaluate_candidate(
            replace(params, stage="C"),
            history,
            holdout_windows or dev_windows[-1:],
            templates_t,
            decision_interval_h=args.decision_interval_h,
            price_index=price_index,
        )
        metrics["competitor_id"] = params.competitor_id
        metrics["stage"] = "C"
        holdout_rows.append(metrics)
        holdout_closes_by_id[params.competitor_id] = closes
        holdout_metrics_by_id[params.competitor_id] = metrics

    holdout_base = next((r for r in holdout_rows if r["competitor_id"] == "baseline"), None)
    holdout_df = rank_leaderboard(holdout_rows, holdout_base)

    winner_pool = holdout_df[holdout_df["competitor_id"] != "baseline"]
    guarded = winner_pool[winner_pool["beats_baseline_guardrail"]]
    recommendations = {
        "best_performer": None,
        "most_robust": None,
        "best_risk_adjusted": None,
        "guardrail": "winner must beat baseline on higher risk-adjusted return and lower/equal drawdown",
    }
    if not guarded.empty:
        recommendations["best_performer"] = str(guarded.sort_values("net_pnl", ascending=False).iloc[0]["competitor_id"])
        recommendations["most_robust"] = str(
            guarded.sort_values(["window_return_std", "top_key_pnl_share"], ascending=[True, True]).iloc[0][
                "competitor_id"
            ]
        )
        recommendations["best_risk_adjusted"] = str(guarded.sort_values("sharpe", ascending=False).iloc[0]["competitor_id"])

    top10 = holdout_df[holdout_df["competitor_id"] != "baseline"].head(10)
    top3_ids = [str(cid) for cid in top10.head(3)["competitor_id"].tolist()]
    top3_params = [params_by_id[cid] for cid in top3_ids if cid in params_by_id]
    config_paths = write_finalist_configs(out_dir, top3_params, holdout_metrics_by_id)

    sensitivity_rows = []
    if top3_ids:
        winner_id = recommendations["best_performer"] or top3_ids[0]
        for variant in perturb_for_sensitivity(params_by_id[winner_id]):
            metrics, _, _ = evaluate_candidate(
                variant,
                history,
                holdout_windows or dev_windows[-1:],
                templates_t,
                decision_interval_h=args.decision_interval_h,
            )
            sensitivity_rows.append(metrics)

    dev_df.to_csv(out_dir / "leaderboard_dev_all_competitors.csv", index=False)
    holdout_df.to_csv(out_dir / "leaderboard_stage_c_holdout.csv", index=False)
    pd.DataFrame(sensitivity_rows).to_csv(out_dir / "winner_sensitivity.csv", index=False)

    top10_payload = []
    for _, row in top10.iterrows():
        cid = str(row["competitor_id"])
        top10_payload.append(
            {
                "competitor_id": cid,
                "metrics": row.to_dict(),
                "params": asdict(params_by_id[cid]),
            }
        )
    (out_dir / "top10_configs.json").write_text(json.dumps(top10_payload, indent=2, default=str) + "\n")
    (out_dir / "top3_attribution.json").write_text(
        json.dumps(
            attribution_for_top(top3_ids, holdout_closes_by_id, holdout_metrics_by_id),
            indent=2,
            default=str,
        )
        + "\n"
    )
    (out_dir / "worst10_failure_analysis.json").write_text(
        json.dumps(explain_failures(holdout_df), indent=2, default=str) + "\n"
    )
    summary = {
        "run_dir": str(out_dir),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "tweets": args.tweets,
            "seed": args.seed,
            "stage_a": args.stage_a,
            "stage_b": args.stage_b,
            "finalists": args.finalists,
            "templates": [asdict(t) for t in templates_t],
            "dev_windows": [asdict(w) for w in dev_windows],
            "holdout_windows": [asdict(w) for w in holdout_windows],
        },
        "recommendations": recommendations,
        "finalist_config_paths": config_paths,
        "baseline_holdout": holdout_base,
        "top10_competitor_ids": [str(cid) for cid in top10["competitor_id"].tolist()],
        "notes": [
            (
                f"Price source: real CLOB BBO prices from {len(args.price_logs)} tick log(s). "
                "Events without matching real price data were skipped (no entry taken)."
                if getattr(args, "price_logs", None) else
                "Price source: deterministic synthetic market maker (no real CLOB logs provided via --price-logs). "
                "Pass --price-logs real_input_ticks.jsonl.gz to use real execution prices."
            ),
            "Costs use Polymarket's documented taker-fee formula via model.fees.FeeModel.",
            "Stage C is held out from Stage A/B selection.",
        ],
    }
    (out_dir / "final_recommendation.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")

    readme = [
        "# Parameter Sweep Report",
        "",
        "Generated outputs:",
        "- `leaderboard_dev_all_competitors.csv`: Stage A/B development leaderboard for all competitors.",
        "- `leaderboard_stage_c_holdout.csv`: held-out finalist stress test leaderboard.",
        "- `top10_configs.json`: full params and metrics for the top 10 held-out finalists.",
        "- `top3_attribution.json`: alpha/cost/risk-rule attribution for the top 3.",
        "- `worst10_failure_analysis.json`: concise diagnosis for fragile profiles.",
        "- `winner_sensitivity.csv`: local perturbation test around the selected winner.",
        "- `finalist_configs/`: ready-to-run `run.py paper` config payloads.",
        "",
        "Important limitation: this historical runner cannot reconstruct true historical CLOB prices unless you add an appended order-book or picks snapshot log. It therefore uses a fixed baseline model as the synthetic market maker, then applies current execution/risk/cost rules consistently across all competitors.",
    ]
    (out_dir / "README.md").write_text("\n".join(readme) + "\n")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Structured walk-forward parameter sweep")
    parser.add_argument("--tweets", default="data/tweets_all.csv")
    parser.add_argument("--out-dir", default="data/reports/sweeps")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--stage-a", type=int, default=36)
    parser.add_argument("--stage-b", type=int, default=18)
    parser.add_argument("--stage-b-parents", type=int, default=6)
    parser.add_argument("--finalists", type=int, default=6)
    parser.add_argument("--train-days", type=int, default=180)
    parser.add_argument("--validate-days", type=int, default=21)
    parser.add_argument("--step-days", type=int, default=21)
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--holdout-days", type=int, default=84)
    parser.add_argument("--decision-interval-h", type=float, default=6.0)
    parser.add_argument("--max-windows", type=int, default=0)
    parser.add_argument("--include-monthly", action="store_true")
    parser.add_argument(
        "--price-logs",
        nargs="+",
        metavar="PATH",
        default=[],
        help="Paths to real_input_ticks.jsonl.gz files from live runs. "
             "When provided, real CLOB BBO prices are used instead of the synthetic market maker. "
             "Events with no matching real price data are skipped (no entry taken).",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Small smoke run: 8 coarse competitors, 4 focused competitors, 2 finalists.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.quick:
        args.stage_a = 8
        args.stage_b = 4
        args.stage_b_parents = 3
        args.finalists = 2
        args.max_windows = args.max_windows or 3
    summary = run_sweep(args)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
