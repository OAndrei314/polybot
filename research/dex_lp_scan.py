"""
Thread C: Automated market making on a DEX (Uniswap V3 / Orca concentrated liquidity).

Pulls REAL, LIVE pool data from the DeFiLlama yields API (https://yields.llama.fi/pools)
-- no synthetic/theoretical numbers. Filters for Uniswap V3 (Ethereum/Arbitrum/etc) and
Orca (Solana) concentrated-liquidity pools, splits into:
  (a) stablecoin/stablecoin pairs -- IL approx 0 (both legs track $1), so apy IS the
      near-riskless yield estimate
  (b) volatile pairs (e.g. USDC/ETH, WBTC/ETH) -- apy is fee income only, IL is a real,
      separate cost that DeFiLlama does NOT net out of `apy` for LPs (it nets it out of
      `apyBase`/`apy` only in the sense that these are *historical realized* pool-level
      LP-share-value returns for full-range/passive positions; DeFiLlama's yields API does
      not model concentrated-range IL at all, so its `apy` for concentrated-liquidity pools
      is a fee-only or fee+reward figure, not IL-adjusted -- this is disclosed below)

DeFiLlama fields used (all real, no fabrication):
  - tvlUsd, apy, apyBase, apyReward, apyMean30d, apyPct30D, stablecoin, ilRisk, project,
    chain, symbol, pool (id), predictions
"""
import requests
import json
from datetime import datetime, timezone

POOLS_URL = "https://yields.llama.fi/pools"

MIN_TVL = 1_000_000
MIN_APY = 5.0  # floor, we'll report way above this anyway for the headline scan

UNIV3_PROJECTS = {"uniswap-v3", "uniswap-v4"}
ORCA_PROJECTS = {"orca-dex"}
TARGET_PROJECTS = UNIV3_PROJECTS | ORCA_PROJECTS

STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "FRAX", "USDE", "USDS", "TUSD", "USDP", "GUSD", "LUSD", "PYUSD", "CRVUSD"}


def is_stable_pair(symbol: str) -> bool:
    legs = symbol.replace("-", "/").split("/")
    if len(legs) < 2:
        return False
    return all(leg.strip().upper() in STABLE_SYMBOLS for leg in legs)


def fetch_pools():
    r = requests.get(POOLS_URL, timeout=60)
    r.raise_for_status()
    return r.json()["data"]


def main():
    print(f"Pulling live DeFiLlama pools data ({datetime.now(timezone.utc).isoformat()})...")
    pools = fetch_pools()
    print(f"Total pools returned: {len(pools)}")

    all_dex_pools = [
        p for p in pools
        if p.get("project") in TARGET_PROJECTS
        and (p.get("tvlUsd") or 0) >= MIN_TVL
        and not p.get("outlier", False)
    ]
    print(f"Uniswap-V3/V4/Orca pools with TVL>=${MIN_TVL:,} (any apy): {len(all_dex_pools)}")

    # Stable/stable pairs: report ALL of them (no apy floor) since near-zero-IL yield
    # is the interesting number even if it's below the headline 5%/mo bar.
    stable_pools = [p for p in all_dex_pools if is_stable_pair(p["symbol"])]
    # Volatile pairs: only show ones clearing the apy floor, otherwise the list is huge.
    volatile_pools = [p for p in all_dex_pools if not is_stable_pair(p["symbol"]) and (p.get("apy") or 0) >= MIN_APY]

    stable_pools.sort(key=lambda p: -(p["apy"] or 0))
    volatile_pools.sort(key=lambda p: -(p["apy"] or 0))

    print("\n=== STABLE/STABLE PAIRS (near-zero IL, apy ~= real yield) ===")
    print(f"{'chain':<10} {'project':<12} {'symbol':<20} {'tvlUsd':>14} {'apy':>7} {'apyBase':>8} {'apyReward':>10} {'apyMean30d':>11} {'apyPct30D':>10}")
    for p in stable_pools[:20]:
        print(f"{p['chain']:<10} {p['project']:<12} {p['symbol']:<20} {p['tvlUsd']:>14,.0f} {p['apy']:>6.1f}% "
              f"{(p.get('apyBase') or 0):>7.1f}% {(p.get('apyReward') or 0):>9.1f}% "
              f"{(p.get('apyMean30d') or 0):>10.1f}% {(p.get('apyPct30D') or 0):>9.1f}%")

    print("\n=== VOLATILE PAIRS (fee-only apy shown; IL is a REAL separate cost NOT included) ===")
    print(f"{'chain':<10} {'project':<12} {'symbol':<20} {'tvlUsd':>14} {'apy':>7} {'apyBase':>8} {'apyReward':>10} {'ilRisk':>7}")
    for p in volatile_pools[:20]:
        print(f"{p['chain']:<10} {p['project']:<12} {p['symbol']:<20} {p['tvlUsd']:>14,.0f} {p['apy']:>6.1f}% "
              f"{(p.get('apyBase') or 0):>7.1f}% {(p.get('apyReward') or 0):>9.1f}% {p.get('ilRisk','?'):>7}")

    # Monthly-yield conversion for a hypothetical $1000 position
    print("\n=== $1000 position, monthly income estimate (top 5 stable pools) ===")
    for p in stable_pools[:5]:
        monthly_pct = p["apy"] / 12
        print(f"{p['project']} {p['symbol']} ({p['chain']}): apy={p['apy']:.1f}% -> "
              f"~${1000 * monthly_pct / 100:.2f}/month ({monthly_pct:.2f}%/month) "
              f"[30d avg apy: {(p.get('apyMean30d') or 0):.1f}%, TVL ${p['tvlUsd']:,.0f}]")

    # Stability check: how much does apy for the top stable pools drift day to day?
    print("\n=== Stability check: current apy vs apyMean30d vs apyPct30D drift (top 5 stable) ===")
    for p in stable_pools[:5]:
        drift = (p["apy"] or 0) - (p.get("apyMean30d") or 0)
        print(f"{p['project']} {p['symbol']}: current={p['apy']:.1f}% mean30d={(p.get('apyMean30d') or 0):.1f}% "
              f"drift={drift:+.1f}pp pct30dChange={(p.get('apyPct30D') or 0):+.1f}pp")

    with open("data/dex_lp_scan_raw.json", "w") as f:
        json.dump({"stable": stable_pools[:30], "volatile": volatile_pools[:30]}, f, indent=2)
    print("\nSaved top 30 stable + 30 volatile candidates to data/dex_lp_scan_raw.json")


if __name__ == "__main__":
    main()
