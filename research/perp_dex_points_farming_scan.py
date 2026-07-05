"""
Thread Q: Perp-DEX points/airdrop farming — a new mechanism family (speculative
token-distribution lottery via zero/near-zero-fee trading volume), beyond every
prior thread in this objective (all of which harvested a *measurable, live* rate:
funding, basis, fees, options premium, momentum, mean-reversion, manager PnL).

This mechanism cannot be backtested the way every other thread was, because the
"return" (a not-yet-issued token's post-TGE market value) does not exist as a
live number until the token launches. What CAN be pulled with real data:
  1. The realized, live, currently-checkable outcome of the last 2-3 completed
     perp-DEX points programs (Hyperliquid HYPE, Lighter LIT, Paradex DIME) via
     CoinGecko's public markets API — current price/FDV/market cap/ATH, no key
     needed.
  2. Real, disclosed program terms (fee schedule, % of supply allocated to the
     airdrop, points-season dates) for currently-open or just-closed programs,
     gathered via web research (cited in FINDINGS_BEYOND_POLYMARKET.md, not
     re-fetched here since there's no public API for "total XP issued to date").

Run: python -m research.perp_dex_points_farming_scan
Output: console table only (no CSV — this thread's conclusion rests on 3 data
points, not a backtest series).
"""

from __future__ import annotations

import sys
import time

import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

COINGECKO = "https://api.coingecko.com/api/v3/coins/markets"

# Completed (TGE-done) perp-DEX points-farming precedents.
# Pre-launch valuation estimates and program terms are from contemporaneous
# public reporting (cited in FINDINGS_BEYOND_POLYMARKET.md); only the
# "current live" columns below are pulled fresh from CoinGecko in this script.
PRECEDENTS = {
    "hyperliquid": {
        "label": "Hyperliquid (HYPE)",
        "tge_date": "2024-11-29",
        "airdrop_pct_supply": "~31%, no VC/presale allocation",
        "farming_cost": "near-zero (trading fees, no dedicated 'zero-fee' program — cost was ordinary taker/maker fees on real trading volume)",
        "pre_launch_estimate": "not widely pre-modeled at the time; came as a positive surprise",
    },
    "lighter": {
        "label": "Lighter (LIT)",
        "tge_date": "2025-12-30",
        "airdrop_pct_supply": "25% of supply (Season 1+2 points, no vesting)",
        "farming_cost": "~$0 (standard accounts paid 0 maker/0 taker fees during both points seasons)",
        "pre_launch_estimate": "community pre-TGE point-value chatter: $60-100+/point implied by early trading",
    },
    "paradex": {
        "label": "Paradex (DIME)",
        "tge_date": "~2026-01",
        "airdrop_pct_supply": "20% of supply to airdrop; ~350M XP ≈ 15% of supply distributed across seasons",
        "farming_cost": "~$0 (retail maker+taker fees are $0 under Paradex's 'Zero Fee Perps' program)",
        "pre_launch_estimate": "analyst FDV estimates pre-TGE: $260M-$520M (one bull case cited $1B)",
    },
}


def fetch_live(coingecko_id: str) -> dict | None:
    for attempt in range(3):
        r = requests.get(COINGECKO, params={"vs_currency": "usd", "ids": coingecko_id}, timeout=20)
        if r.status_code == 200:
            data = r.json()
            return data[0] if data else None
        time.sleep(15)
    return None


def main():
    print("=" * 100)
    print("THREAD Q: Perp-DEX points/airdrop farming — realized outcomes of completed programs")
    print("(live data pulled from CoinGecko /coins/markets, no API key)")
    print("=" * 100)

    rows = []
    for cg_id, meta in PRECEDENTS.items():
        live = fetch_live(cg_id)
        time.sleep(12)  # CoinGecko free tier is aggressively rate-limited
        if live is None:
            print(f"  {meta['label']}: LIVE FETCH FAILED (rate-limited or delisted)")
            continue
        price = live["current_price"]
        fdv = live.get("fully_diluted_valuation")
        mcap = live.get("market_cap")
        ath = live.get("ath")
        ath_date = live.get("ath_date", "")[:10]
        drawdown_from_ath = (price / ath - 1) * 100 if ath else None
        rows.append((meta, price, fdv, mcap, ath, ath_date, drawdown_from_ath))

    print()
    header = f"{'Program':<22}{'TGE':<12}{'Price now':>12}{'FDV now':>16}{'ATH':>10}{'ATH date':>12}{'vs ATH':>10}"
    print(header)
    print("-" * len(header))
    for meta, price, fdv, mcap, ath, ath_date, dd in rows:
        fdv_s = f"${fdv/1e6:,.1f}M" if fdv else "n/a"
        print(f"{meta['label']:<22}{meta['tge_date']:<12}${price:>10.4f}{fdv_s:>16}{ath:>9.3f} {ath_date:>12}{dd:>9.1f}%")

    print()
    print("Disclosed program terms + pre-launch expectation vs realized outcome:")
    for meta, price, fdv, mcap, ath, ath_date, dd in rows:
        print(f"\n  {meta['label']}:")
        print(f"    Airdrop size: {meta['airdrop_pct_supply']}")
        print(f"    Farming cost while live: {meta['farming_cost']}")
        print(f"    Pre-launch expectation: {meta['pre_launch_estimate']}")
        fdv_s = f"${fdv/1e6:,.1f}M" if fdv else "n/a"
        print(f"    REALIZED (live now): FDV {fdv_s}, {dd:+.1f}% from its own ATH ({ath_date})")

    print()
    print("=" * 100)
    print("VERDICT: mechanism produces genuinely bimodal, unpredictable-in-advance outcomes.")
    print("Same near-zero farming cost across all 3; realized value differs by ~2 orders of")
    print("magnitude between the best (Hyperliquid) and worst (Paradex) case. See Thread Q in")
    print("FINDINGS_BEYOND_POLYMARKET.md for the full writeup and NOT VALIDATED verdict.")
    print("=" * 100)


if __name__ == "__main__":
    main()
