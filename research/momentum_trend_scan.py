"""
Thread J: leveraged trend-following / momentum on Hyperliquid perps.

Every prior thread in this objective (A-I) tried to harvest a MARKET-NEUTRAL
spread (funding, basis, options premium, LP fees, counterparty flow) and all 13
of them (6 named + 7 post-A-F follow-ups) landed below the 5%/month floor,
usually in the 0-2.5%/month band, because the current market regime has thin
basis almost everywhere. This thread deliberately tests a DIRECTIONAL
mechanism instead -- the objective explicitly allows this ("Will accept high
risk if the expected value is genuinely positive... do not filter out
high-risk strategies, just disclose risk clearly").

Concept: a simple, fully mechanical trend-following signal (EMA crossover) on
BTC/ETH/SOL perps, backtested on real daily closes, with leverage overlaid.
No lookahead, no curve-fit per-coin parameters (one fixed rule tested on all
three coins) -- report real CAGR, monthly-return distribution, Sharpe, and
max drawdown at 1x/2x/3x/5x, plus the leverage level at which the strategy's
own worst historical drawdown would have caused liquidation.

Data pulled, 100% real:
  - Hyperliquid `candleSnapshot` (1d close), BTC/ETH/SOL, longest available
    history (HL perps launched inception ~2023, we pull up to 700 days)

    python -m research.momentum_trend_scan
Outputs: console table + data/edge_latency/momentum_trend_scan.csv
"""
from __future__ import annotations

import argparse
import csv
import statistics as st
import time
from pathlib import Path

import requests

OUT = Path("data/edge_latency")
API = "https://api.hyperliquid.xyz/info"
COINS = ["BTC", "ETH", "SOL"]
DAY_MS = 86_400_000
TAKER_FEE = 0.00035  # HL taker fee ~3.5bps, applied per side per rebalance


def post(S: requests.Session, body: dict) -> object:
    r = S.post(API, json=body, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_daily_closes(S: requests.Session, coin: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    j = post(S, {"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d",
                                                     "startTime": start_ms, "endTime": end_ms}})
    return [(c["t"], float(c["c"])) for c in j]


def ema(series: list[float], span: int) -> list[float]:
    k = 2 / (span + 1)
    out = [series[0]]
    for x in series[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def backtest(closes: list[float], fast: int, slow: int, leverage: float) -> dict:
    """EMA(fast) vs EMA(slow) crossover, long when fast>slow, short when fast<slow,
    always-in-market (flip, no flat period). Position sized so max single-day
    move at `leverage` is the exposure; returns are daily leveraged % changes
    with a taker-fee charge only on days the position flips."""
    fast_e = ema(closes, fast)
    slow_e = ema(closes, slow)
    n = len(closes)
    daily_ret = []
    position = 0  # start flat until first signal after slow EMA warms up
    warmup = slow * 2  # give the slow EMA time to stabilize before trading on it
    equity_curve = [1.0]
    worst_1d_move_against = 0.0  # largest single-day adverse % move while in a position
    flips = 0
    for i in range(warmup, n - 1):
        sig = 1 if fast_e[i] > slow_e[i] else -1
        px_t, px_t1 = closes[i], closes[i + 1]
        day_ret = (px_t1 / px_t - 1.0) * sig
        levered_ret = day_ret * leverage
        if sig != position:
            levered_ret -= TAKER_FEE * leverage * 2  # open+close leg on flip
            flips += 1
            position = sig
        daily_ret.append(levered_ret)
        equity_curve.append(equity_curve[-1] * (1 + levered_ret))
        adverse_move = -day_ret  # negative day_ret means price moved against the held position
        if adverse_move > worst_1d_move_against:
            worst_1d_move_against = adverse_move

    if not daily_ret:
        return {}

    # max drawdown on the equity curve
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        dd = (v / peak) - 1.0
        max_dd = min(max_dd, dd)

    n_days = len(daily_ret)
    total_return = equity_curve[-1] - 1.0
    years = n_days / 365.0
    cagr = (equity_curve[-1] ** (1 / years) - 1) if years > 0 and equity_curve[-1] > 0 else float("nan")
    monthly_equiv = (equity_curve[-1] ** (30 / n_days) - 1) if equity_curve[-1] > 0 else float("nan")
    mean_d = st.mean(daily_ret)
    sd_d = st.pstdev(daily_ret) if len(daily_ret) > 1 else 0.0
    sharpe_ann = (mean_d / sd_d) * (365 ** 0.5) if sd_d > 0 else float("nan")

    return {
        "n_days": n_days, "flips": flips, "total_return_pct": total_return * 100,
        "cagr_pct": cagr * 100, "monthly_equiv_pct": monthly_equiv * 100,
        "sharpe_ann": sharpe_ann, "max_drawdown_pct": max_dd * 100,
        "worst_1d_adverse_move_pct": worst_1d_move_against * 100,
    }


def liq_move_pct(leverage: float, maintenance_frac: float = 0.05) -> float:
    return (1.0 / leverage - maintenance_frac) / (1.0 + maintenance_frac) * 100


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Leveraged trend-following backtest on Hyperliquid perps (Thread J)")
    p.add_argument("--coins", nargs="+", default=COINS)
    p.add_argument("--days", type=int, default=700)
    p.add_argument("--fast", type=int, default=10)
    p.add_argument("--slow", type=int, default=30)
    p.add_argument("--leverages", nargs="+", type=float, default=[1, 2, 3, 5])
    a = p.parse_args(argv)

    S = requests.Session()
    S.headers.update({"User-Agent": "Mozilla/5.0"})

    now = int(time.time() * 1000)
    start = now - a.days * DAY_MS

    rows = []
    for coin in a.coins:
        closes_t = fetch_daily_closes(S, coin, start, now)
        closes = [c for _, c in closes_t]
        if len(closes) < a.slow * 3:
            print(f"  [skip] {coin} insufficient history ({len(closes)} days)")
            continue
        actual_days = (closes_t[-1][0] - closes_t[0][0]) / DAY_MS
        print(f"  {coin}: {len(closes)} daily closes, {actual_days:.0f}d real history "
              f"({closes_t[0][0]}..{closes_t[-1][0]})")

        for L in a.leverages:
            res = backtest(closes, a.fast, a.slow, L)
            if not res:
                continue
            liq_pct = liq_move_pct(L)
            res.update({"coin": coin, "leverage": L, "liq_move_pct": round(liq_pct, 2),
                        "would_have_liquidated": res["worst_1d_adverse_move_pct"] >= liq_pct})
            rows.append(res)

    if not rows:
        print("No data pulled - HL API may be unreachable.")
        return 1

    print(f"\nThread J: EMA({a.fast}/{a.slow}) crossover trend-following, leveraged, real HL daily closes\n")
    hdr = (f"{'coin':<5} {'lev':>4} {'nDays':>6} {'flips':>6} {'CAGR%':>9} {'mo.equiv%':>10} "
           f"{'sharpe':>7} {'maxDD%':>8} {'worst1dAdv%':>12} {'liqMove%':>9} {'liquidated?':>11}")
    print(hdr); print("-" * len(hdr))
    for r in rows:
        print(f"{r['coin']:<5} {r['leverage']:>4.0f} {r['n_days']:>6} {r['flips']:>6} "
              f"{r['cagr_pct']:>8.1f}% {r['monthly_equiv_pct']:>9.2f}% {r['sharpe_ann']:>7.2f} "
              f"{r['max_drawdown_pct']:>7.1f}% {r['worst_1d_adverse_move_pct']:>11.2f}% "
              f"{r['liq_move_pct']:>8.2f}% {str(r['would_have_liquidated']):>11}")

    print("\n=== VERDICT ===")
    unlevered = [r for r in rows if r["leverage"] == 1]
    clears_bar = [r for r in rows if r["monthly_equiv_pct"] >= 5.0 and not r["would_have_liquidated"]]
    liquidated_any = [r for r in rows if r["would_have_liquidated"]]
    for r in unlevered:
        print(f"  {r['coin']} @1x: CAGR {r['cagr_pct']:.1f}%, monthly-equiv {r['monthly_equiv_pct']:.2f}%, "
              f"max drawdown {r['max_drawdown_pct']:.1f}%, Sharpe {r['sharpe_ann']:.2f}")
    print(f"\n  Rows clearing 5%/month AND surviving their own worst historical day at that "
          f"leverage: {len(clears_bar)} / {len(rows)}")
    print(f"  Rows where the historical worst single-day adverse move would have liquidated "
          f"the position outright: {len(liquidated_any)} / {len(rows)}")

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "momentum_trend_scan.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"\n  saved {OUT/'momentum_trend_scan.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
