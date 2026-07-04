"""
Deribit put credit-spread backtest with trailing IV-rank gating (Thread A follow-up).

Thread A (research/options_premium_scan.py) found naive unhedged weekly short-OTM-put
selling is net-negative on BTC/ETH over the last ~6 months: a few fat-tailed drawdown
weeks each wiped out 20+ weeks of collected premium, because a naked short put has
unbounded (well, strike-bounded) downside. That script's own findings flagged two
untested variants that could plausibly change the verdict:
  1. IV-rank-gated entry -- only sell when current IV is elevated vs. its own recent
     range (the objective's own Thread A thesis), skipping weeks where the vol premium
     isn't there to harvest.
  2. Put credit spreads -- buy a further-OTM put as protection, capping max loss at a
     known, fixed width instead of "however far spot falls."

This script tests BOTH together: a short-delta put + a further-OTM long put (defined
max loss = spread width - net credit), entered only when the trailing 90-day IV-rank
of DVOL is >= a threshold (50th or 70th percentile), using a NO-LOOKAHEAD trailing
window (rank at day d is computed only from DVOL values on or before day d, unlike the
single "current IV rank" print in options_premium_scan.py which compares today against
the *entire* historical window including data that would not have been known at the
time of a past trade).

Data reality check (same as options_premium_scan.py, not re-litigated): Deribit's public
REST does not expose historical option order books, so this is a Black-Scholes
simulation fed with real historical DVOL (ATM IV) + real historical spot, solving for
strikes at target deltas. Same disclosed limitation applies (no real skew, r=0, no
transaction costs in the simulation loop).

    python -m research.options_credit_spread_scan
Outputs: console table + data/edge_latency/options_credit_spread_<CCY>.csv
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from research.options_premium_scan import (
    DAY_MS,
    fetch_dvol_history,
    fetch_price_history,
    put_price_and_strike_for_delta,
)

OUT = Path("data/edge_latency")


def trailing_iv_rank(dvol: dict[int, float], day: int, window_days: int = 90) -> float | None:
    """Percentile rank of dvol[day] within (day - window_days, day], no future data."""
    lo = day - window_days * DAY_MS
    window_vals = sorted(v for d, v in dvol.items() if lo < d <= day)
    if len(window_vals) < 20:  # not enough trailing history to rank meaningfully
        return None
    cur = dvol[day]
    below = sum(1 for v in window_vals if v <= cur)
    return 100.0 * below / len(window_vals)


def backtest_spread(
    dvol: dict[int, float],
    price: dict[int, float],
    currency: str,
    short_delta: float,
    long_delta: float,
    hold_days: int,
    iv_rank_window: int,
    iv_rank_min: float,
) -> list[dict]:
    common_days = sorted(set(dvol) & set(price))
    rows = []
    step = hold_days
    i = 0
    while i + step < len(common_days):
        d0 = common_days[i]
        d1 = common_days[i + step]
        S0 = price[d0]
        S1 = price[d1]
        iv0 = dvol[d0]
        T = step / 365.0
        rank = trailing_iv_rank(dvol, d0, iv_rank_window)

        K_short, prem_short = put_price_and_strike_for_delta(S0, iv0, T, -short_delta)
        K_long, prem_long = put_price_and_strike_for_delta(S0, iv0, T, -long_delta)
        # K_long < K_short by construction (further OTM = lower strike for a put)
        width = K_short - K_long
        net_credit = prem_short - prem_long
        entered = rank is not None and rank >= iv_rank_min

        payoff_short = -max(K_short - S1, 0.0)
        payoff_long = max(K_long - S1, 0.0)
        pnl = net_credit + payoff_short + payoff_long
        max_loss = width - net_credit

        rows.append({
            "currency": currency,
            "entry_day": d0,
            "expiry_day": d1,
            "spot_entry": S0,
            "spot_expiry": S1,
            "iv_pct": iv0,
            "trailing_iv_rank": rank,
            "entered": entered,
            "K_short": K_short,
            "K_long": K_long,
            "width": width,
            "net_credit": net_credit,
            "max_loss": max_loss,
            "pnl_if_entered": pnl if entered else 0.0,
            "pnl_full_signal": pnl,  # what it would have returned if always entered (for comparison)
            "return_on_width_pct_if_entered": 100.0 * pnl / width if entered and width > 0 else 0.0,
            "return_on_width_pct_full_signal": 100.0 * pnl / width if width > 0 else 0.0,
        })
        i += step
    return rows


def summarize(rows: list[dict], gated: bool) -> dict:
    key_pnl_pct = "return_on_width_pct_if_entered" if gated else "return_on_width_pct_full_signal"
    sub = [r for r in rows if (r["entered"] if gated else True)]
    if not sub:
        return {}
    n = len(sub)
    n_loss = sum(1 for r in sub if r[key_pnl_pct] < 0)
    total_pct = sum(r[key_pnl_pct] for r in sub)
    avg_weekly_pct = total_pct / n
    worst = min(sub, key=lambda r: r[key_pnl_pct])
    weeks_per_month = 30.0 / 7.0
    n_skipped = len(rows) - n if gated else 0
    return {
        "n_trades": n,
        "n_skipped_by_gate": n_skipped,
        "win_rate": 1 - n_loss / n,
        "avg_weekly_return_pct": avg_weekly_pct,
        "avg_monthly_return_pct_additive": avg_weekly_pct * weeks_per_month,
        "worst_trade_pct": worst[key_pnl_pct],
        "worst_trade_day": worst["entry_day"],
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Deribit put credit-spread backtest with IV-rank gating")
    p.add_argument("--currencies", nargs="+", default=["BTC", "ETH"])
    p.add_argument("--short-delta", type=float, default=0.10)
    p.add_argument("--long-delta", type=float, default=0.03)
    p.add_argument("--days", type=int, default=195)
    p.add_argument("--hold-days", type=int, default=7)
    p.add_argument("--iv-rank-window", type=int, default=90)
    p.add_argument("--iv-rank-mins", nargs="+", type=float, default=[0.0, 50.0, 70.0])
    a = p.parse_args(argv)

    OUT.mkdir(parents=True, exist_ok=True)
    import requests
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    print(f"put credit spread: short_delta={a.short_delta} long_delta={a.long_delta} "
          f"hold={a.hold_days}d trailing_iv_rank_window={a.iv_rank_window}d\n")
    print(f"{'ccy':<5}{'gate>=':>8}{'n_trd':>7}{'n_skip':>8}{'win%':>8}{'avg wk%':>10}{'~mo%':>8}{'worst%':>9}")

    for ccy in a.currencies:
        dvol = fetch_dvol_history(session, ccy, a.days)
        price = fetch_price_history(session, ccy, a.days)
        if not dvol or not price:
            print(f"{ccy}: insufficient DVOL/price history, skipped")
            continue
        rows = backtest_spread(dvol, price, ccy, a.short_delta, a.long_delta, a.hold_days,
                                a.iv_rank_window, iv_rank_min=0.0)
        with (OUT / f"options_credit_spread_{ccy}.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        for gate_min in a.iv_rank_mins:
            gated_rows = []
            for r in rows:
                r2 = dict(r)
                r2["entered"] = r["trailing_iv_rank"] is not None and r["trailing_iv_rank"] >= gate_min
                gated_rows.append(r2)
            s = summarize(gated_rows, gated=True)
            if not s:
                print(f"{ccy:<5}{gate_min:>7.0f}%   (no trades passed gate)")
                continue
            print(f"{ccy:<5}{gate_min:>7.0f}%{s['n_trades']:>7}{s['n_skipped_by_gate']:>8}"
                  f"{100*s['win_rate']:>7.1f}%{s['avg_weekly_return_pct']:>9.2f}%"
                  f"{s['avg_monthly_return_pct_additive']:>7.1f}%{s['worst_trade_pct']:>8.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
