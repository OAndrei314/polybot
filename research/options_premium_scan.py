"""
Deribit options premium-selling backtest (Thread A: crypto options premium selling).

Concept under test: sell a short-dated (weekly) out-of-the-money put on BTC/ETH,
collect the premium, roll every week. High-IV environments are supposed to pay a
premium above realized vol, so systematically selling should be +EV before
tail risk.

Data reality check: Deribit's public REST does NOT expose historical option
order-book snapshots (no way to ask "what was the 10-delta BTC put worth on
2026-03-14"). What IS available, live and real:
  - DVOL (Deribit's official BTC/ETH implied-vol index), hourly/12h resolution,
    up to ~200 days back via get_volatility_index_data.
  - Daily underlying close price via the BTC-PERPETUAL / ETH-PERPETUAL
    tradingview chart endpoint, same lookback.
  - Live order-book greeks (real delta) for currently-listed instruments via
    get_order_book, used here only to sanity-check the pricing model against
    an actual live quote (not part of the historical backtest).

So the 6-month "backtest" below is a Black-Scholes SIMULATION: real historical
spot price + real historical ATM IV (DVOL) are fed into BS to price a put at
the strike that model-implies a target delta, then the *actual* historical
spot 7 days later (also real data) determines assignment/worthless outcome and
P&L. This is disclosed as a model limitation, not hidden:
  - DVOL is ATM/index-wide IV; real 10-delta puts trade at a volatility skew
    premium above ATM (documented smile), so this likely UNDERSTATES real-world
    premium collected (understates the return, but also proportionally
    understates the model's implied strike distance / assignment frequency
    changes only mildly since delta is solved directly from the same sigma).
  - r (risk-free rate) is set to 0, standard simplification for crypto options.
  - No transaction costs / bid-ask spread deducted in the simulation loop
    (reported separately from a live order-book quote below).

    python -m research.options_premium_scan
    python -m research.options_premium_scan --currencies BTC ETH --deltas 0.05 0.10 0.20
Outputs: console table + data/edge_latency/options_premium_<CCY>.csv
"""
from __future__ import annotations

import argparse
import csv
import math
import time
from pathlib import Path

import requests

OUT = Path("data/edge_latency")
BASE = "https://www.deribit.com/api/v2/public"
DAY_MS = 86_400_000


def _get(session: requests.Session, path: str, params: dict):
    r = session.get(f"{BASE}/{path}", params=params, timeout=20)
    r.raise_for_status()
    j = r.json()
    if "result" not in j:
        raise RuntimeError(f"bad response for {path}: {j}")
    return j["result"]


def fetch_dvol_history(session: requests.Session, currency: str, days: int = 200) -> dict[int, float]:
    """Returns {day_index_ms (UTC midnight-aligned): close IV in %} using 12h candles."""
    now = int(time.time() * 1000)
    start = now - days * DAY_MS
    res = _get(session, "get_volatility_index_data", {
        "currency": currency, "start_timestamp": start, "end_timestamp": now, "resolution": 43200,
    })
    out = {}
    for ts, o, h, l, c in res["data"]:
        day = (ts // DAY_MS) * DAY_MS
        out[day] = c  # last candle of the day wins (dict overwrite, data is time-ordered)
    return out


def fetch_price_history(session: requests.Session, currency: str, days: int = 200) -> dict[int, float]:
    """Returns {day_index_ms: daily close price} from the perpetual's daily candles."""
    now = int(time.time() * 1000)
    start = now - days * DAY_MS
    res = _get(session, "get_tradingview_chart_data", {
        "instrument_name": f"{currency}-PERPETUAL", "start_timestamp": start, "end_timestamp": now, "resolution": "1D",
    })
    if res.get("status") != "ok" or not res.get("ticks"):
        return {}
    out = {}
    for ts, close in zip(res["ticks"], res["close"]):
        day = (ts // DAY_MS) * DAY_MS
        out[day] = close
    return out


# ---------------------------------------------------------------------------
# Black-Scholes (r=0), solved for the strike that gives a target put delta.
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def put_price_and_strike_for_delta(S: float, sigma_annual_pct: float, T_years: float, target_delta: float) -> tuple[float, float]:
    """target_delta is negative (e.g. -0.10). Returns (strike, put_premium)."""
    sigma = sigma_annual_pct / 100.0
    if sigma <= 0 or T_years <= 0:
        return S, 0.0
    # put delta = N(d1) - 1  =>  N(d1) = 1 + target_delta
    target_Nd1 = 1.0 + target_delta
    target_Nd1 = min(max(target_Nd1, 1e-6), 1 - 1e-6)
    # invert normal CDF via bisection on d1
    lo, hi = -8.0, 8.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if _norm_cdf(mid) < target_Nd1:
            lo = mid
        else:
            hi = mid
    d1 = (lo + hi) / 2.0
    sqrtT = math.sqrt(T_years)
    # d1 = (ln(S/K) + 0.5*sigma^2*T) / (sigma*sqrt(T))   [r=0]
    ln_S_K = d1 * sigma * sqrtT - 0.5 * sigma * sigma * T_years
    K = S / math.exp(ln_S_K)
    d2 = d1 - sigma * sqrtT
    premium = K * _norm_cdf(-d2) - S * _norm_cdf(-d1)
    return K, max(premium, 0.0)


def backtest_currency(session: requests.Session, currency: str, deltas: list[float], days: int, hold_days: int,
                       dvol: dict[int, float] | None = None) -> list[dict]:
    if dvol is None:
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
            K, premium = put_price_and_strike_for_delta(S0, iv0, T, -target_delta)
            assigned = S1 < K
            loss = max(K - S1, 0.0)
            pnl = premium - loss
            rows.append({
                "currency": currency,
                "entry_day": d0,
                "expiry_day": d1,
                "target_delta": target_delta,
                "spot_entry": S0,
                "spot_expiry": S1,
                "iv_pct": iv0,
                "strike": K,
                "premium": premium,
                "assigned": assigned,
                "pnl": pnl,
                "collateral": K,
                "return_on_collateral_pct": 100.0 * pnl / K,
            })
        i += step
    return rows


def live_validation(session: requests.Session, currency: str, target_delta: float = -0.10) -> dict | None:
    """Find the currently-listed put closest to target_delta near a ~7-10 day expiry
    and pull its REAL live order-book (mark price, IV, greeks) as a sanity check
    against what the model would predict using current DVOL."""
    instruments = _get(session, "get_instruments", {"currency": currency, "kind": "option", "expired": "false"})
    now = int(time.time() * 1000)
    exps = sorted(set(x["expiration_timestamp"] for x in instruments if x["expiration_timestamp"] > now))
    target_exp = None
    for e in exps:
        dte = (e - now) / DAY_MS
        if 5 <= dte <= 12:
            target_exp = e
            break
    if target_exp is None and exps:
        target_exp = exps[0]
    if target_exp is None:
        return None

    puts = [x for x in instruments if x["expiration_timestamp"] == target_exp and x["option_type"] == "put"]
    puts.sort(key=lambda x: x["strike"])
    # narrow to a plausible OTM band to limit API calls (5-40% below spot)
    idx_book = _get(session, "get_book_summary_by_currency", {"currency": currency, "kind": "option"})
    underlying = None
    for b in idx_book:
        if b["instrument_name"] in [p["instrument_name"] for p in puts]:
            underlying = b.get("underlying_price")
            if underlying:
                break
    if not underlying:
        return None

    candidates = [p for p in puts if 0.55 * underlying <= p["strike"] <= 0.97 * underlying]
    best = None
    best_diff = None
    for p in candidates:
        try:
            ob = _get(session, "get_order_book", {"instrument_name": p["instrument_name"]})
        except Exception:
            continue
        greeks = ob.get("greeks") or {}
        delta = greeks.get("delta")
        if delta is None:
            continue
        diff = abs(delta - target_delta)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best = {
                "instrument_name": p["instrument_name"],
                "strike": p["strike"],
                "delta": delta,
                "mark_iv": ob.get("mark_iv"),
                "mark_price_coin": ob.get("mark_price"),
                "underlying_price": ob.get("underlying_price"),
                "best_bid": ob.get("best_bid_price"),
                "best_ask": ob.get("best_ask_price"),
                "days_to_expiry": (target_exp - now) / DAY_MS,
            }
        time.sleep(0.05)
    return best


def iv_rank(dvol: dict[int, float]) -> dict:
    vals = sorted(dvol.values())
    cur_day = max(dvol)
    cur = dvol[cur_day]
    below = sum(1 for v in vals if v <= cur)
    pct_rank = 100.0 * below / len(vals)
    return {"current_iv": cur, "min": vals[0], "max": vals[-1], "percentile_rank": pct_rank, "n_days": len(vals)}


def summarize(rows: list[dict], target_delta: float) -> dict:
    sub = [r for r in rows if r["target_delta"] == target_delta]
    if not sub:
        return {}
    n = len(sub)
    n_assigned = sum(1 for r in sub if r["assigned"])
    total_pnl_pct = sum(r["return_on_collateral_pct"] for r in sub)
    avg_weekly_pct = total_pnl_pct / n
    worst = min(sub, key=lambda r: r["return_on_collateral_pct"])
    weeks_per_month = 30.0 / 7.0
    return {
        "n_weeks": n,
        "win_rate": 1 - n_assigned / n,
        "avg_weekly_return_pct": avg_weekly_pct,
        "avg_monthly_return_pct_additive": avg_weekly_pct * weeks_per_month,
        "worst_week_pct": worst["return_on_collateral_pct"],
        "worst_week_day": worst["entry_day"],
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Deribit options premium-selling backtest (simulated)")
    p.add_argument("--currencies", nargs="+", default=["BTC", "ETH"])
    p.add_argument("--deltas", nargs="+", type=float, default=[0.05, 0.10, 0.20])
    p.add_argument("--days", type=int, default=195, help="lookback window (capped by DVOL API ~200d)")
    p.add_argument("--hold-days", type=int, default=7, help="days between roll (7 = weekly)")
    a = p.parse_args(argv)

    OUT.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    print(f"{'ccy':<5}{'delta':>7}{'n':>5}{'win%':>8}{'avg wk%':>10}{'~mo%':>8}{'worst wk%':>11}")
    for ccy in a.currencies:
        dvol_hist = fetch_dvol_history(session, ccy, a.days)
        if dvol_hist:
            ivr = iv_rank(dvol_hist)
            print(f"{ccy} IV rank: current={ivr['current_iv']:.1f}% range=[{ivr['min']:.1f}%,{ivr['max']:.1f}%] "
                  f"percentile={ivr['percentile_rank']:.0f}% (n={ivr['n_days']}d)")
        rows = backtest_currency(session, ccy, a.deltas, a.days, a.hold_days, dvol=dvol_hist)
        if not rows:
            print(f"{ccy}: insufficient overlapping DVOL/price history, skipped")
            continue
        with (OUT / f"options_premium_{ccy}.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        for td in a.deltas:
            s = summarize(rows, td)
            if not s:
                continue
            print(f"{ccy:<5}{td:>7.2f}{s['n_weeks']:>5}{100*s['win_rate']:>7.1f}%"
                  f"{s['avg_weekly_return_pct']:>9.2f}%{s['avg_monthly_return_pct_additive']:>7.1f}%"
                  f"{s['worst_week_pct']:>10.1f}%")

        live = live_validation(session, ccy)
        if live:
            print(f"  live check ({ccy}): {live['instrument_name']} delta={live['delta']:.3f} "
                  f"mark_iv={live['mark_iv']:.1f}% strike={live['strike']:.0f} spot={live['underlying_price']:.0f} "
                  f"dte={live['days_to_expiry']:.1f}d bid/ask={live['best_bid']}/{live['best_ask']} (coin-denominated)")
        else:
            print(f"  live check ({ccy}): no matching near-dated instrument found")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
