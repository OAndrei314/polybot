"""
Thread B follow-up: broaden the funding-basis coin universe beyond kBONK/ENA/PUMP.

hyperliquid_leveraged_basis.py (iteration 2) only tested 3 pre-picked coins and found
raw funding APR of just 7-8%/year -- nowhere near the 60-80%/year needed to clear a
5%/month net-yield floor even with survivable leverage. This script asks the broader
question: across ALL ~230 coins Hyperliquid lists, does ANY coin currently have a high
enough (and sustained, not just momentarily spiky) funding rate to change that verdict?

Method (100% real data, no synthetic inputs):
  1. `metaAndAssetCtxs` -- one call, returns current hourly funding rate for every
     listed coin. Annualize and rank descending to find live standout candidates.
  2. For the top N positive-funding candidates (plus BTC/ETH as a liquid baseline),
     pull 90d of real `fundingHistory` (hourly) and `candleSnapshot` (1h close) --
     same functions/methodology as the validated Thread B script -- to check whether
     the live snapshot reflects a SUSTAINED historical rate or a momentary spike, and
     to compute the same liquidation-survivability numbers.
  3. Report: current snapshot APR vs 90d-mean APR (spike-detector), day $ volume
     (capital-capacity ceiling), and net APR at the highest 90d-survivable leverage.

    python -m research.hyperliquid_funding_universe_scan
Outputs: console tables + data/edge_latency/hyperliquid_funding_universe.csv
"""
from __future__ import annotations

import csv
import statistics as st
import time
from pathlib import Path

import requests

OUT = Path("data/edge_latency")
API = "https://api.hyperliquid.xyz/info"
DAY_MS = 86_400_000
TOP_N = 8
HISTORY_DAYS = 90


def post(S: requests.Session, body: dict) -> object:
    r = S.post(API, json=body, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_funding_history(S: requests.Session, coin: str, start_ms: int, end_ms: int) -> list[dict]:
    out: list[dict] = []
    cur = start_ms
    while cur < end_ms:
        j = post(S, {"type": "fundingHistory", "coin": coin, "startTime": cur, "endTime": end_ms})
        if not isinstance(j, list) or not j:
            break
        out.extend(j)
        last_t = j[-1]["time"]
        if last_t <= cur:
            break
        cur = last_t + 1
        if len(j) < 500:
            break
        time.sleep(0.1)
    seen, dedup = set(), []
    for row in out:
        if row["time"] not in seen:
            seen.add(row["time"])
            dedup.append(row)
    return sorted(dedup, key=lambda r: r["time"])


def fetch_hourly_closes(S: requests.Session, coin: str, start_ms: int, end_ms: int) -> list[float]:
    j = post(S, {"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h",
                                                     "startTime": start_ms, "endTime": end_ms}})
    return [float(c["c"]) for c in j]


def max_adverse_move(closes: list[float], window_hours: int) -> float:
    n = len(closes)
    worst = 0.0
    for i in range(n):
        j_end = min(n, i + window_hours + 1)
        local_max = max(closes[i:j_end])
        move = local_max / closes[i] - 1.0
        if move > worst:
            worst = move
    return worst


def liq_move_pct(leverage: float, maintenance_frac: float = 0.05) -> float:
    return (1.0 / leverage - maintenance_frac) / (1.0 + maintenance_frac)


def main(argv=None) -> int:
    S = requests.Session()
    S.headers.update({"User-Agent": "Mozilla/5.0"})

    j = post(S, {"type": "metaAndAssetCtxs"})
    universe, ctx = j[0]["universe"], j[1]

    snapshot = []
    for u, c in zip(universe, ctx):
        try:
            f = float(c["funding"])
            vlm = float(c.get("dayNtlVlm", 0))
        except (KeyError, TypeError, ValueError):
            continue
        apr_now = f * 24 * 365 * 100
        snapshot.append({"coin": u["name"], "apr_now": apr_now, "max_lev": u["maxLeverage"], "day_vol": vlm})

    snapshot.sort(key=lambda r: -r["apr_now"])
    print(f"Full Hyperliquid universe scanned: {len(snapshot)} coins (live current funding, real data)\n")
    print("Top 15 by CURRENT annualized funding APR (single hourly print -- noisy, not yet validated):")
    hdr = f"{'coin':<8} {'aprNow':>8} {'maxLev':>7} {'dayVol$':>14}"
    print(hdr); print("-" * len(hdr))
    for r in snapshot[:15]:
        print(f"{r['coin']:<8} {r['apr_now']:>7.1f}% {r['max_lev']:>6}x {r['day_vol']:>14,.0f}")

    candidates = [r for r in snapshot if r["apr_now"] > 0][:TOP_N]
    candidates += [r for r in snapshot if r["coin"] in ("BTC", "ETH")]  # liquid baseline

    now = int(time.time() * 1000)
    start = now - HISTORY_DAYS * DAY_MS

    print(f"\nPulling {HISTORY_DAYS}d real funding history + price history for "
          f"{len(candidates)} candidates to check sustainability...\n")

    rows = []
    for cand in candidates:
        coin = cand["coin"]
        fh = fetch_funding_history(S, coin, start, now)
        closes = fetch_hourly_closes(S, coin, start, now)
        if len(fh) < 100 or len(closes) < 100:
            print(f"  [skip] {coin}: insufficient history (funding={len(fh)}, candles={len(closes)})")
            continue

        rates = [float(x["fundingRate"]) for x in fh]
        actual_days = (fh[-1]["time"] - fh[0]["time"]) / DAY_MS
        periods_per_yr = len(rates) / actual_days * 365 if actual_days > 0 else 0
        mean_r = st.mean(rates)
        apr_90d = mean_r * periods_per_yr * 100
        pos_frac = sum(1 for r in rates if r > 0) / len(rates)

        adverse = {w: max_adverse_move(closes, w) * 100 for w in (1, 24, 168)}

        # highest leverage that survived every 24h window in the real 90d history
        safe_lev = 1
        for L in (1, 2, 3, 5, 7, 10, 20):
            if L > cand["max_lev"]:
                break
            if adverse[24] < liq_move_pct(L) * 100:
                safe_lev = L
            else:
                break
        net_apr_at_safe_lev = apr_90d * safe_lev / (safe_lev + 1)

        rows.append({
            "coin": coin, "apr_now_snapshot": round(cand["apr_now"], 1),
            "apr_90d_mean": round(apr_90d, 1), "pos_frac_90d": round(pos_frac * 100, 0),
            "day_vol_usd": round(cand["day_vol"]), "max_lev": cand["max_lev"],
            "safe_lev_90d": safe_lev, "net_apr_at_safe_lev": round(net_apr_at_safe_lev, 1),
            "net_pct_per_month": round(net_apr_at_safe_lev / 12, 2),
            "worst_24h_move_pct": round(adverse[24], 2), "worst_7d_move_pct": round(adverse[168], 2),
        })
        time.sleep(0.1)

    if not rows:
        print("No candidate history pulled -- HL API issue.")
        return 1

    print("\n=== 90-day REAL history vs live snapshot (spike-detector) ===")
    hdr2 = (f"{'coin':<8} {'aprNow':>8} {'apr90dMean':>11} {'pos%':>5} {'dayVol$':>13} "
            f"{'safeLev':>8} {'netAPR@lev':>11} {'net%/mo':>8} {'worst24h%':>10} {'worst7d%':>9}")
    print(hdr2); print("-" * len(hdr2))
    for r in rows:
        print(f"{r['coin']:<8} {r['apr_now_snapshot']:>7.1f}% {r['apr_90d_mean']:>10.1f}% "
              f"{r['pos_frac_90d']:>4.0f}% {r['day_vol_usd']:>13,.0f} {r['safe_lev_90d']:>7.0f}x "
              f"{r['net_apr_at_safe_lev']:>10.1f}% {r['net_pct_per_month']:>7.2f}% "
              f"{r['worst_24h_move_pct']:>9.2f}% {r['worst_7d_move_pct']:>8.2f}%")

    print("\n=== VERDICT ===")
    best = max(rows, key=lambda r: r["net_pct_per_month"])
    clears_5pct = [r for r in rows if r["net_pct_per_month"] >= 5.0]
    print(f"  Best candidate by 90d-mean net%/month at survivable leverage: "
          f"{best['coin']} at {best['net_pct_per_month']:.2f}%/month "
          f"(90d-mean raw APR {best['apr_90d_mean']:.1f}%, live snapshot was {best['apr_now_snapshot']:.1f}%)")
    if clears_5pct:
        print(f"  {len(clears_5pct)} coin(s) clear 5%/month on 90d-mean funding at survivable leverage: "
              f"{[r['coin'] for r in clears_5pct]}")
    else:
        print("  0 coins clear 5%/month on 90d-mean (sustained) funding at survivable leverage.")
    print("  Cross-check: does the live single-hour snapshot match the 90d mean, or is it a spike?")
    for r in rows:
        ratio = r["apr_now_snapshot"] / r["apr_90d_mean"] if r["apr_90d_mean"] else float("inf")
        flag = "SPIKE (snapshot >> sustained mean)" if abs(ratio) > 3 or (r["apr_90d_mean"] <= 0 < r["apr_now_snapshot"]) else "consistent"
        print(f"    {r['coin']}: snapshot/90d-mean ratio = {ratio:.2f}x -> {flag}")

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "hyperliquid_funding_universe.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"\n  saved {OUT/'hyperliquid_funding_universe.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
