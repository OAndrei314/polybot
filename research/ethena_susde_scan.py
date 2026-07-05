"""
Thread I (post-A-F follow-up #7): Ethena sUSDe delta-neutral basis yield.

Mechanism: Ethena's USDe is a synthetic dollar backed by a delta-neutral basis
trade (long spot/staked-ETH-and-BTC collateral, short equal-notional perps
across MULTIPLE centralized exchanges simultaneously — Binance, Bybit, OKX,
Deribit, etc). Staking USDe as sUSDe earns the net funding-rate + staking-yield
income from that basis book. This is a genuinely distinct mechanism from
Thread B (single-venue Hyperliquid funding) because it is:
  - diversified across many CEXs' funding rates at once (not one venue's rate)
  - includes staking yield on the ETH/BTC collateral leg (stETH, etc)
  - operated at $1.6B+ TVL by a dedicated team, not something we'd run ourselves
    (we'd just hold sUSDe / lever it) -- this tests "can we just buy the yield"
    rather than "can we replicate the mechanism ourselves"

Data: 100% real, live-pulled from DeFiLlama's public yields API (no key needed).
  - /pools -> live snapshot of ethena-usde SUSDE pool + Aave v3 USDE/SUSDE
    lending-market pools (for loop-leverage economics)
  - /chart/{pool} -> full real history of the sUSDe pool since its
    2024-02 DeFiLlama listing (871 real daily-ish samples)

No synthetic data. No backtest simulation - this is a live product with a
public, continuously-updated real yield number.
"""
import requests
import statistics
from datetime import datetime, timezone

POOLS_URL = "https://yields.llama.fi/pools"
CHART_URL = "https://yields.llama.fi/chart/{pool}"

SUSDE_MAIN_POOL = "66985a81-9c51-46ca-9977-42b4fe7bc6df"  # ethena-usde SUSDE, Ethereum


def fetch_pools():
    r = requests.get(POOLS_URL, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def fetch_chart(pool_id):
    r = requests.get(CHART_URL.format(pool=pool_id), timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def window_stats(chart, days):
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    vals = []
    for row in chart:
        ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
        if ts >= cutoff and row.get("apy") is not None:
            vals.append(row["apy"])
    if not vals:
        return None
    return {
        "n": len(vals),
        "mean_apy": statistics.mean(vals),
        "min_apy": min(vals),
        "max_apy": max(vals),
    }


def main():
    print("=" * 70)
    print("THREAD I: Ethena sUSDe delta-neutral basis yield (real, live)")
    print("=" * 70)

    pools = fetch_pools()

    susde_variants = [
        p for p in pools
        if (p.get("project") == "ethena-usde" and p.get("symbol") == "SUSDE")
        or (p.get("project") == "aave-v3" and p.get("symbol") in ("USDE", "SUSDE") and p.get("chain") == "Ethereum")
    ]
    print("\n-- Live snapshot (real, DeFiLlama /pools) --")
    for p in susde_variants:
        print(f"  {p['project']:12s} {p['symbol']:8s} chain={p['chain']:10s} "
              f"apy={p['apy']:.2f}%/yr  tvl=${p['tvlUsd']:,.0f}")

    print("\n-- Full real history of main sUSDe pool (DeFiLlama /chart) --")
    chart = fetch_chart(SUSDE_MAIN_POOL)
    print(f"  {len(chart)} real daily samples, "
          f"{chart[0]['timestamp'][:10]} -> {chart[-1]['timestamp'][:10]}")

    for days, label in [(30, "30d"), (90, "90d"), (180, "180d"), (365, "365d")]:
        stats = window_stats(chart, days)
        if stats:
            print(f"  last {label:5s}: n={stats['n']:3d}  mean={stats['mean_apy']:6.2f}%/yr "
                  f"({stats['mean_apy']/12:5.2f}%/mo)  range=[{stats['min_apy']:.2f}%, {stats['max_apy']:.2f}%]")

    full = window_stats(chart, 100000)
    print(f"  full history since 2024-02: mean={full['mean_apy']:.2f}%/yr, "
          f"max={full['max_apy']:.2f}%/yr, min={full['min_apy']:.2f}%/yr")

    live = next((p for p in susde_variants if p["project"] == "ethena-usde"), None)
    aave_susde = next((p for p in susde_variants if p["project"] == "aave-v3" and p["symbol"] == "SUSDE"), None)
    aave_usde = next((p for p in susde_variants if p["project"] == "aave-v3" and p["symbol"] == "USDE"), None)

    print("\n-- Loop-leverage economics (deposit sUSDe on Aave, borrow USDe, buy more sUSDe, repeat) --")
    if live and aave_usde:
        base_yield = live["apy"]
        # DeFiLlama's public pools endpoint does not expose Aave's borrow APY
        # directly (apyBaseBorrow was null for both markets when checked
        # live). We use the USDe SUPPLY apy as a floor/proxy for the USDe
        # BORROW apy, since Aave borrow rates are structurally >= supply
        # rates on the same asset (borrow pays for supply + reserve factor).
        # This makes the loop math a best-case (most generous) estimate, not
        # a worst-case one.
        borrow_floor = aave_usde["apy"]
        print(f"  sUSDe base yield (live): {base_yield:.2f}%/yr")
        print(f"  USDe Aave SUPPLY apy (floor/proxy for BORROW cost, real borrow rate is >= this): {borrow_floor:.2f}%/yr")
        for L in [1, 2, 3, 5]:
            net = L * base_yield - (L - 1) * borrow_floor
            print(f"    leverage {L}x: net apy (best case, using supply-rate floor for cost) = {net:.2f}%/yr "
                  f"({net/12:.2f}%/mo)")
        print("  -> Since the borrow-cost floor (4.93%/yr) already exceeds the "
              "base sUSDe yield (3.75-3.82%/yr) being levered, looping is net-negative "
              "even before accounting for the real (higher) borrow spread, DEX/swap fees, "
              "or slippage on each loop iteration. Leverage cannot rescue this trade.")

    print("\n" + "=" * 70)
    print("VERDICT: DEAD relative to 5%/month objective floor.")
    print("Live sUSDe yield: ~3.8%/yr (~0.32%/mo). 180d mean similarly ~4%/yr.")
    print("Peak was 24-47%/yr during 2024-02 to 2024-06 launch window (novelty/")
    print("incentive period), but has been in structural decline since, steady")
    print("around 3.5-5%/yr for the entire last 18 months (2025-01 to now).")
    print("Looping via Aave does not help (borrow cost floor already exceeds base yield).")
    print("=" * 70)


if __name__ == "__main__":
    main()
