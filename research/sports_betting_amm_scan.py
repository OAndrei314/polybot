"""
Thread O: Decentralized sports-betting AMM "house" liquidity (Azuro, Overtime Markets).

Concept being tested: instead of trying to find mispriced bets (Polymarket, already
DEAD/geo-blocked per prior threads), be the counterparty -- deposit into an AMM
"liquidity pool" that acts as the sportsbook itself and earns the house edge across
many bets. This is a genuinely new mechanism family (manager/agency + underwriting
risk, not market-neutral yield or directional/relative-value trading).

Pulls real, live data from:
- DeFiLlama /protocols and /protocol/{slug} (chain-level TVL history) for azuro and overtime
- DeFiLlama /pools (yields.llama.fi) for azuro's LP pool APY snapshots + short history

No live per-share NAV / historical-return time series is publicly queryable for either
protocol via free APIs (Azuro's LP pools don't expose pricePerShare on DeFiLlama; Overtime's
sportsbook-vault NAV isn't in DeFiLlama's chainTvls, which only has protocol-wide TVL).
Historical *return* figures for Overtime therefore come from a secondary source (DL News,
May 2024 reporting on Overtime's own dashboard) rather than a script-reproducible pull --
this is disclosed explicitly, not fabricated as live data.
"""
import requests

POOLS_URL = "https://yields.llama.fi/pools"
PROTOCOL_URL = "https://api.llama.fi/protocol/{slug}"
PROTOCOLS_URL = "https://api.llama.fi/protocols"


def get_protocol_tvl_summary(slug, chains):
    r = requests.get(PROTOCOL_URL.format(slug=slug), timeout=30)
    r.raise_for_status()
    d = r.json()
    cd = d.get("chainTvls", {})
    out = {}
    for chain in chains:
        if chain not in cd:
            continue
        tvl = cd[chain].get("tvl", [])
        if not tvl:
            continue
        peak = max(tvl, key=lambda x: x["totalLiquidityUSD"])
        last = tvl[-1]
        out[chain] = {
            "n_samples": len(tvl),
            "peak_usd": peak["totalLiquidityUSD"],
            "peak_date": peak["date"],
            "last_usd": last["totalLiquidityUSD"],
            "last_date": last["date"],
            "collapse_pct": (
                (last["totalLiquidityUSD"] - peak["totalLiquidityUSD"]) / peak["totalLiquidityUSD"] * 100
                if peak["totalLiquidityUSD"] > 0 else None
            ),
        }
    return out


def get_azuro_pool_snapshots():
    r = requests.get(POOLS_URL, timeout=30)
    r.raise_for_status()
    data = r.json()["data"]
    return [p for p in data if p.get("project") == "azuro"]


def main():
    print("=== Thread O: Decentralized sports-betting AMM house liquidity ===\n")

    print("--- Azuro Protocol TVL history (Polygon / Base / xDai / Gnosis) ---")
    azuro_tvl = get_protocol_tvl_summary("azuro", ["Polygon", "Base", "xDai", "Chiliz", "Linea"])
    for chain, s in azuro_tvl.items():
        print(f"{chain:10s} n={s['n_samples']:5d}  peak=${s['peak_usd']:>12,.0f}  "
              f"last=${s['last_usd']:>12,.0f}  collapse={s['collapse_pct']:.1f}%")

    print("\n--- Azuro live LP pool snapshots (yields.llama.fi) ---")
    for p in get_azuro_pool_snapshots():
        print(f"{p['chain']:10s} {p['symbol']:5s} tvl=${p['tvlUsd']:>10,.0f}  "
              f"live_apy={p['apy']:.2f}%  apyMean30d={p.get('apyMean30d')}  "
              f"outlier_flag={p.get('outlier')}  n_daily_samples={p.get('count')}")

    print("\n--- Overtime Markets (Thales) TVL history ---")
    overtime_tvl = get_protocol_tvl_summary(
        "overtime", ["Optimism", "Arbitrum", "Base", "Polygon"]
    )
    for chain, s in overtime_tvl.items():
        print(f"{chain:10s} n={s['n_samples']:5d}  peak=${s['peak_usd']:>12,.0f}  "
              f"last=${s['last_usd']:>12,.0f}  collapse={s['collapse_pct']:.1f}%")

    print("\n--- Overtime historical LP *returns* (NOT script-reproducible; source: DL News, May 2024) ---")
    print("Arbitrum Single LP:  +106% since inception (~1yr, mid-2023->mid-2024)  ~6-9%/month compounded")
    print("Optimism Single LP:  +59% since inception")
    print("Optimism Parlay LP:  +33% since inception")
    print("Base Single LP:      -9% since inception  (thin volume, one big bettor win dominated the pool)")
    print("Base Parlay LP:      -7% since inception")
    print("Weekly range across vaults: -6% to +23%; aggregate $1.6M LP gains on $27M wagered")
    print("No update found for 2025-2026 performance as of this scan (2026-07-05).")


if __name__ == "__main__":
    main()
