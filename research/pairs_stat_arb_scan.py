"""
Thread K: statistical arbitrage / mean-reversion pairs trading on correlated
crypto majors (Hyperliquid perps).

Every prior mechanism in this objective was either market-neutral yield
harvesting (funding, basis, options premium, LP fees, counterparty flow --
14 variants, all dead, best XMR funding-basis at 2.04%/month) or single-asset
directional momentum (Thread J, dead, 2.22%/month best case with -33.5% max
drawdown). This thread tests a THIRD, genuinely distinct mechanism family:
relative-value / pairs mean-reversion between two correlated coins. This is
still market-neutral (equal-notional long one leg, short the other) but the
signal is price-ratio divergence, not funding or basis -- a real, previously
untested angle.

Concept: compute the log-price ratio (spread) between two historically
correlated coins (BTC/ETH, ETH/SOL, BTC/SOL), z-score it over a rolling
window, and mechanically fade extreme divergence (short the outperformer /
long the underperformer when |z| exceeds an entry threshold, exit when the
spread reverts toward its rolling mean). No lookahead: the rolling mean/std
at day t only uses days <= t.

Data pulled, 100% real:
  - Hyperliquid `candleSnapshot` (1d close), BTC/ETH/SOL/AVAX/LINK, up to
    700 days (same endpoint/venue used in Threads B, D, J, G-cross-checks)

    python -m research.pairs_stat_arb_scan
Outputs: console table + data/edge_latency/pairs_stat_arb_scan.csv
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics as st
import time
from itertools import combinations
from pathlib import Path

import requests

OUT = Path("data/edge_latency")
API = "https://api.hyperliquid.xyz/info"
COINS = ["BTC", "ETH", "SOL", "AVAX", "LINK"]
DAY_MS = 86_400_000
TAKER_FEE = 0.00035  # HL taker fee ~3.5bps per side per fill


def post(S: requests.Session, body: dict) -> object:
    r = S.post(API, json=body, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_daily_closes(S: requests.Session, coin: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    j = post(S, {"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d",
                                                     "startTime": start_ms, "endTime": end_ms}})
    return [(c["t"], float(c["c"])) for c in j]


def rolling_zscore(spread: list[float], window: int) -> list[float]:
    """z-score of spread[i] using only spread[i-window:i] (no lookahead); NaN until warmed up."""
    z = [float("nan")] * len(spread)
    for i in range(window, len(spread)):
        hist = spread[i - window:i]
        mu = st.mean(hist)
        sd = st.pstdev(hist)
        z[i] = (spread[i] - mu) / sd if sd > 0 else 0.0
    return z


def backtest_pair(closes_a: list[float], closes_b: list[float], window: int,
                   entry_z: float, exit_z: float, leverage: float) -> dict:
    n = min(len(closes_a), len(closes_b))
    a, b = closes_a[:n], closes_b[:n]
    spread = [math.log(a[i]) - math.log(b[i]) for i in range(n)]
    z = rolling_zscore(spread, window)

    position = 0  # +1 = long spread (long A, short B), -1 = short spread, 0 = flat
    daily_ret = []
    equity_curve = [1.0]
    trades = 0
    worst_1d_adverse = 0.0
    for i in range(window, n - 1):
        zi = z[i]
        if math.isnan(zi):
            daily_ret.append(0.0)
            equity_curve.append(equity_curve[-1])
            continue

        new_position = position
        if position == 0:
            if zi >= entry_z:
                new_position = -1  # spread too high -> short A, long B
            elif zi <= -entry_z:
                new_position = 1
        else:
            if abs(zi) <= exit_z:
                new_position = 0

        # daily return of the spread itself (long A/short B captures spread_ret; short is -spread_ret)
        ret_a = a[i + 1] / a[i] - 1.0
        ret_b = b[i + 1] / b[i] - 1.0
        spread_ret = ret_a - ret_b  # equal-notional long A short B pnl (approx, on 1 unit each side)
        day_ret = spread_ret * position * leverage

        if new_position != position:
            trades += 1
            day_ret -= TAKER_FEE * leverage * 4  # close 2 legs + open 2 legs
            position = new_position

        daily_ret.append(day_ret)
        equity_curve.append(equity_curve[-1] * (1 + day_ret))
        if position != 0 and -day_ret > worst_1d_adverse:
            worst_1d_adverse = -day_ret

    if not daily_ret or all(r == 0 for r in daily_ret):
        return {}

    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        max_dd = min(max_dd, (v / peak) - 1.0)

    n_days = len(daily_ret)
    total_return = equity_curve[-1] - 1.0
    monthly_equiv = (equity_curve[-1] ** (30 / n_days) - 1) if equity_curve[-1] > 0 else float("nan")
    mean_d = st.mean(daily_ret)
    sd_d = st.pstdev(daily_ret) if len(daily_ret) > 1 else 0.0
    sharpe_ann = (mean_d / sd_d) * (365 ** 0.5) if sd_d > 0 else float("nan")
    days_in_position = sum(1 for i in range(len(daily_ret)) if daily_ret[i] != 0)

    return {
        "n_days": n_days, "trades": trades, "total_return_pct": total_return * 100,
        "monthly_equiv_pct": monthly_equiv * 100, "sharpe_ann": sharpe_ann,
        "max_drawdown_pct": max_dd * 100, "worst_1d_adverse_pct": worst_1d_adverse * 100,
    }


def liq_move_pct(leverage: float, maintenance_frac: float = 0.05) -> float:
    return (1.0 / leverage - maintenance_frac) / (1.0 + maintenance_frac) * 100


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Pairs mean-reversion stat-arb backtest on Hyperliquid perps (Thread K)")
    p.add_argument("--coins", nargs="+", default=COINS)
    p.add_argument("--days", type=int, default=700)
    p.add_argument("--window", type=int, default=20)
    p.add_argument("--entry-z", type=float, default=2.0)
    p.add_argument("--exit-z", type=float, default=0.5)
    p.add_argument("--leverages", nargs="+", type=float, default=[1, 2, 3])
    a = p.parse_args(argv)

    S = requests.Session()
    S.headers.update({"User-Agent": "Mozilla/5.0"})

    now = int(time.time() * 1000)
    start = now - a.days * DAY_MS

    closes_by_coin = {}
    for coin in a.coins:
        ct = fetch_daily_closes(S, coin, start, now)
        closes = [c for _, c in ct]
        closes_by_coin[coin] = closes
        print(f"  {coin}: {len(closes)} daily closes pulled")

    rows = []
    for coin_a, coin_b in combinations(a.coins, 2):
        ca, cb = closes_by_coin[coin_a], closes_by_coin[coin_b]
        if len(ca) < a.window * 3 or len(cb) < a.window * 3:
            continue
        for L in a.leverages:
            res = backtest_pair(ca, cb, a.window, a.entry_z, a.exit_z, L)
            if not res:
                continue
            liq_pct = liq_move_pct(L)
            res.update({"pair": f"{coin_a}/{coin_b}", "leverage": L, "liq_move_pct": round(liq_pct, 2),
                        "would_have_liquidated": res["worst_1d_adverse_pct"] >= liq_pct})
            rows.append(res)

    if not rows:
        print("No data pulled - HL API may be unreachable, or insufficient history.")
        return 1

    print(f"\nThread K: pairs mean-reversion stat-arb (window={a.window}d, entry_z={a.entry_z}, "
          f"exit_z={a.exit_z}), real HL daily closes\n")
    hdr = (f"{'pair':<9} {'lev':>4} {'nDays':>6} {'trades':>7} {'mo.equiv%':>10} "
           f"{'sharpe':>7} {'maxDD%':>8} {'worst1dAdv%':>12} {'liqMove%':>9} {'liquidated?':>11}")
    print(hdr); print("-" * len(hdr))
    for r in rows:
        print(f"{r['pair']:<9} {r['leverage']:>4.0f} {r['n_days']:>6} {r['trades']:>7} "
              f"{r['monthly_equiv_pct']:>9.2f}% {r['sharpe_ann']:>7.2f} "
              f"{r['max_drawdown_pct']:>7.1f}% {r['worst_1d_adverse_pct']:>11.2f}% "
              f"{r['liq_move_pct']:>8.2f}% {str(r['would_have_liquidated']):>11}")

    print("\n=== VERDICT ===")
    clears_bar = [r for r in rows if r["monthly_equiv_pct"] >= 5.0 and not r["would_have_liquidated"]]
    best = max(rows, key=lambda r: r["monthly_equiv_pct"])
    print(f"  Best row: {best['pair']} @{best['leverage']:.0f}x -> {best['monthly_equiv_pct']:.2f}%/month, "
          f"maxDD {best['max_drawdown_pct']:.1f}%, {best['trades']} trades over {best['n_days']} days")
    print(f"  Rows clearing 5%/month AND surviving their own worst historical day at that "
          f"leverage: {len(clears_bar)} / {len(rows)}")

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "pairs_stat_arb_scan.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"\n  saved {OUT/'pairs_stat_arb_scan.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
