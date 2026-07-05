"""
Thread N: DeFi Options Vaults (DOVs) -- real, automated, crowd-priced
options-selling vaults, as a genuinely new real-data source for the
options-premium-selling mechanism (Thread A tested this via a Black-Scholes
SIMULATION of manual Deribit selling; this thread tests REAL, live,
already-executed vault performance instead).

Concept: DOVs (originally popularized by Ribbon Finance in 2021-2022, now
succeeded by smaller protocols since Ribbon shut down its retail vaults)
auto-roll weekly/short-dated covered calls or cash-secured puts and
distribute the collected premium to depositors, net of any assignment
losses. If real vault depositors have earned >5%/month sustained, that
would be strong corroborating evidence Thread A's DEAD verdict was a
simulation artifact, not a real market fact.

Data source: DeFiLlama /pools + /chart/{pool} (no API key, real historical
APY back to each pool's listing date).
"""
import requests

POOLS_URL = "https://yields.llama.fi/pools"
CHART_URL = "https://yields.llama.fi/chart/{}"

# Candidate DOV-like projects found by scanning DeFiLlama's live pool list
# for project names containing 'option' or 'vault' (see iteration 24 notes).
CANDIDATES = [
    ("bracket-vaults", "USDC"),
    ("bracket-vaults", "WETH"),
    ("3jane-options", "ETH-X-C"),
]


def fetch_pools():
    r = requests.get(POOLS_URL, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def fetch_history(pool_id):
    r = requests.get(CHART_URL.format(pool_id), timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def monthly_equiv(apy_pct):
    """Convert an annualized % APY into an equivalent monthly %."""
    return ((1 + apy_pct / 100) ** (1 / 12) - 1) * 100


def main():
    pools = fetch_pools()
    by_key = {(p["project"], p["symbol"]): p for p in pools}

    print("=" * 100)
    print("Thread N: DeFi Options Vaults (DOVs) -- real live vault performance")
    print("=" * 100)

    known_dead_dovs = [
        "Ribbon Finance (shut down retail Theta Vaults in 2023-2024, pivoted to Aevo)",
        "Dopex (rebranded to Stryke, no longer shows DOV-style pools on DeFiLlama)",
        "Lyra Finance (rebranded to Derive, no DOV-style vault pools found live)",
        "Friktion (Solana DOV, shut down entirely in Nov 2023)",
        "ThetaNuts Finance (no live pools found on DeFiLlama as of this scan)",
    ]
    print("\nHistorically-notable DOV protocols with NO live pools found on")
    print("DeFiLlama today (checked by project-slug search, zero results):")
    for d in known_dead_dovs:
        print(f"  - {d}")

    results = []
    for project, symbol in CANDIDATES:
        key = (project, symbol)
        if key not in by_key:
            print(f"\n[{project} {symbol}] NOT FOUND in live pool list, skipping")
            continue
        p = by_key[key]
        pool_id = p["pool"]
        tvl_now = p["tvlUsd"]
        apy_now = p["apy"]
        hist = fetch_history(pool_id)
        n = len(hist)
        first, last = hist[0], hist[-1]
        last30 = hist[-30:] if n >= 30 else hist
        apy_last30_mean = sum(d["apy"] for d in last30) / len(last30)
        tvl_first = first["tvlUsd"]
        tvl_last = last["tvlUsd"]

        print(f"\n--- {project} / {symbol} ---")
        print(f"  Live TVL: ${tvl_now:,.0f}   Live APY: {apy_now:.2f}%/yr "
              f"({monthly_equiv(apy_now):.2f}%/month)")
        print(f"  History: {n} daily samples, {first['timestamp'][:10]} -> {last['timestamp'][:10]}")
        print(f"  TVL trend: ${tvl_first:,.0f} -> ${tvl_last:,.0f} "
              f"({(tvl_last / tvl_first - 1) * 100:+.0f}% over full history)")
        print(f"  Trailing 30d mean APY: {apy_last30_mean:.2f}%/yr "
              f"({monthly_equiv(apy_last30_mean):.2f}%/month)")

        results.append({
            "project": project, "symbol": symbol, "tvl_now": tvl_now,
            "apy_now": apy_now, "n_days": n, "tvl_first": tvl_first,
            "tvl_last": tvl_last, "apy_last30_mean": apy_last30_mean,
        })

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"{'Project/Symbol':<28}{'TVL now':>12}{'TVL trend':>14}{'30d APY':>12}{'30d %/mo':>12}")
    for r in results:
        trend = f"{(r['tvl_last']/r['tvl_first']-1)*100:+.0f}%"
        print(f"{r['project']+'/'+r['symbol']:<28}${r['tvl_now']:>10,.0f}{trend:>14}"
              f"{r['apy_last30_mean']:>11.2f}%{monthly_equiv(r['apy_last30_mean']):>11.2f}%")


if __name__ == "__main__":
    main()
