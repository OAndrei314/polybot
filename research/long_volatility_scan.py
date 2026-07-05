"""
Long-volatility (buy weekly OTM strangles) backtest -- Thread R, the inverse
of Thread A.

Concept under test: Thread A (iteration 1) found that SELLING weekly 5/10/20-
delta OTM puts on BTC/ETH was net-negative over the last ~6 months because
two single crash weeks (2026-01-25, 2026-05-31) each wiped out 20+ weeks of
collected premium. If the seller lost money on those weeks, the mirror-image
BUYER of optionality on those same weeks made money -- so the natural,
previously-untested follow-up is: does systematically BUYING an OTM strangle
(long put + long call, same target |delta| on each leg) every week, funded
only by the premium paid (not a large collateral base), turn Thread A's own
fat-tail finding into a positive-EV long-vol harvesting strategy instead of a
short-vol one?

This reuses the exact same real data sources and Black-Scholes machinery
already validated in research/options_premium_scan.py (real Deribit DVOL
history + real BTC/ETH-PERPETUAL daily closes, strike solved for a target
delta) -- only the position (long instead of short) and the payoff
(two-sided instead of one-sided) differ. Same disclosed model limitations
apply: DVOL is ATM/index-wide IV (real skew would move OTM strikes' actual
quoted premium somewhat), r=0, no bid-ask spread deducted in the backtest
loop (spread cost is a real live one-way friction that is *disclosed*, not
modeled, since Deribit does not expose historical option order-book spread).

Capital base for a long strangle is the PREMIUM PAID (defined, small, 100%-
loss-at-worst), not the strike-sized collateral a cash-secured short put
needs -- so "return on capital deployed" numbers here are on a much smaller
base and are expected to look larger in percentage terms than Thread A's;
this is disclosed explicitly in the output, not an apples-to-apples
comparison to Thread A's return-on-collateral figures.

    python -m research.long_volatility_scan
    python -m research.long_volatility_scan --currencies BTC ETH --deltas 0.10 0.20
Outputs: console table + data/edge_latency/long_vol_<CCY>.csv
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import requests

from research.options_premium_scan import (
    fetch_dvol_history,
    fetch_price_history,
    iv_rank,
    live_validation,
    put_price_and_strike_for_delta,
)

OUT = Path("data/edge_latency")


def call_price_and_strike_for_delta(S: float, sigma_annual_pct: float, T_years: float, target_delta: float):
    """target_delta is positive (e.g. 0.10). Call delta = N(d1). Returns (strike, call_premium)."""
    sigma = sigma_annual_pct / 100.0
    if sigma <= 0 or T_years <= 0:
        return S, 0.0
    import math
    target_Nd1 = min(max(target_delta, 1e-6), 1 - 1e-6)

    def _norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    lo, hi = -8.0, 8.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if _norm_cdf(mid) < target_Nd1:
            lo = mid
        else:
            hi = mid
    d1 = (lo + hi) / 2.0
    sqrtT = math.sqrt(T_years)
    ln_S_K = d1 * sigma * sqrtT - 0.5 * sigma * sigma * T_years
    K = S / math.exp(-ln_S_K)  # call strike is ABOVE spot: K = S * exp(-ln_S_K)... solved below
    # For a call: d1 = (ln(S/K) + 0.5*sigma^2*T) / (sigma*sqrt(T))  =>  ln(S/K) = d1*sigma*sqrtT - 0.5*sigma^2*T
    # so ln(K/S) = -[d1*sigma*sqrtT - 0.5*sigma^2*T]  =>  K = S * exp(-(d1*sigma*sqrtT - 0.5*sigma^2*T))
    K = S * math.exp(-(d1 * sigma * sqrtT - 0.5 * sigma * sigma * T_years))
    d2 = d1 - sigma * sqrtT
    premium = S * _norm_cdf(d1) - K * _norm_cdf(d2)
    return K, max(premium, 0.0)


def backtest_strangle(session: requests.Session, currency: str, deltas: list[float], days: int, hold_days: int) -> list[dict]:
    dvol = fetch_dvol_history(session, currency, days)
    price = fetch_price_history(session, currency, days)
    common_days = sorted(set(dvol) & set(price))
    if len(common_days) < hold_days + 2:
        return []

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
        for target_delta in deltas:
            Kp, put_prem = put_price_and_strike_for_delta(S0, iv0, T, -target_delta)
            Kc, call_prem = call_price_and_strike_for_delta(S0, iv0, T, target_delta)
            premium_paid = put_prem + call_prem
            payoff = max(Kp - S1, 0.0) + max(S1 - Kc, 0.0)
            pnl = payoff - premium_paid
            rows.append({
                "currency": currency,
                "entry_day": d0,
                "expiry_day": d1,
                "target_delta": target_delta,
                "spot_entry": S0,
                "spot_expiry": S1,
                "iv_pct": iv0,
                "put_strike": Kp,
                "call_strike": Kc,
                "premium_paid": premium_paid,
                "payoff": payoff,
                "pnl": pnl,
                "return_on_premium_pct": 100.0 * pnl / premium_paid if premium_paid > 0 else 0.0,
                "realized_move_pct": 100.0 * abs(S1 - S0) / S0,
            })
        i += step
    return rows


def summarize(rows: list[dict], target_delta: float) -> dict:
    sub = [r for r in rows if r["target_delta"] == target_delta]
    if not sub:
        return {}
    n = len(sub)
    n_profitable = sum(1 for r in sub if r["pnl"] > 0)
    total_ret_pct = sum(r["return_on_premium_pct"] for r in sub)
    avg_weekly_pct = total_ret_pct / n
    worst = min(sub, key=lambda r: r["return_on_premium_pct"])
    best = max(sub, key=lambda r: r["return_on_premium_pct"])
    weeks_per_month = 30.0 / 7.0
    total_premium = sum(r["premium_paid"] for r in sub)
    total_pnl = sum(r["pnl"] for r in sub)
    return {
        "n_weeks": n,
        "win_rate": n_profitable / n,
        "avg_weekly_return_on_premium_pct": avg_weekly_pct,
        "avg_monthly_return_pct_additive": avg_weekly_pct * weeks_per_month,
        "pooled_return_on_total_premium_pct": 100.0 * total_pnl / total_premium if total_premium > 0 else 0.0,
        "worst_week_pct": worst["return_on_premium_pct"],
        "best_week_pct": best["return_on_premium_pct"],
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Long-volatility (buy weekly OTM strangle) backtest, simulated")
    p.add_argument("--currencies", nargs="+", default=["BTC", "ETH"])
    p.add_argument("--deltas", nargs="+", type=float, default=[0.05, 0.10, 0.20])
    p.add_argument("--days", type=int, default=195)
    p.add_argument("--hold-days", type=int, default=7)
    a = p.parse_args(argv)

    OUT.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    print("Long strangle (buy OTM put + OTM call, same |delta|), funded by premium paid")
    print(f"{'ccy':<5}{'delta':>7}{'n':>5}{'win%':>8}{'avg wk%':>10}{'~mo%(add)':>11}{'pooled%':>10}{'worst wk%':>11}{'best wk%':>10}")
    for ccy in a.currencies:
        rows = backtest_strangle(session, ccy, a.deltas, a.days, a.hold_days)
        if not rows:
            print(f"{ccy}: insufficient overlapping DVOL/price history, skipped")
            continue
        with (OUT / f"long_vol_{ccy}.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        for td in a.deltas:
            s = summarize(rows, td)
            if not s:
                continue
            print(f"{ccy:<5}{td:>7.2f}{s['n_weeks']:>5}{100*s['win_rate']:>7.1f}%"
                  f"{s['avg_weekly_return_on_premium_pct']:>9.2f}%{s['avg_monthly_return_pct_additive']:>10.1f}%"
                  f"{s['pooled_return_on_total_premium_pct']:>9.1f}%{s['worst_week_pct']:>10.1f}%{s['best_week_pct']:>9.1f}%")

        # Real live bid-ask spread on a currently-listed deep-OTM put, as a friction-cost
        # sanity check the backtest itself cannot model (Deribit has no historical
        # option order-book data -- see options_premium_scan.py's docstring).
        live = live_validation(session, ccy, target_delta=-0.05)
        if live and live.get("best_bid") and live.get("best_ask") and live["best_ask"] > 0:
            mid = (live["best_bid"] + live["best_ask"]) / 2.0
            spread_pct_of_mid = 100.0 * (live["best_ask"] - live["best_bid"]) / mid if mid > 0 else float("nan")
            print(f"  live friction check ({ccy}, ~5-delta put {live['instrument_name']}): "
                  f"bid/ask={live['best_bid']}/{live['best_ask']} (coin-denominated) "
                  f"spread={spread_pct_of_mid:.1f}% of mid -- this one-way round-trip cost is NOT "
                  f"deducted in the backtest above and is paid on EVERY weekly roll, win or lose")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
