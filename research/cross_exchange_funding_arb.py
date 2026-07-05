"""
New mechanism (not in the objective's original A-F list, flagged as an untested
follow-up in iteration 6): cross-exchange perp-perp funding-rate arbitrage.

Thread B (validated but sub-bar, see FINDINGS_BEYOND_POLYMARKET.md) captures funding by
holding SPOT long + PERP short on Hyperliquid. Its main disclosed risk is that the spot
leg lives in a different wallet/venue from the margin account, so capital can't always be
moved fast enough to top up margin during a violent move -- that's the actual liquidation
driver, not basis divergence (HL keeps mark~=oracle within bps per Thread D).

This script asks a different question: instead of spot+perp, go PERP+PERP on the SAME
coin across TWO exchanges (Hyperliquid vs Bybit) -- long the side with lower/negative
funding, short the side with higher funding. Both legs are perpetuals tracking the same
underlying, so:
  - you collect the funding RATE DIFFERENTIAL (potentially larger than any single
    exchange's rate, if the two venues' funding cycles diverge)
  - the delta-neutral pair should track each other tightly (both are perps arbitraged
    against a similar spot), which is a DIFFERENT (not necessarily smaller) risk profile
    than spot-vs-perp: now BOTH legs carry independent liquidation risk from a leveraged
    position, not just one.

Method (100% real data, no synthetic inputs):
  1. Pull live current funding for HL's full ~231-coin universe (metaAndAssetCtxs) and
     Bybit's full linear-perp universe (~500+ symbols via /v5/market/tickers), each
     already annualized using each venue's own funding interval (HL=1h always; Bybit
     varies per-symbol, from `fundingIntervalHour`).
  2. Match coins across the two naming schemes (HL "kBONK" <-> Bybit "1000BONKUSDT",
     etc.) and compute the live annualized differential.
  3. For the top-|differential| matched candidates, pull 30d of REAL funding history from
     BOTH exchanges (HL fundingHistory, Bybit /v5/market/funding/history) to check
     whether the live snapshot differential is sustained or a momentary spike (the same
     spike-detector methodology validated in iteration 7's hyperliquid_funding_universe_scan).
  4. Pull 30d of REAL hourly price history from both exchanges for the top sustained
     candidate and compute the cross-exchange price-tracking spread (same methodology as
     Thread D's cex_dex_arb.py) -- this is the actual risk metric for this mechanism,
     since a large cross-exchange price gap during a violent move would put one leg deep
     in the money and the other deep against, both independently margined.

    python -m research.cross_exchange_funding_arb
Outputs: console tables + data/edge_latency/cross_exchange_funding_arb.csv
"""
from __future__ import annotations

import csv
import re
import statistics as st
import time
from pathlib import Path

import requests

OUT = Path("data/edge_latency")
HL_API = "https://api.hyperliquid.xyz/info"
BYBIT_API = "https://api.bybit.com"
DAY_MS = 86_400_000
TOP_N = 10
HISTORY_DAYS = 30  # Bybit funding-history REST is slow to paginate; 30d is enough to
                    # distinguish "sustained" from "one-off spike" per iteration 7's own
                    # finding that spikes revert within days, not weeks.


def hl_post(S: requests.Session, body: dict) -> object:
    r = S.post(HL_API, json=body, timeout=20)
    r.raise_for_status()
    return r.json()


def normalize_bybit_symbol(sym: str) -> tuple[str, float] | None:
    """Strip USDT and any 1000x-style multiplier prefix; return (base, multiplier)."""
    if not sym.endswith("USDT"):
        return None
    base = sym[:-4]
    m = re.match(r"^(\d+)(.+)$", base)
    if m:
        mult = float(m.group(1))
        return m.group(2), mult
    return base, 1.0


def normalize_hl_coin(coin: str) -> tuple[str, float]:
    if coin.startswith("k") and len(coin) > 1 and coin[1].isupper():
        return coin[1:], 1000.0
    return coin, 1.0


def fetch_hl_funding_history(S: requests.Session, coin: str, start_ms: int, end_ms: int) -> list[dict]:
    out: list[dict] = []
    cur = start_ms
    while cur < end_ms:
        j = hl_post(S, {"type": "fundingHistory", "coin": coin, "startTime": cur, "endTime": end_ms})
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


def fetch_bybit_funding_history(S: requests.Session, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    out: list[dict] = []
    cursor_end = end_ms
    for _ in range(20):  # safety cap on pagination loops
        r = S.get(f"{BYBIT_API}/v5/market/funding/history",
                   params={"category": "linear", "symbol": symbol, "endTime": cursor_end, "limit": 200},
                   timeout=20)
        r.raise_for_status()
        j = r.json()
        lst = j.get("result", {}).get("list", [])
        if not lst:
            break
        out.extend(lst)
        oldest = min(int(x["fundingRateTimestamp"]) for x in lst)
        if oldest <= start_ms or len(lst) < 200:
            break
        cursor_end = oldest - 1
        time.sleep(0.1)
    seen, dedup = set(), []
    for row in out:
        t = int(row["fundingRateTimestamp"])
        if start_ms <= t <= end_ms and t not in seen:
            seen.add(t)
            dedup.append(row)
    return sorted(dedup, key=lambda r: int(r["fundingRateTimestamp"]))


def fetch_hl_hourly_closes(S: requests.Session, coin: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    j = hl_post(S, {"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h",
                                                        "startTime": start_ms, "endTime": end_ms}})
    return [(int(c["t"]), float(c["c"])) for c in j]


def fetch_bybit_hourly_closes(S: requests.Session, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    out = []
    cursor_end = end_ms
    for _ in range(6):
        r = S.get(f"{BYBIT_API}/v5/market/kline",
                   params={"category": "linear", "symbol": symbol, "interval": "60",
                            "end": cursor_end, "limit": 200}, timeout=20)
        r.raise_for_status()
        j = r.json()
        lst = j.get("result", {}).get("list", [])
        if not lst:
            break
        out.extend((int(row[0]), float(row[4])) for row in lst)  # [ts, o,h,l,close,...]
        oldest = min(int(row[0]) for row in lst)
        if oldest <= start_ms or len(lst) < 200:
            break
        cursor_end = oldest - 1
        time.sleep(0.1)
    seen, dedup = set(), []
    for t, c in out:
        if start_ms <= t <= end_ms and t not in seen:
            seen.add(t)
            dedup.append((t, c))
    return sorted(dedup)


def main(argv=None) -> int:
    S = requests.Session()
    S.headers.update({"User-Agent": "Mozilla/5.0"})

    # 1. Live HL universe
    j = hl_post(S, {"type": "metaAndAssetCtxs"})
    universe, ctx = j[0]["universe"], j[1]
    hl_live: dict[str, dict] = {}
    skipped_delisted = 0
    for u, c in zip(universe, ctx):
        if u.get("isDelisted"):
            skipped_delisted += 1
            continue
        try:
            f = float(c["funding"])
            oi = float(c.get("openInterest") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if oi <= 0:
            # zero open interest -> market is listed but untradeable (no live book depth);
            # STG was caught this way: isDelisted=True AND openInterest=0, funding pinned
            # at 0.0 for its entire history, which produced a spurious 38.87%/month
            # "candidate" before this filter was added.
            skipped_delisted += 1
            continue
        base, _ = normalize_hl_coin(u["name"])
        hl_live[base] = {"raw_coin": u["name"], "apr": f * 24 * 365 * 100, "max_lev": u["maxLeverage"]}
    if skipped_delisted:
        print(f"(filtered {skipped_delisted} delisted/zero-open-interest HL markets out of the universe)")

    # 2. Live Bybit universe
    r = S.get(f"{BYBIT_API}/v5/market/tickers", params={"category": "linear"}, timeout=20)
    r.raise_for_status()
    bybit_list = r.json()["result"]["list"]
    bybit_live: dict[str, dict] = {}
    for row in bybit_list:
        norm = normalize_bybit_symbol(row["symbol"])
        if norm is None:
            continue
        base, _ = norm
        try:
            fr = float(row["fundingRate"])
            interval_h = float(row.get("fundingIntervalHour") or 8)
            vol = float(row.get("turnover24h") or 0)
        except (TypeError, ValueError):
            continue
        if interval_h <= 0:
            continue
        apr = fr * (24 / interval_h) * 365 * 100
        # keep the higher-volume symbol if duplicate bases collide
        if base not in bybit_live or vol > bybit_live[base]["vol"]:
            bybit_live[base] = {"raw_symbol": row["symbol"], "apr": apr, "vol": vol, "interval_h": interval_h}

    print(f"HL universe: {len(hl_live)} coins live | Bybit linear universe: {len(bybit_live)} bases live\n")

    matched = []
    for base, hl in hl_live.items():
        by = bybit_live.get(base)
        if by is None:
            continue
        diff_apr = by["apr"] - hl["apr"]  # positive => short Bybit / long HL; negative => reverse
        matched.append({
            "base": base, "hl_coin": hl["raw_coin"], "bybit_symbol": by["raw_symbol"],
            "hl_apr": hl["apr"], "bybit_apr": by["apr"], "diff_apr": diff_apr,
            "bybit_vol24h": by["vol"], "hl_max_lev": hl["max_lev"],
        })

    print(f"Matched {len(matched)} coins listed on BOTH Hyperliquid and Bybit linear perps.\n")
    matched.sort(key=lambda r: -abs(r["diff_apr"]))

    print("Top 15 by |live funding-rate differential| (snapshot, noisy, not yet validated):")
    hdr = f"{'coin':<10} {'hlAPR':>9} {'bybitAPR':>9} {'diffAPR':>9} {'bybitVol24h$':>14}"
    print(hdr); print("-" * len(hdr))
    for r_ in matched[:15]:
        print(f"{r_['base']:<10} {r_['hl_apr']:>8.1f}% {r_['bybit_apr']:>8.1f}% "
              f"{r_['diff_apr']:>8.1f}% {r_['bybit_vol24h']:>14,.0f}")

    # filter for enough Bybit liquidity to be tradable at test-scale capital ($500-5000)
    liquid = [m for m in matched if m["bybit_vol24h"] > 500_000]
    candidates = liquid[:TOP_N]

    now = int(time.time() * 1000)
    start = now - HISTORY_DAYS * DAY_MS

    print(f"\nValidating top {len(candidates)} liquid candidates against {HISTORY_DAYS}d REAL "
          f"funding history on both venues (spike-detector)...\n")

    rows = []
    for cand in candidates:
        hl_coin, by_sym = cand["hl_coin"], cand["bybit_symbol"]
        try:
            hl_fh = fetch_hl_funding_history(S, hl_coin, start, now)
            by_fh = fetch_bybit_funding_history(S, by_sym, start, now)
        except requests.RequestException as e:
            print(f"  [skip] {cand['base']}: fetch error {e}")
            continue
        if len(hl_fh) < 50 or len(by_fh) < 5:
            print(f"  [skip] {cand['base']}: insufficient history (hl={len(hl_fh)}, bybit={len(by_fh)})")
            continue

        hl_rates = [float(x["fundingRate"]) for x in hl_fh]
        hl_days = (hl_fh[-1]["time"] - hl_fh[0]["time"]) / DAY_MS
        hl_apr_hist = st.mean(hl_rates) * (len(hl_rates) / hl_days * 365) * 100 if hl_days > 0 else 0.0

        by_rates = [float(x["fundingRate"]) for x in by_fh]
        by_days = (int(by_fh[-1]["fundingRateTimestamp"]) - int(by_fh[0]["fundingRateTimestamp"])) / DAY_MS
        by_apr_hist = st.mean(by_rates) * (len(by_rates) / by_days * 365) * 100 if by_days > 0 else 0.0

        diff_apr_hist = by_apr_hist - hl_apr_hist
        diff_apr_now = cand["diff_apr"]
        ratio = abs(diff_apr_now) / abs(diff_apr_hist) if diff_apr_hist else float("inf")
        spike = abs(ratio) > 3 or (abs(diff_apr_hist) < 1 and abs(diff_apr_now) > 5)

        rows.append({
            "coin": cand["base"], "diff_apr_now_snapshot": round(diff_apr_now, 1),
            "diff_apr_30d_mean": round(diff_apr_hist, 1),
            "hl_apr_30d": round(hl_apr_hist, 1), "bybit_apr_30d": round(by_apr_hist, 1),
            "spike_flag": "SPIKE" if spike else "consistent",
            "net_pct_per_month_1x": round(abs(diff_apr_hist) / 12, 2),
            "bybit_vol24h_usd": round(cand["bybit_vol24h"]),
        })
        time.sleep(0.15)

    if not rows:
        print("No candidate history validated -- API issue or no liquid overlapping coins.")
        return 1

    print("\n=== 30-day REAL funding-differential history vs live snapshot (spike-detector) ===")
    hdr2 = (f"{'coin':<10} {'diffNow':>9} {'diff30dMean':>12} {'hlAPR30d':>9} {'bybitAPR30d':>11} "
            f"{'flag':>10} {'net%/mo@1x':>11}")
    print(hdr2); print("-" * len(hdr2))
    for r_ in sorted(rows, key=lambda r: -abs(r["diff_apr_30d_mean"])):
        print(f"{r_['coin']:<10} {r_['diff_apr_now_snapshot']:>8.1f}% {r_['diff_apr_30d_mean']:>11.1f}% "
              f"{r_['hl_apr_30d']:>8.1f}% {r_['bybit_apr_30d']:>10.1f}% {r_['spike_flag']:>10} "
              f"{r_['net_pct_per_month_1x']:>10.2f}%")

    best = max(rows, key=lambda r: abs(r["diff_apr_30d_mean"]))
    print(f"\nBest sustained candidate: {best['coin']} at {best['net_pct_per_month_1x']:.2f}%/month "
          f"(1x, unlevered) from a {best['diff_apr_30d_mean']:.1f}%/yr real 30d-mean funding differential.")

    # 3. Price-tracking risk for the best candidate BY 30D-MEAN (the trustworthy historical
    # figure, already used for net_pct_per_month_1x above). "spike_flag" only says whether the
    # LIVE snapshot overstates this coin's rate -- it does not disqualify the 30d-mean itself.
    track_coin = best["coin"] if best["net_pct_per_month_1x"] > 0 else None

    if track_coin:
        match = next(m for m in candidates if m["base"] == track_coin)
        print(f"\nPulling {HISTORY_DAYS}d real hourly price history from both venues for {track_coin} "
              f"to quantify cross-exchange price-tracking (basis) risk...")
        hl_px = dict(fetch_hl_hourly_closes(S, match["hl_coin"], start, now))
        by_px = dict(fetch_bybit_hourly_closes(S, match["bybit_symbol"], start, now))
        common_ts = sorted(set(hl_px) & set(by_px))
        # align to nearest hour bucket since exact timestamps may differ by exchange
        def bucket(t): return t // 3_600_000
        hl_b = {bucket(t): p for t, p in hl_px.items()}
        by_b = {bucket(t): p for t, p in by_px.items()}
        common_b = sorted(set(hl_b) & set(by_b))
        if len(common_b) >= 24:
            spreads = [abs(hl_b[b] - by_b[b]) / by_b[b] * 100 for b in common_b]
            print(f"  {len(common_b)} aligned hourly samples. Cross-exchange price spread: "
                  f"mean={st.mean(spreads):.3f}%  max={max(spreads):.3f}%  p99={sorted(spreads)[int(len(spreads)*0.99)]:.3f}%")
        else:
            print(f"  Insufficient aligned price samples ({len(common_b)}) to quantify tracking risk.")
    else:
        print("\nNo positive candidate found -- skipping price-tracking risk check.")

    print("\n=== VERDICT ===")
    clears_5pct = [r for r in rows if r["net_pct_per_month_1x"] >= 5.0]
    if clears_5pct:
        print(f"  {len(clears_5pct)} coin(s) clear 5%/month on real 30d-mean funding differential: "
              f"{[r['coin'] for r in clears_5pct]}")
    else:
        print("  0 coins clear 5%/month on real 30d-mean (sustained) cross-exchange funding differential at 1x.")

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "cross_exchange_funding_arb.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"\n  saved {OUT/'cross_exchange_funding_arb.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
